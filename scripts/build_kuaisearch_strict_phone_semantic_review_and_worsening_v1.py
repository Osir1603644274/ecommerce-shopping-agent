#!/usr/bin/env python3
"""Build a frozen strict-phone semantic review sample and post-hoc worsening diagnostic."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from analyze_kuaisearch_lite_phone_behavior_v1 import SOURCES, classify_category, stream_jsonl, utc_now  # noqa: E402
from run_kuaisearch_lite_phone_brand_profile_v1 import normalize_brand, rerank, session_metrics, sha256_file  # noqa: E402


def leaf_seed(seed: int, leaf: str) -> int:
    digest = hashlib.sha256(f"{seed}:{leaf}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def blind_id(seed: int, reviewer: str, item_id: int) -> str:
    digest = hashlib.sha256(f"{seed}:{reviewer}:{item_id}".encode("utf-8")).hexdigest()[:16]
    return f"{reviewer}-{digest}"


def classify_delta(value: float, epsilon: float = 1e-12) -> str:
    if value > epsilon:
        return "improved"
    if value < -epsilon:
        return "worsened"
    return "unchanged"


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--attempt001", type=Path, required=True)
    parser.add_argument("--review-output", type=Path, required=True)
    parser.add_argument("--diagnostic-output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    if sha256_file(Path(__file__).resolve()) != manifest["code"]["builderSha256"]:
        raise RuntimeError("builder hash mismatch")
    attempt_receipt_path = args.attempt001 / "receipt.json"
    split_path = args.attempt001 / "split_assignments.json"
    attempt = json.loads(attempt_receipt_path.read_text(encoding="utf-8"))
    split = json.loads(split_path.read_text(encoding="utf-8"))
    if attempt["status"] != "HOLD" or attempt["sealedTest"]["status"] != "NOT_RUN_VALIDATION_HOLD":
        raise RuntimeError("unexpected attempt001 authority")
    validation_users = [int(value) for value in split["splits"]["validation"]]
    validation_set = set(validation_users)
    selected_config = dict(attempt["development"]["selected"]["config"])
    allocations = {str(key): int(value) for key, value in manifest["sampling"]["allocationByLeaf"].items()}
    seed = int(manifest["sampling"]["seed"])
    rngs = {leaf: random.Random(leaf_seed(seed, leaf)) for leaf in allocations}
    seen = collections.Counter()
    reservoirs: dict[str, list[dict[str, Any]]] = {leaf: [] for leaf in allocations}
    flags = bytearray(1)
    item_meta: dict[int, dict[str, Any]] = {}
    started_at = utc_now()

    def consume_item(row: dict[str, Any], _: int) -> None:
        nonlocal flags
        item_id = int(row["item_id"])
        if item_id >= len(flags):
            flags.extend(b"\0" * (item_id + 1 - len(flags)))
        names = (
            str(row.get("category_level1_name") or ""),
            str(row.get("category_level2_name") or ""),
            str(row.get("category_level3_name") or ""),
        )
        strict, _ = classify_category(names)
        flags[item_id] = 1 if strict else 0
        if not strict:
            return
        leaf = names[2] if names[2] and names[2] != "UNKNOWN" else names[1]
        item_meta[item_id] = {"brandNormalized": normalize_brand(row.get("brand_name")), "leafCategory": leaf}
        if leaf not in allocations:
            raise RuntimeError(f"unregistered strict leaf: {leaf}")
        seen[leaf] += 1
        sample_row = {
            "sourceItemId": item_id,
            "title": str(row.get("item_title") or ""),
            "brand": str(row.get("brand_name") or ""),
            "seller": str(row.get("seller_name") or ""),
            "categoryL1": names[0], "categoryL2": names[1], "categoryL3": names[2],
        }
        reservoir = reservoirs[leaf]
        target = allocations[leaf]
        if len(reservoir) < target:
            reservoir.append(sample_row)
        else:
            index = rngs[leaf].randrange(seen[leaf])
            if index < target:
                reservoir[index] = sample_row

    item_source = stream_jsonl(SOURCES["items_lite"], consume_item)
    if dict(seen) != manifest["sampling"]["sourceLeafCounts"]:
        raise RuntimeError(f"strict leaf counts changed: {dict(seen)}")
    if any(len(reservoirs[leaf]) != allocations[leaf] for leaf in allocations):
        raise RuntimeError("reservoir allocation mismatch")

    sampled = [row for leaf in sorted(reservoirs) for row in reservoirs[leaf]]
    if len(sampled) != manifest["sampling"]["total"]:
        raise RuntimeError("sample size mismatch")
    reviewer_rows: dict[str, list[dict[str, Any]]] = {}
    mapping_rows = []
    for reviewer_index, reviewer in enumerate(("reviewer01", "reviewer02"), start=1):
        order = list(sampled)
        random.Random(seed + reviewer_index).shuffle(order)
        reviewer_rows[reviewer] = [{
            "schemaVersion": "kuaisearch-strict-phone-semantic-blind-item-v1",
            "blindItemId": blind_id(seed, reviewer, int(row["sourceItemId"])),
            "title": row["title"],
            "brand": row["brand"],
            "seller": row["seller"],
            "review": {"semanticLabel": None, "confidence": None, "rationale": None},
            "labelContract": ["PHONE_BODY", "NON_PHONE", "UNCERTAIN"],
        } for row in order]
    for row in sampled:
        mapping_rows.append({
            "reviewer01BlindItemId": blind_id(seed, "reviewer01", int(row["sourceItemId"])),
            "reviewer02BlindItemId": blind_id(seed, "reviewer02", int(row["sourceItemId"])),
            **row,
        })

    raw_sessions: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)

    def consume_recall(row: dict[str, Any], _: int) -> None:
        user_id = int(row["user_id"])
        if user_id not in validation_set:
            return
        candidates = [int(value) for value in row.get("impressed_item_ids", []) if 0 <= int(value) < len(flags) and flags[int(value)]]
        clicked = [int(value) for value in row.get("clicked_item_ids", []) if 0 <= int(value) < len(flags) and flags[int(value)]]
        if candidates or clicked:
            raw_sessions[user_id].append({
                "sessionId": int(row["session_id"]), "timeIndex": int(row["time_index"]),
                "candidates": candidates, "clicked": clicked,
            })

    recall_source = stream_jsonl(SOURCES["recall_lite"], consume_recall)
    minimum_history = int(manifest["diagnostic"]["minimumStrictEarlierClicks"])
    session_deltas: dict[str, list[float]] = collections.defaultdict(list)
    user_session_deltas: dict[int, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    evaluable_sessions = profile_nonempty_sessions = category_contract_contamination = 0
    for user_id in validation_users:
        grouped: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
        for session in raw_sessions.get(user_id, []):
            grouped[session["timeIndex"]].append(session)
        history: list[int] = []
        for time_index in sorted(grouped):
            sessions = sorted(grouped[time_index], key=lambda row: row["sessionId"])
            if len(history) >= minimum_history:
                for session in sessions:
                    candidates = session["candidates"]
                    positives = set(session["clicked"]) & set(candidates)
                    if len(candidates) < 2 or not positives:
                        continue
                    evaluable_sessions += 1
                    category_contract_contamination += sum(item_id not in item_meta for item_id in candidates + history)
                    treatment, profile_nonempty, _ = rerank(candidates, history, item_meta, selected_config)
                    profile_nonempty_sessions += int(profile_nonempty)
                    baseline_metrics = session_metrics(candidates, positives)
                    treatment_metrics = session_metrics(treatment, positives)
                    for metric in ("ndcgAt10", "hitAt3", "mrr"):
                        delta = treatment_metrics[metric] - baseline_metrics[metric]
                        session_deltas[metric].append(delta)
                        user_session_deltas[user_id][metric].append(delta)
            for session in sessions:
                history.extend(session["clicked"])

    user_deltas: dict[str, list[float]] = collections.defaultdict(list)
    any_metric_worse_users = 0
    for user_id in sorted(user_session_deltas):
        user_has_worse = False
        for metric in ("ndcgAt10", "hitAt3", "mrr"):
            values = user_session_deltas[user_id][metric]
            mean = sum(values) / len(values)
            user_deltas[metric].append(mean)
            user_has_worse = user_has_worse or mean < -1e-12
        any_metric_worse_users += int(user_has_worse)

    def summarize(values: list[float]) -> dict[str, Any]:
        counts = collections.Counter(classify_delta(value) for value in values)
        total = len(values)
        return {
            "population": total,
            "improved": counts["improved"],
            "unchanged": counts["unchanged"],
            "worsened": counts["worsened"],
            "worsenedRate": round(counts["worsened"] / total, 8) if total else None,
        }

    args.review_output.mkdir(parents=True, exist_ok=False)
    reviewer01_path = args.review_output / "reviewer01_blind.jsonl"
    reviewer02_path = args.review_output / "reviewer02_blind.jsonl"
    mapping_path = args.review_output / "source_mapping.jsonl"
    write_jsonl(reviewer01_path, reviewer_rows["reviewer01"])
    write_jsonl(reviewer02_path, reviewer_rows["reviewer02"])
    write_jsonl(mapping_path, mapping_rows)
    review_receipt = {
        "schemaVersion": "kuaisearch-strict-phone-semantic-review-package-v1",
        "startedAt": started_at, "finishedAt": utc_now(),
        "dataset": manifest["dataset"],
        "sampling": manifest["sampling"],
        "actualLeafCounts": dict(seen),
        "actualSampleCounts": {leaf: len(rows) for leaf, rows in reservoirs.items()},
        "sourceReceipt": item_source,
        "blindBoundary": "procedural blind only; source_mapping is in the same public workspace and must not be shown to reviewers before both finish",
        "semanticPurityEstablished": False,
        "reviewStatus": "PENDING_TWO_INDEPENDENT_CODEX_REVIEWS",
        "artifacts": {
            "reviewer01": {"path": reviewer01_path.name, "sha256": sha256_file(reviewer01_path)},
            "reviewer02": {"path": reviewer02_path.name, "sha256": sha256_file(reviewer02_path)},
            "sourceMapping": {"path": mapping_path.name, "sha256": sha256_file(mapping_path)},
        },
    }
    review_receipt_path = args.review_output / "receipt.json"
    review_receipt_path.write_text(json.dumps(review_receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    diagnostic = {
        "schemaVersion": "kuaisearch-phone-brand-profile-posthoc-worsening-v1",
        "role": "NON_CONFIRMATORY_POSTHOC_DIAGNOSTIC",
        "attempt001ReceiptSha256": sha256_file(attempt_receipt_path),
        "selectedConfigFixedFromAttempt001": selected_config,
        "developmentSelectionExecuted": False,
        "validationGateExecuted": False,
        "sealedTestExecuted": False,
        "decisionChanged": False,
        "sourceReceipts": {"itemsLite": item_source, "recallLite": recall_source},
        "evaluableSessions": evaluable_sessions,
        "evaluableUsers": len(user_session_deltas),
        "profileNonemptyCoverage": {
            "numerator": profile_nonempty_sessions,
            "denominator": evaluable_sessions,
            "rate": round(profile_nonempty_sessions / evaluable_sessions, 8) if evaluable_sessions else None,
        },
        "categoryContractContaminationCount": category_contract_contamination,
        "semanticPurityClaimAllowed": False,
        "sessionWorsening": {metric: summarize(values) for metric, values in session_deltas.items()},
        "userWorsening": {metric: summarize(values) for metric, values in user_deltas.items()},
        "usersWithAnyMetricMeanWorsened": any_metric_worse_users,
        "usersWithAnyMetricMeanWorsenedRate": round(any_metric_worse_users / len(user_session_deltas), 8) if user_session_deltas else None,
        "falseInfluenceBoundary": "descriptive worsening rate only; not a preregistered False Influence Rate and not a causal memory-harm estimate",
        "authoritativeDecisionPreserved": "DESCRIPTIVE_HOLD_WITH_PROVENANCE_GAP",
    }
    args.diagnostic_output.mkdir(parents=True, exist_ok=False)
    diagnostic_path = args.diagnostic_output / "receipt.json"
    diagnostic_path.write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "reviewStatus": review_receipt["reviewStatus"],
        "sampleCounts": review_receipt["actualSampleCounts"],
        "diagnosticRole": diagnostic["role"],
        "evaluableSessions": evaluable_sessions,
        "evaluableUsers": len(user_session_deltas),
        "decisionChanged": False,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
