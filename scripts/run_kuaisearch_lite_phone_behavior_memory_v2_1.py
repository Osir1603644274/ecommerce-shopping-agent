#!/usr/bin/env python3
"""Public KuaiSearch phone behavior-memory regression with sealed-safe routing.

V2.1 preserves the frozen V2 treatment while repairing two evaluation defects:
sealed rows are routed before JSON decoding, and acceptance is also gated on
all-requested-user demonstrated utility rather than only positive-label users.
This public run is an iterative regression, not sealed confirmation evidence.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import math
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from analyze_kuaisearch_lite_phone_behavior_v1 import (  # noqa: E402
    BASE_URL,
    REVISION,
    SOURCES,
    classify_category,
    stream_jsonl,
    utc_now,
)
from run_kuaisearch_lite_phone_behavior_memory_v2 import (  # noqa: E402
    bootstrap,
    macro_user,
    normalize_text,
    percentile,
    rerank,
    session_metrics,
    sha256_file,
    title_tokens,
)


ROUTE_PATTERN_TEXT = r'^\{"user_id":\s*([0-9]+),'
ROUTE_PATTERN = re.compile(ROUTE_PATTERN_TEXT.encode("ascii"))
METRICS = ("ndcgAt10", "hitAt3", "mrr")
ZERO_METRICS = {metric: 0.0 for metric in METRICS}


def route_recall_line(
    raw: bytes,
    public_users: set[int],
    sealed_users: set[int],
    *,
    loads: Callable[[bytes], Any] = json.loads,
) -> tuple[str, int, dict[str, Any] | None]:
    """Route by the first user_id envelope field before any JSON decoding."""
    match = ROUTE_PATTERN.match(raw)
    if match is None:
        raise ValueError("recall row does not match the frozen user_id-first envelope")
    user_id = int(match.group(1))
    if user_id in public_users:
        row = loads(raw)
        if not isinstance(row, dict) or int(row.get("user_id", -1)) != user_id:
            raise ValueError("decoded user_id differs from the routed envelope")
        return "public", user_id, row
    if user_id in sealed_users:
        return "sealed", user_id, None
    return "other", user_id, None


def stream_routed_recall(
    source: dict[str, Any],
    public_users: set[int],
    sealed_users: set[int],
    consume_public: Callable[[dict[str, Any], int], None],
) -> dict[str, Any]:
    if public_users & sealed_users:
        raise ValueError("public and sealed users overlap")
    url = f"{BASE_URL}/{source['path']}"
    started = time.perf_counter()
    full_digest = hashlib.sha256()
    public_digest = hashlib.sha256()
    sealed_digest = hashlib.sha256()
    byte_count = row_count = 0
    route_counts: collections.Counter[str] = collections.Counter()
    buffer = b""

    def consume_raw(raw: bytes) -> None:
        nonlocal row_count
        if not raw:
            return
        row_count += 1
        route, _, row = route_recall_line(raw, public_users, sealed_users)
        route_counts[route] += 1
        if route == "public":
            public_digest.update(raw)
            public_digest.update(b"\n")
            assert row is not None
            consume_public(row, row_count)
        elif route == "sealed":
            sealed_digest.update(raw)
            sealed_digest.update(b"\n")

    with requests.get(url, stream=True, timeout=(30, 600), verify=False) as response:
        response.raise_for_status()
        for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
            if not chunk:
                continue
            full_digest.update(chunk)
            byte_count += len(chunk)
            buffer += chunk
            lines = buffer.split(b"\n")
            buffer = lines.pop()
            for raw in lines:
                consume_raw(raw)
        if buffer:
            consume_raw(buffer)

    actual_hash = full_digest.hexdigest()
    if byte_count != source["bytes"] or actual_hash != source["sha256"]:
        raise RuntimeError(
            f"source verification failed for {source['path']}: "
            f"bytes={byte_count} sha256={actual_hash}"
        )
    return {
        "path": source["path"],
        "url": url,
        "expectedBytes": source["bytes"],
        "actualBytes": byte_count,
        "expectedSha256": source["sha256"],
        "actualSha256": actual_hash,
        "rows": row_count,
        "publicDecodedRows": route_counts["public"],
        "sealedRawRowsSkippedBeforeJsonDecode": route_counts["sealed"],
        "otherRawRowsSkippedBeforeJsonDecode": route_counts["other"],
        "publicRawShardSha256": public_digest.hexdigest(),
        "sealedRawShardSha256": sealed_digest.hexdigest(),
        "routing": {
            "version": "user-id-first-envelope-v1",
            "anchoredPattern": ROUTE_PATTERN_TEXT,
            "jsonDecodedRoutes": ["public"],
            "jsonNotDecodedRoutes": ["sealed", "other"],
        },
        "verification": "PASS",
        "wallClockSeconds": round(time.perf_counter() - started, 3),
    }


def _macro_or_zero(rows: Iterable[dict[str, float]]) -> dict[str, float]:
    values = list(rows)
    return macro_user(values) if values else dict(ZERO_METRICS)


def _mean_or_zero(values: Iterable[float]) -> float:
    rows = list(values)
    return statistics.fmean(rows) if rows else 0.0


def evaluate(
    user_ids: list[int],
    records_by_user: dict[int, list[dict[str, Any]]],
    item_meta: dict[int, dict[str, Any]],
    config: dict[str, Any],
    *,
    bootstrap_seed: int,
    bootstrap_samples: int,
    include_per_user: bool = False,
) -> dict[str, Any]:
    per_user: list[dict[str, Any]] = []
    counts: collections.Counter[str] = collections.Counter()
    candidate_failures = hard_filtered = b_c_divergence = 0
    input_duplicate_sessions = 0
    latency_ns: list[int] = []
    profile_bytes: list[int] = []
    change_sessions = top1_change_sessions = 0

    for user_id in user_ids:
        conditional_rows: dict[str, list[dict[str, float]]] = {arm: [] for arm in "ABC"}
        demonstrated_rows: dict[str, list[dict[str, float]]] = {arm: [] for arm in "ABC"}
        for record in records_by_user.get(user_id, []):
            counts["allCandidateSessions"] += 1
            candidates = list(record["candidates"])
            if len(candidates) < 2:
                counts["candidateLessThanTwo"] += 1
                continue
            counts["candidateAtLeastTwo"] += 1
            if len(set(candidates)) != len(candidates):
                input_duplicate_sessions += 1
            future_clicks = set(record["futureStrictClicks"])
            positives = future_clicks & set(candidates)
            if not future_clicks:
                counts["noFutureStrictClick"] += 1
            elif not positives:
                counts["futureStrictClickOutsideCandidates"] += 1
            else:
                counts["futurePositiveInCandidates"] += 1

            orders: dict[str, list[int]] = {}
            observations: dict[str, dict[str, Any]] = {}
            for arm in "ABC":
                order, observation = rerank(
                    candidates, record["history"], item_meta, arm=arm, config=config,
                )
                orders[arm] = order
                observations[arm] = observation
                candidate_failures += int(
                    len(order) != len(candidates)
                    or collections.Counter(order) != collections.Counter(candidates)
                )
                metrics = session_metrics(order, positives) if positives else dict(ZERO_METRICS)
                demonstrated_rows[arm].append(metrics)
                if positives:
                    conditional_rows[arm].append(metrics)

            hard_filtered += max(0, len(candidates) - len(orders["C"]))
            b_c_divergence += int(orders["B"] != orders["C"])
            latency_ns.append(int(observations["C"]["latencyNs"]))
            profile_bytes.append(int(observations["C"]["profileBytes"]))
            change_sessions += int(orders["A"] != orders["C"])
            top1_change_sessions += int(orders["A"][0] != orders["C"][0])

        conditional = {arm: _macro_or_zero(conditional_rows[arm]) for arm in "ABC"}
        demonstrated = {arm: _macro_or_zero(demonstrated_rows[arm]) for arm in "ABC"}
        per_user.append({
            "userId": user_id,
            "conditionalEligible": bool(conditional_rows["A"]),
            "conditional": conditional,
            "demonstrated": demonstrated,
            "futurePositiveSessions": len(conditional_rows["A"]),
            "candidateAtLeastTwoSessions": len(demonstrated_rows["A"]),
        })

    conditional_users = [row for row in per_user if row["conditionalEligible"]]
    conditional_macro = {
        arm: {
            metric: _mean_or_zero(row["conditional"][arm][metric] for row in conditional_users)
            for metric in METRICS
        }
        for arm in "ABC"
    }
    demonstrated_macro = {
        arm: {
            metric: _mean_or_zero(row["demonstrated"][arm][metric] for row in per_user)
            for metric in METRICS
        }
        for arm in "ABC"
    }
    conditional_deltas = {
        metric: [
            row["conditional"]["C"][metric] - row["conditional"]["A"][metric]
            for row in conditional_users
        ]
        for metric in METRICS
    }
    demonstrated_deltas = {
        metric: [
            row["demonstrated"]["C"][metric] - row["demonstrated"]["A"][metric]
            for row in per_user
        ]
        for metric in METRICS
    }
    c_vs_b = {
        metric: _mean_or_zero(
            row["conditional"]["C"][metric] - row["conditional"]["B"][metric]
            for row in conditional_users
        )
        for metric in METRICS
    }
    result: dict[str, Any] = {
        "requestedUsers": len(user_ids),
        "evaluableUsers": len(conditional_users),
        "counts": dict(counts),
        "candidateAtLeast2Coverage": (
            counts["candidateAtLeastTwo"] / max(1, counts["allCandidateSessions"])
        ),
        "futureStrictClickSessionCoverage": (
            (counts["futurePositiveInCandidates"] + counts["futureStrictClickOutsideCandidates"])
            / max(1, counts["candidateAtLeastTwo"])
        ),
        "futurePositiveSessionCoverage": (
            counts["futurePositiveInCandidates"] / max(1, counts["candidateAtLeastTwo"])
        ),
        "evaluableUserCoverage": len(conditional_users) / max(1, len(user_ids)),
        "conditionalMacroUser": conditional_macro,
        "demonstratedUtilityMacroAllRequestedUsers": demonstrated_macro,
        "conditionalCMinusAMean": {
            metric: _mean_or_zero(values) for metric, values in conditional_deltas.items()
        },
        "conditionalCMinusABootstrap95User": {
            metric: bootstrap(values, seed=bootstrap_seed + index, samples=bootstrap_samples)
            for index, (metric, values) in enumerate(conditional_deltas.items())
        },
        "demonstratedCMinusAMean": {
            metric: _mean_or_zero(values) for metric, values in demonstrated_deltas.items()
        },
        "demonstratedCMinusABootstrap95AllRequestedUsers": {
            metric: bootstrap(values, seed=bootstrap_seed + 10 + index, samples=bootstrap_samples)
            for index, (metric, values) in enumerate(demonstrated_deltas.items())
        },
        "conditionalCMinusBMean": c_vs_b,
        "candidateSetFailures": candidate_failures,
        "inputDuplicateCandidateSessions": input_duplicate_sessions,
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
        "conditionalWorstUserCMinusA": {
            metric: min(values, default=0.0) for metric, values in conditional_deltas.items()
        },
        "demonstratedWorstRequestedUserCMinusA": {
            metric: min(values, default=0.0) for metric, values in demonstrated_deltas.items()
        },
    }
    if include_per_user:
        result["perUser"] = per_user
    return result


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
    conditional_ci = result["conditionalCMinusABootstrap95User"]
    demonstrated_ci = result["demonstratedCMinusABootstrap95AllRequestedUsers"]
    checks = {
        "minimumEvaluableUsers": result["evaluableUsers"] >= gates["minimumEvaluableUsers"],
        "minimumCandidateAtLeast2Coverage": (
            result["candidateAtLeast2Coverage"] >= gates["minimumCandidateAtLeast2Coverage"]
        ),
        "minimumFuturePositiveSessionCoverage": (
            result["futurePositiveSessionCoverage"] >= gates["minimumFuturePositiveSessionCoverage"]
        ),
        "minimumEvaluableUserCoverage": (
            result["evaluableUserCoverage"] >= gates["minimumEvaluableUserCoverage"]
        ),
        "conditionalNdcgBootstrapLowerPositive": (
            conditional_ci["ndcgAt10"] is not None
            and conditional_ci["ndcgAt10"][0] > gates["conditionalNdcgBootstrapLowerGreaterThan"]
        ),
        "conditionalHitBootstrapNonInferior": (
            conditional_ci["hitAt3"] is not None
            and conditional_ci["hitAt3"][0] >= gates["conditionalHitBootstrapLowerAtLeast"]
        ),
        "conditionalMrrBootstrapNonInferior": (
            conditional_ci["mrr"] is not None
            and conditional_ci["mrr"][0] >= gates["conditionalMrrBootstrapLowerAtLeast"]
        ),
        "demonstratedNdcgBootstrapLowerPositive": (
            demonstrated_ci["ndcgAt10"] is not None
            and demonstrated_ci["ndcgAt10"][0] > gates["demonstratedNdcgBootstrapLowerGreaterThan"]
        ),
        "demonstratedHitBootstrapNonInferior": (
            demonstrated_ci["hitAt3"] is not None
            and demonstrated_ci["hitAt3"][0] >= gates["demonstratedHitBootstrapLowerAtLeast"]
        ),
        "demonstratedMrrBootstrapNonInferior": (
            demonstrated_ci["mrr"] is not None
            and demonstrated_ci["mrr"][0] >= gates["demonstratedMrrBootstrapLowerAtLeast"]
        ),
        "candidateMultisetPreserved": result["candidateSetFailures"] == 0,
        "inputCandidatesUnique": result["inputDuplicateCandidateSessions"] == 0,
        "hardFilteringZero": result["hardFilteredCandidates"] == 0,
        "bAndCDiffer": result["bCDivergenceSessions"] > 0,
        "minimumChangeRate": result["changeRate"] >= gates["minimumChangeRate"],
        "maximumChangeRate": result["changeRate"] <= gates["maximumChangeRate"],
        "latencyP95": result["rerankLatencyMs"]["p95"] <= gates["maximumRerankP95Ms"],
        "profileP95Bytes": result["profileBytes"]["p95"] <= gates["maximumProfileP95Bytes"],
    }
    return {"checks": checks, "decision": "ACCEPT" if all(checks.values()) else "HOLD"}


def _write_per_user(path: Path, result: dict[str, Any]) -> None:
    rows = result.pop("perUser")
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {args.output_dir}")

    started_at = utc_now()
    manifest_raw = args.manifest.read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest.get("schemaVersion") != "kuaisearch-lite-phone-behavior-memory-v2.1-preregistration":
        raise ValueError("manifest version mismatch")
    if manifest["dataset"]["revision"] != REVISION:
        raise ValueError("dataset revision mismatch")
    if sha256_file(Path(__file__)) != manifest["code"]["runnerSha256"]:
        raise ValueError("runner hash mismatch")
    helper_path = SCRIPT_DIR / "run_kuaisearch_lite_phone_behavior_memory_v2.py"
    if sha256_file(helper_path) != manifest["code"]["v2HelperRunnerSha256"]:
        raise ValueError("V2 helper runner hash mismatch")
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

    def consume_public_recall(row: dict[str, Any], _: int) -> None:
        user_id = int(row["user_id"])
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

    recall_receipt = stream_routed_recall(
        SOURCES["recall_lite"], public_users, sealed_users, consume_public_recall,
    )
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
                            "sessionId": session["sessionId"],
                            "timeIndex": time_index,
                            "history": list(history),
                            "candidates": list(session["candidates"]),
                            "futureStrictClicks": set(session["clicked"]),
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
    if config != manifest["frozenSelectedConfig"]:
        raise RuntimeError("development selection drifted from the V2 frozen config")

    development = evaluate(
        list(map(int, splits["development"])), records_by_user, item_meta, config,
        bootstrap_seed=manifest["bootstrap"]["seed"],
        bootstrap_samples=manifest["bootstrap"]["samples"], include_per_user=True,
    )
    validation = evaluate(
        list(map(int, splits["validation"])), records_by_user, item_meta, config,
        bootstrap_seed=manifest["bootstrap"]["seed"] + 100,
        bootstrap_samples=manifest["bootstrap"]["samples"], include_per_user=True,
    )
    gate = validation_gate(validation, manifest["validationGates"])

    args.output_dir.mkdir(parents=True, exist_ok=False)
    grid_path = args.output_dir / "development-grid.jsonl"
    with grid_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in grid_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    development_user_path = args.output_dir / "development-per-user.jsonl"
    validation_user_path = args.output_dir / "validation-per-user.jsonl"
    _write_per_user(development_user_path, development)
    _write_per_user(validation_user_path, validation)

    report = {
        "schemaVersion": "kuaisearch-lite-phone-behavior-memory-v2.1-report",
        "decision": (
            "PUBLIC_REGRESSION_ACCEPT_AUTHORITY_ELIGIBLE"
            if gate["decision"] == "ACCEPT" else "HOLD_PUBLIC_REGRESSION"
        ),
        "startedAt": started_at,
        "completedAt": utc_now(),
        "dataset": {"id": "benchen4395/KuaiSearch", "revision": REVISION},
        "sourceReceipts": {"itemsLite": item_receipt, "recallLite": recall_receipt},
        "split": {
            "developmentUsers": len(splits["development"]),
            "validationUsers": len(splits["validation"]),
            "sealedUsersNotEvaluated": len(splits["sealed-test"]),
            "sealedRowsSkippedBeforeJsonDecode": recall_receipt[
                "sealedRawRowsSkippedBeforeJsonDecode"
            ],
            "publishedAssignmentSha256": manifest["split"]["publishedAssignmentSha256"],
        },
        "strictPhoneItems": len(item_meta),
        "gridConfigurations": len(grid_rows),
        "selectedConfig": config,
        "development": development,
        "validation": {"result": validation, "gate": gate},
        "authority": {
            "sealedStatus": "NOT_RUN_BY_PUBLIC_PROCESS",
            "mayRunOnlyAfterIndependentV2_1Audit": gate["decision"] == "ACCEPT",
        },
        "explicitBoundaries": {
            "publicRunNature": "ITERATIVE_REGRESSION_NOT_CONFIRMATION",
            "behaviorMemoryAuthority": "low",
            "futureClickLabelsIndependentOfProfileFeatures": True,
            "purchaseQuality": False,
            "causalEffect": False,
            "explicitPreferenceQuality": False,
            "productionSwitchAuthority": False,
        },
    }
    report_path = args.output_dir / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    receipt_path = args.output_dir / "receipt.json"
    receipt = {
        "schemaVersion": "kuaisearch-lite-phone-behavior-memory-v2.1-receipt",
        "decision": report["decision"],
        "manifestSha256": hashlib.sha256(manifest_raw).hexdigest(),
        "runnerSha256": sha256_file(Path(__file__)),
        "v2HelperRunnerSha256": sha256_file(helper_path),
        "streamAnalyzerSha256": sha256_file(analyzer_path),
        "splitAssignmentsSha256": sha256_file(split_path),
        "developmentGridSha256": sha256_file(grid_path),
        "developmentPerUserSha256": sha256_file(development_user_path),
        "validationPerUserSha256": sha256_file(validation_user_path),
        "reportSha256": sha256_file(report_path),
        "sealedExecuted": False,
        "sealedRowsJsonDecoded": 0,
    }
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    artifacts = [grid_path, development_user_path, validation_user_path, report_path, receipt_path]
    (args.output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256_file(path)}  {path.name}\n" for path in artifacts),
        encoding="ascii",
    )
    print(json.dumps({
        "decision": report["decision"],
        "selectedConfig": config,
        "validationGate": gate,
        "output": str(report_path),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
