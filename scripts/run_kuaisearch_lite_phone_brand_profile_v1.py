#!/usr/bin/env python3
"""Frozen KuaiSearch-Lite strict-phone brand-profile offline pilot."""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from analyze_kuaisearch_lite_phone_behavior_v1 import (  # noqa: E402
    REVISION,
    SOURCES,
    classify_category,
    stream_jsonl,
    utc_now,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_brand(value: Any) -> str | None:
    brand = str(value or "").strip().casefold()
    if not brand or brand in {"unknown", "其他/other", "其他", "无品牌", "other"}:
        return None
    return brand


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
    return round(float(ordered[index]), 6)


def session_metrics(order: list[int], positives: set[int]) -> dict[str, float]:
    gains = [1.0 if item_id in positives else 0.0 for item_id in order]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains[:10]))
    ideal_positives = min(len(positives), 10)
    idcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_positives))
    first = next((index for index, item_id in enumerate(order) if item_id in positives), None)
    return {
        "ndcgAt10": dcg / idcg if idcg else 0.0,
        "hitAt3": 1.0 if any(item_id in positives for item_id in order[:3]) else 0.0,
        "mrr": 1.0 / (first + 1) if first is not None else 0.0,
    }


def build_brand_profile(
    history: list[int],
    item_meta: dict[int, dict[str, Any]],
    repeat_cap: int,
    half_life_clicks: int,
) -> dict[str, float]:
    positions: dict[str, list[int]] = collections.defaultdict(list)
    for index, item_id in enumerate(history):
        brand = item_meta[item_id]["brandNormalized"]
        if brand:
            positions[brand].append(index)
    scores: dict[str, float] = {}
    newest_index = len(history) - 1
    for brand, indices in positions.items():
        kept = indices[-repeat_cap:]
        scores[brand] = sum(0.5 ** ((newest_index - index) / half_life_clicks) for index in kept)
    maximum = max(scores.values(), default=0.0)
    return {brand: score / maximum for brand, score in scores.items()} if maximum else {}


def rerank(
    candidates: list[int],
    history: list[int],
    item_meta: dict[int, dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[int], bool, int]:
    started = time.perf_counter_ns()
    profile = build_brand_profile(
        history,
        item_meta,
        int(config["repeatClickCapPerBrand"]),
        int(config["halfLifeClicks"]),
    )
    alpha = float(config["alpha"])
    denominator = max(1, len(candidates) - 1)
    scored: list[tuple[float, int, int]] = []
    for index, item_id in enumerate(candidates):
        base = 1.0 - index / denominator
        brand = item_meta[item_id]["brandNormalized"]
        preference = profile.get(brand, 0.0) if brand else 0.0
        final = (1.0 - alpha) * base + alpha * preference
        scored.append((-final, index, item_id))
    scored.sort()
    order = [row[2] for row in scored]
    elapsed = time.perf_counter_ns() - started
    return order, bool(profile), elapsed


def bootstrap_ci(values: list[float], seed: int, samples: int) -> list[float] | None:
    if not values or samples <= 0:
        return None
    rng = random.Random(seed)
    size = len(values)
    means = []
    for _ in range(samples):
        means.append(sum(values[rng.randrange(size)] for _ in range(size)) / size)
    means.sort()
    lower = means[max(0, math.floor(0.025 * samples))]
    upper = means[min(samples - 1, math.ceil(0.975 * samples) - 1)]
    return [round(lower, 8), round(upper, 8)]


def evaluate(
    user_ids: list[int],
    records_by_user: dict[int, list[dict[str, Any]]],
    item_meta: dict[int, dict[str, Any]],
    config: dict[str, Any],
    bootstrap_seed: int,
    bootstrap_samples: int,
) -> dict[str, Any]:
    per_user_baseline: dict[int, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    per_user_treatment: dict[int, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    opportunity_sessions = candidate_gte2 = no_positive = evaluable = 0
    unjudged_candidates = positive_candidates = 0
    changed = top1_changed = profile_nonempty = 0
    preserved = 0
    latencies_ms: list[float] = []
    contamination = 0

    for user_id in user_ids:
        for record in records_by_user.get(user_id, []):
            candidates = record["candidates"]
            positives = record["positives"]
            if not candidates:
                continue
            opportunity_sessions += 1
            candidate_gte2 += int(len(candidates) >= 2)
            no_positive += int(not positives)
            unjudged_candidates += len(candidates) - len(positives)
            positive_candidates += len(positives)
            if len(candidates) < 2 or not positives:
                continue
            evaluable += 1
            assert all(item_id in item_meta for item_id in candidates)
            assert all(item_id in item_meta for item_id in record["history"])
            treatment, has_profile, elapsed_ns = rerank(candidates, record["history"], item_meta, config)
            profile_nonempty += int(has_profile)
            latencies_ms.append(elapsed_ns / 1_000_000)
            if set(treatment) != set(candidates) or len(treatment) != len(candidates):
                contamination += 1
            else:
                preserved += 1
            changed += int(treatment != candidates)
            top1_changed += int(treatment[0] != candidates[0])
            baseline_metrics = session_metrics(candidates, positives)
            treatment_metrics = session_metrics(treatment, positives)
            for metric, value in baseline_metrics.items():
                per_user_baseline[user_id][metric].append(value)
                per_user_treatment[user_id][metric].append(treatment_metrics[metric])

    metric_names = ("ndcgAt10", "hitAt3", "mrr")
    baseline_macro: dict[str, float] = {}
    treatment_macro: dict[str, float] = {}
    delta_macro: dict[str, float] = {}
    intervals: dict[str, list[float] | None] = {}
    worst_users: dict[str, dict[str, Any] | None] = {}
    evaluable_users = sorted(per_user_baseline)
    for metric_index, metric in enumerate(metric_names):
        user_baselines = {
            user_id: statistics.fmean(per_user_baseline[user_id][metric]) for user_id in evaluable_users
        }
        user_treatments = {
            user_id: statistics.fmean(per_user_treatment[user_id][metric]) for user_id in evaluable_users
        }
        deltas = {user_id: user_treatments[user_id] - user_baselines[user_id] for user_id in evaluable_users}
        baseline_macro[metric] = round(statistics.fmean(user_baselines.values()), 8) if user_baselines else 0.0
        treatment_macro[metric] = round(statistics.fmean(user_treatments.values()), 8) if user_treatments else 0.0
        delta_macro[metric] = round(statistics.fmean(deltas.values()), 8) if deltas else 0.0
        intervals[metric] = bootstrap_ci(
            list(deltas.values()), bootstrap_seed + metric_index, bootstrap_samples
        )
        if deltas:
            worst_id = min(deltas, key=lambda user_id: (deltas[user_id], user_id))
            worst_users[metric] = {
                "anonymizedUserId": worst_id,
                "delta": round(deltas[worst_id], 8),
                "sessions": len(per_user_baseline[worst_id][metric]),
            }
        else:
            worst_users[metric] = None

    return {
        "config": config,
        "eligibleUsers": len(user_ids),
        "evaluableUsers": len(evaluable_users),
        "opportunitySessions": opportunity_sessions,
        "candidateAtLeast2Sessions": candidate_gte2,
        "candidateAtLeast2Coverage": round(candidate_gte2 / opportunity_sessions, 8) if opportunity_sessions else 0.0,
        "noPositiveSessionsExcluded": no_positive,
        "evaluableSessions": evaluable,
        "positiveCandidates": positive_candidates,
        "unjudgedCandidates": unjudged_candidates,
        "unjudgedSemantics": "not observed positive; never interpreted as dislike or negative preference",
        "baselineMacroUser": baseline_macro,
        "treatmentMacroUser": treatment_macro,
        "deltaMacroUser": delta_macro,
        "bootstrap95UserDelta": intervals,
        "worstUser": worst_users,
        "changedSessions": changed,
        "changeRate": round(changed / evaluable, 8) if evaluable else 0.0,
        "top1ChangeRate": round(top1_changed / evaluable, 8) if evaluable else 0.0,
        "profileNonemptyCoverage": round(profile_nonempty / evaluable, 8) if evaluable else 0.0,
        "candidateSetPreservationRate": round(preserved / evaluable, 8) if evaluable else 0.0,
        "crossCategoryContaminationCount": contamination,
        "hardFilteredCandidates": 0,
        "purchaseSignalsUsed": 0,
        "latencyMs": {
            "p50": percentile(latencies_ms, 0.50),
            "p95": percentile(latencies_ms, 0.95),
            "max": round(max(latencies_ms), 6) if latencies_ms else None,
        },
    }


def choose_development(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def key(row: dict[str, Any]) -> tuple[Any, ...]:
        config = row["config"]
        metrics = row["treatmentMacroUser"]
        return (
            metrics["ndcgAt10"], metrics["hitAt3"], metrics["mrr"],
            -float(config["alpha"]),
            -int(config["repeatClickCapPerBrand"]),
            -int(config["halfLifeClicks"]),
        )
    return max(rows, key=key)


def validation_gates(result: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "minimumEvaluableUsers": result["evaluableUsers"] >= gates["minimumEvaluableUsers"],
        "minimumCandidateAtLeast2Coverage": result["candidateAtLeast2Coverage"] >= gates["minimumCandidateAtLeast2Coverage"],
        "ndcgBootstrapLowerPositive": result["bootstrap95UserDelta"]["ndcgAt10"][0] > gates["ndcgBootstrapLowerGreaterThan"],
        "hitBootstrapNonInferior": result["bootstrap95UserDelta"]["hitAt3"][0] >= gates["hitAt3BootstrapLowerAtLeast"],
        "mrrBootstrapNonInferior": result["bootstrap95UserDelta"]["mrr"][0] >= gates["mrrBootstrapLowerAtLeast"],
        "minimumChangeRate": result["changeRate"] >= gates["minimumChangeRate"],
        "maximumChangeRate": result["changeRate"] <= gates["maximumChangeRate"],
        "candidateSetPreserved": result["candidateSetPreservationRate"] == 1.0,
        "crossCategoryContaminationZero": result["crossCategoryContaminationCount"] == 0,
        "hardFilteringZero": result["hardFilteredCandidates"] == 0,
        "purchaseUseZero": result["purchaseSignalsUsed"] == 0,
        "latencyP95": result["latencyMs"]["p95"] <= gates["maximumOfflineRerankP95Ms"],
    }
    return {"checks": checks, "decision": "ACCEPT" if all(checks.values()) else "HOLD"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest_bytes = args.manifest.read_bytes()
    manifest = json.loads(manifest_bytes)
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if manifest["dataset"]["revision"] != REVISION:
        raise RuntimeError("manifest revision mismatch")
    if sha256_file(Path(__file__).resolve()) != manifest["code"]["runnerSha256"]:
        raise RuntimeError("runner hash mismatch")

    started_at = utc_now()
    flags = bytearray(1)
    item_meta: dict[int, dict[str, Any]] = {}
    leaf_samples: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)

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
        if strict:
            leaf = names[2] if names[2] and names[2] != "UNKNOWN" else names[1]
            item_meta[item_id] = {
                "brandNormalized": normalize_brand(row.get("brand_name")),
                "sellerName": str(row.get("seller_name") or ""),
                "leafCategory": leaf,
            }
            if len(leaf_samples[leaf]) < 3:
                leaf_samples[leaf].append({
                    "itemId": item_id,
                    "title": str(row.get("item_title") or ""),
                    "brand": str(row.get("brand_name") or ""),
                    "categoryL1": names[0], "categoryL2": names[1], "categoryL3": names[2],
                })

    item_source = stream_jsonl(SOURCES["items_lite"], consume_item)
    if any(len(samples) < 3 for samples in leaf_samples.values()):
        raise RuntimeError("fewer than three real title samples for a strict leaf")

    raw_sessions: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    recall_diagnostics = collections.Counter()

    def consume_recall(row: dict[str, Any], _: int) -> None:
        candidates = [int(item_id) for item_id in row.get("impressed_item_ids", []) if 0 <= int(item_id) < len(flags) and flags[int(item_id)]]
        clicked = [int(item_id) for item_id in row.get("clicked_item_ids", []) if 0 <= int(item_id) < len(flags) and flags[int(item_id)]]
        if len(candidates) != len(set(candidates)):
            raise RuntimeError("duplicate strict candidate within session")
        recall_diagnostics["strictClickedNotImpressed"] += sum(item_id not in set(candidates) for item_id in clicked)
        recall_diagnostics["purchasesObservedButIgnored"] += len(row.get("purchased_item_ids", []))
        if candidates or clicked:
            raw_sessions[int(row["user_id"])].append({
                "sessionId": int(row["session_id"]),
                "timeIndex": int(row["time_index"]),
                "candidates": candidates,
                "clicked": clicked,
            })

    recall_source = stream_jsonl(SOURCES["recall_lite"], consume_recall)

    records_by_user: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    eligible_users: list[int] = []
    for user_id, sessions in raw_sessions.items():
        grouped: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
        for session in sessions:
            grouped[session["timeIndex"]].append(session)
        history: list[int] = []
        has_later_click_target = False
        for time_index in sorted(grouped):
            current_sessions = sorted(grouped[time_index], key=lambda row: row["sessionId"])
            if len(history) >= manifest["eligibility"]["minimumStrictEarlierClicks"]:
                for session in current_sessions:
                    has_later_click_target = has_later_click_target or bool(session["clicked"])
                    if session["candidates"]:
                        positive_set = set(session["clicked"]) & set(session["candidates"])
                        records_by_user[user_id].append({
                            "sessionId": session["sessionId"],
                            "timeIndex": time_index,
                            "history": list(history),
                            "candidates": list(session["candidates"]),
                            "positives": positive_set,
                        })
            for session in current_sessions:
                history.extend(session["clicked"])
        if has_later_click_target:
            eligible_users.append(user_id)
    eligible_users.sort()

    split_seed = int(manifest["split"]["seed"])
    shuffled = list(eligible_users)
    random.Random(split_seed).shuffle(shuffled)
    dev_end = math.floor(len(shuffled) * 0.60)
    validation_end = dev_end + math.floor(len(shuffled) * 0.20)
    splits = {
        "development": sorted(shuffled[:dev_end]),
        "validation": sorted(shuffled[dev_end:validation_end]),
        "sealed-test": sorted(shuffled[validation_end:]),
    }
    if set(splits["development"]) & set(splits["validation"]) or set(splits["development"]) & set(splits["sealed-test"]) or set(splits["validation"]) & set(splits["sealed-test"]):
        raise RuntimeError("user split contamination")

    args.output_dir.mkdir(parents=True, exist_ok=False)
    split_payload = {
        "schemaVersion": "kuaisearch-lite-phone-brand-profile-user-split-v1",
        "seed": split_seed,
        "eligibleUsers": len(eligible_users),
        "splits": splits,
        "splitHashes": {name: stable_json_hash(ids) for name, ids in splits.items()},
    }
    split_path = args.output_dir / "split_assignments.json"
    split_path.write_text(json.dumps(split_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    grid_rows: list[dict[str, Any]] = []
    for alpha in manifest["treatment"]["grid"]["alpha"]:
        for cap in manifest["treatment"]["grid"]["repeatClickCapPerBrand"]:
            for half_life in manifest["treatment"]["grid"]["halfLifeClicks"]:
                config = {"alpha": alpha, "repeatClickCapPerBrand": cap, "halfLifeClicks": half_life}
                grid_rows.append(evaluate(
                    splits["development"], records_by_user, item_meta, config,
                    int(manifest["bootstrap"]["seed"]), 0,
                ))
    selected_grid_row = choose_development(grid_rows)
    selected_config = selected_grid_row["config"]
    selected_dev = evaluate(
        splits["development"], records_by_user, item_meta, selected_config,
        int(manifest["bootstrap"]["seed"]), int(manifest["bootstrap"]["samples"]),
    )
    validation = evaluate(
        splits["validation"], records_by_user, item_meta, selected_config,
        int(manifest["bootstrap"]["seed"]) + 100, int(manifest["bootstrap"]["samples"]),
    )
    gate = validation_gates(validation, manifest["validationGates"])
    if gate["decision"] == "ACCEPT":
        sealed_test = {
            "status": "EXECUTED_ONCE_AFTER_VALIDATION_ACCEPT",
            "result": evaluate(
                splits["sealed-test"], records_by_user, item_meta, selected_config,
                int(manifest["bootstrap"]["seed"]) + 200, int(manifest["bootstrap"]["samples"]),
            ),
        }
    else:
        sealed_test = {"status": "NOT_RUN_VALIDATION_HOLD", "result": None}

    grid_path = args.output_dir / "development_grid.jsonl"
    with grid_path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in grid_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    receipt = {
        "schemaVersion": "kuaisearch-lite-phone-brand-profile-offline-pilot-v1",
        "status": gate["decision"],
        "startedAt": started_at,
        "finishedAt": utc_now(),
        "manifest": {"path": str(args.manifest.resolve()), "sha256": manifest_hash},
        "code": manifest["code"],
        "dataset": {"id": "benchen4395/KuaiSearch", "revision": REVISION, "variant": "Lite"},
        "sourceReceipts": {"itemsLite": item_source, "recallLite": recall_source},
        "strictPhone": {
            "itemCount": len(item_meta),
            "realTitleSamplesByLeaf": dict(sorted(leaf_samples.items())),
        },
        "eligibility": {
            "eligibleUsers": len(eligible_users),
            "strictClickedNotImpressed": recall_diagnostics["strictClickedNotImpressed"],
            "sameTimeIndexCrossing": 0,
        },
        "split": {
            "names": ["development", "validation", "sealed-test"],
            "counts": {name: len(ids) for name, ids in splits.items()},
            "hashes": split_payload["splitHashes"],
            "crossSplitUserOverlap": 0,
        },
        "development": {
            "gridConfigurations": len(grid_rows),
            "selectionRule": manifest["developmentSelection"],
            "selected": selected_dev,
        },
        "validation": {"executedOnce": True, "result": validation, "gate": gate},
        "sealedTest": sealed_test,
        "safety": {
            "crossCategoryContamination": validation["crossCategoryContaminationCount"],
            "hardFilteredCandidates": validation["hardFilteredCandidates"],
            "purchaseSignalsUsed": validation["purchaseSignalsUsed"],
            "exposureNotClickedAsPreferenceNegative": False,
            "productionSwitchChanged": False,
        },
        "artifacts": {
            "splitAssignments": {"path": split_path.name, "sha256": sha256_file(split_path)},
            "developmentGrid": {"path": grid_path.name, "sha256": sha256_file(grid_path)},
        },
    }
    receipt_path = args.output_dir / "receipt.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": receipt["status"],
        "eligibleUsers": len(eligible_users),
        "splitCounts": receipt["split"]["counts"],
        "selectedConfig": selected_config,
        "validationGate": gate,
        "sealedTestStatus": sealed_test["status"],
        "output": str(receipt_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
