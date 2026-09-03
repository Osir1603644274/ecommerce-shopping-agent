"""Independent V4 A/B executor.

The CLI never supplies a production adapter in this version because the current
production chat path cannot make compiled Context the treatment arm's sole
history input.  Tests may inject a fixture adapter to exercise the writer and
binding logic without an Agent or model call.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = Path(__file__).resolve().parent
DEFAULT_PACKAGE = ROOT / "agent/evaluation/real_user_multiturn_replay_20260902_v4"
ARMS = ("RAW_FULL_CONTROL", "CONTEXT_TREATMENT")
PRODUCER = "formal_real_user_multiturn_replay_runner"


class ExecutionHold(RuntimeError):
    """Formal execution is unavailable or an execution binding failed closed."""


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ExecutionHold(f"expected JSON object: {path.name}")
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ExecutionHold(f"expected JSON object: {path.name}:{number}")
        rows.append(value)
    return rows


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ExecutionHold(f"cannot load {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def production_wiring_audit() -> dict[str, Any]:
    """Frozen source-location audit; it performs no imports and no calls."""

    sources = {
        "chatEndpoint": ROOT / "agent/app/main.py",
        "agentRuntime": ROOT / "agent/app/llm.py",
        "modelReceipts": ROOT / "agent/app/model_call_observability.py",
        "agentTrace": ROOT / "agent/app/agent_trace.py",
    }
    return {
        "status": "IN_PROGRESS_INTERNAL_ARM_WIRED_FORMAL_RUNTIME_HOLD",
        "formalExecutionAllowed": False,
        "sourceFiles": {
            name: {
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": sha_file(path),
            }
            for name, path in sources.items()
        },
        "findings": [
            {
                "code": "DEFAULT_WEB_HISTORY_UNCHANGED",
                "path": "agent/app/main.py",
                "lines": [2100, 2107, 2177, 2179, 2511, 2518, 2593, 2595],
                "detail": "ordinary chat and durable paths remain unchanged and never receive an evaluation capability",
            },
            {
                "code": "INTERNAL_CAPABILITY_DEFAULT_OFF",
                "path": "agent/app/evaluation_context_arm.py",
                "lines": [1, 20],
                "detail": "only an in-process opaque object can select an evaluation arm; there is no HTTP or environment switch",
            },
            {
                "code": "FORMAL_LANE_RUNTIME_NOT_BUNDLED",
                "path": "agent/evaluation/real_user_multiturn_ab_executor_20260902_v1/runner.py",
                "lines": [286, 390],
                "detail": "ReactV1TurnAdapter exists but requires an isolated controlled-world lane runtime",
            },
            {
                "code": "FORMAL_EXECUTION_NOT_VALIDATED",
                "path": "agent/evaluation/real_user_multiturn_ab_executor_20260902_v1/runner.py",
                "lines": [570, 590],
                "detail": "no model, Agent or formal A/B attempt has been run",
            },
        ],
        "minimumWiring": [
            "evaluation-only arm policy at SessionMemory/history load boundary",
            "RAW_FULL_CONTROL forwards complete same-arm transcript; CONTEXT_TREATMENT forwards zero raw prior messages",
            "authoritative ContextCompiler model_view injection before every provider phase, not shadow observation",
            "server-owned branch clone with isolated stable runId/taskId/sessionId and continuous revisions",
            "provider and tool boundary receipts with unique callId, requestSha256, resultSha256, usage and durationMs",
            "default-off setting; production default remains unchanged",
        ],
    }


def package_preflight(package: Path = DEFAULT_PACKAGE) -> dict[str, Any]:
    """Validate the frozen execution inputs without running the SUT."""

    failures: list[str] = []
    required = (
        "execution_authority.json", "execution_config_snapshot.json",
        "execution_receipt.schema.json", "paired_output.schema.json",
        "replay_contract.json", "package_manifest.json", "conversations.jsonl",
        "build_replay_schedule.py", "blind_packet_builder.py", "SHA256SUMS.txt",
    )
    for name in required:
        if not (package / name).is_file():
            failures.append(f"missing:{name}")
    if failures:
        return {"status": "HOLD", "failures": failures}

    authority = load_json(package / "execution_authority.json")
    snapshot = load_json(package / "execution_config_snapshot.json")
    manifest = load_json(package / "package_manifest.json")
    replay = load_json(package / "replay_contract.json")
    output_schema = load_json(package / "paired_output.schema.json")
    receipt_schema = load_json(package / "execution_receipt.schema.json")
    package_id = authority.get("packageId")
    if authority.get("status") != "PREREGISTERED_BEFORE_FORMAL_EXECUTION":
        failures.append("execution_authority_not_preregistered")
    for label, value in (
        ("snapshot", snapshot.get("packageId")),
        ("manifest", manifest.get("packageId")),
    ):
        if value != package_id:
            failures.append(f"{label}_package_id_mismatch")
    if output_schema.get("properties", {}).get("executionBinding", {}).get("properties", {}).get("packageId", {}).get("const") != package_id:
        failures.append("paired_schema_package_id_mismatch")
    if receipt_schema.get("properties", {}).get("packageId", {}).get("const") != package_id:
        failures.append("receipt_schema_package_id_mismatch")
    snapshot_hash = sha_file(package / "execution_config_snapshot.json")
    if authority.get("configSnapshot", {}).get("sha256") != snapshot_hash:
        failures.append("execution_snapshot_hash_mismatch")

    field_map = {
        "controlledWorldHash": "controlledWorld",
        "modelConfigurationHash": "modelConfiguration",
        "toolConfigurationHash": "toolConfiguration",
        "budgetConfigurationHash": "budgetConfiguration",
        "policyConfigurationHash": "policyConfiguration",
    }
    expected = authority.get("expectedTraceHashes", {})
    for field, component_name in field_map.items():
        component = snapshot.get("components", {}).get(component_name)
        if not isinstance(component, dict) or expected.get(field) != sha_text(canonical(component)):
            failures.append(f"{field}_authority_mismatch")
            continue
        for source in component.get("sourceFiles", []):
            path = ROOT / str(source.get("path", ""))
            if not path.is_file() or sha_file(path) != source.get("sha256"):
                failures.append(f"frozen_source_drift:{source.get('path')}")

    if snapshot.get("components", {}).get("budgetConfiguration", {}).get("automaticRetries") != 0:
        failures.append("automatic_retries_not_zero")
    if replay.get("pairing", {}).get("automaticRetries") != 0:
        failures.append("replay_retries_not_zero")

    conversations = load_jsonl(package / "conversations.jsonl")
    builder = _load_module(package / "build_replay_schedule.py", "rumr_executor_schedule")
    schedule = builder.build_schedule(conversations, 20260902)
    if len(schedule) != 42 or any(row.get("automaticRetries") != 0 for row in schedule):
        failures.append("frozen_schedule_not_42_zero_retry_rows")

    checksum_rows: dict[str, str] = {}
    for line in (package / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator:
            failures.append("invalid_checksum_inventory")
            continue
        checksum_rows[name] = digest
    regular = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.name != "SHA256SUMS.txt"
    }
    if set(checksum_rows) != regular:
        failures.append("checksum_inventory_coverage_mismatch")
    for name, digest in checksum_rows.items():
        path = package / name
        if not path.is_file() or sha_file(path) != digest:
            failures.append(f"checksum_mismatch:{name}")

    wiring = production_wiring_audit()
    failures.append("formal_lane_runtime_not_bundled_or_validated")
    return {
        "status": "PASS" if not failures else "HOLD",
        "packageId": package_id,
        "scheduledArmTurns": len(schedule),
        "expectedTraceHashes": expected,
        "failures": sorted(set(failures)),
        "productionWiring": wiring,
        "modelCalls": 0,
        "agentRuns": 0,
        "formalAttempts": 0,
    }


def dry_run(package: Path = DEFAULT_PACKAGE) -> dict[str, Any]:
    conversations = load_jsonl(package / "conversations.jsonl")
    builder = _load_module(package / "build_replay_schedule.py", "rumr_executor_dry_schedule")
    rows = builder.build_schedule(conversations, 20260902)
    return {
        "status": "DRY_RUN_NO_MODEL_NO_AGENT",
        "rowCount": len(rows),
        "conversationCount": len(conversations),
        "scoredArmTurnCount": sum(bool(row["scoredFollowup"]) for row in rows),
        "zeroRetries": all(row["automaticRetries"] == 0 for row in rows),
        "scheduleSha256": sha_text(canonical(rows)),
        "formalExecutionAllowed": False,
    }


@dataclass(frozen=True)
class LaneBinding:
    run_id: str
    task_id: str
    session_id: str
    branch_point_state_hash: str
    pre_state_revision: int


class TurnAdapter(Protocol):
    fixture_only: bool

    def begin_conversation(self, conversation: dict[str, Any], arms: tuple[str, str]) -> dict[str, LaneBinding]: ...

    def execute_turn(
        self,
        row: dict[str, Any],
        lane: LaneBinding,
        prior_raw_same_arm_dialogue: list[dict[str, str]] | None,
    ) -> dict[str, Any]: ...


class ReactV1TurnAdapter:
    """Formal adapter over the real ``run_agent`` entry; no runtime is bundled.

    ``lane_runtime`` owns isolated TaskState/session stores and the frozen
    offline catalog.  This executor never falls back to the web endpoint.
    """

    fixture_only = False

    def __init__(self, lane_runtime: Any, run_agent_callable: Any | None = None) -> None:
        self._runtime = lane_runtime
        if run_agent_callable is None:
            from agent.app.llm import run_agent as current_run_agent
            run_agent_callable = current_run_agent
        self._run_agent = run_agent_callable

    def begin_conversation(
        self, conversation: dict[str, Any], arms: tuple[str, str],
    ) -> dict[str, LaneBinding]:
        lanes = self._runtime.begin_conversation(conversation, arms)
        if not isinstance(lanes, dict):
            raise ExecutionHold("lane runtime did not return server bindings")
        return lanes

    def execute_turn(
        self, row: dict[str, Any], lane: LaneBinding,
        prior_raw_same_arm_dialogue: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        return asyncio.run(self._execute_turn(row, lane, prior_raw_same_arm_dialogue))

    async def _execute_turn(
        self, row: dict[str, Any], lane: LaneBinding,
        prior_raw_same_arm_dialogue: list[dict[str, str]] | None,
    ) -> dict[str, Any]:
        from agent.app.evaluation_context_arm import issue_evaluation_context_arm
        from agent.app.settings import settings

        if settings.agent_control_runtime != "react_v1":
            raise ExecutionHold("formal evaluator requires current react_v1")
        if not settings.agent_react_live_enabled or not settings.agent_graph_v2_durable_enabled:
            raise ExecutionHold("react_v1 execution or durable safety gate is disabled")
        state = await self._runtime.load_task_state(row, lane)
        if state.task_id != lane.task_id or state.session_id != lane.session_id:
            raise ExecutionHold("lane TaskState identity mismatch")
        if state.revision != lane.pre_state_revision:
            raise ExecutionHold("lane TaskState revision mismatch")
        snapshot = load_json(DEFAULT_PACKAGE / "execution_config_snapshot.json")
        model = snapshot["components"]["modelConfiguration"]["model"]
        capability = issue_evaluation_context_arm(
            arm=row["arm"], run_id=lane.run_id, task_id=lane.task_id,
            session_id=lane.session_id, model=model,
            model_client=self._runtime.model_client(row, lane),
            tool_transport=self._runtime.tool_transport(row, lane),
            provider_max_retries=0,
        )
        final_state = state

        async def capture(current: Any, _phase: str) -> None:
            nonlocal final_state
            final_state = current
            await self._runtime.persist_task_state(row, lane, current)

        history = prior_raw_same_arm_dialogue if row["arm"] == ARMS[0] else None
        started = time.perf_counter()
        answer, _tool_traces, _turn_messages, run_id, summary = await self._run_agent(
            row["rawUserText"], history=history, task_state=state,
            on_task_state=capture, domain_hint="ecommerce", session_id=lane.session_id,
            evaluation_context_arm=capability,
        )
        duration = (time.perf_counter() - started) * 1000.0
        if run_id != lane.run_id:
            raise ExecutionHold("react_v1 returned a different lane runId")
        if summary is None or summary.control_policy != "react_v1":
            raise ExecutionHold("turn did not execute current react_v1")
        calls = capability.ledger.snapshot()
        if capability.context_binding_hash is None:
            raise ExecutionHold("react_v1 emitted no authoritative context binding")
        reference_hash, scope_hash = self._runtime.state_binding_hashes(final_state)
        failed = summary.agent_status != "ok"
        return {
            "runId": lane.run_id, "taskId": lane.task_id, "sessionId": lane.session_id,
            "preStateRevision": lane.pre_state_revision,
            "postStateRevision": final_state.revision,
            "historyMode": row["historyMode"],
            "rawPriorMessageCount": len(history or []),
            "compiledContextUsed": row["arm"] == ARMS[1],
            "contextBindingHash": capability.context_binding_hash,
            "referenceContextBindingHash": reference_hash,
            "candidateScopeHash": scope_hash,
            "toolCalls": calls["toolCalls"], "modelCalls": calls["modelCalls"],
            "durationMs": duration, "status": "FAILED" if failed else "SUCCEEDED",
            "finalAnswer": answer, "publicEvidence": [],
            "failureCode": (summary.failure_code or "react_v1_failed") if failed else None,
        }


def _validate_call(call: dict[str, Any], expected_type: str) -> None:
    required = ({"callId", "callType", "status", "durationMs", "requestSha256", "resultSha256"}
                | ({"toolName"} if expected_type == "tool" else {"model", "usage"}))
    if set(call) != required or call.get("callType") != expected_type:
        raise ExecutionHold(f"invalid {expected_type} call receipt")
    for field in ("requestSha256", "resultSha256"):
        if not isinstance(call[field], str) or len(call[field]) != 64:
            raise ExecutionHold(f"invalid {expected_type} {field}")


def _build_output(
    *, package_id: str, schema_version: str, row: dict[str, Any], lane: LaneBinding,
    result: dict[str, Any], dialogue: list[dict[str, str]], expected_hashes: dict[str, str],
) -> dict[str, Any]:
    if result.get("runId") != lane.run_id or result.get("taskId") != lane.task_id or result.get("sessionId") != lane.session_id:
        raise ExecutionHold("adapter identity differs from server lane binding")
    if result.get("preStateRevision") != lane.pre_state_revision:
        raise ExecutionHold("adapter preStateRevision is discontinuous")
    expected_history = "raw_full_same_arm" if row["arm"] == ARMS[0] else "compiled_context_no_raw_history"
    if result.get("historyMode") != expected_history:
        raise ExecutionHold("adapter did not apply the frozen arm history mode")
    if row["arm"] == ARMS[1] and (result.get("rawPriorMessageCount") != 0 or result.get("compiledContextUsed") is not True):
        raise ExecutionHold("treatment was exposed to raw history or did not use compiled Context")
    if row["arm"] == ARMS[0] and result.get("compiledContextUsed") is not False:
        raise ExecutionHold("control unexpectedly used compiled Context")
    for call in result.get("toolCalls", []):
        _validate_call(call, "tool")
    for call in result.get("modelCalls", []):
        _validate_call(call, "model")
    status = result.get("status")
    if status not in {"SUCCEEDED", "FAILED"}:
        raise ExecutionHold("adapter status invalid")
    answer = result.get("finalAnswer")
    if not isinstance(answer, str) or (status == "SUCCEEDED" and not answer):
        raise ExecutionHold("adapter final answer invalid")
    failure = result.get("failureCode")
    if (status == "SUCCEEDED" and failure is not None) or (status == "FAILED" and not isinstance(failure, str)):
        raise ExecutionHold("adapter failureCode invalid")

    arm_payload = {
        "packageId": package_id, "conversationId": row["conversationId"], "arm": row["arm"],
        "runId": lane.run_id, "taskId": lane.task_id, "sessionId": lane.session_id,
        "branchPointStateHash": lane.branch_point_state_hash,
    }
    binding = {
        "packageId": package_id, "executionOrdinal": row["executionOrdinal"],
        "scheduleRowSha256": row["scheduleRowSha256"], "runId": lane.run_id,
        "taskId": lane.task_id, "sessionId": lane.session_id,
        "branchPointStateHash": lane.branch_point_state_hash,
        "armStateBindingSha256": sha_text(canonical(arm_payload)),
        "inputMessageSha256": row["inputMessageSha256"],
    }
    model_calls = result.get("modelCalls", [])
    usage = {key: sum(int(call["usage"][key]) for call in model_calls) for key in ("promptTokens", "completionTokens", "totalTokens")}
    trace = {
        "preStateRevision": result["preStateRevision"], "postStateRevision": result["postStateRevision"],
        **expected_hashes,
        "contextBindingHash": result["contextBindingHash"],
        "referenceContextBindingHash": result["referenceContextBindingHash"],
        "candidateScopeHash": result["candidateScopeHash"],
        "toolCalls": result.get("toolCalls", []), "modelCalls": model_calls,
        **usage, "durationMs": result["durationMs"], "automaticRetryCount": 0,
    }
    execution_binding_hash = sha_text(canonical(binding))
    trace_hash = sha_text(canonical(trace))
    trace_binding = {
        "schemaVersion": "real-user-multiturn-trace-binding-v4", "packageId": package_id,
        "conversationId": row["conversationId"], "turnId": row["turnId"],
        "semanticTurn": row["semanticTurn"], "arm": row["arm"],
        "executionOrdinal": row["executionOrdinal"], "runId": lane.run_id,
        "taskId": lane.task_id, "sessionId": lane.session_id,
        "executionBindingSha256": execution_binding_hash, "traceSha256": trace_hash, "status": status,
    }
    return {
        "schemaVersion": schema_version, "conversationId": row["conversationId"],
        "turnId": row["turnId"], "semanticTurn": row["semanticTurn"], "arm": row["arm"],
        "executionBinding": binding, "executionBindingSha256": execution_binding_hash,
        "trace": trace, "traceSha256": trace_hash,
        "traceBindingSha256": sha_text(canonical(trace_binding)), "status": status,
        "dialogue": dialogue, "dialogueSha256": sha_text(canonical(dialogue)),
        "finalAnswer": answer, "finalAnswerSha256": sha_text(answer),
        "publicEvidence": result.get("publicEvidence", []), "failureCode": failure,
    }


def run_with_adapter(
    adapter: TurnAdapter, output_dir: Path, *, package: Path = DEFAULT_PACKAGE,
    run_set_id: str = "fixture-run", allow_fixture: bool = False,
) -> dict[str, Any]:
    """Run 42 rows with an injected adapter; the CLI never calls this in V1."""

    if getattr(adapter, "fixture_only", False) and not allow_fixture:
        raise ExecutionHold("fixture adapter is forbidden for formal execution")
    if output_dir.exists():
        raise ExecutionHold("refusing to overwrite attempt directory")
    authority = load_json(package / "execution_authority.json")
    expected_hashes = dict(authority["expectedTraceHashes"])
    schema = load_json(package / "paired_output.schema.json")
    schema_version = schema["properties"]["schemaVersion"]["const"]
    package_id = authority["packageId"]
    conversations = load_jsonl(package / "conversations.jsonl")
    schedule_module = _load_module(package / "build_replay_schedule.py", "rumr_executor_live_schedule")
    schedule = schedule_module.build_schedule(conversations, 20260902)
    by_conversation: dict[str, list[dict[str, Any]]] = {}
    for row in schedule:
        by_conversation.setdefault(row["conversationId"], []).append(row)

    output_dir.mkdir(parents=True, exist_ok=False)
    started = {
        "schemaVersion": "real-user-multiturn-executor-started-v1", "runSetId": run_set_id,
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "executionAuthoritySha256": sha_file(package / "execution_authority.json"),
        "executionConfigSnapshotSha256": sha_file(package / "execution_config_snapshot.json"),
        "runnerSha256": sha_file(Path(__file__)), "automaticRetries": 0,
        "fixtureOnly": bool(getattr(adapter, "fixture_only", False)),
    }
    started_path = output_dir / "started.json"
    started_path.write_text(json.dumps(started, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    outputs: list[dict[str, Any]] = []
    private_receipts: list[dict[str, Any]] = []
    output_path = output_dir / "paired_outputs.jsonl"
    private_path = output_dir / "private_turn_receipts.jsonl"
    conversation_map = {item["conversationId"]: item for item in conversations}
    for conversation_id, rows in by_conversation.items():
        conversation = conversation_map[conversation_id]
        lanes = adapter.begin_conversation(conversation, ARMS)
        if set(lanes) != set(ARMS):
            raise ExecutionHold("adapter did not create both isolated lanes")
        if len({lanes[arm].branch_point_state_hash for arm in ARMS}) != 1:
            raise ExecutionHold("adapter lanes do not share the real branch point")
        dialogue_by_arm: dict[str, list[dict[str, str]]] = {arm: [] for arm in ARMS}
        for row in rows:
            arm = row["arm"]
            lane = lanes[arm]
            prior = [dict(item) for item in dialogue_by_arm[arm]] if arm == ARMS[0] else None
            result = adapter.execute_turn(row, lane, prior)
            answer = result.get("finalAnswer", "")
            dialogue_by_arm[arm].extend([
                {"role": "user", "content": row["rawUserText"]},
                {"role": "assistant", "content": answer},
            ])
            output = _build_output(
                package_id=package_id, schema_version=schema_version, row=row, lane=lane,
                result=result, dialogue=[dict(item) for item in dialogue_by_arm[arm]],
                expected_hashes=expected_hashes,
            )
            Draft202012Validator(schema).validate(output)
            outputs.append(output)
            private = {
                "executionOrdinal": row["executionOrdinal"], "conversationId": conversation_id,
                "turnId": row["turnId"], "arm": arm, "historyMode": result["historyMode"],
                "rawPriorMessageCount": result["rawPriorMessageCount"],
                "compiledContextUsed": result["compiledContextUsed"],
                "adapterResultSha256": sha_text(canonical(result)),
            }
            private_receipts.append(private)
            with output_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical(output) + "\n")
                handle.flush(); os.fsync(handle.fileno())
            with private_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical(private) + "\n")
                handle.flush(); os.fsync(handle.fileno())
            lanes[arm] = LaneBinding(
                lane.run_id, lane.task_id, lane.session_id, lane.branch_point_state_hash,
                int(result["postStateRevision"]),
            )
    if len(outputs) != 42:
        raise ExecutionHold(f"incomplete attempt: {len(outputs)}/42 rows")

    canonical_outputs = "".join(canonical(row) + "\n" for row in outputs)
    receipt = {
        "schemaVersion": "real-user-multiturn-execution-receipt-v4", "packageId": package_id,
        "ownership": "RUNNER_OWNED", "producer": PRODUCER, "runSetId": run_set_id,
        "executionAuthoritySha256": sha_file(package / "execution_authority.json"),
        "executionConfigSnapshotSha256": sha_file(package / "execution_config_snapshot.json"),
        "pairedOutputsCanonicalSha256": sha_text(canonical_outputs), "pairedOutputRowCount": 42,
        "expectedTraceHashes": expected_hashes,
        "observedModelCallCount": sum(len(row["trace"]["modelCalls"]) for row in outputs),
        "observedToolCallCount": sum(len(row["trace"]["toolCalls"]) for row in outputs),
    }
    receipt["receiptBindingSha256"] = sha_text(canonical(receipt))
    receipt_schema = load_json(package / "execution_receipt.schema.json")
    Draft202012Validator(receipt_schema).validate(receipt)
    (output_dir / "execution_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    blind = _load_module(package / "blind_packet_builder.py", "rumr_executor_blind_validator")
    blind.validate_outputs(outputs, conversations, receipt, 20260902)
    witness_paths = (started_path, output_path, private_path, output_dir / "execution_receipt.json")
    (output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha_file(path)}  {path.name}\n" for path in witness_paths),
        encoding="utf-8",
        newline="\n",
    )
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--package", type=Path, default=DEFAULT_PACKAGE)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    package = args.package.resolve()
    if args.preflight:
        result = package_preflight(package)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "PASS" else 2
    if args.dry_run:
        print(json.dumps(dry_run(package), ensure_ascii=False, indent=2))
        return 0
    raise ExecutionHold(
        "HOLD_PRODUCTION_ARM_SWITCH_NOT_WIRED: no formal adapter is shipped; "
        "do not substitute the fixture adapter or production chat endpoint"
    )


if __name__ == "__main__":
    raise SystemExit(main())
