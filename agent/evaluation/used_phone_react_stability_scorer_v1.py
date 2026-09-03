"""Aggregate preregistered repeated ReAct/fixed_v1 paired receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.used_phone_harness_behavior_scorer_v1 import (
    score_receipts as score_action_receipts,
)
from evaluation.used_phone_react_sequence_scorer_v1 import (
    score_receipts as score_sequence_receipts,
)


TARGET_SCENARIOS = {"UPHB-V1-015", "UPHB-V1-022", "UPHB-V1-024"}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(int(round((len(ordered) - 1) * fraction)), len(ordered) - 1)
    return round(ordered[index], 2)


def _assert_target_batch(path: Path, receipts: list[dict[str, Any]]) -> None:
    scenarios = {str(item.get("scenarioId")) for item in receipts}
    if scenarios != TARGET_SCENARIOS or len(receipts) != 9:
        raise ValueError(f"{path} is not an exact 3-scenario/9-turn batch")


def score_stability(
    react_paths: list[Path],
    fixed_paths: list[Path],
) -> dict[str, Any]:
    if not react_paths or len(react_paths) != len(fixed_paths):
        raise ValueError("paired stability scoring requires equal non-zero run counts")

    react_runs: list[dict[str, Any]] = []
    fixed_runs: list[dict[str, Any]] = []
    react_latencies: list[float] = []
    fixed_latencies: list[float] = []
    stage_calls: Counter[str] = Counter()
    stage_durations: Counter[str] = Counter()

    for index, path in enumerate(react_paths, start=1):
        receipts = _read_jsonl(path)
        _assert_target_batch(path, receipts)
        sequence = score_sequence_receipts(path)
        action = score_action_receipts(path)
        for receipt in receipts:
            duration = receipt.get("runnerDurationMs")
            if isinstance(duration, (int, float)):
                react_latencies.append(float(duration))
            attribution = receipt.get("modelAttribution")
            if isinstance(attribution, dict):
                for stage, count in (attribution.get("modelCallCounts") or {}).items():
                    if isinstance(stage, str) and isinstance(count, int):
                        stage_calls[stage] += count
                for stage, duration_ms in (
                    attribution.get("llmDurationByStageMs") or {}
                ).items():
                    if isinstance(stage, str) and isinstance(duration_ms, (int, float)):
                        stage_durations[stage] += float(duration_ms)
        react_runs.append({
            "pairIndex": index,
            "receipts": str(path),
            "receiptsSha256": _sha256(path),
            "threeScenarioGate": sequence["threeScenarioAdaptiveGate"]["status"],
            "completeTurns": sequence["completeSequenceTurns"],
            "successfulTurns": sequence["successfulTurns"],
            "failedClosedTurns": sequence["failedClosedTurns"],
            "publishedOptionViolations": sequence["publishedOptionViolationCount"],
            "actionGateAccuracy": action["actionGateAccuracy"],
        })

    for index, path in enumerate(fixed_paths, start=1):
        receipts = _read_jsonl(path)
        _assert_target_batch(path, receipts)
        action = score_action_receipts(path)
        for receipt in receipts:
            duration = receipt.get("runnerDurationMs")
            if isinstance(duration, (int, float)):
                fixed_latencies.append(float(duration))
        fixed_runs.append({
            "pairIndex": index,
            "receipts": str(path),
            "receiptsSha256": _sha256(path),
            "evaluatedTurns": action["evaluatedTurns"],
            "passedTurns": action["passedTurns"],
            "excludedCounts": action["excludedCounts"],
            "actionGateAccuracy": action["actionGateAccuracy"],
            "failureTargets": [
                f"{item['scenarioId']}:{item['turnId']}"
                for item in action["failures"]
            ],
        })

    react_accepts = sum(
        item["threeScenarioGate"] == "ACCEPT" for item in react_runs
    )
    fixed_evaluated = sum(item["evaluatedTurns"] for item in fixed_runs)
    fixed_passed = sum(item["passedTurns"] for item in fixed_runs)
    return {
        "schemaVersion": "used-phone-react-stability-score-v1",
        "scope": "three_scenario_repeated_live_behavior_and_cost_only",
        "pairedRunCount": len(react_runs),
        "react": {
            "acceptedRuns": react_accepts,
            "runAcceptanceRate": round(react_accepts / len(react_runs), 4),
            "meanTurnLatencyMs": round(statistics.fmean(react_latencies), 2),
            "p95TurnLatencyMs": _percentile(react_latencies, 0.95),
            "modelCallCounts": dict(stage_calls),
            "llmDurationByStageMs": {
                key: round(value, 2) for key, value in stage_durations.items()
            },
            "runs": react_runs,
        },
        "fixedV1": {
            "evaluatedTurns": fixed_evaluated,
            "passedTurns": fixed_passed,
            "actionGateAccuracy": (
                round(fixed_passed / fixed_evaluated, 4)
                if fixed_evaluated else None
            ),
            "meanTurnLatencyMs": round(statistics.fmean(fixed_latencies), 2),
            "p95TurnLatencyMs": _percentile(fixed_latencies, 0.95),
            "runs": fixed_runs,
        },
        "status": "ACCEPT" if react_accepts == len(react_runs) else "HOLD",
        "claimBoundary": {
            "scoresRepeatedBehaviorStability": True,
            "scoresModelCallAttribution": True,
            "scoresAnswerQuality": False,
            "provesGeneralSuperiority": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--react-receipts", type=Path, action="append", required=True)
    parser.add_argument("--fixed-receipts", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite score: {args.output}")
    report = score_stability(args.react_receipts, args.fixed_receipts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
