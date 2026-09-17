"""Synthetic-only training preparation, gate, and safe-resume tests.

The sole torch tests use tiny CPU linear layers; no real model or CUDA is used.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import train_pairwise as training
from metrics_v2 import canonical_hash


def selected(qid, text, source="kuaisearch"):
    return {"query_id": qid, "text": text, "source": source, "split": "train", "native_split": "train",
            "query_key": training.query_key(text), "native_origin": {"native_split": "train"}}


def sample_data(count=1):
    queries, labels, docs = {}, {}, {}
    for index in range(count):
        qid = f"closure-ku-train-{index:016x}"
        queries[qid] = selected(qid, f"query{index}")
        doc_high, doc_low, doc_unknown = [f"kuaisearch:{index}-{suffix}" for suffix in ("high", "low", "unknown")]
        labels[qid] = {doc_high: 3, doc_low: 1, doc_unknown: "UNKNOWN"}
        for docid in labels[qid]:
            docs[docid] = {"source": "kuaisearch", "text": "original catalog text " + docid}
    return queries, labels, docs


def gate_data(count=6, diagnostic=1):
    queries, rankings, labels = {}, {}, {}
    for i in range(count):
        qid = f"query-{i}"
        docs = [f"kuaisearch:{i}-{j}" for j in range(12)]
        queries[qid] = {"query_id": qid, "query": f"query{i}", "source": "kuaisearch",
                        "query_cohort": "diagnostic" if i < diagnostic else "main"}
        rankings[qid] = docs
        labels[qid] = {doc: (3 if j >= 10 else 1) for j, doc in enumerate(docs)}
    return queries, rankings, labels


class PairConstructionTests(unittest.TestCase):
    def test_pairs_preserve_high_low_direction_and_exclude_unknown(self):
        queries, labels, docs = sample_data()
        result = training.build_pairs(queries, labels, docs, minimum_valid=1)
        self.assertEqual(result["status"], "READY_FOR_EXPLICIT_TRAIN")
        self.assertEqual(len(result["pairs"]), 1)
        pair = result["pairs"][0]
        self.assertEqual((pair["high_grade"], pair["low_grade"]), (3, 1))
        self.assertIn("high", pair["high_document_id"])
        self.assertIn("low", pair["low_document_id"])
        self.assertNotIn("unknown", pair["high_text"] + pair["low_text"])
        self.assertEqual(pair["high_text"], docs[pair["high_document_id"]]["text"])

    def test_fixed_seed_cap32_is_input_order_independent(self):
        queries, _, _ = sample_data()
        qid = next(iter(queries))
        labels = {qid: {f"kuaisearch:{i}": i % 4 for i in range(40)}}
        docs = {d: {"source": "kuaisearch", "text": d} for d in labels[qid]}
        first = training.build_pairs(queries, labels, docs, minimum_valid=1)
        second = training.build_pairs(queries, {qid: dict(reversed(list(labels[qid].items())))}, docs, minimum_valid=1)
        self.assertEqual(first, second)
        self.assertEqual(len(first["pairs"]), 32)
        priorities = [row["sampling_priority_sha256"] for row in first["pairs"]]
        self.assertEqual(priorities, sorted(priorities))

    def test_minimum50_counts_valid_queries_not_pairs(self):
        queries, labels, docs = sample_data(49)
        result = training.build_pairs(queries, labels, docs)
        self.assertEqual(result["status"], "INSUFFICIENT_TRAINING_QUERIES")
        self.assertEqual(result["valid_query_count"], 49)
        queries, labels, docs = sample_data(50)
        result = training.build_pairs(queries, labels, docs)
        self.assertEqual(result["status"], "READY_FOR_EXPLICIT_TRAIN")
        self.assertEqual(training.validate_prepared_pairs(result["pairs"]), {"pairs": 50, "valid_queries": 50})

    def test_single_grade_and_unknown_only_queries_are_not_valid(self):
        queries, labels, docs = sample_data(2)
        q1, q2 = queries
        labels[q1] = {doc: 3 for doc in labels[q1]}
        labels[q2] = {doc: "UNKNOWN" for doc in labels[q2]}
        result = training.build_pairs(queries, labels, docs, minimum_valid=1)
        self.assertEqual(result["valid_query_count"], 0)
        self.assertEqual(result["pairs"], [])

    def test_identical_text_different_grade_cannot_train_direction(self):
        queries, labels, docs = sample_data()
        for doc in docs.values():
            doc["text"] = "same exact encoder input"
        result = training.build_pairs(queries, labels, docs, minimum_valid=1)
        self.assertEqual(result["valid_query_count"], 0)
        self.assertEqual(result["per_query"][0]["identical_text_pairs_skipped"], 1)

    def test_missing_real_catalog_text_fails_closed(self):
        queries, labels, docs = sample_data()
        docs.pop(next(iter(docs)))
        with self.assertRaisesRegex(ValueError, "Missing original catalog"):
            training.build_pairs(queries, labels, docs)

    def test_duplicate_pair_and_direction_tampering_rejected(self):
        queries, labels, docs = sample_data()
        pair = training.build_pairs(queries, labels, docs, minimum_valid=1)["pairs"][0]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            training.validate_prepared_pairs([pair, pair], minimum_valid=1)
        for update in ({"high_grade": True}, {"high_grade": 0}, {"low_grade": "UNKNOWN"},
                       {"query_id": "closure-ku-test-secret"}, {"sampling_priority_sha256": "0" * 64}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                training.validate_prepared_pairs([{**pair, **update}], minimum_valid=1)

    def test_qrel_duplicate_bool_and_unreviewed_pool_rejected(self):
        queries, labels, _ = sample_data()
        qid = next(iter(queries))
        rows = [{"pair_id": str(i), "query_id": qid, "query": queries[qid]["text"], "document_id": doc, "grade": grade}
                for i, (doc, grade) in enumerate(labels[qid].items())]
        self.assertEqual(training.validate_training_qrels(rows, queries, expected_pool_size=3)[0], labels)
        with self.assertRaisesRegex(ValueError, "Duplicate training qrel"):
            training.validate_training_qrels(rows + [rows[0]], queries, expected_pool_size=3)
        with self.assertRaises(ValueError):
            training.validate_training_qrels([{**rows[0], "grade": True}, *rows[1:]], queries, expected_pool_size=3)
        with self.assertRaisesRegex(ValueError, "frozen40|frozen 40"):
            training.validate_training_qrels(rows[:-1], queries, expected_pool_size=3)


class GateTests(unittest.TestCase):
    def test_trigger_counts_five_main_queries_not_ten_witness_pairs(self):
        queries, rankings, labels = gate_data()
        witnesses = training.ranking_error_witnesses(queries, rankings, labels)
        self.assertEqual(len(witnesses), 5)
        self.assertEqual(len({row["query_id"] for row in witnesses}), 5)
        self.assertNotIn("query-0", {row["query_id"] for row in witnesses})
        for row in witnesses:
            self.assertGreater(row["higher"]["grade"], row["lower"]["grade"])
            self.assertGreater(row["higher"]["rank"], 10)
            self.assertLessEqual(row["lower"]["rank"], 10)

    def test_unknown_unjudged_and_beyond300_do_not_trigger(self):
        queries, rankings, labels = gate_data(1, diagnostic=0)
        qid = next(iter(queries))
        labels[qid][rankings[qid][10]] = "UNKNOWN"
        del labels[qid][rankings[qid][11]]
        rankings[qid] += [f"kuaisearch:extra-{i}" for i in range(288)] + ["kuaisearch:outside300"]
        labels[qid]["kuaisearch:outside300"] = 3
        self.assertEqual(len(rankings[qid]), 301)
        self.assertEqual(training.ranking_error_witnesses(queries, rankings, labels), [])

    def test_selects_best_old_ce_before_gate_and_ties_earlier(self):
        queries, labels = {}, {}
        for qid, source in (("q-ku", "kuaisearch"), ("q-mu", "multicpr")):
            queries[qid] = {"query_id": qid, "query": qid, "source": source, "query_cohort": "main"}
            labels[qid] = {source + ":high": 3, source + ":low": 0}
        rankings = {method: {} for method in training.evaluation.METHODS}
        for method in rankings:
            for qid, query in queries.items():
                high, low = query["source"] + ":high", query["source"] + ":low"
                rankings[method][qid] = [high, low] if method in ("epoch1", "epoch3") else [low, high]
        result = training.select_old_ce(queries, rankings, labels)
        self.assertEqual(result["selected_method"], "epoch1")
        self.assertEqual(result["eligibility"]["total_query_count"], 2)
        self.assertEqual(result["eligibility"]["eligible_query_count"], 2)
        for method in rankings:
            for qid, query in queries.items():
                rankings[method][qid] = [query["source"] + ":high", query["source"] + ":low"]
        self.assertEqual(training.select_old_ce(queries, rankings, labels)["selected_method"], "base_ce")

    def test_no_valid_selection_cannot_trigger(self):
        queries, _, labels = gate_data(1, diagnostic=0)
        qid = next(iter(queries))
        labels[qid] = {"kuaisearch:none": 0}
        rankings = {method: {qid: ["kuaisearch:none"]} for method in training.evaluation.METHODS}
        result = training.select_old_ce(queries, rankings, labels)
        self.assertEqual(result["status"], "NO_VALID_SELECTION")
        self.assertIsNone(result["selected_method"])


class IsolationTests(unittest.TestCase):
    def test_dev_heldout_history_and_training_family_collisions_rejected(self):
        original = selected("closure-ku-train-1", "品牌手机旗舰商品")
        cases = [({original["query_id"]: original}, {}, [{"query_key": original["query_key"]}], {}),
                 ({original["query_id"]: original}, {"heldout": {"query_key": original["query_key"]}}, [], {}),
                 ({original["query_id"]: original}, {}, [], {"dev": {"query": "品牌 手机旗舰商品"}}),
                 ({original["query_id"]: original, "closure-ku-train-2": {**original, "query_id": "closure-ku-train-2"}}, {}, [], {})]
        for args in cases:
            with self.assertRaisesRegex(ValueError, "family overlap"):
                training.validate_isolation(*args)

    def test_near_family_isolation_and_safe_distinct_queries(self):
        q = selected("closure-ku-train-1", "abcdefghijklmnopqrst")
        with self.assertRaisesRegex(ValueError, "family overlap"):
            training.validate_isolation({q["query_id"]: q}, {}, [{"query_key": "abcdefghijklmnopqrstu"}], {})
        result = training.validate_isolation({q["query_id"]: q}, {"heldout": {"query_key": "differentgoods"}},
                                            [{"query_key": "historicquery"}], {"dev": {"query": "anotherquery"}})
        self.assertEqual(result["status"], "QUERY_FAMILY_ISOLATION_PASS")

    def test_selected_test_or_duplicate_query_rejected(self):
        rows = [selected("closure-ku-train-1", "queryone"), selected("closure-mu-train-2", "querytwo", "multicpr")]
        self.assertEqual(len(training.selected_queries(rows, split="train", expected_per_source=1)), 2)
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            training.selected_queries(rows + [rows[0]], split="train", expected_per_source=1)
        with self.assertRaisesRegex(ValueError, "split mismatch"):
            training.selected_queries([{**rows[0], "split": "test"}, rows[1]], split="train", expected_per_source=1)


class ReviewBindingTests(unittest.TestCase):
    def make_fixture(self, root, grades=(3, 3), final=3, author_same=False):
        qrel = {"pair_id": "p1", "query_id": "closure-ku-train-1", "query": "original query", "document_id": "kuaisearch:1", "grade": final}
        pair = {"pair_id": "p1", "query_id": "anonymous-query", "query": qrel["query"], "document": {"title": "real title"}}
        mapping = {"p1": {"pair_id": "p1", "query_id": qrel["query_id"], "document_id": qrel["document_id"],
                           "catalog_text_sha256": hashlib.sha256(b"actual catalog text").hexdigest(), "review_document_sha256": canonical_hash(pair["document"])}}
        bindings = []
        rubric = b"same independently frozen rubric"
        rubric_sha = hashlib.sha256(rubric).hexdigest()
        def save(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(training.encoded(value))
            return training.evaluation.sha(path)
        for role, grade in zip(("A", "B", "T"), grades):
            directory = root / role
            packet = directory / "input"
            packet.mkdir(parents=True)
            (packet / "RUBRIC.md").write_bytes(rubric)
            (packet / "pairs.jsonl").write_bytes(training.encoded([pair], lines=True))
            input_sha = save(packet / "INPUT_MANIFEST.json", {"files": {"RUBRIC.md": rubric_sha, "pairs.jsonl": training.evaluation.sha(packet / "pairs.jsonl")}})
            receipt_sha = save(directory / "receipt.json", {"human_gold": False, "self_review_completed": True})
            (directory / "judgments.jsonl").write_bytes(training.encoded([{"pair_id": "p1", "grade": grade}], lines=True))
            collected_sha = save(directory / "COLLECTED.json", {"role": role, "input_manifest_sha256": input_sha,
                "judgments_sha256": training.evaluation.sha(directory / "judgments.jsonl"), "receipt_sha256": receipt_sha,
                "model_metadata": {"source_thread_id": "same-author" if author_same else "author-" + role, "model": "synthetic-model"},
                "validation": {"status": "STRUCTURE_AND_EXACT_EVIDENCE_PASS_NOT_ACCURACY"}})
            bindings.append({"collected_path": str(directory / "COLLECTED.json"), "collected_sha256": collected_sha,
                             "input_manifest_path": str(packet / "INPUT_MANIFEST.json"), "input_manifest_sha256": input_sha,
                             "context_isolation": "no_prior_conversation"})
        return {"review_bindings": bindings, "rubric_sha256": rubric_sha}, [qrel], {"p1": (qrel["query_id"], qrel["document_id"])}, mapping

    def test_independent_majority_and_all_different_unknown(self):
        for grades, final, category in (((3, 3), 3, "AB_agreement"), ((3, 2, 2), 2, "third_majority"), ((3, 2, 1), "UNKNOWN", "no_majority_unknown")):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                manifest, qrels, pair_ids, mapping = self.make_fixture(root, grades=grades, final=final)
                with mock.patch.object(training, "DATA", root):
                    result = training.validate_review_majority(manifest, qrels, pair_ids, training.evaluation.Evidence(), mapping)
                self.assertEqual(result["resolutions"], {category: 1})

    def test_same_author_unresolved_and_wrong_final_labels_rejected(self):
        for kwargs in ({"author_same": True}, {"grades": (3, 2)}, {"grades": (3, 3), "final": 2}):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                manifest, qrels, pair_ids, mapping = self.make_fixture(root, **kwargs)
                with mock.patch.object(training, "DATA", root), self.assertRaises(ValueError):
                    training.validate_review_majority(manifest, qrels, pair_ids, training.evaluation.Evidence(), mapping)

    def test_anonymous_pair_cannot_be_rebound_to_other_native_document(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest, qrels, pair_ids, mapping = self.make_fixture(root)
            altered = copy.deepcopy(mapping)
            altered["p1"]["document_id"] = "kuaisearch:other"
            with self.assertRaisesRegex(ValueError, "mapping mismatch"):
                training.validate_training_mapping(list(altered.values()), qrels)
            mapping["p1"]["review_document_sha256"] = "0" * 64
            with mock.patch.object(training, "DATA", root), self.assertRaisesRegex(ValueError, "mapping hash mismatch"):
                training.validate_review_majority(manifest, qrels, pair_ids, training.evaluation.Evidence(), mapping)


class ResumeTests(unittest.TestCase):
    def settings(self):
        return training.fixed_settings("a" * 64, "b" * 64, "c" * 64, "d" * 64, {"code": "fixed"})

    def make_checkpoint(self, run, epoch, settings):
        run.mkdir(parents=True, exist_ok=True)
        (run / "config.json").write_bytes(training.encoded(settings))
        path = run / "checkpoints" / f"epoch-{epoch}"
        path.mkdir(parents=True)
        files = {}
        for name in ("adapter_model.safetensors", "adapter_config.json", "optimizer.pt", "rng.pt"):
            (path / name).write_bytes(("synthetic " + name).encode())
            files[name] = {"sha256": training.evaluation.sha(path / name), "bytes": (path / name).stat().st_size}
        with mock.patch.object(training, "ROOT", run):
            training.seal_checkpoint_files(path, epoch, 5, training.evaluation.sha(run / "config.json"), {"pair_count": 50})
        return path

    def test_immutable_config_and_full_optimizer_rng_resume_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            settings = self.settings()
            checkpoint = self.make_checkpoint(run, 1, settings)
            original_bytes = {p: p.read_bytes() for p in run.rglob("*") if p.is_file()}
            result = training.resume_checkpoint(run, settings)
            self.assertEqual(result[0], 1)
            self.assertEqual({p: p.read_bytes() for p in original_bytes}, original_bytes)
            with self.assertRaisesRegex(ValueError, "Immutable training"):
                training.resume_checkpoint(run, {**settings, "learning_rate": 1e-4})
            (checkpoint / "rng.pt").write_bytes(b"tampered RNG")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                training.resume_checkpoint(run, settings)

    def test_missing_optimizer_gap_and_orphan_checkpoint_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            checkpoint = self.make_checkpoint(run, 2, self.settings())
            with self.assertRaisesRegex(ValueError, "Noncontiguous"):
                training.resume_checkpoint(run, self.settings())
            (checkpoint / "optimizer.pt").unlink()
            with self.assertRaisesRegex(ValueError, "Unexpected/missing"):
                training.resume_checkpoint(run, self.settings())
            (run / "config.json").unlink()
            with self.assertRaisesRegex(ValueError, "without immutable config"):
                training.resume_checkpoint(run, self.settings())

    def test_pending_epoch_is_preserved_and_not_treated_as_committed(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            self.make_checkpoint(run, 1, self.settings())
            pending = run / "checkpoints/.pending-epoch-2-crash"
            pending.mkdir()
            (pending / "partial").write_text("keep me", encoding="utf-8")
            self.assertEqual(training.resume_checkpoint(run, self.settings())[0], 1)
            self.assertEqual((pending / "partial").read_text(encoding="utf-8"), "keep me")

    def test_final_completion_binds_all_checkpoint_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp) / "run"
            for epoch in (1, 2, 3):
                self.make_checkpoint(run, epoch, self.settings())
            completion = training.training_completion(run, training.evaluation.sha(run / "config.json"))
            (run / "training-complete.json").write_bytes(training.encoded(completion))
            self.assertEqual(training.resume_checkpoint(run, self.settings())[0], 3)
            complete_path = run / "checkpoints/epoch-1/complete.json"
            changed = json.loads(complete_path.read_text(encoding="utf-8"))
            changed["extra_metadata"] = "changed after final completion"
            complete_path.write_bytes(training.encoded(changed))
            (complete_path.parent / "inference-state.json").write_bytes(training.encoded(training.checkpoint_inference_state(complete_path.parent, changed)))
            with self.assertRaisesRegex(ValueError, "receipt hash mismatch"):
                training.resume_checkpoint(run, self.settings())

    def test_production_sealer_works_with_actual_old_integrity_and_runtime_model_binding(self):
        import retrieval_runtime
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = root / "models/base-model"
            base.mkdir(parents=True)
            (base / "config.json").write_text('{"model_type":"synthetic"}', encoding="utf-8")
            (base / "model.safetensors").write_bytes(b"synthetic base model bytes, not loaded")
            info = {"path": str(base), "model": training.MODEL, "revision": training.REVISION,
                    "files": [{"name": p.name, "sha256": training.evaluation.sha(p), "bytes": p.stat().st_size} for p in sorted(base.iterdir())]}
            (base.parent / "manifest.json").write_bytes(training.encoded({"reranker": info}))
            checkpoint = root / "run/checkpoints/epoch-1"
            checkpoint.mkdir(parents=True)
            (checkpoint / "adapter_config.json").write_bytes(training.encoded({"base_model_name_or_path": str(base)}))
            for name in ("adapter_model.safetensors", "optimizer.pt", "rng.pt", "tokenizer.json"):
                (checkpoint / name).write_bytes(("synthetic " + name).encode())
            with mock.patch.object(training, "ROOT", root):
                training.seal_checkpoint_files(checkpoint, 1, 4, "c" * 64, {"pair_count": 50})
            actual = retrieval_runtime.model_binding(checkpoint)
            self.assertEqual(actual["path"], str(checkpoint.resolve()))
            self.assertEqual(actual["inference"]["base_model"], info)
            self.assertEqual(actual["inference"]["adapter_state_sha256"], training.evaluation.sha(checkpoint / "inference-state.json"))
            self.assertEqual(actual["inference"]["max_length"], 256)
            (checkpoint / "tokenizer.json").write_bytes(b"tampered inference input")
            with self.assertRaisesRegex(ValueError, "hash drift"):
                retrieval_runtime.model_binding(checkpoint)

    def test_insufficient_train_manifest_never_enters_gpu_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_path = root / "MANIFEST.json"
            manifest_path.write_bytes(training.encoded({"status": "INSUFFICIENT_TRAINING_QUERIES", "valid_query_count": 49}))
            args = argparse.Namespace(prepared_manifest=manifest_path, prepared_sha256=training.evaluation.sha(manifest_path), run_name="unused")
            with mock.patch.object(training, "DATA", root), mock.patch.object(training, "RunLock") as lock:
                with self.assertRaisesRegex(ValueError, "insufficient"):
                    training.train(args)
                lock.assert_not_called()
            self.assertFalse(list(root.rglob("STARTED.json")))


@unittest.skipUnless(importlib.util.find_spec("torch"), "torch unavailable for tiny CPU loss check")
class CpuLossTests(unittest.TestCase):
    def test_linear_optimizer_step_increases_positive_margin(self):
        import torch
        model = torch.nn.Linear(1, 1, bias=False, device="cpu")
        with torch.no_grad():
            model.weight.zero_()
        optimizer = torch.optim.SGD(model.parameters(), lr=.1)
        high_x, low_x = torch.ones(3, 1), torch.zeros(3, 1)
        initial = float((model(high_x) - model(low_x)).mean().detach())
        loss = training.pairwise_loss(model(high_x), model(low_x))
        loss.backward()
        self.assertLess(float(model.weight.grad), 0)
        optimizer.step()
        final = float((model(high_x) - model(low_x)).mean().detach())
        self.assertGreater(final, initial)

    def test_microbatch_two_accumulation_equals_full_pair_mean_including_tail(self):
        import torch
        for count in (16, 3):
            full = torch.nn.Linear(1, 1, bias=False, device="cpu")
            micro = torch.nn.Linear(1, 1, bias=False, device="cpu")
            with torch.no_grad():
                full.weight.fill_(.2)
                micro.weight.copy_(full.weight)
            high = torch.arange(1, count + 1, dtype=torch.float32).view(-1, 1) / count
            low = torch.zeros_like(high)
            training.pairwise_loss(full(high), full(low)).backward()
            for begin in range(0, count, 2):
                loss = training.pairwise_loss(micro(high[begin:begin + 2]), micro(low[begin:begin + 2]), reduction="sum") / count
                loss.backward()
            self.assertTrue(torch.allclose(full.weight.grad, micro.weight.grad, atol=1e-7, rtol=1e-6))

    def test_nonfinite_scores_rejected(self):
        import torch
        with self.assertRaisesRegex(ValueError, "Nonfinite"):
            training.pairwise_loss(torch.tensor([float("nan")]), torch.tensor([0.]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
