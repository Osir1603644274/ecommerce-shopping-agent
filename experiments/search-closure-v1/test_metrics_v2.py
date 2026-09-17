"""Synthetic-only evaluator checks; never reads historical or sealed labels.

Run: python -m unittest discover -s experiments/search-closure-v1 -p test_metrics_v2.py -v
"""
from __future__ import annotations

import copy
import itertools
import json
from pathlib import Path
import tempfile
import unittest

import baseline_eval as adapter
from metrics_v2 import (aggregate, build_common_pool, canonical_hash, dcg,
                        paired_interval_bootstrap, query_metrics, shared_eligibility)


def record(qid, source, ranking, labels, method="base", pool=None):
    return {"query_id": qid, "source": source, "method": method,
            **query_metrics(ranking, labels, common_pool=pool)}


class MetricTests(unittest.TestCase):
    def test_exhaustive_small_pool_bounds_cover_every_defined_completion(self):
        # Every partial assignment of three docs, every ranking permutation, two cutoffs.
        docs = ["a", "b", "c"]
        for partial in itertools.product((0, 1, 2, 3, "UNKNOWN"), repeat=3):
            labels = dict(zip(docs, partial))
            unknown = [d for d in docs if labels[d] == "UNKNOWN"]
            for ranking in itertools.permutations(docs):
                for cutoff in (1, 2, 3):
                    result = query_metrics(list(ranking), labels, k=cutoff)
                    for assignment in itertools.product(range(4), repeat=len(unknown)):
                        concrete = {**labels, **dict(zip(unknown, assignment))}
                        ideal = dcg(sorted(concrete.values(), reverse=True), cutoff)
                        if ideal == 0:
                            continue
                        exact = dcg([concrete[d] for d in ranking], cutoff) / ideal
                        self.assertLessEqual(result["ndcg_lower_bound_at_10"], exact + 1e-12)
                        self.assertLessEqual(exact, result["ndcg_upper_bound_at_10"] + 1e-12)
                        if result["ndcg_at_10"] is not None:
                            self.assertAlmostEqual(exact, result["ndcg_at_10"])

    def test_unique_zero_with_unknown_outside_topk(self):
        result = query_metrics(["zero"], {"zero": 0, "positive": 3, "unknown": "UNKNOWN"})
        self.assertEqual(result["status"], "point")
        self.assertEqual(result["ndcg_at_10"], 0)

    def test_all_zero_is_explicit_no_gain(self):
        result = query_metrics(["a"], {"a": 0, "b": 0})
        self.assertEqual(result["status"], "no_gain")
        self.assertIsNone(result["ndcg_at_10"])
        self.assertIsNone(result["ndcg_lower_bound_at_10"])
        self.assertIsNone(result["ndcg_upper_bound_at_10"])

    def test_unknown_gain_not_guaranteed(self):
        result = query_metrics(["a"], {"a": "UNKNOWN"})
        self.assertEqual([result["ndcg_lower_bound_at_10"], result["ndcg_upper_bound_at_10"]], [0, 1])
        self.assertTrue(result["gain_not_guaranteed"])
        self.assertIsNone(result["ndcg_at_10"])

    def test_missing_labels_never_convert_to_zero(self):
        labels = {"a": 2}
        pool = build_common_pool(labels, {"old": ["a", "new"], "new": ["different", "a"]})
        result = query_metrics(["a", "new"], labels, common_pool=pool)
        self.assertEqual(labels, {"a": 2})
        self.assertEqual(result["status"], "missing_labels")
        self.assertEqual(result["common_pool_missing_label_count"], 2)
        self.assertEqual(result["outside_qrel_pool_top10_ids"], ["new"])

    def test_common_pool_bound_is_method_independent(self):
        labels = {"a": 3, "b": 0}
        pool = build_common_pool(labels, {"old": ["a"], "new": ["outside"]})
        old = query_metrics(["a"], labels, common_pool=pool)
        new = query_metrics(["outside"], labels, common_pool=pool)
        self.assertEqual(old["common_pool_sha256"], new["common_pool_sha256"])
        self.assertEqual(old["idcg_possible"], new["idcg_possible"])
        self.assertEqual(old["common_pool_missing_label_count"], 1)

    def test_short_return_coverage_uses_ten_slots(self):
        result = query_metrics(["a", "b"], {"a": 3, "b": "UNKNOWN"})
        self.assertEqual(result["judged_at_10"], .1)
        self.assertEqual(result["returned_at_10"], 2)
        self.assertEqual(result["missing_return_slots_at_10"], 8)
        self.assertTrue(result["short_return_at_10"])

    def test_recall_thresholds_and_depths(self):
        labels = {"a": 3, "b": 2, "c": 1, "d": 3, "e": "UNKNOWN"}
        ranking = ["a", "c"] + [f"other{i}" for i in range(99)] + ["b", "d"]
        result = query_metrics(ranking, labels)
        recall = result["known_pooled_recall"]
        self.assertEqual(recall["grade_ge_2"]["10"]["value"], 1 / 3)
        self.assertEqual(recall["grade_ge_2"]["100"]["value"], 1 / 3)
        self.assertEqual(recall["grade_ge_2"]["300"]["value"], 1)
        self.assertEqual(recall["grade_eq_3"]["10"]["value"], .5)
        self.assertEqual(recall["grade_eq_3"]["300"]["value"], 1)

    def test_no_known_positive_recall_is_undefined(self):
        result = query_metrics(["a"], {"a": 1, "b": "UNKNOWN"})
        for metric in result["known_pooled_recall"].values():
            for depth in metric.values():
                self.assertIsNone(depth["value"])
                self.assertEqual(depth["denominator"], 0)
                self.assertEqual(depth["status"], "no_known_relevant")

    def test_ranking_duplicate_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Duplicate ranked"):
            query_metrics(["a", "a"], {"a": 3})

    def test_boolean_float_and_invalid_grades_fail_closed(self):
        for grade in (True, False, -1, 4, 3.0, None, "3", "unknown", [], {}):
            with self.subTest(grade=grade), self.assertRaises(ValueError):
                query_metrics(["a"], {"a": grade})

    def test_missing_qrels_and_empty_ranking_status(self):
        result = query_metrics(["a"], {})
        self.assertEqual(result["status"], "missing_qrels")
        self.assertEqual(result["ndcg_upper_bound_at_10"], 1)
        empty = query_metrics([], {})
        self.assertEqual(empty["status"], "missing_qrels")
        self.assertIsNone(empty["ndcg_lower_bound_at_10"])

    def test_invalid_common_pool_fails_closed(self):
        for pool in (["a", "a"], ["b"], ["a", 1]):
            with self.assertRaises(ValueError):
                query_metrics(["a"], {"a": 3}, common_pool=pool)


class AggregationTests(unittest.TestCase):
    def test_equal_source_mean_not_query_micro_mean(self):
        rows = [record("ku1", "kuaisearch", ["yes"], {"yes": 3}),
                record("ku2", "kuaisearch", ["yes"], {"yes": 3}),
                record("mu1", "multicpr", ["no"], {"yes": 3, "no": 0})]
        result = aggregate(rows)
        self.assertEqual(result["equal_source_mean_lower"], .5)
        self.assertNotEqual(result["equal_source_mean_lower"], 2 / 3)

    def test_no_gain_retained_and_explicit_conditional_mean(self):
        rows = [record("ku1", "kuaisearch", ["yes"], {"yes": 3}),
                record("ku2", "kuaisearch", ["no"], {"no": 0}),
                record("mu1", "multicpr", ["yes"], {"yes": 3})]
        result = aggregate(rows)
        self.assertIsNone(result["equal_source_mean_lower"])
        self.assertEqual(result["query_count"], 3)
        ku = result["by_source"]["kuaisearch"]
        self.assertEqual(ku["query_count"], 2)
        self.assertEqual(ku["eligible_query_count"], 1)
        self.assertEqual(ku["unscorable_query_ids"], ["ku2"])
        self.assertEqual(ku["conditional_mean_lower"], 1)

    def test_shared_eligibility_uses_same_subset_for_all_methods(self):
        rows = []
        for method in ("old", "new"):
            rows += [record("ku1", "kuaisearch", ["yes"], {"yes": 3}, method),
                     record("ku2", "kuaisearch", ["no"], {"no": 0}, method),
                     record("mu1", "multicpr", ["yes"], {"yes": 3}, method)]
        result = shared_eligibility(rows, ["old", "new"])
        self.assertEqual(result["status"], "COMMON_CONDITIONAL_SUBSET_ONLY")
        self.assertEqual(result["eligible_query_ids"], ["ku1", "mu1"])
        self.assertEqual(result["total_query_count"], 3)
        self.assertEqual(result["excluded_queries"], {"ku2": {"old": "no_gain", "new": "no_gain"}})
        self.assertEqual(len(result["manifest_sha256"]), 64)

    def test_shared_eligibility_no_source_returns_no_valid_selection(self):
        rows = []
        for method in ("old", "new"):
            rows += [record("ku1", "kuaisearch", ["yes"], {"yes": 3}, method),
                     record("mu1", "multicpr", ["no"], {"no": 0}, method)]
        self.assertEqual(shared_eligibility(rows, ["old", "new"])["status"], "NO_VALID_SELECTION")

    def test_shared_eligibility_rejects_method_dependent_query_drop(self):
        rows = [record("q1", "kuaisearch", ["yes"], {"yes": 3}, "old"),
                record("q2", "multicpr", ["yes"], {"yes": 3}, "old"),
                record("q1", "kuaisearch", ["yes"], {"yes": 3}, "new")]
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            shared_eligibility(rows, ["old", "new"])

    def test_unknown_difference_interval_covers_real_paired_differences(self):
        labels = {"a": 3, "b": "UNKNOWN", "c": 0}
        base, new = {}, {}
        for qid, source in (("ku", "kuaisearch"), ("mu", "multicpr")):
            base[qid] = record(qid, source, ["a", "b", "c"], labels)
            new[qid] = record(qid, source, ["b", "a", "c"], labels)
        result = paired_interval_bootstrap(base, new, repetitions=100)
        interval = result["equal_source_mean_difference_interval"]
        for grade in range(4):
            concrete = {**labels, "b": grade}
            delta = query_metrics(["b", "a", "c"], concrete)["ndcg_at_10"] - query_metrics(["a", "b", "c"], concrete)["ndcg_at_10"]
            self.assertLessEqual(interval[0], delta + 1e-12)
            self.assertLessEqual(delta, interval[1] + 1e-12)
        self.assertEqual(interval, result["ci95_outer_envelope"])
        self.assertFalse(result["positive_improvement_supported"])

    def test_bootstrap_is_paired_equal_source_and_deterministic_10000(self):
        base, new = {}, {}
        for qid, source in (("ku1", "kuaisearch"), ("ku2", "kuaisearch"), ("mu1", "multicpr")):
            labels = {"yes": 3, "no": 0}
            base[qid] = record(qid, source, ["no"], labels)
            new[qid] = record(qid, source, ["yes"] if source == "kuaisearch" else ["no"], labels)
        first = paired_interval_bootstrap(base, new)
        second = paired_interval_bootstrap(base, new)
        self.assertEqual(first, second)
        self.assertEqual(first["repetitions"], 10000)
        self.assertEqual(first["equal_source_mean_difference_interval"], [.5, .5])
        self.assertEqual(first["ci95_outer_envelope"], [.5, .5])
        self.assertTrue(first["positive_improvement_supported"])

    def test_bootstrap_no_gain_is_not_dropped(self):
        base = {"q": record("q", "kuaisearch", ["no"], {"no": 0})}
        result = paired_interval_bootstrap(base, copy.deepcopy(base))
        self.assertEqual(result["query_count"], 1)
        self.assertEqual(result["unscorable_query_ids"], ["q"])
        self.assertIsNone(result["ci95_outer_envelope"])

    def test_pair_identity_and_pool_faults_fail_closed(self):
        base = {"q": record("q", "kuaisearch", ["yes"], {"yes": 3})}
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            paired_interval_bootstrap(base, {"other": base["q"]})
        changed = copy.deepcopy(base)
        changed["q"]["common_pool_sha256"] = "different"
        with self.assertRaisesRegex(ValueError, "pool mismatch"):
            paired_interval_bootstrap(base, changed)
        changed = {"q": record("q", "kuaisearch", ["yes"], {"yes": 2})}
        with self.assertRaisesRegex(ValueError, "qrel hash mismatch"):
            paired_interval_bootstrap(base, changed)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.queries = adapter.load_queries([
            {"query_id": "q1", "query": "example", "cohort": "main", "source": "kuaisearch"},
            {"query_id": "q2", "query": "example2", "cohort": "diagnostic", "source": "multicpr"},
        ], fixed_development=False)

    def rankrow(self, method="old", qid="q1"):
        source = self.queries[qid]["source"]
        docs = [source + ":1", source + ":2"]
        return {"method": method, "query_id": qid, "source": source,
                "ranking": docs, "ranking_sha256": canonical_hash(docs)}

    def qrel(self, fixture_id="q1", **changes):
        return {"query_id": fixture_id, "query": self.queries[fixture_id]["query"],
                "query_cohort": self.queries[fixture_id]["query_cohort"],
                "document_id": self.queries[fixture_id]["source"] + ":1", "grade": 3, **changes}

    def test_rank_schema_supports_scored_outputs_without_resorting(self):
        row = self.rankrow()
        ranking = row.pop("ranking")
        row["results"] = [{"document_id": d, "rank": i, "score": -i} for i, d in enumerate(ranking, 1)]
        self.assertEqual(adapter.ranking_from_row(row), ranking)

    def test_rank_schema_faults(self):
        invalid = []
        row = self.rankrow(); row["ranking_sha256"] = "0" * 64; invalid.append(row)
        row = self.rankrow(); row["ranking"] += [row["ranking"][0]]; invalid.append(row)
        row = self.rankrow(); row["results"] = []; invalid.append(row)
        row = self.rankrow(); ranking = row.pop("ranking"); row["results"] = [{"document_id": ranking[0], "rank": 2}]; invalid.append(row)
        row = self.rankrow(); ranking = row.pop("ranking"); row["results"] = [{"document_id": ranking[0], "rank": 1, "score": float("nan")}]; invalid.append(row)
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                adapter.ranking_from_row(row)

    def test_all_methods_share_exact_query_set(self):
        rows = [self.rankrow(m, q) for m in ("old", "new") for q in self.queries]
        self.assertEqual(set(adapter.load_rankings(rows, self.queries, methods=("old", "new"))), {"old", "new"})
        with self.assertRaisesRegex(ValueError, "identical query set"):
            adapter.load_rankings(rows[:-1], self.queries, methods=("old", "new"))
        with self.assertRaisesRegex(ValueError, "Duplicate method/query"):
            adapter.load_rankings(rows + [rows[0]], self.queries, methods=("old", "new"))

    def test_qrel_identity_cohort_and_grade_faults(self):
        good = [self.qrel(), self.qrel("q2")]
        self.assertEqual(adapter.load_qrels(good, self.queries)[0]["q1"], {"kuaisearch:1": 3})
        for change in ({"grade": True}, {"grade": 4}, {"query": "changed"},
                       {"query_cohort": "diagnostic"}, {"document_id": "multicpr:1"},
                       {"qid": "different"}, {"grade": "UNKNOWN", "unknown_causes": "bad"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                adapter.load_qrels([self.qrel(**change), good[1]], self.queries)
        with self.assertRaisesRegex(ValueError, "Duplicate qrel"):
            adapter.load_qrels(good + [good[0]], self.queries)
        with self.assertRaisesRegex(ValueError, "no qrels"):
            adapter.load_qrels(good[:1], self.queries)

    def test_file_hash_mismatch_and_between_read_mutation_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.jsonl"
            path.write_text('{"one":1}\n', encoding="utf-8")
            evidence = adapter.Evidence()
            expected = adapter.sha(path)
            self.assertEqual(evidence.rows(path, expected), [{"one": 1}])
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                adapter.Evidence().rows(path, "0" * 64)
            path.write_text('{"one":2}\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                evidence.recheck()

    def test_duplicate_json_keys_and_nonfinite_fail_closed(self):
        for raw in ('{"grade":0,"grade":3}', '{"score":NaN}', '{"score":Infinity}'):
            with self.assertRaises(ValueError):
                adapter.parse_json(raw)

    def test_non_dev_id_and_cohort_count_drift_fail_closed(self):
        for qid in ("s1-ku-test-0123456789abcdef", "s1-ku-dev-invalid"):
            with self.assertRaises(ValueError):
                adapter.source_from_qid(qid)
        with self.assertRaisesRegex(ValueError, "40 development"):
            adapter.load_queries([{"query_id": "s1-ku-dev-0123456789abcdef", "query": "example", "cohort": "main"}])

    def test_complete_synthetic_adapter_exposes_coverage_manifest(self):
        # Four rows so both main and diagnostic include both sources.
        queries = {}
        for qid, source, cohort in (("k1", "kuaisearch", "main"), ("m1", "multicpr", "main"),
                                    ("k2", "kuaisearch", "diagnostic"), ("m2", "multicpr", "diagnostic")):
            queries[qid] = {"query_id": qid, "query": qid, "source": source, "query_cohort": cohort}
        labels = {qid: {queries[qid]["source"] + ":1": 3 if qid != "k2" else 0} for qid in queries}
        rankings = {method: {qid: list(labels[qid]) for qid in queries} for method in ("base_ce", "new")}
        result = adapter.evaluate(queries, rankings, labels, {qid: {} for qid in queries}, repetitions=10)
        self.assertEqual(len(result["per_query"]), 8)
        self.assertEqual(result["shared_eligibility"]["main"]["status"], "FULL_COHORT_SELECTION_AVAILABLE")
        self.assertEqual(result["shared_eligibility"]["diagnostic"]["status"], "NO_VALID_SELECTION")
        self.assertEqual(result["shared_eligibility"]["all"]["status"], "COMMON_CONDITIONAL_SUBSET_ONLY")
        self.assertEqual(result["common_conditional"]["all"]["query_ids"], ["k1", "m1", "m2"])
        self.assertIsNone(result["summary"]["all"]["base_ce"]["equal_source_mean_lower"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
