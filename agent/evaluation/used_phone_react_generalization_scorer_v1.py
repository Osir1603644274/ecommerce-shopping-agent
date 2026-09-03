"""Score preregistered ReAct V0 held-out behavior without judging answer quality."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if any(not isinstance(value, dict) for value in values):
        raise ValueError(f"{path}: expected JSON objects")
    return values


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 2)


def _expected_turns(dataset: list[dict[str, Any]]) -> tuple[
    set[tuple[str, str]], dict[str, str]
]:
    expected: set[tuple[str, str]] = set()
    classes: dict[str, str] = {}
    for scenario in dataset:
        scenario_id = scenario.get("scenarioId")
        scenario_class = scenario.get("generalizationClass")
        turns = scenario.get("turns")
        if not isinstance(scenario_id, str) or not isinstance(scenario_class, str):
            raise ValueError("dataset scenario identity/class invalid")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"{scenario_id}: turns invalid")
        classes[scenario_id] = scenario_class
        for turn in turns:
            turn_id = turn.get("turnId") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str):
                raise ValueError(f"{scenario_id}: turnId invalid")
            key = (scenario_id, turn_id)
            if key in expected:
                raise ValueError(f"duplicate dataset turn: {key}")
            expected.add(key)
    return expected, classes


def _receipt_map(
    path: Path, expected: set[tuple[str, str]]
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _read_jsonl(path):
        key = (row.get("scenarioId"), row.get("turnId"))
        if key in result:
            raise ValueError(f"{path}: duplicate receipt {key}")
        result[key] = row
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        raise ValueError(f"{path}: receipt set mismatch; missing={missing}, extra={extra}")
    return result


def _sequence_signature(receipt: dict[str, Any]) -> str:
    sequence = receipt.get("reactSequence")
    actions = sequence.get("actions") if isinstance(sequence, dict) else None
    if not isinstance(actions, list):
        return "NOT_OBSERVED"
    return "->".join(
        str(item.get("kind") or "DECISION_FAILED")
        for item in actions if isinstance(item, dict)
    )


def _option_violations(receipt: dict[str, Any]) -> int:
    sequence = receipt.get("reactSequence")
    actions = sequence.get("actions") if isinstance(sequence, dict) else None
    if not isinstance(actions, list):
        return 0
    violations = 0
    for action in actions:
        if not isinstance(action, dict) or action.get("status") != "accepted":
            continue
        option_id = action.get("optionId")
        published = action.get("publishedOptionIds")
        if not isinstance(option_id, str) or not isinstance(published, list) or option_id not in published:
            violations += 1
    return violations


def _react_calls(receipt: dict[str, Any]) -> int:
    attribution = receipt.get("modelAttribution")
    counts = attribution.get("modelCallCounts") if isinstance(attribution, dict) else None
    value = counts.get("react_decision", 0) if isinstance(counts, dict) else 0
    return int(value) if isinstance(value, int) else 0


def _model_after_observation(receipt: dict[str, Any]) -> bool:
    sequence = receipt.get("reactSequence")
    actions = sequence.get("actions") if isinstance(sequence, dict) else None
    return bool(
        isinstance(actions, list)
        and any(
            isinstance(action, dict)
            and index > 0
            and action.get("decisionSource") == "model"
            for index, action in enumerate(actions)
        )
    )


def _target_check(
    key: tuple[str, str], receipt: dict[str, Any], target: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    sequence = receipt.get("reactSequence")
    actions = sequence.get("actions") if isinstance(sequence, dict) else None
    outcomes = sequence.get("outcomes") if isinstance(sequence, dict) else None
    kinds = [
        item.get("kind")
        for item in actions or []
        if isinstance(item, dict)
    ]
    prefix = target.get("requiredPrefix") or []
    if kinds[: len(prefix)] != prefix:
        failures.append("required_prefix_missing")
    terminals = target.get("allowedTerminalKinds") or []
    if not kinds or kinds[-1] not in terminals:
        failures.append("terminal_kind_invalid")
    required_error = target.get("requiredToolErrorCode")
    if required_error is not None and not any(
        isinstance(item, dict) and item.get("errorCode") == required_error
        for item in outcomes or []
    ):
        failures.append("required_tool_error_missing")
    if _react_calls(receipt) != target.get("requiredReactDecisionCalls"):
        failures.append("react_decision_call_count_mismatch")
    if target.get("requiredModelAfterObservation") and not _model_after_observation(receipt):
        failures.append("model_after_observation_missing")
    answer = str(receipt.get("answer") or "")
    if target.get("requiredEvidenceBoundary"):
        if "帧率" not in answer or not any(word in answer for word in ("散热", "发热", "温度")):
            failures.append("evidence_boundary_terms_missing")
        if not any(word in answer for word in ("无法", "不能", "没有", "缺少", "未")):
            failures.append("evidence_gap_not_explicit")
    if target.get("requiredStaleScopeClarification") and not any(
        word in answer for word in ("旧", "之前", "先前", "原来", "硬条件")
    ):
        failures.append("stale_scope_boundary_not_explicit")
    return failures


def score_generalization(
    *,
    dataset_path: Path,
    preregistration_path: Path,
    react_paths: list[Path],
    fixed_paths: list[Path],
) -> dict[str, Any]:
    dataset = _read_jsonl(dataset_path)
    prereg = _read_json(preregistration_path)
    if _sha256(dataset_path) != prereg.get("datasetSha256"):
        raise ValueError("dataset hash does not match preregistration")
    expected, classes = _expected_turns(dataset)
    required_pairs = prereg.get("pairedRunCount")
    if len(react_paths) != required_pairs or len(fixed_paths) != required_pairs:
        raise ValueError("paired run count does not match preregistration")

    adaptive_targets = {
        tuple(key.split(":")): value
        for key, value in (prereg.get("adaptiveTargets") or {}).items()
    }
    forbidden_classes = set(prereg.get("reactDecisionForbiddenClasses") or [])
    failures: list[dict[str, Any]] = []
    option_violations = 0
    outside_target_calls = 0
    stage_calls: Counter[str] = Counter()
    stage_durations: Counter[str] = Counter()
    react_latencies: list[float] = []
    fixed_latencies: list[float] = []
    target_evidence: dict[str, list[dict[str, Any]]] = {
        f"{key[0]}:{key[1]}": [] for key in adaptive_targets
    }
    run_summaries: list[dict[str, Any]] = []

    for run_index, path in enumerate(react_paths, 1):
        receipts = _receipt_map(path, expected)
        run_failures = 0
        for key, receipt in receipts.items():
            if (
                receipt.get("status") != "ok"
                or receipt.get("enteredRuntime") != "react_v0"
                or receipt.get("runtimeMatches") is not True
                or not isinstance(receipt.get("answer"), str)
                or not receipt.get("answer")
            ):
                failures.append({"run": run_index, "target": f"{key[0]}:{key[1]}", "code": "react_receipt_incomplete"})
                run_failures += 1
            sequence = receipt.get("reactSequence")
            actions = sequence.get("actions") if isinstance(sequence, dict) else None
            outcomes = sequence.get("outcomes") if isinstance(sequence, dict) else None
            if not isinstance(actions, list) or not actions or not isinstance(outcomes, list):
                failures.append({"run": run_index, "target": f"{key[0]}:{key[1]}", "code": "sequence_not_observed"})
                run_failures += 1
            option_violations += _option_violations(receipt)
            calls = _react_calls(receipt)
            if key not in adaptive_targets and calls:
                outside_target_calls += calls
                failures.append({"run": run_index, "target": f"{key[0]}:{key[1]}", "code": "react_decision_outside_target"})
                run_failures += 1
            if classes[key[0]] in forbidden_classes and calls:
                failures.append({"run": run_index, "target": f"{key[0]}:{key[1]}", "code": "react_decision_in_forbidden_class"})
                run_failures += 1
            if key in adaptive_targets:
                target_failures = _target_check(key, receipt, adaptive_targets[key])
                for code in target_failures:
                    failures.append({"run": run_index, "target": f"{key[0]}:{key[1]}", "code": code})
                    run_failures += 1
                target_evidence[f"{key[0]}:{key[1]}"].append({
                    "run": run_index,
                    "requestId": receipt.get("requestId"),
                    "signature": _sequence_signature(receipt),
                    "reactDecisionCalls": calls,
                    "modelAfterObservation": _model_after_observation(receipt),
                    "failures": target_failures,
                })
            duration = receipt.get("runnerDurationMs")
            if isinstance(duration, (int, float)):
                react_latencies.append(float(duration))
            attribution = receipt.get("modelAttribution")
            if isinstance(attribution, dict):
                for stage, count in (attribution.get("modelCallCounts") or {}).items():
                    if isinstance(stage, str) and isinstance(count, int):
                        stage_calls[stage] += count
                for stage, duration_ms in (attribution.get("llmDurationByStageMs") or {}).items():
                    if isinstance(stage, str) and isinstance(duration_ms, (int, float)):
                        stage_durations[stage] += float(duration_ms)
        run_summaries.append({
            "runtime": "react_v0",
            "run": run_index,
            "receipts": str(path),
            "receiptsSha256": _sha256(path),
            "turnCount": len(receipts),
            "failureCount": run_failures,
        })

    for run_index, path in enumerate(fixed_paths, 1):
        receipts = _receipt_map(path, expected)
        run_failures = 0
        for key, receipt in receipts.items():
            if (
                receipt.get("status") != "ok"
                or receipt.get("runtimeMatches") is not True
                or not isinstance(receipt.get("answer"), str)
                or not receipt.get("answer")
            ):
                failures.append({"run": run_index, "runtime": "fixed_v1", "target": f"{key[0]}:{key[1]}", "code": "fixed_receipt_incomplete"})
                run_failures += 1
            duration = receipt.get("runnerDurationMs")
            if isinstance(duration, (int, float)):
                fixed_latencies.append(float(duration))
        run_summaries.append({
            "runtime": "fixed_v1",
            "run": run_index,
            "receipts": str(path),
            "receiptsSha256": _sha256(path),
            "turnCount": len(receipts),
            "failureCount": run_failures,
        })

    if option_violations:
        failures.append({"code": "published_option_violation", "count": option_violations})
    return {
        "schemaVersion": "used-phone-react-generalization-score-v1",
        "status": "ACCEPT" if not failures else "HOLD",
        "scope": "preregistered_nine_scenario_two_paired_live_runs",
        "datasetSha256": _sha256(dataset_path),
        "preregistrationSha256": _sha256(preregistration_path),
        "scenarioCount": len(dataset),
        "turnCountPerRun": len(expected),
        "pairedRunCount": len(react_paths),
        "publishedOptionViolationCount": option_violations,
        "reactDecisionCallsOutsideAdaptiveTargets": outside_target_calls,
        "adaptiveTargetEvidence": target_evidence,
        "modelCallCounts": dict(stage_calls),
        "llmDurationByStageMs": {
            key: round(value, 2) for key, value in stage_durations.items()
        },
        "latencyMs": {
            "react_v0": {
                "mean": round(statistics.fmean(react_latencies), 2),
                "p95": _percentile(react_latencies, 0.95),
            },
            "fixed_v1": {
                "mean": round(statistics.fmean(fixed_latencies), 2),
                "p95": _percentile(fixed_latencies, 0.95),
            },
        },
        "runs": run_summaries,
        "failureCount": len(failures),
        "failures": failures,
        "humanReviewStatus": "HOLD_PENDING_INDEPENDENT_REVIEW",
        "claimBoundary": {
            "scoresExecutionAndRoutingContracts": True,
            "scoresAnswerQuality": False,
            "provesStatisticalSignificance": False,
            "provesGeneralSuperiority": False,
            "authorizesDefaultRuntimeSwitch": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--preregistration", required=True, type=Path)
    parser.add_argument("--react-receipts", action="append", required=True, type=Path)
    parser.add_argument("--fixed-receipts", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite score: {args.output}")
    report = score_generalization(
        dataset_path=args.dataset,
        preregistration_path=args.preregistration,
        react_paths=args.react_receipts,
        fixed_paths=args.fixed_receipts,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
