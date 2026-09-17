"""Current-source model-in-the-loop Context A/B for the 2026-09-04 program.

The runner reuses the already-tested isolated Redis lane and frozen 439-product
transport.  It writes every completed arm turn durably and never retries or
overwrites an attempt directory.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import random
import statistics
import sys
import time
from typing import Any


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
DATASET = HERE / "p1/conversations.jsonl"
SOURCE_FREEZE = HERE / "p0/source_freeze.json"
ARMS = ("RAW_FULL_CONTROL", "CONTEXT_TREATMENT")
SEED = 20260904


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def append_jsonl(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def build_schedule(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    rows: list[dict[str, Any]] = []
    ordinal = 0
    for conversation in conversations:
        for turn in conversation["turns"]:
            order = list(ARMS)
            rng.shuffle(order)
            for arm in order:
                ordinal += 1
                row = {
                    "executionOrdinal": ordinal,
                    "conversationId": conversation["conversationId"],
                    "turnId": turn["turnId"],
                    "semanticTurn": turn["semanticTurn"],
                    "arm": arm,
                    "rawUserText": turn["rawUserText"],
                    "inputMessageSha256": turn["messageSha256"],
                    "historyMode": (
                        "raw_full_same_arm"
                        if arm == ARMS[0]
                        else "compiled_context_no_raw_history"
                    ),
                    "turnProvenance": turn["turnProvenance"],
                    "expectedModelRoute": turn.get("expectedModelRoute"),
                    "automaticRetries": 0,
                }
                row["scheduleRowSha256"] = sha_text(canonical(row))
                rows.append(row)
    return rows


def _semantic_state(state: Any) -> dict[str, Any]:
    """Project state to user-visible semantics, excluding identity and clocks."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            omitted = {
                "taskId", "sessionId", "runId", "capabilityId", "revision",
                "createdAt", "updatedAt", "issuedAt", "expiresAt", "observedAt",
                "bindingSha256", "contextBindingHash", "requestFingerprint",
            }
            return {
                key: clean(item)
                for key, item in sorted(value.items())
                if key not in omitted
            }
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value

    facts = [
        {"key": item.key, "value": item.value, "certainty": item.certainty, "source": item.source}
        for item in state.facts
    ]
    constraints = [
        {"key": item.key, "operator": item.operator, "value": item.value, "source": item.source}
        for item in state.constraints
    ]
    domain = state.domain_state if isinstance(state.domain_state, dict) else {}
    return clean({
        "status": state.status,
        "goal": state.goal,
        "facts": facts,
        "constraints": constraints,
        "unknowns": state.unknowns,
        "pendingQuestions": state.pending_questions,
        "shoppingGuide": domain.get("shoppingGuide"),
    })


def _static_model_route_check(conversations: list[dict[str, Any]]) -> dict[str, Any]:
    from datetime import datetime as dt
    from agent.app.llm import _deterministic_used_phone_task_state_decision
    from agent.app.task_state import TaskFact, TaskState

    now = dt.now(timezone.utc)
    state = TaskState(
        taskId="preflight-task",
        taskType="ecommerce_guide",
        sessionId="preflight-session",
        status="ready",
        revision=3,
        goal="手机",
        facts=[TaskFact(key="category", value="phone", certainty="confirmed", source="user")],
        constraints=[],
        unknowns=[],
        pendingQuestions=[],
        domainState={"shoppingGuide": {
            "mode": "recommend", "category": "phone", "requirements": [],
            "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
        }},
        createdAt=now,
        updatedAt=now,
    )
    rows: list[dict[str, Any]] = []
    for conversation in conversations:
        turn = conversation["turns"][-1]
        if turn["turnProvenance"] != "AI_CURATED_CONTEXT_STRESS":
            raise RuntimeError(f"{conversation['conversationId']}: final stress turn missing")
        decision, observation = _deterministic_used_phone_task_state_decision(
            state, turn["rawUserText"]
        )
        rows.append({
            "conversationId": conversation["conversationId"],
            "turnId": turn["turnId"],
            "modelFallback": decision is None,
            "deterministicRoute": (observation or {}).get("route"),
        })
    return {
        "checked": len(rows),
        "allModelFallback": all(item["modelFallback"] for item in rows),
        "rows": rows,
    }


def preflight(output: Path | None = None) -> dict[str, Any]:
    from agent.app.settings import settings
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2.lane_runtime import (
        CATALOG_PATH,
        EXPECTED_CATALOG_SHA256,
        EXPECTED_PRODUCTS,
        _read_jsonl,
    )

    conversations = load_jsonl(DATASET)
    schedule = build_schedule(conversations)
    route_check = _static_model_route_check(conversations)
    old_snapshot = json.loads(
        (ROOT / "agent/evaluation/real_user_multiturn_replay_20260902_v4/execution_config_snapshot.json")
        .read_text(encoding="utf-8")
    )
    old_adapter_model = old_snapshot["components"]["modelConfiguration"]["model"]
    checks = {
        "outputAbsent": output is None or not output.exists(),
        "conversationCountIs8": len(conversations) == 8,
        "userTurnCountIs29": sum(len(item["turns"]) for item in conversations) == 29,
        "armTurnCountIs58": len(schedule) == 58,
        "derivedStressCountIs8": sum(
            turn["turnProvenance"] == "AI_CURATED_CONTEXT_STRESS"
            for item in conversations for turn in item["turns"]
        ) == 8,
        "allDerivedRequireModelFallback": route_check["allModelFallback"],
        "catalogHashMatches": sha_file(CATALOG_PATH) == EXPECTED_CATALOG_SHA256,
        "catalogCountMatches": len(_read_jsonl(CATALOG_PATH)) == EXPECTED_PRODUCTS,
        "deepseekKeyConfigured": bool(settings.deepseek_api_key),
        "currentModelMatchesAdapterBinding": settings.deepseek_model == old_adapter_model,
        "reactV1Enabled": settings.agent_control_runtime == "react_v1" and settings.agent_react_live_enabled,
        "durableEnabled": settings.agent_graph_v2_durable_enabled,
        "contextModeIsContextPack": settings.agent_context_mode == "context_pack",
    }
    return {
        "schemaVersion": "context-program-p4-preflight-v1",
        "status": "PASS" if all(checks.values()) else "HOLD",
        "checkedAt": utc_now(),
        "checks": checks,
        "model": settings.deepseek_model,
        "datasetSha256": sha_file(DATASET),
        "sourceFreezeSha256": sha_file(SOURCE_FREEZE),
        "routeCheck": route_check,
    }


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def _call_usage(calls: list[dict[str, Any]]) -> dict[str, int]:
    return {
        key: sum(int(call.get("usage", {}).get(key, 0)) for call in calls)
        for key in ("promptTokens", "completionTokens", "totalTokens")
    }


def execute(output: Path, run_set_id: str) -> dict[str, Any]:
    if output.exists():
        raise RuntimeError(f"refusing to overwrite {output}")
    gate = preflight(output)
    if gate["status"] != "PASS":
        raise RuntimeError(f"P4 preflight HOLD: {gate['checks']}")

    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v1.runner import LaneBinding
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2.lane_runtime import IsolatedLaneRuntime
    from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2.runner import (
        PersistentLoopReactV1TurnAdapter,
    )

    class RecordingAdapter(PersistentLoopReactV1TurnAdapter):
        """Record fail-closed terminals instead of throwing away their evidence."""

        async def _execute_turn(self, row, lane, prior_raw_same_arm_dialogue):  # noqa: ANN001
            from agent.app.evaluation_context_arm import issue_evaluation_context_arm
            from agent.app.settings import settings

            state = await self._runtime.load_task_state(row, lane)
            if state.task_id != lane.task_id or state.session_id != lane.session_id:
                raise RuntimeError("lane TaskState identity mismatch before execution")
            if state.revision != lane.pre_state_revision:
                raise RuntimeError("lane TaskState revision mismatch before execution")
            capability = issue_evaluation_context_arm(
                arm=row["arm"],
                run_id=lane.run_id,
                task_id=lane.task_id,
                session_id=lane.session_id,
                model=settings.deepseek_model,
                model_client=self._runtime.model_client(row, lane),
                tool_transport=self._runtime.tool_transport(row, lane),
                provider_max_retries=0,
            )
            final_state = state

            async def capture(current, _phase):  # noqa: ANN001
                nonlocal final_state
                final_state = current
                await self._runtime.persist_task_state(row, lane, current)

            history = prior_raw_same_arm_dialogue if row["arm"] == ARMS[0] else None
            started_at = time.perf_counter()
            answer, _tool_traces, _turn_messages, observed_run_id, summary = await self._run_agent(
                row["rawUserText"],
                history=history,
                task_state=state,
                on_task_state=capture,
                domain_hint="ecommerce",
                session_id=lane.session_id,
                evaluation_context_arm=capability,
            )
            duration = (time.perf_counter() - started_at) * 1000.0
            calls = capability.ledger.snapshot()
            reference_hash, scope_hash = self._runtime.state_binding_hashes(final_state)
            identity_matched = observed_run_id == lane.run_id
            summary_valid = summary is not None and summary.control_policy == "react_v1"
            summary_failed = summary is None or summary.agent_status != "ok"
            safe_stop_like = any(marker in answer for marker in (
                "无法继续执行", "状态构建失败", "安全停止", "超过了总执行时间限制",
            ))
            failed = not identity_matched or not summary_valid or summary_failed or safe_stop_like
            if not identity_matched:
                failure_code = "evaluation_run_identity_mismatch"
            elif safe_stop_like:
                failure_code = "safe_stop_like_terminal"
            elif summary is None:
                failure_code = "missing_trace_summary"
            elif summary.control_policy != "react_v1":
                failure_code = "unexpected_control_policy"
            elif summary.agent_status != "ok":
                failure_code = summary.failure_code or "react_v1_failed"
            else:
                failure_code = None
            return {
                "runId": lane.run_id,
                "observedRunId": observed_run_id,
                "runIdentityMatched": identity_matched,
                "taskId": lane.task_id,
                "sessionId": lane.session_id,
                "preStateRevision": lane.pre_state_revision,
                "postStateRevision": final_state.revision,
                "historyMode": row["historyMode"],
                "rawPriorMessageCount": len(history or []),
                "compiledContextUsed": row["arm"] == ARMS[1],
                "contextBindingHash": capability.context_binding_hash,
                "referenceContextBindingHash": reference_hash,
                "candidateScopeHash": scope_hash,
                "toolCalls": calls["toolCalls"],
                "modelCalls": calls["modelCalls"],
                "durationMs": duration,
                "status": "FAILED" if failed else "SUCCEEDED",
                "finalAnswer": answer,
                "safeStopLikeAnswer": safe_stop_like,
                "failureCode": failure_code,
            }

    conversations = load_jsonl(DATASET)
    schedule = build_schedule(conversations)
    by_conversation: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in schedule:
        by_conversation[row["conversationId"]].append(row)
    conversation_map = {item["conversationId"]: item for item in conversations}

    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "preflight.json", gate)
    dependencies = {
        "laneRuntime": ROOT / "agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/lane_runtime.py",
        "persistentAdapter": ROOT / "agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/runner.py",
        "baseAdapter": ROOT / "agent/evaluation/real_user_multiturn_ab_executor_20260902_v1/runner.py",
    }
    started = {
        "schemaVersion": "context-program-p4-started-v1",
        "runSetId": run_set_id,
        "startedAt": utc_now(),
        "automaticRetries": 0,
        "datasetSha256": sha_file(DATASET),
        "sourceFreezeSha256": sha_file(SOURCE_FREEZE),
        "runnerSha256": sha_file(Path(__file__)),
        "dependencySha256": {name: sha_file(path) for name, path in dependencies.items()},
        "armPolicy": {
            ARMS[0]: "complete same-arm raw dialogue",
            ARMS[1]: "compiled Context with no raw history",
        },
        "semanticStateProjection": "status_goal_facts_constraints_unknowns_questions_shoppingGuide_without_identity_clocks",
    }
    write_json(output / "started.json", started)

    runtime = IsolatedLaneRuntime(namespace=run_set_id)
    adapter = RecordingAdapter(runtime)
    output_path = output / "paired_outputs.jsonl"
    private_path = output / "private_turn_receipts.jsonl"
    rows_out: list[dict[str, Any]] = []
    try:
        for conversation_id, rows in by_conversation.items():
            conversation = conversation_map[conversation_id]
            lanes = adapter.begin_conversation(conversation, ARMS)
            if set(lanes) != set(ARMS):
                raise RuntimeError("lane runtime did not create both arms")
            if len({lanes[arm].branch_point_state_hash for arm in ARMS}) != 1:
                raise RuntimeError("arms do not share branch point")
            dialogue: dict[str, list[dict[str, str]]] = {arm: [] for arm in ARMS}
            for row in rows:
                arm = row["arm"]
                base = lanes[arm]
                lane = LaneBinding(
                    runtime.turn_run_id(row, base), base.task_id, base.session_id,
                    base.branch_point_state_hash, base.pre_state_revision,
                )
                prior = [dict(item) for item in dialogue[arm]] if arm == ARMS[0] else None
                result = adapter.execute_turn(row, lane, prior)
                answer = result["finalAnswer"]
                dialogue[arm].extend([
                    {"role": "user", "content": row["rawUserText"]},
                    {"role": "assistant", "content": answer},
                ])
                post_state = adapter._loop.run_until_complete(runtime.load_task_state(row, lane))
                semantic_state = _semantic_state(post_state)
                calls = result.get("modelCalls", [])
                usage = _call_usage(calls)
                recorded = {
                    "schemaVersion": "context-program-p4-arm-turn-v1",
                    "executionOrdinal": row["executionOrdinal"],
                    "scheduleRowSha256": row["scheduleRowSha256"],
                    "conversationId": conversation_id,
                    "turnId": row["turnId"],
                    "semanticTurn": row["semanticTurn"],
                    "turnProvenance": row["turnProvenance"],
                    "expectedModelRoute": row["expectedModelRoute"],
                    "arm": arm,
                    "runId": lane.run_id,
                    "observedRunId": result["observedRunId"],
                    "runIdentityMatched": result["runIdentityMatched"],
                    "taskId": lane.task_id,
                    "sessionId": lane.session_id,
                    "branchPointStateHash": lane.branch_point_state_hash,
                    "historyMode": result["historyMode"],
                    "rawPriorMessageCount": result["rawPriorMessageCount"],
                    "compiledContextUsed": result["compiledContextUsed"],
                    "preStateRevision": result["preStateRevision"],
                    "postStateRevision": result["postStateRevision"],
                    "status": result["status"],
                    "failureCode": result["failureCode"],
                    "safeStopLikeAnswer": result["safeStopLikeAnswer"],
                    "durationMs": result["durationMs"],
                    "contextBindingHash": result["contextBindingHash"],
                    "referenceContextBindingHash": result["referenceContextBindingHash"],
                    "candidateScopeHash": result["candidateScopeHash"],
                    "modelCalls": calls,
                    "toolCalls": result.get("toolCalls", []),
                    **usage,
                    "semanticStateSha256": sha_text(canonical(semantic_state)),
                    "finalAnswer": answer,
                    "finalAnswerSha256": sha_text(answer),
                    "dialogue": [dict(item) for item in dialogue[arm]],
                    "dialogueSha256": sha_text(canonical(dialogue[arm])),
                    "automaticRetryCount": 0,
                }
                append_jsonl(output_path, recorded)
                append_jsonl(private_path, {
                    "executionOrdinal": row["executionOrdinal"],
                    "semanticState": semantic_state,
                    "recordSha256": sha_text(canonical(recorded)),
                })
                rows_out.append(recorded)
                lanes[arm] = LaneBinding(
                    lane.run_id, lane.task_id, lane.session_id,
                    lane.branch_point_state_hash, int(result["postStateRevision"]),
                )
                print(
                    f"P4 {len(rows_out)}/{len(schedule)} {conversation_id} "
                    f"t{row['semanticTurn']} {arm} status={result['status']} "
                    f"model={len(calls)} tools={len(result.get('toolCalls', []))}",
                    flush=True,
                )
    finally:
        adapter._loop.close()

    if len(rows_out) != len(schedule):
        raise RuntimeError(f"incomplete P4 attempt: {len(rows_out)}/{len(schedule)}")

    by_pair: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows_out:
        by_pair[(row["conversationId"], row["turnId"])][row["arm"]] = row
    if len(by_pair) != 29 or any(set(pair) != set(ARMS) for pair in by_pair.values()):
        raise RuntimeError("P4 pair binding incomplete")

    pair_results: list[dict[str, Any]] = []
    for (conversation_id, turn_id), pair in sorted(by_pair.items()):
        control, treatment = pair[ARMS[0]], pair[ARMS[1]]
        tool_control = [(item["toolName"], item["resultSha256"]) for item in control["toolCalls"]]
        tool_treatment = [(item["toolName"], item["resultSha256"]) for item in treatment["toolCalls"]]
        pair_results.append({
            "conversationId": conversation_id,
            "turnId": turn_id,
            "turnProvenance": control["turnProvenance"],
            "bothSucceeded": control["status"] == treatment["status"] == "SUCCEEDED",
            "semanticStateExact": control["semanticStateSha256"] == treatment["semanticStateSha256"],
            "toolResultSequenceExact": tool_control == tool_treatment,
            "finalAnswerExact": control["finalAnswerSha256"] == treatment["finalAnswerSha256"],
            "modelCallsBoth": bool(control["modelCalls"]) and bool(treatment["modelCalls"]),
        })

    metrics: dict[str, Any] = {}
    for arm in ARMS:
        selected = [row for row in rows_out if row["arm"] == arm]
        latencies = [float(row["durationMs"]) for row in selected]
        metrics[arm] = {
            "armTurns": len(selected),
            "succeeded": sum(row["status"] == "SUCCEEDED" for row in selected),
            "modelCalls": sum(len(row["modelCalls"]) for row in selected),
            "toolCalls": sum(len(row["toolCalls"]) for row in selected),
            "promptTokens": sum(row["promptTokens"] for row in selected),
            "completionTokens": sum(row["completionTokens"] for row in selected),
            "totalTokens": sum(row["totalTokens"] for row in selected),
            "latencyMeanMs": statistics.mean(latencies),
            "latencyP50Ms": percentile(latencies, 0.50),
            "latencyP95Ms": percentile(latencies, 0.95),
        }

    stress = [item for item in pair_results if item["turnProvenance"] == "AI_CURATED_CONTEXT_STRESS"]
    gates = {
        "all58ArmTurnsSucceeded": all(item["bothSucceeded"] for item in pair_results),
        "all29PostStateSemanticsExact": all(item["semanticStateExact"] for item in pair_results),
        "all29ToolResultSequencesExact": all(item["toolResultSequenceExact"] for item in pair_results),
        "all8StressTurnsCalledModelInBothArms": len(stress) == 8 and all(item["modelCallsBoth"] for item in stress),
        "historyPolicyEnforced": all(
            (row["arm"] == ARMS[0] and not row["compiledContextUsed"])
            or (
                row["arm"] == ARMS[1]
                and row["compiledContextUsed"]
                and row["rawPriorMessageCount"] == 0
            )
            for row in rows_out
        ),
        "zeroAutomaticRetries": all(row["automaticRetryCount"] == 0 for row in rows_out),
        "all58RunIdentitiesMatched": all(row["runIdentityMatched"] for row in rows_out),
        "noSafeStopLikeAnswers": not any(row["safeStopLikeAnswer"] for row in rows_out),
    }
    result = {
        "schemaVersion": "context-program-p4-result-v1",
        "programId": "context_program_v1_20260904",
        "status": "COMPLETE",
        "completedAt": utc_now(),
        "strictDecision": (
            "BOUNDED_MODEL_IN_LOOP_STATE_AND_TOOL_FIDELITY_ACCEPT"
            if all(gates.values())
            else "HOLD_MODEL_IN_LOOP_FIDELITY"
        ),
        "execution": {
            "conversationCount": 8,
            "userTurnCount": 29,
            "armTurnCount": 58,
            "stressTurnCount": 8,
        },
        "gates": gates,
        "pairEquality": {
            "semanticStateExactPairs": sum(item["semanticStateExact"] for item in pair_results),
            "toolResultSequenceExactPairs": sum(item["toolResultSequenceExact"] for item in pair_results),
            "finalAnswerExactPairs": sum(item["finalAnswerExact"] for item in pair_results),
            "pairCount": len(pair_results),
        },
        "metrics": metrics,
        "pairResults": pair_results,
        "claimBoundary": {
            "dataset": "8 real-user prefixes plus 8 AI-curated stress follow-ups; not an independent human holdout",
            "semanticStateAndToolFidelity": "eligible for bounded automated gate",
            "finalAnswerSemanticEquivalence": "not established unless byte-exact; no independent cross-model judge is configured",
            "productionDefaultMayChange": False,
        },
    }
    write_json(output / "result.json", result)
    receipt = {
        "schemaVersion": "context-program-p4-receipt-v1",
        "ownership": "RUNNER_OWNED",
        "runSetId": run_set_id,
        "pairedOutputRowCount": len(rows_out),
        "pairedOutputsSha256": sha_file(output_path),
        "privateTurnReceiptsSha256": sha_file(private_path),
        "resultSha256": sha_file(output / "result.json"),
        "observedModelCallCount": sum(len(row["modelCalls"]) for row in rows_out),
        "observedToolCallCount": sum(len(row["toolCalls"]) for row in rows_out),
        "automaticRetries": 0,
    }
    receipt["receiptBindingSha256"] = sha_text(canonical(receipt))
    write_json(output / "receipt.json", receipt)
    witnesses = (
        output / "preflight.json", output / "started.json", output_path,
        private_path, output / "result.json", output / "receipt.json",
    )
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha_file(path)}  {path.name}\n" for path in witnesses),
        encoding="utf-8",
        newline="\n",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--preflight", action="store_true")
    modes.add_argument("--execute", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-set-id", default="ctxprog-p4-attempt001")
    args = parser.parse_args()
    if args.preflight:
        print(json.dumps(preflight(args.output), ensure_ascii=False, indent=2))
        return 0
    if args.output is None:
        parser.error("--execute requires --output")
    result = execute(args.output.resolve(), args.run_set_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
