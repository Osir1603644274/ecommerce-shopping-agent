"""Pure-Python pooled metrics; uncertainty never creates a relevance label.

All participating rankings for a query must use one common calculation pool:
the qrel pool plus the union of their Top-k documents. Outside-qrel documents
are unjudged, not negative. Recall denominators are *known* qrel positives.

nDCG bounds deliberately relax numerator/denominator dependence. They are
conservative, not necessarily attainable extrema. They apply only to this
finite calculation pool, and not to unobserved full-corpus relevance. When a
completion has no positive gain, nDCG is undefined; bounds concern the other
completions, and gain_not_guaranteed explicitly records this limitation.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import random
import statistics

VERSION = "search-closure-pooled-metrics-v2.0"
SOURCES = ("kuaisearch", "multicpr")
UNKNOWN = "UNKNOWN"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def validate_grade(grade):
    require((type(grade) is int and 0 <= grade <= 3) or
            (type(grade) is str and grade == UNKNOWN), f"Invalid grade: {grade!r}")


def validate_ranking(ranking):
    require(type(ranking) is list, "Ranking must be an ordered list")
    require(all(type(d) is str and d and d == d.strip() for d in ranking), "Invalid ranked ID")
    require(len(ranking) == len(set(ranking)), "Duplicate ranked document ID")


def validate_judgments(judgments):
    require(type(judgments) is dict, "Judgments must be a document-grade mapping")
    for docid, grade in judgments.items():
        require(type(docid) is str and docid and docid == docid.strip(), "Invalid qrel document ID")
        validate_grade(grade)


def dcg(grades, k=10):
    require(type(k) is int and k > 0, "Invalid metric cutoff")
    require(all(type(g) is int and 0 <= g <= 3 for g in grades), "DCG needs numeric grades")
    return math.fsum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(grades[:k]))


def build_common_pool(judgments, rankings, k=10):
    """Union is shared across all methods, and never persisted as numeric qrels."""
    validate_judgments(judgments)
    require(type(rankings) is dict and rankings, "No participating rankings")
    pool = set(judgments)
    for ranking in rankings.values():
        validate_ranking(ranking)
        pool.update(ranking[:k])
    return sorted(pool)


def query_metrics(ranking, judgments, *, common_pool=None, k=10, unknown_causes=None):
    validate_ranking(ranking)
    validate_judgments(judgments)
    require(type(k) is int and k > 0, "Invalid metric cutoff")
    top = ranking[:k]
    if common_pool is None:
        common_pool = sorted(set(judgments) | set(top))
    require(type(common_pool) in (list, tuple, set), "Invalid common pool")
    require(all(type(d) is str and d for d in common_pool), "Invalid common pool ID")
    require(len(common_pool) == len(set(common_pool)), "Duplicate common pool ID")
    pool = set(common_pool)
    require(set(judgments) <= pool and set(top) <= pool, "Common pool omits qrels or Top-k")
    known = {d: g for d, g in judgments.items() if type(g) is int}
    unknown = {d for d, g in judgments.items() if g == UNKNOWN}
    missing = pool - set(judgments)
    uncertain = unknown | missing
    known_ideal = dcg(sorted(known.values(), reverse=True), k)
    possible_ideal = dcg(sorted([*known.values(), *([3] * len(uncertain))], reverse=True), k)
    numerator_lower = dcg([known.get(d, 0) for d in top], k)
    numerator_upper = dcg([known[d] if d in known else 3 for d in top], k)
    lower = upper = point = None
    if possible_ideal > 0:
        lower = numerator_lower / possible_ideal
        upper = min(1.0, numerator_upper / known_ideal) if known_ideal > 0 else 1.0
        # No retrieved item could carry gain => exact zero conditional on IDCG>0.
        if numerator_upper == 0:
            upper = 0.0
        # Publish a unique point only if defined for *every* completion.
        if known_ideal > 0 and (not uncertain or lower == upper):
            point = numerator_lower / known_ideal if not uncertain else lower
    if possible_ideal == 0:
        status = "no_gain" if judgments else "missing_qrels"
    elif not judgments:
        status = "missing_qrels"
    elif missing:
        status = "missing_labels"
    elif point is not None:
        status = "point"
    else:
        status = "bounded"
    recalls = {}
    for name, positive in (("grade_ge_2", {d for d, g in known.items() if g >= 2}),
                           ("grade_eq_3", {d for d, g in known.items() if g == 3})):
        recalls[name] = {}
        for depth in (10, 100, 300):
            numerator = len(set(ranking[:depth]) & positive)
            recalls[name][str(depth)] = {
                "value": numerator / len(positive) if positive else None,
                "numerator": numerator, "denominator": len(positive),
                "status": "known_positive_only" if positive else "no_known_relevant",
            }
    causes = Counter()
    for docid in unknown:
        values = (unknown_causes or {}).get(docid, ["not_recorded"])
        require(type(values) is list and all(type(v) is str and v for v in values), "Invalid unknown causes")
        causes.update(set(values) or ["not_recorded"])
    return {
        "status": status, "ndcg_at_10": point,
        "ndcg_lower_bound_at_10": lower, "ndcg_upper_bound_at_10": upper,
        "metric_cutoff": k, "gain_not_guaranteed": known_ideal == 0,
        "idcg_known": known_ideal, "idcg_possible": possible_ideal,
        "bound_kind": "conservative_numerator_denominator_relaxation",
        "judged_at_10": sum(d in known for d in top) / k,
        "judged_count_at_10": sum(d in known for d in top),
        "returned_at_10": len(top), "short_return_at_10": len(top) < k,
        "missing_return_slots_at_10": max(0, k - len(top)),
        "ranking_length": len(ranking), "unknown_top10": sum(d in unknown for d in top),
        "outside_qrel_pool_top10": sum(d not in judgments for d in top),
        "outside_qrel_pool_top10_ids": [d for d in top if d not in judgments],
        "qrel_pool_count": len(judgments), "pool_known_count": len(known),
        "pool_unknown_count": len(unknown), "common_pool_count": len(pool),
        "common_pool_missing_label_count": len(missing),
        "common_pool_sha256": canonical_hash(sorted(pool)),
        "qrel_mapping_sha256": canonical_hash(judgments),
        "unknown_cause_counts": dict(sorted(causes.items())), "known_pooled_recall": recalls,
        "scope": "Finite common pool; silver-label uncertainty; not full-corpus recall or relevance.",
    }


def shared_eligibility(records, methods, *, sources=SOURCES):
    """One explicit, method-independent denominator for limited dev selection.

    All methods need the same qrel/candidate pool and complete query identities.
    A no-gain query stays in total coverage but not the common conditional mean.
    There is no valid equal-source selection if either source loses all queries.
    """
    by_method = {method: {} for method in methods}
    for row in records:
        require(row["method"] in by_method, "Foreign method in eligibility")
        require(row["query_id"] not in by_method[row["method"]], "Duplicate method/query eligibility")
        by_method[row["method"]][row["query_id"]] = row
    require(by_method and all(by_method.values()), "Empty eligibility input")
    identities = set(next(iter(by_method.values())))
    require(all(set(rows) == identities for rows in by_method.values()), "Eligibility query identity mismatch")
    eligible, excluded, source_by_query = [], {}, {}
    for qid in sorted(identities):
        rows = [by_method[m][qid] for m in methods]
        require(len({r["common_pool_sha256"] for r in rows}) == 1, "Eligibility common pool mismatch")
        require(len({r["qrel_mapping_sha256"] for r in rows}) == 1, "Eligibility qrel hash mismatch")
        require(len({r["source"] for r in rows}) == 1, "Eligibility source mismatch")
        source_by_query[qid] = rows[0]["source"]
        require(source_by_query[qid] in sources, "Unknown eligibility source")
        defined = [r["ndcg_lower_bound_at_10"] is not None and r["ndcg_upper_bound_at_10"] is not None for r in rows]
        require(all(defined) or not any(defined), "Method-dependent scoring eligibility on common pool")
        if all(defined):
            eligible.append(qid)
        else:
            excluded[qid] = {m: by_method[m][qid]["status"] for m in methods}
    counts = {source: {"total": sum(s == source for s in source_by_query.values()),
                       "eligible": sum(source_by_query[q] == source for q in eligible)} for source in sources}
    valid = bool(eligible) and all(v["eligible"] for v in counts.values())
    result = {
        "status": "FULL_COHORT_SELECTION_AVAILABLE" if valid and not excluded else
                  "COMMON_CONDITIONAL_SUBSET_ONLY" if valid else "NO_VALID_SELECTION",
        "methods": sorted(methods), "total_query_count": len(identities),
        "all_query_ids": sorted(identities), "eligible_query_count": len(eligible),
        "eligible_query_ids": eligible, "excluded_queries": excluded, "source_counts": counts,
        "source_by_query": source_by_query,
        "scope": "Only the declared common subset; excluded no-gain queries remain in coverage.",
    }
    result["manifest_sha256"] = canonical_hash(result)
    return result


def _mean(values):
    return statistics.fmean(values) if values else None


def aggregate(records, *, sources=SOURCES):
    """Keep every query in denominators; incomplete groups have no primary mean."""
    require(records, "Empty metric group")
    require(len({r["query_id"] for r in records}) == len(records), "Duplicate query in aggregation")
    require(all(r["source"] in sources for r in records), "Unknown metric source")
    by_source = {}
    for source in sources:
        rows = [r for r in records if r["source"] == source]
        eligible = [r for r in rows if r["ndcg_lower_bound_at_10"] is not None]
        exact = [r for r in rows if r["ndcg_at_10"] is not None]
        full = bool(rows) and len(eligible) == len(rows)
        by_source[source] = {
            "query_count": len(rows), "query_ids": sorted(r["query_id"] for r in rows),
            "status_counts": dict(sorted(Counter(r["status"] for r in rows).items())),
            "eligible_query_count": len(eligible), "point_query_count": len(exact),
            "unscorable_query_ids": sorted(r["query_id"] for r in rows if r not in eligible),
            "status": "complete" if full else "incomplete_or_empty_source",
            "mean_lower": _mean([r["ndcg_lower_bound_at_10"] for r in rows]) if full else None,
            "mean_upper": _mean([r["ndcg_upper_bound_at_10"] for r in rows]) if full else None,
            "mean_point": _mean([r["ndcg_at_10"] for r in exact]) if len(exact) == len(rows) else None,
            "conditional_mean_lower": _mean([r["ndcg_lower_bound_at_10"] for r in eligible]),
            "conditional_mean_upper": _mean([r["ndcg_upper_bound_at_10"] for r in eligible]),
            "mean_judged_at_10": _mean([r["judged_at_10"] for r in rows]),
            "short_return_query_count": sum(r["short_return_at_10"] for r in rows),
            "outside_qrel_pool_top10_count": sum(r["outside_qrel_pool_top10"] for r in rows),
        }
    complete = all(r["status"] == "complete" for r in by_source.values())
    return {
        "query_count": len(records), "query_ids": sorted(r["query_id"] for r in records),
        "status": "complete" if complete else "incomplete_no_gain_or_empty_source",
        "source_weighting": "equal source mean; equal query weights within source",
        "equal_source_mean_lower": _mean([s["mean_lower"] for s in by_source.values()]) if complete else None,
        "equal_source_mean_upper": _mean([s["mean_upper"] for s in by_source.values()]) if complete else None,
        "by_source": by_source,
        "no_gain_policy": "undefined, retained with status; never silently zero-filled or discarded",
    }


def _quantile(values, p):
    values = sorted(values)
    index = (len(values) - 1) * p
    lo, hi = math.floor(index), math.ceil(index)
    return values[lo] + (values[hi] - values[lo]) * (index - lo)


def paired_interval_bootstrap(base, new, *, sources=SOURCES, seed=20260909, repetitions=10000):
    """Paired query bootstrap with independent within-source resampling.

    The pointwise difference interval is [L_new-U_base, U_new-L_base].
    CI95 is the outer percentile envelope of bootstrapped lower/upper means.
    It does not model systematic silver-label bias or the full corpus.
    """
    require(type(seed) is int and type(repetitions) is int and repetitions > 0, "Invalid bootstrap setup")
    require(set(base) == set(new) and base, "Paired query identity mismatch")
    differences, groups, missing = {}, {s: [] for s in sources}, []
    for qid in sorted(base):
        old, current = base[qid], new[qid]
        require(old["source"] == current["source"] and old["source"] in groups, "Paired source mismatch")
        require(old["common_pool_sha256"] == current["common_pool_sha256"], "Paired common pool mismatch")
        require(old["qrel_mapping_sha256"] == current["qrel_mapping_sha256"], "Paired qrel hash mismatch")
        bounds = [old["ndcg_lower_bound_at_10"], old["ndcg_upper_bound_at_10"],
                  current["ndcg_lower_bound_at_10"], current["ndcg_upper_bound_at_10"]]
        if any(v is None for v in bounds):
            require(all(v is None for v in bounds), "One-sided no-gain query on a common pool")
            differences[qid] = {"source": old["source"], "status": "undefined_no_gain", "interval": None}
            missing.append(qid)
            continue
        require(all(type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 1 for v in bounds), "Invalid nDCG bound")
        require(bounds[0] <= bounds[1] and bounds[2] <= bounds[3], "Reversed nDCG bounds")
        interval = [bounds[2] - bounds[1], bounds[3] - bounds[0]]
        differences[qid] = {"source": old["source"], "status": "bounded", "interval": interval}
        groups[old["source"]].append(interval)
    complete = not missing and all(groups.values())
    by_source = {}
    for source, intervals in groups.items():
        original_count = sum(base[q]["source"] == source for q in base)
        by_source[source] = {"query_count": original_count, "eligible_query_count": len(intervals),
                             "conditional_mean_interval": [_mean([x[i] for x in intervals]) for i in (0, 1)]}
    result = {
        "status": "complete" if complete else "incomplete_no_gain_or_empty_source",
        "query_count": len(base), "query_ids": sorted(base), "unscorable_query_ids": missing,
        "seed": seed, "repetitions": repetitions, "per_query": differences, "by_source": by_source,
        "equal_source_mean_difference_interval": None, "ci95_outer_envelope": None,
        "orientation": "new minus base", "positive_improvement_supported": False,
        "scope": "Paired stratified query sampling; does not cover systematic silver-label bias.",
    }
    if not complete:
        return result
    result["equal_source_mean_difference_interval"] = [
        _mean([_mean([x[i] for x in intervals]) for intervals in groups.values()]) for i in (0, 1)]
    rng = random.Random(seed)
    samples = [[], []]
    source_samples = {s: [[], []] for s in sources}
    for _ in range(repetitions):
        means = []
        for source in sources:
            intervals = groups[source]
            selected = [intervals[rng.randrange(len(intervals))] for _ in intervals]
            mean = [_mean([x[i] for x in selected]) for i in (0, 1)]
            means.append(mean)
            for i in (0, 1):
                source_samples[source][i].append(mean[i])
        for i in (0, 1):
            samples[i].append(_mean([x[i] for x in means]))
    result["ci95_outer_envelope"] = [_quantile(samples[0], .025), _quantile(samples[1], .975)]
    for source in sources:
        result["by_source"][source]["ci95_outer_envelope"] = [
            _quantile(source_samples[source][0], .025), _quantile(source_samples[source][1], .975)]
    result["positive_improvement_supported"] = result["ci95_outer_envelope"][0] > 0
    return result
