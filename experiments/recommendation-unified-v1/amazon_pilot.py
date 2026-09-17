"""CPU-only Amazon Luxury Beauty development pilot; never scores the final split.

Predictors accept only catalog, fit artifacts and public histories. Review text,
ratings aggregates and related-product metadata never enter model features.
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import gzip
import hashlib
import html
import itertools
import json
import math
import os
from pathlib import Path
import re
from typing import Iterable

SOURCE = "amazon2018_luxury_beauty"
ARMS = ("popular", "itemcf", "content_tfidf")


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def read_rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                yield line_number, json.loads(line)


def write_rows(path: Path, rows: Iterable[dict]) -> None:
    """Atomic immutable artifacts. An interrupted stage can reproduce equal files."""
    temporary = path.with_name(path.name + ".partial")
    with temporary.open("wb") as stream:
        for row in rows:
            stream.write(canonical(row))
    if path.exists():
        if sha256(path) != sha256(temporary):
            raise ValueError(f"refusing to overwrite a different artifact: {path}")
        temporary.unlink()
    else:
        os.replace(temporary, path)


def write_json(path: Path, value: dict) -> None:
    write_rows(path, [value])


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def clean_reviews(path: Path):
    groups = {}
    raw_count = 0
    for line_number, row in read_rows(path):
        raw_count += 1
        user, item, timestamp, rating = (row.get(k) for k in ("reviewerID", "asin", "unixReviewTime", "overall"))
        if not isinstance(user, str) or not user or not isinstance(item, str) or not item:
            raise ValueError(f"invalid review identity at line {line_number}")
        if type(timestamp) is not int or timestamp < 0 or type(rating) not in (int, float) or not 1 <= rating <= 5:
            raise ValueError(f"invalid timestamp/rating at line {line_number}")
        group = groups.setdefault((user, item, timestamp), {"ratings": set(), "hashes": set(), "rows": 0, "first_line": line_number})
        group["ratings"].add(float(rating))
        group["hashes"].add(hashlib.sha256(canonical(row)).hexdigest())
        group["rows"] += 1
    events, quarantine = [], []
    exact_duplicates = same_rating_collapsed = conflict_rows = conflict_duplicates = 0
    for (user, item, timestamp), group in sorted(groups.items()):
        exact_duplicates += group["rows"] - len(group["hashes"])
        if len(group["ratings"]) != 1:
            conflict_rows += group["rows"]
            conflict_duplicates += group["rows"] - len(group["hashes"])
            quarantine.append({"user_id": user, "item_id": item, "timestamp": timestamp,
                               "ratings": sorted(group["ratings"]), "raw_rows": group["rows"],
                               "unique_full_records": len(group["hashes"]), "reason": "rating_conflict_entire_group"})
            continue
        same_rating_collapsed += len(group["hashes"]) - 1
        events.append({"user_id": user, "item_id": item, "timestamp": timestamp,
                       "rating": next(iter(group["ratings"])), "event_kind": "review",
                       "timestamp_kind": "unix_seconds", "first_source_line": group["first_line"]})
    audit = {"raw_rows": raw_count, "group_count": len(groups), "clean_events": len(events),
             "exact_duplicate_rows": exact_duplicates, "same_rating_nonidentical_rows_collapsed": same_rating_collapsed,
             "rating_conflict_groups": len(quarantine), "rating_conflict_raw_rows": conflict_rows,
             "exact_duplicate_rows_within_quarantined_groups": conflict_duplicates,
             "nonconflicting_exact_duplicate_rows": exact_duplicates - conflict_duplicates}
    assert raw_count == len(events) + exact_duplicates - conflict_duplicates + same_rating_collapsed + conflict_rows
    return events, audit, quarantine


def display_text(value) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())


def category_text(value) -> str:
    if isinstance(value, list):
        return " ".join(filter(None, (category_text(part) for part in value)))
    return display_text(value)


def clean_metadata(path: Path):
    groups = {}
    raw_count = 0
    for line_number, row in read_rows(path):
        raw_count += 1
        item = row.get("asin")
        if not isinstance(item, str) or not item:
            raise ValueError(f"invalid metadata ASIN at line {line_number}")
        digest = hashlib.sha256(canonical(row)).hexdigest()
        if item not in groups:
            groups[item] = {"hashes": set(), "rows": 0, "product": {
                "item_id": item, "source": SOURCE, "title": display_text(row.get("title")),
                "brand": display_text(row.get("brand")), "category": category_text(row.get("category")),
                "price_minor": None, "currency": None, "money_unit": None,
                "price_missing_reason": "not_used_in_this_pilot", "first_source_line": line_number}}
        groups[item]["hashes"].add(digest)
        groups[item]["rows"] += 1
    catalog, quarantine, excluded = [], [], {}
    exact_duplicates = 0
    for item, group in sorted(groups.items()):
        exact_duplicates += group["rows"] - len(group["hashes"])
        if len(group["hashes"]) > 1:
            excluded[item] = "conflicting_metadata"
            quarantine.append({"item_id": item, "raw_rows": group["rows"],
                               "unique_full_records": len(group["hashes"]), "reason": "metadata_conflict_entire_id"})
        elif not group["product"]["title"]:
            excluded[item] = "missing_title"
        else:
            catalog.append(group["product"])
    audit = {"raw_rows": raw_count, "unique_asins": len(groups), "exact_duplicate_rows": exact_duplicates,
             "conflict_asins": len(quarantine), "missing_title_asins": sum(v == "missing_title" for v in excluded.values()),
             "catalog_items": len(catalog)}
    return catalog, audit, quarantine, excluded


def boundaries(events: list[dict]) -> dict:
    if not events:
        raise ValueError("no cleaned events")
    times = sorted(event["timestamp"] for event in events)
    return {"fit_last_inclusive": times[math.ceil(len(times) * .70) - 1],
            "dev_last_inclusive": times[math.ceil(len(times) * .85) - 1],
            "rule": "global cleaned-event empirical 70/85 percentiles; ties remain in earlier partition"}


def partition(timestamp: int, threshold: dict) -> str:
    if timestamp <= threshold["fit_last_inclusive"]:
        return "fit"
    return "dev" if timestamp <= threshold["dev_last_inclusive"] else "final"


def dev_requests(events: list[dict], threshold: dict):
    fit_by_user, dev_by_user = collections.defaultdict(list), collections.defaultdict(list)
    reviewed_fit = collections.defaultdict(set)
    for event in events:
        split = partition(event["timestamp"], threshold)
        if split == "fit":
            reviewed_fit[event["user_id"]].add(event["item_id"])
            if event["rating"] >= 4:
                fit_by_user[event["user_id"]].append(event)
        elif split == "dev":
            dev_by_user[event["user_id"]].append(event)
    selected = []
    for user, dev_events in sorted(dev_by_user.items()):
        history = sorted(fit_by_user[user], key=lambda event: (event["timestamp"], event["item_id"]))
        if not history:
            continue
        seen = reviewed_fit[user]
        for timestamp, day_events in itertools.groupby(sorted(dev_events, key=lambda event: event["timestamp"]), key=lambda event: event["timestamp"]):
            targets = sorted((event for event in day_events if event["rating"] >= 4 and event["item_id"] not in seen),
                             key=lambda event: event["item_id"])
            if targets:
                strict_history = [event for event in history if event["timestamp"] < timestamp]
                if strict_history:
                    selected.append((user, strict_history, targets))
                break
    public, labels = [], []
    for index, (user, history, targets) in enumerate(selected, 1):
        request_id = f"amazon-dev-{index:06d}"
        public.append({"request_id": request_id, "source": SOURCE, "user_namespace": SOURCE,
                       "user_id": user, "cutoff_timestamp": targets[0]["timestamp"],
                       "seen_all_fit_item_ids": sorted(reviewed_fit[user]),
                       "history": [{k: event[k] for k in ("item_id", "timestamp", "event_kind", "timestamp_kind")} for event in history]})
        labels.append({"request_id": request_id, "target_item_ids": [target["item_id"] for target in targets],
                       "ratings_by_item": {target["item_id"]: target["rating"] for target in targets},
                       "timestamp": targets[0]["timestamp"], "event_kind": "review"})
    return public, labels, fit_by_user, {"users_with_any_dev_event": len(dev_by_user),
                                       "eligible_dev_users": len(selected),
                                       "users_with_fit_positive_history": sum(bool(v) for v in fit_by_user.values())}


def prepare(review_path: Path, metadata_path: Path, out: Path) -> dict:
    events, review_audit, review_quarantine = clean_reviews(review_path)
    catalog, meta_audit, meta_quarantine, excluded = clean_metadata(metadata_path)
    threshold = boundaries(events)
    public, labels, fit_by_user, cohort_audit = dev_requests(events, threshold)
    protocol = {"schema_version": 1, "status": "DEVELOPMENT_PILOT_NOT_FINAL_EVALUATION", "source": SOURCE,
                "source_files": {str(path.resolve()): sha256(path) for path in (review_path, metadata_path)},
                "implementation_sha256": sha256(Path(__file__)),
                "thresholds": threshold, "positive_event": "review with overall >= 4; never click/purchase",
                "candidate_catalog": "all nonconflicting static metadata ASINs with a nonempty normalized title",
                "history": "only fit positive reviews strictly before request time; no dev history updates",
                "target": "all new positive ASINs on each user's earliest eligible dev day, absent from ALL fit reviews regardless of rating; all eligible users; unordered target set",
                "seen_item_exclusion": "both targets and predictions exclude every fit-reviewed ASIN, including low ratings; interest features use only positive fit history",
                "timestamp_precision": "observed daily UTC timestamps; same-day interactions are unordered, never a within-day causal sequence",
                "cold_item": "zero positive fit users, including targets outside candidate catalog",
                "arms": list(ARMS), "top_k": 100, "ndcg_k": 10, "tuning": "none",
                "itemcf": "binary fit user-item co-occurrence / sqrt(item user counts), history score sum",
                "content_tfidf": "static catalog title/brand/category; log TF, smoothed static-catalog IDF, unit document vectors, mean history then unit query",
                "content_tokenization": "HTML unescape; lowercase ASCII alphanumeric tokens length >= 2; no review text",
                "metadata_time_limit": "static metadata has no verified historical snapshot time; content is a static retrieval experiment",
                "final_policy": "partition and joinability counts only; no final requests, labels, predictions or scores",
                "unretrieved_policy": "every eligible dev target remains in denominator; out-of-catalog targets score zero",
                "metrics": "macro user Hit@100, Recall@100 and binary nDCG@10 over full target sets; ideal DCG includes out-of-catalog positives",
                "item_slices": "warm/cold metrics use each request's respective target subset with original candidate ranks; mixed requests occur in both slices",
                "duplicate_policy": "full canonical-record hash exact dedup; same user/ASIN/time same rating collapses; conflicting ratings quarantine entire group; metadata conflict quarantines ASIN"}
    # Freeze the complete algorithm/threshold contract before writing predictions.
    write_json(out / "PROTOCOL.json", protocol)
    catalog_ids = {product["item_id"] for product in catalog}
    split_counts = collections.Counter(partition(event["timestamp"], threshold) for event in events)
    join_counts = collections.Counter()
    join_failures = []
    for event in events:
        if event["item_id"] not in catalog_ids:
            split = partition(event["timestamp"], threshold)
            reason = excluded.get(event["item_id"], "metadata_absent")
            join_counts[f"{split}:{reason}"] += 1
            if split != "final":
                join_failures.append({"split": split, "reason": reason, **event})
    user_items = {user: sorted({event["item_id"] for event in history}) for user, history in sorted(fit_by_user.items()) if history}
    reviewed_fit = collections.defaultdict(set)
    for event in events:
        if partition(event["timestamp"], threshold) == "fit":
            reviewed_fit[event["user_id"]].add(event["item_id"])
    counts = collections.Counter(item for items in user_items.values() for item in items)
    fit = {"source": SOURCE, "user_positive_items": user_items,
           "user_reviewed_items": {user: sorted(items) for user, items in sorted(reviewed_fit.items())},
           "positive_item_user_counts": dict(sorted(counts.items())),
           "positive_events": sum(len(history) for history in fit_by_user.values()), "fit_last_inclusive": threshold["fit_last_inclusive"]}
    audit = {"reviews": review_audit, "metadata": meta_audit, "cohort": cohort_audit,
             "partition_event_counts": dict(split_counts), "join_failure_counts": dict(sorted(join_counts.items())),
             "dev_target_items": sum(len(label["target_item_ids"]) for label in labels),
             "dev_targets_outside_catalog": sum(item not in catalog_ids for label in labels for item in label["target_item_ids"]),
             "no_final_evaluation": True}
    write_rows(out / "catalog.jsonl", catalog)
    write_rows(out / "public_histories.jsonl", public)
    write_rows(out / "labels.private.jsonl", labels)
    write_json(out / "fit_artifacts.json", fit)
    write_rows(out / "fit_events.jsonl", (event for event in events if partition(event["timestamp"], threshold) == "fit"))
    write_rows(out / "review_quarantine.jsonl", review_quarantine)
    write_rows(out / "metadata_quarantine.jsonl", meta_quarantine)
    write_rows(out / "join_failures.fit_dev.jsonl", join_failures)
    write_json(out / "SOURCE_AUDIT.json", audit)
    feature_files = ("PROTOCOL.json", "catalog.jsonl", "public_histories.jsonl", "fit_artifacts.json")
    write_json(out / "PREPARED.json", {"feature_artifacts_sha256": {name: sha256(out / name) for name in feature_files},
                                        "labels_sha256": sha256(out / "labels.private.jsonl")})
    return audit


def tokenize(product: dict) -> list[str]:
    return re.findall(r"[a-z0-9]{2,}", " ".join(product.get(field, "") for field in ("title", "brand", "category")).lower())


def content_index(catalog: list[dict]):
    term_counts = {product["item_id"]: collections.Counter(tokenize(product)) for product in catalog}
    document_frequency = collections.Counter(term for counts in term_counts.values() for term in counts)
    idf = {term: math.log((1 + len(catalog)) / (1 + count)) + 1 for term, count in document_frequency.items()}
    vectors, postings = {}, collections.defaultdict(list)
    for item, counts in term_counts.items():
        weights = {term: (1 + math.log(count)) * idf[term] for term, count in counts.items()}
        norm = math.sqrt(sum(weight * weight for weight in weights.values()))
        vector = {term: weight / norm for term, weight in weights.items()} if norm else {}
        vectors[item] = vector
        for term, weight in vector.items():
            postings[term].append((item, weight))
    return vectors, postings, {"vocabulary_terms": len(idf), "nonempty_document_vectors": sum(bool(v) for v in vectors.values()),
                                "idf_scope": "entire static metadata catalog; no behavior or labels"}


def cf_index(fit: dict, catalog_ids: set[str]):
    co_users = collections.defaultdict(collections.Counter)
    counts = fit["positive_item_user_counts"]
    for items in fit["user_positive_items"].values():
        known = sorted(set(items) & catalog_ids)
        for left, right in itertools.combinations(known, 2):
            co_users[left][right] += 1
            co_users[right][left] += 1
    return {left: {right: count / math.sqrt(counts[left] * counts[right]) for right, count in row.items()}
            for left, row in co_users.items()}


def check_public(request: dict, fit: dict) -> set[str]:
    expected = {"request_id", "source", "user_namespace", "user_id", "cutoff_timestamp", "history", "seen_all_fit_item_ids"}
    if set(request) != expected or request["source"] != SOURCE or request["user_namespace"] != SOURCE:
        raise ValueError("public request schema/source mismatch; labels and query text are forbidden")
    known = set(fit["user_positive_items"].get(request["user_id"], []))
    all_reviewed = set(fit["user_reviewed_items"].get(request["user_id"], []))
    if set(request["seen_all_fit_item_ids"]) != all_reviewed or len(request["seen_all_fit_item_ids"]) != len(all_reviewed):
        raise ValueError("seen-all-fit identities do not match fit artifacts")
    seen = set()
    for event in request["history"]:
        if set(event) != {"item_id", "timestamp", "event_kind", "timestamp_kind"}:
            raise ValueError("public history has unexpected fields")
        if event["event_kind"] != "review" or event["timestamp_kind"] != "unix_seconds":
            raise ValueError("history must retain review and timestamp semantics")
        if not event["timestamp"] < request["cutoff_timestamp"] or event["timestamp"] > fit["fit_last_inclusive"]:
            raise ValueError("history is not strictly earlier fit evidence")
        if event["item_id"] not in known:
            raise ValueError("history item is not in fit positive artifacts")
        seen.add(event["item_id"])
    if not seen.issubset(all_reviewed):
        raise ValueError("positive history must be a subset of all reviewed items")
    return all_reviewed


def top_items(scores: dict, seen: set[str], catalog_ids: set[str], k: int = 100) -> list[str]:
    ranked = sorted(((item, score) for item, score in scores.items()
                     if item in catalog_ids and item not in seen and math.isfinite(score) and score > 0),
                    key=lambda pair: (-pair[1], pair[0]))
    return [item for item, _ in ranked[:k]]


def predict(catalog: list[dict], fit: dict, public: list[dict]):
    """Pure independent prediction API: it receives no target or label argument."""
    catalog_ids = {product["item_id"] for product in catalog}
    if len(catalog_ids) != len(catalog):
        raise ValueError("duplicate catalog identity")
    vectors, postings, content_audit = content_index(catalog)
    neighbors = cf_index(fit, catalog_ids)
    predictions = []
    request_ids = set()
    for request in public:
        if request["request_id"] in request_ids:
            raise ValueError("duplicate request_id")
        request_ids.add(request["request_id"])
        seen = check_public(request, fit)
        interest_items = {event["item_id"] for event in request["history"]}
        cf_scores = collections.Counter()
        for item in sorted(interest_items):
            cf_scores.update(neighbors.get(item, {}))
        query = collections.Counter()
        for item in sorted(interest_items):
            query.update(vectors.get(item, {}))
        # Mean and sum have identical direction after unit normalization.
        norm = math.sqrt(sum(weight * weight for weight in query.values()))
        content_scores = collections.Counter()
        if norm:
            for term, weight in sorted(query.items()):
                for item, value in postings[term]:
                    content_scores[item] += weight / norm * value
        scores_by_arm = {"popular": fit["positive_item_user_counts"], "itemcf": cf_scores, "content_tfidf": content_scores}
        for arm in ARMS:
            predictions.append({"request_id": request["request_id"], "arm": arm, "source": SOURCE,
                                "item_ids": top_items(scores_by_arm[arm], seen, catalog_ids)})
    return predictions, {"content": content_audit, "cf_items_with_neighbors": len(neighbors),
                         "cf_directed_edges": sum(len(row) for row in neighbors.values()),
                         "prediction_rows": len(predictions), "no_popularity_backfill": True}


def predict_files(out: Path) -> dict:
    # The predictor never opens labels. Digest validation reads only public/fit files.
    if (out / "PREPARED.json").exists():
        for name, expected in read_json(out / "PREPARED.json")["feature_artifacts_sha256"].items():
            if name not in {"PROTOCOL.json", "catalog.jsonl", "public_histories.jsonl", "fit_artifacts.json"}:
                raise ValueError("unexpected feature artifact path")
            if sha256(out / name) != expected:
                raise ValueError(f"frozen feature artifact changed: {name}")
    catalog = [row for _, row in read_rows(out / "catalog.jsonl")]
    fit = read_json(out / "fit_artifacts.json")
    public = [row for _, row in read_rows(out / "public_histories.jsonl")]
    predictions, audit = predict(catalog, fit, public)
    write_rows(out / "predictions.jsonl", predictions)
    write_json(out / "MODEL_AUDIT.json", audit)
    return audit


def sparsity(size: int) -> str:
    return "1" if size == 1 else "2-4" if size <= 4 else "5-9" if size <= 9 else "10+"


def target_metrics(ids: list[str], targets: set[str]) -> dict:
    if not targets:
        raise ValueError("an evaluation target set must be nonempty")
    hits = set(ids) & targets
    dcg = sum(1 / math.log2(rank + 1) for rank, item in enumerate(ids[:10], 1) if item in targets)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(10, len(targets)) + 1))
    return {"targets": len(targets), "retrieved_targets": len(hits), "hit_at_100": int(bool(hits)),
            "recall_at_100": len(hits) / len(targets), "ndcg_at_10": dcg / ideal,
            "candidate_count": len(ids)}


def evaluate(predictions: list[dict], public: list[dict], labels: list[dict], fit: dict, catalog: list[dict]):
    requests = {row["request_id"]: row for row in public}
    truth = {row["request_id"]: row for row in labels}
    if len(truth) != len(labels) or len(requests) != len(public) or set(truth) != set(requests):
        raise ValueError("exactly one private target set per public request is required")
    for label in labels:
        targets = label["target_item_ids"]
        if not targets or len(targets) != len(set(targets)):
            raise ValueError("target sets must be nonempty and contain unique identities")
        if set(targets) & set(requests[label["request_id"]]["seen_all_fit_item_ids"]):
            raise ValueError("targets must exclude all fit-reviewed items, including low ratings")
    catalog_ids = {product["item_id"] for product in catalog}
    indexed = {}
    for row in predictions:
        key = (row["request_id"], row["arm"])
        if key in indexed or row["request_id"] not in requests or row["arm"] not in ARMS or row["source"] != SOURCE:
            raise ValueError("invalid/duplicate prediction identity")
        ids = row["item_ids"]
        if len(ids) != len(set(ids)) or len(ids) > 100 or not set(ids).issubset(catalog_ids):
            raise ValueError("invalid prediction catalog or duplicate identity")
        if set(ids) & set(requests[row["request_id"]]["seen_all_fit_item_ids"]):
            raise ValueError("prediction contains already reviewed items")
        indexed[key] = ids
    if len(indexed) != len(requests) * len(ARMS):
        raise ValueError("missing predictions must be explicit empty lists, not dropped users")
    per_user, buckets = [], collections.defaultdict(list)
    unions = {arm: set() for arm in ARMS}
    for request_id, request in sorted(requests.items()):
        targets = set(truth[request_id]["target_item_ids"])
        warm_targets = {item for item in targets if fit["positive_item_user_counts"].get(item, 0)}
        cold_targets = targets - warm_targets
        history_size = len({event["item_id"] for event in request["history"]})
        sparse = sparsity(history_size)
        for arm in ARMS:
            ids = indexed[(request_id, arm)]
            record = {"request_id": request_id, "arm": arm, "target_item_ids": sorted(targets),
                      "targets_in_catalog": len(targets & catalog_ids), "warm_targets": len(warm_targets),
                      "cold_targets": len(cold_targets), "history_items": history_size, "sparsity_slice": sparse,
                      "target_ranks": {item: ids.index(item) + 1 if item in ids else None for item in sorted(targets)},
                      **target_metrics(ids, targets)}
            per_user.append(record)
            unions[arm].update(ids)
            for bucket in ("all", "history_" + sparse):
                buckets[(arm, bucket)].append(record)
            for bucket, subset in (("warm_item", warm_targets), ("cold_item", cold_targets)):
                if subset:
                    buckets[(arm, bucket)].append(target_metrics(ids, subset))
    metrics = {}
    for arm in ARMS:
        metrics[arm] = {}
        for bucket in ("all", "warm_item", "cold_item", "history_1", "history_2-4", "history_5-9", "history_10+"):
            rows = buckets[(arm, bucket)]
            target_count = sum(row["targets"] for row in rows)
            retrieved_count = sum(row["retrieved_targets"] for row in rows)
            metrics[arm][bucket] = {"users": len(rows), "targets": target_count,
                                   "retrieved_targets": retrieved_count,
                                   "micro_recall_at_100": retrieved_count / target_count if target_count else None,
                                   **{name: sum(row[name] for row in rows) / len(rows) if rows else None
                                      for name in ("hit_at_100", "recall_at_100", "ndcg_at_10", "candidate_count")}}
        metrics[arm]["candidate_coverage"] = {"distinct_recommended_items": len(unions[arm]),
                                               "catalog_items": len(catalog_ids),
                                               "fraction": len(unions[arm]) / len(catalog_ids) if catalog_ids else 0}
    return per_user, {"status": "DEVELOPMENT_PILOT_NOT_FINAL_EVALUATION", "eligible_dev_users": len(requests),
                      "target_policy": "unordered new positive ASIN set on earliest eligible dev day",
                      "target_items": sum(len(row["target_item_ids"]) for row in labels),
                      "targets_outside_catalog": sum(item not in catalog_ids for row in labels for item in row["target_item_ids"]),
                      "metrics": metrics, "final_evaluated": False, "superiority_claim": False}


def peak_memory_mb() -> float | None:
    if os.name != "nt":
        return None
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                                "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                                "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    process = ctypes.windll.kernel32.GetCurrentProcess
    process.restype = ctypes.c_void_p
    get_info = ctypes.windll.psapi.GetProcessMemoryInfo
    get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if get_info(process(), ctypes.byref(counters), counters.cb):
        return round(counters.PeakWorkingSetSize / (1024 * 1024), 3)
    return None


def evaluate_files(out: Path) -> dict:
    if sha256(out / "labels.private.jsonl") != read_json(out / "PREPARED.json")["labels_sha256"]:
        raise ValueError("private labels changed since preparation")
    rows = lambda filename: [row for _, row in read_rows(out / filename)]
    per_user, result = evaluate(rows("predictions.jsonl"), rows("public_histories.jsonl"), rows("labels.private.jsonl"),
                                read_json(out / "fit_artifacts.json"), rows("catalog.jsonl"))
    result["source_audit"] = read_json(out / "SOURCE_AUDIT.json")
    result["protocol_sha256"] = sha256(out / "PROTOCOL.json")
    result["peak_process_working_set_mb"] = peak_memory_mb()
    result["memory_measurement_scope"] = "current Python process, including prepare/predict if run in one invocation"
    write_rows(out / "per_user.private.jsonl", per_user)
    write_json(out / "RESULT.json", result)
    return result


def seal(out: Path) -> dict:
    hashes = {path.name: sha256(path) for path in sorted(out.iterdir()) if path.is_file() and path.name != "MANIFEST.json"}
    if any(name.endswith(".partial") for name in hashes):
        raise ValueError("unfinished partial artifact prevents sealing")
    manifest = {"status": "SEALED_DEVELOPMENT_PILOT", "artifacts_sha256": hashes}
    write_json(out / "MANIFEST.json", manifest)
    return manifest


def verify_sealed(out: Path) -> None:
    manifest = read_json(out / "MANIFEST.json")
    for filename, expected in manifest["artifacts_sha256"].items():
        if sha256(out / filename) != expected:
            raise ValueError(f"sealed artifact changed: {filename}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("run", "prepare", "predict", "evaluate", "seal"), default="run")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    out = args.output
    if out.exists() and not args.resume:
        raise ValueError("output already exists; choose a new attempt or use explicit --resume")
    out.mkdir(parents=True, exist_ok=args.resume)
    if (out / "MANIFEST.json").exists():
        verify_sealed(out)
        print(json.dumps({"status": "already_sealed_verified", "output": str(out)}))
        return
    if args.stage in ("run", "prepare"):
        prepare(args.reviews, args.metadata, out)
    if args.stage in ("run", "predict"):
        if not (out / "PROTOCOL.json").exists():
            raise ValueError("freeze PROTOCOL.json before predictions")
        predict_files(out)
    if args.stage in ("run", "evaluate"):
        if (out / "RESULT.json").exists():
            result = read_json(out / "RESULT.json")
        else:
            result = evaluate_files(out)
        print(json.dumps({"status": result["status"], "eligible_dev_users": result["eligible_dev_users"],
                          "peak_process_working_set_mb": result["peak_process_working_set_mb"], "output": str(out)}))
    if args.stage in ("run", "seal"):
        seal(out)


if __name__ == "__main__":
    main()
