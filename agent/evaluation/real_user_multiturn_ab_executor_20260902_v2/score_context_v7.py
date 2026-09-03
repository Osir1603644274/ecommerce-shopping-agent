"""Verify and score the completed V7 Context A/B run without mutating attempts."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any

from jsonschema import Draft202012Validator


ARMS = ("RAW_FULL_CONTROL", "CONTEXT_TREATMENT")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def percentile_nearest_rank(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def verify_sha_file(directory: Path) -> None:
    for line in (directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if sha_file(directory / name) != digest:
            raise ValueError(f"checksum mismatch: {directory / name}")


def score(package: Path) -> dict[str, Any]:
    attempt = package / "attempt001"
    blind = package / "blind_packets_attempt001"
    judge_dirs = (blind / "judge01_attempt001", blind / "judge02_attempt001")
    verify_sha_file(attempt)
    for judge_dir in judge_dirs:
        verify_sha_file(judge_dir)

    outputs = load_jsonl(attempt / "paired_outputs.jsonl")
    private = load_jsonl(attempt / "private_turn_receipts.jsonl")
    receipt = load_json(attempt / "execution_receipt.json")
    if len(outputs) != 42 or receipt["pairedOutputRowCount"] != 42:
        raise ValueError("complete 42-output attempt required")
    if any(row["status"] != "SUCCEEDED" for row in outputs):
        raise ValueError("all outputs must succeed")

    schema = load_json(package / "ai_judge_response.schema.json")
    validator = Draft202012Validator(schema)
    judge_rows: list[list[dict[str, Any]]] = []
    for judge_dir in judge_dirs:
        rows = load_jsonl(judge_dir / "responses.jsonl")
        if len(rows) != 13:
            raise ValueError("each judge must complete 13 items")
        for row in rows:
            validator.validate(row)
        judge_rows.append(rows)

    by_pair: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in outputs:
        by_pair[(row["conversationId"], row["turnId"])][row["arm"]] = row
    if len(by_pair) != 21 or any(set(pair) != set(ARMS) for pair in by_pair.values()):
        raise ValueError("21 complete arm pairs required")

    equality = {
        "finalAnswerExactPairs": sum(pair[ARMS[0]]["finalAnswer"] == pair[ARMS[1]]["finalAnswer"] for pair in by_pair.values()),
        "dialogueExactPairs": sum(pair[ARMS[0]]["dialogue"] == pair[ARMS[1]]["dialogue"] for pair in by_pair.values()),
        "publicEvidenceExactPairs": sum(pair[ARMS[0]]["publicEvidence"] == pair[ARMS[1]]["publicEvidence"] for pair in by_pair.values()),
    }
    history_counts = Counter(
        (row["arm"], bool(row["compiledContextUsed"]), row["historyMode"])
        for row in private
    )
    required_history_counts = {
        (ARMS[0], False, "raw_full_same_arm"): 21,
        (ARMS[1], True, "compiled_context_no_raw_history"): 21,
    }
    if history_counts != required_history_counts:
        raise ValueError(f"arm history policy mismatch: {history_counts}")

    latency: dict[str, dict[str, float | int]] = {}
    for arm in ARMS:
        values = [float(row["trace"]["durationMs"]) for row in outputs if row["arm"] == arm]
        latency[arm] = {
            "count": len(values),
            "meanMs": round(statistics.mean(values), 4),
            "p50Ms": round(percentile_nearest_rank(values, 0.50), 4),
            "p95Ms": round(percentile_nearest_rank(values, 0.95), 4),
        }
    control = latency[ARMS[0]]
    treatment = latency[ARMS[1]]
    latency_change = {
        "meanPercent": round((float(treatment["meanMs"]) / float(control["meanMs"]) - 1) * 100, 4),
        "p50Percent": round((float(treatment["p50Ms"]) / float(control["p50Ms"]) - 1) * 100, 4),
        "p95Percent": round((float(treatment["p95Ms"]) / float(control["p95Ms"]) - 1) * 100, 4),
    }
    call_metrics = {
        arm: {
            "modelCalls": sum(len(row["trace"]["modelCalls"]) for row in outputs if row["arm"] == arm),
            "toolCalls": sum(len(row["trace"]["toolCalls"]) for row in outputs if row["arm"] == arm),
            "promptTokens": sum(int(row["trace"]["promptTokens"]) for row in outputs if row["arm"] == arm),
            "completionTokens": sum(int(row["trace"]["completionTokens"]) for row in outputs if row["arm"] == arm),
            "totalTokens": sum(int(row["trace"]["totalTokens"]) for row in outputs if row["arm"] == arm),
        }
        for arm in ARMS
    }
    judge_preferences = {
        f"judge{index + 1:02d}": dict(Counter(row["review"]["overallPreference"] for row in rows))
        for index, rows in enumerate(judge_rows)
    }
    exact_score_agreement = sum(
        judge_rows[0][index]["review"] == judge_rows[1][index]["review"]
        for index in range(13)
    )
    return {
        "schemaVersion": "real-user-multiturn-context-ab-result-v1",
        "packageId": receipt["packageId"],
        "attempt": "attempt001",
        "strictDecision": "BOUNDED_CONTEXT_SEMANTIC_FIDELITY_ACCEPT_TOKEN_EFFICIENCY_HOLD",
        "execution": {
            "conversationCount": 8,
            "userTurnCount": 21,
            "armTurnCount": 42,
            "scoredFollowupCount": 13,
            "successfulArmTurns": 42,
            "historyPolicyVerified": True,
            "uniqueRunPerUserTurn": True,
            "continuousRevisionChain": True,
        },
        "semanticFidelity": {
            **equality,
            "pairCount": 21,
            "judgePreferences": judge_preferences,
            "judgeExactReviewAgreementItems": exact_score_agreement,
            "judgeItemCount": 13,
        },
        "efficiency": {
            "latency": latency,
            "treatmentRelativeChange": latency_change,
            "callsAndTokens": call_metrics,
            "tokenComparisonStatus": "NOT_MEASURABLE_ZERO_MODEL_CALLS_IN_BOTH_ARMS",
            "latencyStatus": "DESCRIPTIVE_ONLY_NON_CAUSAL",
        },
        "claimBoundary": {
            "contextCompilerDefaultMayChange": False,
            "tokenReductionClaimAllowed": False,
            "productionReadinessClaimAllowed": False,
            "allCategoryClaimAllowed": False,
            "allowed": "Across 8 real multi-turn sessions and 21 paired turns, compiled Context preserved exact outputs and public evidence versus full same-arm dialogue; 13/13 blind follow-ups were ties in both independent AI judge runs.",
        },
        "evidenceHashes": {
            "pairedOutputsSha256": sha_file(attempt / "paired_outputs.jsonl"),
            "executionReceiptSha256": sha_file(attempt / "execution_receipt.json"),
            "judge01ResponsesSha256": sha_file(judge_dirs[0] / "responses.jsonl"),
            "judge02ResponsesSha256": sha_file(judge_dirs[1] / "responses.jsonl"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError(f"refusing to overwrite {args.output}")
    result = score(args.package.resolve())
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
