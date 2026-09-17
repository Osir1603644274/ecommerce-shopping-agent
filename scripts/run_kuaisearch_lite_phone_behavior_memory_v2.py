#!/usr/bin/env python3
"""Future-click evaluation for low-authority phone behavior memory.

The public process may use only the previously published development and
validation users.  The old sealed users are skipped before any behavior field
is consumed and must be evaluated by a separate authority process.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import random
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterable

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
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def normalize_text(value: object) -> str | None:
    text = str(value or "").strip().casefold()
    return None if text in {"", "unknown", "其他/other", "其他", "无品牌", "other"} else text


def title_tokens(value: object) -> frozenset[str]:
    text = str(value or "").casefold()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    for segment in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.update(segment[index:index + 2] for index in range(max(0, len(segment) - 1)))
        if len(segment) == 1:
            tokens.add(segment)
    return frozenset(token for token in tokens if token)


def session_metrics(order: list[int], positives: set[int]) -> dict[str, float]:
    gains = [1.0 if item_id in positives else 0.0 for item_id in order]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains[:10]))
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(positives), 10)))
    first = next((index for index, item_id in enumerate(order) if item_id in positives), None)
    return {
        "ndcgAt10": dcg / ideal if ideal else 0.0,
        "hitAt3": float(any(item_id in positives for item_id in order[:3])),
        "mrr": 1.0 / (first + 1) if first is not None else 0.0,
    }


def feature_profile(
    history: list[int], item_meta: dict[int, dict[str, Any]],
    field: str, *, cap: int, half_life: int,
) -> dict[str, float]:
    positions: dict[str, list[int]] = collections.defaultdict(list)
    for index, item_id in enumerate(history):
        value = item_meta[item_id].get(field)
        if value:
            positions[str(value)].append(index)
    newest = len(history) - 1
    scores = {
        value: sum(0.5 ** ((newest - index) / half_life) for index in indices[-cap:])
        for value, indices in positions.items()
    }
    maximum = max(scores.values(), default=0.0)
    return {key: value / maximum for key, value in scores.items()} if maximum else {}


def title_affinity(
    candidate_tokens: frozenset[str], history: list[int],
    item_meta: dict[int, dict[str, Any]], *, cap: int, half_life: int,
) -> float:
    if not candidate_tokens:
        return 0.0
    newest = len(history) - 1
    best = 0.0
    for index in range(max(0, len(history) - cap), len(history)):
        other = item_meta[history[index]]["titleTokens"]
        union = candidate_tokens | other
        if not union:
            continue
        similarity = len(candidate_tokens & other) / len(union)
        best = max(best, similarity * 0.5 ** ((newest - index) / half_life))
    return best


def raw_brand_scores(history: list[int], item_meta: dict[int, dict[str, Any]]) -> dict[str, float]:
    counts = collections.Counter(
        item_meta[item_id]["brand"] for item_id in history if item_meta[item_id]["brand"]
    )
    maximum = max(counts.values(), default=0)
    return {key: value / maximum for key, value in counts.items()} if maximum else {}


def rerank(
    candidates: list[int], history: list[int], item_meta: dict[int, dict[str, Any]],
    *, arm: str, config: dict[str, Any],
) -> tuple[list[int], dict[str, Any]]:
    if arm not in {"A", "B", "C"}:
        raise ValueError("unknown arm")
    if arm == "A" or not history:
        return list(candidates), {"profileBytes": 2, "changed": False, "latencyNs": 0}
    started = time.perf_counter_ns()
    alpha = float(config["alpha"])
    raw_brand = raw_brand_scores(history, item_meta)
    brand = feature_profile(
        history, item_meta, "brand", cap=int(config["repeatCap"]),
        half_life=int(config["halfLifeClicks"]),
    )
    seller = feature_profile(
        history, item_meta, "seller", cap=int(config["repeatCap"]),
        half_life=int(config["halfLifeClicks"]),
    )
    profile_payload = raw_brand if arm == "B" else {"brand": brand, "seller": seller}
    denominator = max(1, len(candidates) - 1)
    scored = []
    for index, item_id in enumerate(candidates):
        base = 1.0 - index / denominator
        meta = item_meta[item_id]
        if arm == "B":
            preference = raw_brand.get(meta["brand"], 0.0) if meta["brand"] else 0.0
        else:
            weights = config["featureWeights"]
            preference = (
                float(weights["brand"]) * (brand.get(meta["brand"], 0.0) if meta["brand"] else 0.0)
                + float(weights["seller"]) * (seller.get(meta["seller"], 0.0) if meta["seller"] else 0.0)
                + float(weights["title"]) * title_affinity(
                    meta["titleTokens"], history, item_meta,
                    cap=int(config["titleHistoryCap"]), half_life=int(config["halfLifeClicks"]),
                )
            )
        final = (1.0 - alpha) * base + alpha * preference
        scored.append((-final, index, item_id))
    scored.sort()
    order = [row[2] for row in scored]
    elapsed = time.perf_counter_ns() - started
    payload_bytes = len(json.dumps(profile_payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return order, {"profileBytes": payload_bytes, "changed": order != candidates, "latencyNs": elapsed}


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
    return round(float(ordered[index]), 8)


def bootstrap(values: list[float], *, seed: int, samples: int) -> list[float] | None:
    if not values or samples <= 0:
        return None
    rng = random.Random(seed)
    size = len(values)
    means = sorted(
        sum(values[rng.randrange(size)] for _ in range(size)) / size
        for _ in range(samples)
    )
    return [means[math.floor(0.025 * samples)], means[math.ceil(0.975 * samples) - 1]]


def macro_user(rows: Iterable[dict[str, float]]) -> dict[str, float]:
    rows = list(rows)
    return {
        metric: statistics.fmean(row[metric] for row in rows)
        for metric in ("ndcgAt10", "hitAt3", "mrr")
    }


def evaluate(
    user_ids: list[int], records_by_user: dict[int, list[dict[str, Any]]],
    item_meta: dict[int, dict[str, Any]], config: dict[str, Any], *,
    bootstrap_seed: int, bootstrap_samples: int,
) -> dict[str, Any]:
    per_user: dict[int, dict[str, Any]] = {}
    counts = collections.Counter()
    candidate_failures = hard_filtered = b_c_divergence = 0
    latency_ns: list[int] = []
    profile_bytes: list[int] = []
    change_sessions = top1_change_sessions = 0
    for user_id in user_ids:
        arm_rows: dict[str, list[dict[str, float]]] = {arm: [] for arm in "ABC"}
        demonstrated_rows: dict[str, list[dict[str, float]]] = {arm: [] for arm in "ABC"}
        for record in records_by_user.get(user_id, []):
            counts["allCandidateSessions"] += 1
            candidates = record["candidates"]
            if len(candidates) < 2:
                counts["candidateLessThanTwo"] += 1
                continue
            counts["candidateAtLeastTwo"] += 1
            positives = record["positives"]
            if not positives:
                counts["noFuturePositiveInCandidates"] += 1
            orders: dict[str, list[int]] = {}
            observations: dict[str, dict[str, Any]] = {}
            for arm in "ABC":
                order, observation = rerank(
                    candidates, record["history"], item_meta, arm=arm, config=config,
                )
                orders[arm] = order
                observations[arm] = observation
                candidate_failures += int(set(order) != set(candidates) or len(order) != len(candidates))
                demonstrated_rows[arm].append(session_metrics(order, positives) if positives else {
                    "ndcgAt10": 0.0, "hitAt3": 0.0, "mrr": 0.0,
                })
                if positives:
                    arm_rows[arm].append(session_metrics(order, positives))
            hard_filtered += max(0, len(candidates) - len(orders["C"]))
            b_c_divergence += int(orders["B"] != orders["C"])
            latency_ns.append(int(observations["C"]["latencyNs"]))
            profile_bytes.append(int(observations["C"]["profileBytes"]))
            change_sessions += int(orders["A"] != orders["C"])
            top1_change_sessions += int(orders["A"][0] != orders["C"][0])
        if arm_rows["A"]:
            per_user[user_id] = {
                "conditional": {arm: macro_user(arm_rows[arm]) for arm in "ABC"},
                "demonstrated": {arm: macro_user(demonstrated_rows[arm]) for arm in "ABC"},
                "labeledSessions": len(arm_rows["A"]),
                "allCandidateAtLeastTwoSessions": len(demonstrated_rows["A"]),
            }
    user_values = list(per_user.values())
    conditional = {
        arm: {
            metric: statistics.fmean(row["conditional"][arm][metric] for row in user_values)
            for metric in ("ndcgAt10", "hitAt3", "mrr")
        } for arm in "ABC"
    }
    demonstrated = {
        arm: {
            metric: statistics.fmean(row["demonstrated"][arm][metric] for row in user_values)
            for metric in ("ndcgAt10", "hitAt3", "mrr")
        } for arm in "ABC"
    }
    deltas = {
        metric: [
            row["conditional"]["C"][metric] - row["conditional"]["A"][metric]
            for row in user_values
        ] for metric in ("ndcgAt10", "hitAt3", "mrr")
    }
    c_vs_b = {
        metric: statistics.fmean(
            row["conditional"]["C"][metric] - row["conditional"]["B"][metric]
            for row in user_values
        ) for metric in ("ndcgAt10", "hitAt3", "mrr")
    }
    return {
        "requestedUsers": len(user_ids),
        "evaluableUsers": len(user_values),
        "counts": dict(counts),
        "candidateAtLeast2Coverage": counts["candidateAtLeastTwo"] / max(1, counts["allCandidateSessions"]),
        "futurePositiveSessionCoverage": (
            (counts["candidateAtLeastTwo"] - counts["noFuturePositiveInCandidates"])
            / max(1, counts["candidateAtLeastTwo"])
        ),
        "evaluableUserCoverage": len(user_values) / max(1, len(user_ids)),
        "conditionalMacroUser": conditional,
        "demonstratedUtilityMacroUser": demonstrated,
        "cMinusAMean": {metric: statistics.fmean(values) for metric, values in deltas.items()},
        "cMinusABootstrap95User": {
            metric: bootstrap(values, seed=bootstrap_seed + index, samples=bootstrap_samples)
            for index, (metric, values) in enumerate(deltas.items())
        },
        "cMinusBMean": c_vs_b,
        "candidateSetFailures": candidate_failures,
        "hardFilteredCandidates": hard_filtered,
        "bCDivergenceSessions": b_c_divergence,
        "changeRate": change_sessions / max(1, counts["candidateAtLeastTwo"]),
        "top1ChangeRate": top1_change_sessions / max(1, counts["candidateAtLeastTwo"]),
        "rerankLatencyMs": {
            "p50": percentile([value / 1_000_000 for value in latency_ns], 0.50),
            "p95": percentile([value / 1_000_000 for value in latency_ns], 0.95),
            "max": max(latency_ns, default=0) / 1_000_000,
        },
        "profileBytes": {
            "p50": percentile([float(value) for value in profile_bytes], 0.50),
            "p95": percentile([float(value) for value in profile_bytes], 0.95),
            "max": max(profile_bytes, default=0),
        },
        "worstUserCMinusA": {
            metric: min(values, default=0.0) for metric, values in deltas.items()
        },
    }


def choose_development(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def key(row: dict[str, Any]) -> tuple[Any, ...]:
        metrics = row["conditionalMacroUser"]["C"]
        config = row["config"]
        return (
            metrics["ndcgAt10"], metrics["hitAt3"], metrics["mrr"],
            -float(config["alpha"]), float(config["featureWeights"]["title"]),
        )
    return max(rows, key=key)


def validation_gate(result: dict[str, Any], gates: dict[str, Any]) -> dict[str, Any]:
    ci = result["cMinusABootstrap95User"]
    checks = {
        "minimumEvaluableUsers": result["evaluableUsers"] >= gates["minimumEvaluableUsers"],
        "minimumCandidateAtLeast2Coverage": result["candidateAtLeast2Coverage"] >= gates["minimumCandidateAtLeast2Coverage"],
        "minimumEvaluableUserCoverage": result["evaluableUserCoverage"] >= gates["minimumEvaluableUserCoverage"],
        "ndcgBootstrapLowerPositive": ci["ndcgAt10"][0] > gates["ndcgBootstrapLowerGreaterThan"],
        "hitBootstrapNonInferior": ci["hitAt3"][0] >= gates["hitBootstrapLowerAtLeast"],
        "mrrBootstrapNonInferior": ci["mrr"][0] >= gates["mrrBootstrapLowerAtLeast"],
        "candidateSetPreserved": result["candidateSetFailures"] == 0,
        "hardFilteringZero": result["hardFilteredCandidates"] == 0,
        "bAndCDiffer": result["bCDivergenceSessions"] > 0,
        "minimumChangeRate": result["changeRate"] >= gates["minimumChangeRate"],
        "maximumChangeRate": result["changeRate"] <= gates["maximumChangeRate"],
        "latencyP95": result["rerankLatencyMs"]["p95"] <= gates["maximumRerankP95Ms"],
        "profileP95Bytes": result["profileBytes"]["p95"] <= gates["maximumProfileP95Bytes"],
    }
    return {"checks": checks, "decision": "ACCEPT" if all(checks.values()) else "HOLD"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {args.output_dir}")
    manifest_raw = args.manifest.read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get("schemaVersion") != "kuaisearch-lite-phone-behavior-memory-v2-preregistration":
        raise ValueError("manifest version mismatch")
    if manifest["dataset"]["revision"] != REVISION:
        raise ValueError("dataset revision mismatch")
    if sha256_file(Path(__file__)) != manifest["code"]["runnerSha256"]:
        raise ValueError("runner hash mismatch")
    analyzer_path = SCRIPT_DIR / "analyze_kuaisearch_lite_phone_behavior_v1.py"
    if sha256_file(analyzer_path) != manifest["code"]["streamAnalyzerSha256"]:
        raise ValueError("stream analyzer hash mismatch")
    split_path = Path(manifest["split"]["publishedAssignmentPath"])
    if sha256_file(split_path) != manifest["split"]["publishedAssignmentSha256"]:
        raise ValueError("published split hash mismatch")
    split_payload = json.loads(split_path.read_text(encoding="utf-8"))
    splits = split_payload["splits"]
    public_users = set(map(int, splits["development"])) | set(map(int, splits["validation"]))
    sealed_users = set(map(int, splits["sealed-test"]))
    if public_users & sealed_users:
        raise ValueError("split user overlap")

    flags = bytearray(1)
    item_meta: dict[int, dict[str, Any]] = {}

    def consume_item(row: dict[str, Any], _: int) -> None:
        nonlocal flags
        item_id = int(row["item_id"])
        if item_id >= len(flags):
            flags.extend(b"\0" * (item_id + 1 - len(flags)))
        names = tuple(str(row.get(key) or "") for key in (
            "category_level1_name", "category_level2_name", "category_level3_name",
        ))
        strict, _ = classify_category(names)
        flags[item_id] = int(strict)
        if strict:
            item_meta[item_id] = {
                "brand": normalize_text(row.get("brand_name")),
                "seller": normalize_text(row.get("seller_name")),
                "titleTokens": title_tokens(row.get("item_title")),
            }

    item_receipt = stream_jsonl(SOURCES["items_lite"], consume_item)
    raw_sessions: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    sealed_rows_skipped = 0

    def consume_recall(row: dict[str, Any], _: int) -> None:
        nonlocal sealed_rows_skipped
        user_id = int(row["user_id"])
        if user_id in sealed_users:
            sealed_rows_skipped += 1
            return
        if user_id not in public_users:
            return
        candidates = [
            int(item_id) for item_id in row.get("impressed_item_ids", [])
            if 0 <= int(item_id) < len(flags) and flags[int(item_id)]
        ]
        clicked = [
            int(item_id) for item_id in row.get("clicked_item_ids", [])
            if 0 <= int(item_id) < len(flags) and flags[int(item_id)]
        ]
        if candidates or clicked:
            raw_sessions[user_id].append({
                "sessionId": int(row["session_id"]),
                "timeIndex": int(row["time_index"]),
                "candidates": candidates,
                "clicked": clicked,
            })

    recall_receipt = stream_jsonl(SOURCES["recall_lite"], consume_recall)
    records_by_user: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    minimum_history = int(manifest["eligibility"]["minimumStrictEarlierClicks"])
    for user_id, sessions in raw_sessions.items():
        grouped: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
        for session in sessions:
            grouped[session["timeIndex"]].append(session)
        history: list[int] = []
        for time_index in sorted(grouped):
            current = sorted(grouped[time_index], key=lambda row: row["sessionId"])
            if len(history) >= minimum_history:
                for session in current:
                    if session["candidates"]:
                        records_by_user[user_id].append({
                            "sessionId": session["sessionId"], "timeIndex": time_index,
                            "history": list(history), "candidates": list(session["candidates"]),
                            "positives": set(session["clicked"]) & set(session["candidates"]),
                        })
            for session in current:
                history.extend(session["clicked"])

    base_config = {
        "repeatCap": manifest["treatment"]["repeatCap"],
        "titleHistoryCap": manifest["treatment"]["titleHistoryCap"],
        "halfLifeClicks": manifest["treatment"]["halfLifeClicks"],
    }
    grid_rows = []
    for alpha in manifest["developmentGrid"]["alpha"]:
        for weights in manifest["developmentGrid"]["featureWeights"]:
            config = {**base_config, "alpha": alpha, "featureWeights": weights}
            result = evaluate(
                list(map(int, splits["development"])), records_by_user, item_meta, config,
                bootstrap_seed=manifest["bootstrap"]["seed"], bootstrap_samples=0,
            )
            result["config"] = config
            grid_rows.append(result)
    selected = choose_development(grid_rows)
    config = selected["config"]
    development = evaluate(
        list(map(int, splits["development"])), records_by_user, item_meta, config,
        bootstrap_seed=manifest["bootstrap"]["seed"],
        bootstrap_samples=manifest["bootstrap"]["samples"],
    )
    validation = evaluate(
        list(map(int, splits["validation"])), records_by_user, item_meta, config,
        bootstrap_seed=manifest["bootstrap"]["seed"] + 100,
        bootstrap_samples=manifest["bootstrap"]["samples"],
    )
    gate = validation_gate(validation, manifest["validationGates"])
    args.output_dir.mkdir(parents=True, exist_ok=False)
    grid_path = args.output_dir / "development-grid.jsonl"
    with grid_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in grid_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    report = {
        "schemaVersion": "kuaisearch-lite-phone-behavior-memory-v2-report",
        "decision": "PUBLIC_VALIDATION_ACCEPT_PENDING_AUTHORITY" if gate["decision"] == "ACCEPT" else "HOLD_PUBLIC_VALIDATION",
        "startedAt": utc_now(),
        "dataset": {"id": "benchen4395/KuaiSearch", "revision": REVISION},
        "sourceReceipts": {"itemsLite": item_receipt, "recallLite": recall_receipt},
        "split": {
            "developmentUsers": len(splits["development"]),
            "validationUsers": len(splits["validation"]),
            "sealedUsersNotEvaluated": len(splits["sealed-test"]),
            "sealedRowsSkippedBeforeBehaviorAccess": sealed_rows_skipped,
            "publishedAssignmentSha256": manifest["split"]["publishedAssignmentSha256"],
        },
        "strictPhoneItems": len(item_meta),
        "gridConfigurations": len(grid_rows),
        "selectedConfig": config,
        "development": development,
        "validation": {"result": validation, "gate": gate},
        "authority": {
            "sealedStatus": "NOT_RUN_BY_PUBLIC_PROCESS",
            "mayRunOnlyIfValidationAccept": gate["decision"] == "ACCEPT",
        },
        "explicitBoundaries": {
            "behaviorMemoryAuthority": "low",
            "futureClickLabelsIndependentOfProfileFeatures": True,
            "purchaseQuality": False,
            "causalEffect": False,
            "explicitPreferenceQuality": False,
            "productionSwitchAuthority": False,
        },
    }
    report_path = args.output_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    receipt_path = args.output_dir / "receipt.json"
    receipt_path.write_text(json.dumps({
        "schemaVersion": "kuaisearch-lite-phone-behavior-memory-v2-receipt",
        "decision": report["decision"],
        "manifestSha256": hashlib.sha256(manifest_raw).hexdigest(),
        "runnerSha256": sha256_file(Path(__file__)),
        "streamAnalyzerSha256": sha256_file(analyzer_path),
        "splitAssignmentsSha256": sha256_file(split_path),
        "developmentGridSha256": sha256_file(grid_path),
        "reportSha256": sha256_file(report_path),
        "sealedExecuted": False,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    (args.output_dir / "SHA256SUMS.txt").write_text(
        f"{sha256_file(grid_path)}  development-grid.jsonl\n"
        f"{sha256_file(report_path)}  report.json\n"
        f"{sha256_file(receipt_path)}  receipt.json\n",
        encoding="ascii",
    )
    print(json.dumps({
        "decision": report["decision"], "selectedConfig": config,
        "validationGate": gate, "output": str(report_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
