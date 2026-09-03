"""Paired live scorer for the corrected TaskState semantic-source experiment."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


MODULE_PATH = Path(__file__).resolve()
EVALUATION_ROOT = MODULE_PATH.parent
ASSET_ROOT = EVALUATION_ROOT / "assets" / "shopping_task_state_context_ab_v2_20260827"
TREATMENT_SERVER = EVALUATION_ROOT / "shopping_task_state_context_ab_v2_server.py"
HTTP_RUNNER = EVALUATION_ROOT / "used_phone_harness_behavior_runner_v1.py"
ACTION_SCORER = EVALUATION_ROOT / "used_phone_harness_behavior_scorer_v1.py"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _value_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 2)


def _key(row: dict[str, Any]) -> tuple[str, str]:
    return str(row["scenarioId"]), str(row["turnId"])


def _action(row: dict[str, Any]) -> tuple[str | None, str | None]:
    selected = row.get("selectedAction") or {}
    kind = selected.get("kind")
    return kind, selected.get("toolName") if kind == "CALL_TOOL" else None


def _normalized_scope(row: dict[str, Any]) -> dict[str, Any] | None:
    task_state = row.get("taskState") or {}
    domain = task_state.get("domainState") or {}
    scope = domain.get("candidateScope")
    if not isinstance(scope, dict):
        return None
    ignored = {
        "scopeId",
        "taskId",
        "sourceRevision",
        "sourcePlanId",
        "sourceStepId",
        "createdAt",
    }
    return {key: value for key, value in scope.items() if key not in ignored}


def _semantic_state(row: dict[str, Any]) -> dict[str, Any]:
    task_state = row.get("taskState") or {}
    domain = task_state.get("domainState") or {}
    guide = domain.get("shoppingGuide") or {}
    return {
        "status": task_state.get("status"),
        "goal": task_state.get("goal"),
        "unknowns": task_state.get("unknowns") or [],
        "pendingQuestions": task_state.get("pendingQuestions") or [],
        "mode": guide.get("mode"),
        "category": guide.get("category"),
        "requirements": guide.get("requirements") or [],
        "comparedIds": guide.get("comparedIds") or [],
        "evidenceStatus": guide.get("evidenceStatus"),
    }


def _score_index(score: dict[str, Any]) -> dict[tuple[str, str], bool]:
    failures = {
        (str(row["scenarioId"]), str(row["turnId"]))
        for row in score.get("failures") or []
    }
    return {key: False for key in failures}


async def _fetch_trace_metrics(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    debug_key: str,
    run_id: str | None,
) -> dict[str, Any]:
    if not run_id:
        return {"status": "not_observed"}
    response = await client.get(
        f"{base_url.rstrip('/')}/internal/debug/agent-runs/{run_id}",
        headers={"X-Agent-Debug-Key": debug_key},
    )
    response.raise_for_status()
    trace = response.json()
    return {
        "status": "observed",
        "runId": run_id,
        "enteredRuntime": trace.get("enteredRuntime"),
        "contextPackHash": trace.get("contextPackHash"),
        "contextTokenCount": trace.get("contextTokenCount"),
        "contextViews": [
            {
                "type": item.get("type"),
                "hash": item.get("hash"),
                "tokenCount": item.get("tokenCount"),
            }
            for item in (trace.get("contextViews") or [])
            if isinstance(item, dict)
        ],
        "toolCallCount": len(trace.get("toolCalls") or []),
        "totalDurationMs": trace.get("totalDurationMs"),
        "degraded": trace.get("degraded"),
        "degradedReasons": trace.get("degradedReasons") or [],
    }


async def score_live_pair(
    *,
    control_receipts: Path,
    treatment_receipts: Path,
    control_action_score: Path,
    treatment_action_score: Path,
    control_base_url: str,
    treatment_base_url: str,
    debug_key: str,
    treatment_status_url: str,
    output_dir: Path,
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    control = _read_jsonl(control_receipts)
    treatment = _read_jsonl(treatment_receipts)
    control_by_key = {_key(row): row for row in control}
    treatment_by_key = {_key(row): row for row in treatment}
    if set(control_by_key) != set(treatment_by_key):
        raise ValueError("paired receipt keys do not match")

    control_score = json.loads(control_action_score.read_text(encoding="utf-8"))
    treatment_score = json.loads(treatment_action_score.read_text(encoding="utf-8"))
    control_failures = _score_index(control_score)
    treatment_failures = _score_index(treatment_score)

    pairs: list[dict[str, Any]] = []
    model_calls = {"control": Counter(), "treatment": Counter()}
    model_durations = {"control": Counter(), "treatment": Counter()}
    runner_durations = {"control": [], "treatment": []}
    context_tokens = {"control": [], "treatment": []}
    view_tokens = {"control": [], "treatment": []}

    async with httpx.AsyncClient(timeout=20.0) as client:
        treatment_status_response = await client.get(
            treatment_status_url,
            headers={"X-Agent-Debug-Key": debug_key},
        )
        treatment_status_response.raise_for_status()
        treatment_status = treatment_status_response.json()

        for key in sorted(control_by_key):
            left = control_by_key[key]
            right = treatment_by_key[key]
            left_trace, right_trace = await asyncio.gather(
                _fetch_trace_metrics(
                    client,
                    base_url=control_base_url,
                    debug_key=debug_key,
                    run_id=left.get("runId"),
                ),
                _fetch_trace_metrics(
                    client,
                    base_url=treatment_base_url,
                    debug_key=debug_key,
                    run_id=right.get("runId"),
                ),
            )
            for arm, row, trace in (
                ("control", left, left_trace),
                ("treatment", right, right_trace),
            ):
                attribution = row.get("modelAttribution") or {}
                model_calls[arm].update(attribution.get("modelCallCounts") or {})
                model_durations[arm].update(attribution.get("llmDurationByStageMs") or {})
                duration = row.get("runnerDurationMs")
                if isinstance(duration, (int, float)):
                    runner_durations[arm].append(float(duration))
                token_count = trace.get("contextTokenCount")
                if type(token_count) is int:
                    context_tokens[arm].append(token_count)
                for view in trace.get("contextViews") or []:
                    view_count = view.get("tokenCount")
                    if type(view_count) is int:
                        view_tokens[arm].append(view_count)

            answer_hashes = (_value_hash(left.get("answer")), _value_hash(right.get("answer")))
            guide_hashes = (
                _value_hash(left.get("guideResult")),
                _value_hash(right.get("guideResult")),
            )
            control_action_pass = key not in control_failures
            treatment_action_pass = key not in treatment_failures
            pair = {
                "scenarioId": key[0],
                "turnId": key[1],
                "controlStatus": left.get("status"),
                "treatmentStatus": right.get("status"),
                "controlAgentFailureCode": (left.get("requestTrace") or {}).get("agentFailureCode"),
                "treatmentAgentFailureCode": (right.get("requestTrace") or {}).get("agentFailureCode"),
                "controlAction": list(_action(left)),
                "treatmentAction": list(_action(right)),
                "actionEqual": _action(left) == _action(right),
                "controlActionPass": control_action_pass,
                "treatmentActionPass": treatment_action_pass,
                "treatmentRegression": control_action_pass and not treatment_action_pass,
                "answerSha256": {"control": answer_hashes[0], "treatment": answer_hashes[1]},
                "guideResultSha256": {"control": guide_hashes[0], "treatment": guide_hashes[1]},
                "userVisibleDifference": answer_hashes[0] != answer_hashes[1] or guide_hashes[0] != guide_hashes[1],
                "semanticStateEqual": _semantic_state(left) == _semantic_state(right),
                "normalizedCandidateScopeEqual": _normalized_scope(left) == _normalized_scope(right),
                "toolNamesEqual": [item.get("tool") for item in left.get("toolTrace") or []]
                == [item.get("tool") for item in right.get("toolTrace") or []],
                "controlTrace": left_trace,
                "treatmentTrace": right_trace,
            }
            pairs.append(pair)

    trace_path = output_dir / "paired-trace-metrics.jsonl"
    trace_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in pairs),
        encoding="utf-8",
        newline="\n",
    )

    regressions = [
        {"scenarioId": row["scenarioId"], "turnId": row["turnId"], "controlAction": row["controlAction"], "treatmentAction": row["treatmentAction"]}
        for row in pairs
        if row["treatmentRegression"]
    ]
    common_failures = [
        {"scenarioId": row["scenarioId"], "turnId": row["turnId"]}
        for row in pairs
        if not row["controlActionPass"] and not row["treatmentActionPass"]
    ]
    projection_failure_turns = [
        {
            "scenarioId": row["scenarioId"],
            "turnId": row["turnId"],
            "failureCode": row["treatmentAgentFailureCode"],
        }
        for row in pairs
        if row["treatmentAgentFailureCode"] == "context_pack_build_failed"
    ]
    both_action_safe = (
        control_score.get("passedTurns") == control_score.get("evaluatedTurns")
        and treatment_score.get("passedTurns") == treatment_score.get("evaluatedTurns")
    )
    safety_passed = (
        both_action_safe
        and not regressions
        and all(row["controlStatus"] == row["treatmentStatus"] == "ok" for row in pairs)
        and treatment_status.get("projectionFailures") == 0
        and treatment_status.get("projectionCount", 0) > 0
    )
    visible_differences = sum(row["userVisibleDifference"] for row in pairs)
    verdict = (
        "HOLD_LIVE_SAFETY_REGRESSION"
        if regressions
        else "HOLD_BOTH_ARMS_SAFETY_NOT_PASSED"
        if not both_action_safe
        else "READY_FOR_BLIND_REVIEW"
        if visible_differences
        else "HOLD_NO_USER_VISIBLE_DIFFERENCE"
    )
    report = {
        "schemaVersion": "shopping-task-state-context-live-score-v2",
        "experimentId": "shopping-task-state-context-ab-v2-20260827",
        "status": "COMPLETE",
        "verdict": verdict,
        "liveSafetyPassed": safety_passed,
        "pairedTurnCount": len(pairs),
        "controlActionGate": {
            "evaluated": control_score.get("evaluatedTurns"),
            "passed": control_score.get("passedTurns"),
            "accuracy": control_score.get("actionGateAccuracy"),
        },
        "treatmentActionGate": {
            "evaluated": treatment_score.get("evaluatedTurns"),
            "passed": treatment_score.get("passedTurns"),
            "accuracy": treatment_score.get("actionGateAccuracy"),
        },
        "treatmentRegressions": regressions,
        "commonActionFailures": common_failures,
        "treatmentProjectionFailureTurns": projection_failure_turns,
        "confirmedProjectionFailureCause": (
            "V2 requirement source provenance is lossy: detailed inferred source text in the shared CandidateScope requirementsSnapshot is reduced to source=inferred in ShoppingTaskStateV2, so the fail-closed equality check rejects the join."
            if projection_failure_turns
            else None
        ),
        "userVisibleDifferenceTurns": visible_differences,
        "semanticStateEqualTurns": sum(row["semanticStateEqual"] for row in pairs),
        "normalizedCandidateScopeEqualTurns": sum(row["normalizedCandidateScopeEqual"] for row in pairs),
        "toolNamesEqualTurns": sum(row["toolNamesEqual"] for row in pairs),
        "treatmentProjection": treatment_status,
        "callsByStage": {
            arm: dict(sorted(counts.items())) for arm, counts in model_calls.items()
        },
        "llmDurationByStageMs": {
            arm: {key: round(value, 3) for key, value in sorted(counts.items())}
            for arm, counts in model_durations.items()
        },
        "toolCalls": {
            "control": sum(len(row.get("toolTrace") or []) for row in control),
            "treatment": sum(len(row.get("toolTrace") or []) for row in treatment),
        },
        "tokens": {
            "providerInputOutput": {
                "status": "UNAVAILABLE_NOT_INSTRUMENTED",
                "input": None,
                "output": None,
            },
            "contextPackObserved": {
                arm: {
                    "count": len(values),
                    "sum": sum(values),
                    "mean": round(statistics.fmean(values), 2) if values else None,
                }
                for arm, values in context_tokens.items()
            },
            "contextViewsObserved": {
                arm: {
                    "count": len(values),
                    "sum": sum(values),
                    "mean": round(statistics.fmean(values), 2) if values else None,
                }
                for arm, values in view_tokens.items()
            },
        },
        "runnerLatencyMs": {
            arm: {
                "p50": _percentile(values, 0.50),
                "p95": _percentile(values, 0.95),
                "mean": round(statistics.fmean(values), 2) if values else None,
            }
            for arm, values in runner_durations.items()
        },
        "callAttributionBoundary": {
            "taskStateDecisionStages": ["task_state"],
            "reactDecisionStages": ["react_decision"],
            "excludedFromTaskStateOrReactDecision": ["task_manager", "final_answer"],
        },
        "humanReviewPacketGenerated": False,
        "nextGate": "STOP_NO_HUMAN_PACKET",
    }
    score_path = output_dir / "score.json"
    score_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schemaVersion": "shopping-task-state-context-live-manifest-v2",
        "experimentId": report["experimentId"],
        "finishedAt": datetime.now(timezone.utc).isoformat(),
        "controlReceipts": str(control_receipts),
        "controlReceiptsSha256": _sha256(control_receipts),
        "treatmentReceipts": str(treatment_receipts),
        "treatmentReceiptsSha256": _sha256(treatment_receipts),
        "controlActionScoreSha256": _sha256(control_action_score),
        "treatmentActionScoreSha256": _sha256(treatment_action_score),
        "preregistrationSha256": _sha256(ASSET_ROOT / "manifest.json"),
        "selectionSha256": _sha256(ASSET_ROOT / "selection.json"),
        "liveScorer": str(MODULE_PATH),
        "liveScorerSha256": _sha256(MODULE_PATH),
        "treatmentServer": str(TREATMENT_SERVER),
        "treatmentServerSha256": _sha256(TREATMENT_SERVER),
        "httpRunnerSha256": _sha256(HTTP_RUNNER),
        "actionScorerSha256": _sha256(ACTION_SCORER),
        "pairedTraceMetricsSha256": _sha256(trace_path),
        "scoreSha256": _sha256(score_path),
        "privateOracleReadByLiveRunner": False,
        "privateOracleReadByScorer": True,
        "productionContractChanged": False,
        "fixedRuntimeChanged": False,
        "reactLiveChanged": False,
        "humanReviewPacketGenerated": False,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"manifest": manifest, "score": report}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-receipts", type=Path, required=True)
    parser.add_argument("--treatment-receipts", type=Path, required=True)
    parser.add_argument("--control-action-score", type=Path, required=True)
    parser.add_argument("--treatment-action-score", type=Path, required=True)
    parser.add_argument("--control-base-url", required=True)
    parser.add_argument("--treatment-base-url", required=True)
    parser.add_argument("--treatment-status-url", required=True)
    parser.add_argument("--debug-key", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(score_live_pair(**vars(args)))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
