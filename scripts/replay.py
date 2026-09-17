#!/usr/bin/env python
"""CLI replay tool — replay a recorded JSONL session through the Harness.

Usage:
  python -m scripts.replay inspect <recording.jsonl>   # list calls in a recording
  python -m scripts.replay check <recording.jsonl>     # hash-exact check (no model, no network)
  python -m scripts.replay replay <recording.jsonl>    # replay via Harness with ReplayTransport

The replay subcommand re-drives the Harness with ReplayTransport. It validates
each reconstructed ContextView at runtime and, when a trace baseline exists,
compares phase order and final action in addition to tool calls. It is
NOT a file-self-comparison — it runs the real harness state machine.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("replay")


# ── helpers ───────────────────────────────────────────────────────────────────


def _load_recording(path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Read a JSONL recording. Returns (session_header, [call_records])."""
    if not path.exists():
        raise FileNotFoundError(f"Recording not found: {path}")
    session = None
    calls: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("type") == "session":
                session = record
                # A recording file may contain several appended runs. The CLI
                # replays the latest complete run instead of mixing calls from
                # unrelated sessions.
                calls = []
            elif record.get("type") == "trace_baseline":
                if session is not None and record.get("runId") == session.get("runId"):
                    session = {**session, **record}
            else:
                calls.append(record)
    return session, calls


def _stable_args_hash(arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _stable_output_hash(output: Any) -> str:
    payload = json.dumps(output, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ── inspect subcommand ────────────────────────────────────────────────────────


def cmd_inspect(path: Path) -> None:
    """List all recorded calls — no model, no external calls."""
    session, calls = _load_recording(path)
    if session:
        print(f"Session: {session.get('runId', 'unknown')}")
        print(f"Recorded: {session.get('recordedAt', 'unknown')}")
        print(f"ContextPack hash: {session.get('contextPackHash', 'none')}")
        print(f"Tool calls: {len(calls)}")
        print()
    if not calls:
        print("(no calls recorded)")
        return
    for i, call in enumerate(calls):
        status = "OK" if call.get("ok", True) else "FAIL"
        print(
            f"  [{i}] {call.get('toolName', 'unknown')} "
            f"hash={call.get('argumentsHash', '?')} "
            f"keys={call.get('argumentsKeys', [])} "
            f"→ {status} "
            f"(out_hash={call.get('outputHash', '?')})"
        )


# ── check subcommand (fully offline, no model, no external calls) ─────────────


def cmd_check(path: Path) -> int:
    """Fully-offline hash check.

    Verifies that stored output hashes match computed hashes.
    Also validates call sequence integrity.
    Returns 0 if all calls pass; non-zero otherwise.
    """
    session, calls = _load_recording(path)
    if not calls:
        logger.warning("No calls to check")
        return 0

    failures = 0
    prev_step = -1

    for i, call in enumerate(calls):
        tool_name = call.get("toolName", "")
        recorded_out_hash = call.get("outputHash", "")
        recorded_out = call.get("output")
        step_index = call.get("stepIndex", i)

        logger.info("Checking call [%d/%d]: %s", i + 1, len(calls), tool_name)

        if step_index <= prev_step:
            logger.warning(
                "  [%d] %s: non-sequential step index %d (prev=%d)",
                i, tool_name, step_index, prev_step,
            )
        prev_step = step_index

        if "argumentsHash" not in call or "outputHash" not in call:
            failures += 1
            logger.error(
                "  [%d] %s: missing required fields (argumentsHash or outputHash)",
                i, tool_name,
            )
            continue

        if recorded_out is not None:
            fresh_hash = _stable_output_hash(recorded_out)
            if recorded_out_hash and fresh_hash != recorded_out_hash:
                failures += 1
                logger.error(
                    "  [%d] %s: output hash mismatch (recorded=%s, computed=%s)",
                    i, tool_name, recorded_out_hash, fresh_hash,
                )
            else:
                logger.info("  [%d] %s: hash OK (%s)", i, tool_name, recorded_out_hash or fresh_hash)
        else:
            logger.info("  [%d] %s: no stored output to verify (hash=%s)", i, tool_name, recorded_out_hash)

    if failures:
        logger.error("%d/%d calls had issues", failures, len(calls))
        return 1
    logger.info("All %d calls verified OK", len(calls))
    return 0


# ── replay subcommand (drives Harness with ReplayTransport) ────────────────────


async def _replay_harness(
    session: dict[str, Any] | None,
    calls: list[dict[str, Any]],
    strict: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Replay recorded tool calls through the Harness via ReplayTransport.

    Constructs a ReplayTransport from the recorded calls, then calls
    run_harness_step() in a loop to drive the full Harness state machine.
    Verifies:
      - All recorded calls are consumed in order
      - Tool names and argument hashes match (strict mode)
      - Output hashes match
      - No live LLM/network calls are made

    Returns (results_list, mismatch_count).
    """
    from copy import deepcopy
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, patch

    from app.agent_trace import TraceBuilder
    from app.context_pack import build_context_pack
    from app.context_view import ContextProjector
    from app.control.validation_contracts import expected_output_contracts_for_tool
    from app.harness import run_harness_step
    from app.planning import PlanArgumentSource, PlanStep, TaskPlan
    from app.react_graph import ReActGraphRuntime, run_controlled_react_graph
    from app.task_state import (
        TaskState,
        TaskStateRevisionConflictError,
    )
    from app.tool_transport import ReplayTransport
    from app.tools import TOOL_SCHEMAS

    mismatches = 0
    results: list[dict[str, Any]] = []

    if not calls:
        return results, 0

    # ── Build a ReplayTransport from the recording ─────────────────────
    temp_path = Path(os.environ.get("TEMP", "/tmp")) / f"_replay_harness_{int(time.time())}.jsonl"
    try:
        with open(temp_path, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"type": "session", "runId": session.get("runId", "replay-cli") if session else "replay-cli",
                     "recordedAt": session.get("recordedAt", "") if session else ""},
                    ensure_ascii=False,
                ) + "\n"
            )
            for call in calls:
                fh.write(json.dumps(call, ensure_ascii=False) + "\n")

        replay_transport = ReplayTransport(temp_path, strict=strict)

        # ── Reconstruct a deterministic executable Plan ───────────────
        # Recordings persist cleansed concrete arguments. They are projected
        # into system-policy sources so Executor still re-resolves and checks
        # every argument instead of calling ReplayTransport directly.
        steps: list[PlanStep] = []
        policies: dict[str, Any] = {}
        tool_names: set[str] = set()
        for index, call in enumerate(calls):
            arguments = call.get("arguments")
            if not isinstance(arguments, dict):
                logger.error(
                    "Recording call %d lacks replayable arguments; create a new recording",
                    index,
                )
                return results, len(calls)
            tool_name = str(call.get("toolName", ""))
            contracts = expected_output_contracts_for_tool(tool_name)
            if not contracts:
                logger.error("Tool %s has no unified Harness contract", tool_name)
                return results, len(calls)
            sources: dict[str, PlanArgumentSource] = {}
            for argument_name, value in arguments.items():
                policy_key = f"replay_step_{index}_{argument_name}"
                policies[policy_key] = deepcopy(value)
                sources[argument_name] = PlanArgumentSource(
                    kind="system_policy",
                    reference=policy_key,
                )
            steps.append(
                PlanStep(
                    stepId=f"replay-step-{index}",
                    description=f"Replay recorded call {index}: {tool_name}",
                    toolName=tool_name,
                    arguments=deepcopy(arguments),
                    argumentSources=sources,
                    expectedOutput={name: True for name in contracts},
                )
            )
            tool_names.add(tool_name)

        now = datetime.now(timezone.utc)
        plan = TaskPlan(
            planId="replay-plan",
            basedOnRevision=1,
            steps=steps,
        )
        state = TaskState(
            taskId=(session or {}).get("taskId", "replay-task"),
            revision=1,
            status="ready",
            goal=(session or {}).get("goal", "Offline Harness replay"),
            taskType=(session or {}).get("taskType", "replay"),
            activePlan=plan,
            createdAt=now,
            updatedAt=now,
            domainState={},
        )
        store = {"state": state}

        async def persist(task_id: str, state_patch):
            current = store["state"]
            if task_id != current.task_id:
                raise RuntimeError("Replay TaskState identity mismatch")
            if state_patch.expected_revision != current.revision:
                raise TaskStateRevisionConflictError(
                    state_patch.expected_revision,
                    current.revision,
                )
            updated = current.model_copy(deep=True)
            updated.revision = current.revision + 1
            updated.updated_at = datetime.now(timezone.utc)
            if state_patch.status is not None:
                updated.status = state_patch.status
            if "active_plan" in state_patch.model_fields_set:
                updated.active_plan = (
                    state_patch.active_plan.model_copy(deep=True)
                    if state_patch.active_plan is not None
                    else None
                )
            domain_state = dict(updated.domain_state)
            for key, value in state_patch.domain_state_patch.items():
                if value is None:
                    domain_state.pop(key, None)
                else:
                    domain_state[key] = deepcopy(value)
            updated.domain_state = domain_state
            store["state"] = updated
            return updated

        schemas = [
            schema
            for schema in TOOL_SCHEMAS
            if schema["function"]["name"] in tool_names
        ]
        if len(schemas) != len(tool_names):
            logger.error("Recording references a tool without a runtime schema")
            return results, len(calls)

        # ── Drive the real Harness loop; any LLM call is a replay failure ──
        mock_client = AsyncMock()
        mock_client.chat = AsyncMock()
        mock_client.chat.completions = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("LLM invoked during offline replay"),
        )

        async def replay_call(tool_name: str, arguments: dict[str, Any]):
            return await replay_transport(tool_name, arguments, None)

        pack = await build_context_pack(
            state,
            allowed_tools=sorted(tool_names),
            run_id="offline-replay",
        )
        projector = ContextProjector(pack)
        trace_builder = TraceBuilder("offline-replay", mode="context_pack")

        with patch("app.executor.update_task_state", new=persist), \
             patch("app.validator.update_task_state", new=persist), \
             patch("app.replanner.update_task_state", new=persist):
            final_action = ""
            async def capture_transition(harness_result):
                store["state"] = harness_result.task_state
                results.append({
                    "step": len(results),
                    "tool": (
                        harness_result.executor_result.execution_result.tool_name
                        if harness_result.executor_result is not None
                        and harness_result.executor_result.execution_result is not None
                        else "validator"
                    ),
                    "action": harness_result.action,
                    "ok": harness_result.action in {"continue_to_executor", "task_completed"},
                    "executorOutcome": (
                        harness_result.executor_result.outcome
                        if harness_result.executor_result is not None
                        else None
                    ),
                    "validatorOutcome": (
                        harness_result.validator_result.outcome
                        if harness_result.validator_result is not None
                        else None
                    ),
                    "executionOutcome": (
                        harness_result.executor_result.execution_result.outcome
                        if harness_result.executor_result is not None
                        and harness_result.executor_result.execution_result is not None
                        else None
                    ),
                    "executionError": (
                        harness_result.executor_result.execution_result.error_type
                        if harness_result.executor_result is not None
                        and harness_result.executor_result.execution_result is not None
                        else None
                    ),
                    "errorCode": (
                        harness_result.executor_result.error_code
                        if harness_result.executor_result is not None
                        and harness_result.executor_result.error_code
                        else harness_result.validator_result.error_code
                        if harness_result.validator_result is not None
                        else None
                    ),
                    "reason": (
                        harness_result.executor_result.reason
                        if harness_result.executor_result is not None
                        and harness_result.executor_result.reason
                        else harness_result.validator_result.reason
                        if harness_result.validator_result is not None
                        else None
                    ),
                })

            try:
                graph_state = await run_controlled_react_graph(
                    store["state"],
                    ReActGraphRuntime(
                        user_message=state.goal,
                        client=mock_client,
                        model="offline-replay",
                        resolve_tool_schemas=lambda _state: schemas,
                        step_runner=run_harness_step,
                        tool_caller=replay_call,
                        trace_builder=trace_builder,
                        projector=projector,
                        max_transitions=len(calls) + 2,
                        system_policies=policies,
                        on_transition=capture_transition,
                    ),
                )
                final_action = graph_state["action"]
            except Exception as exc:
                mismatches += 1
                logger.exception("LangGraph Harness replay failed: %s", exc)

            if final_action != "task_completed":
                mismatches += 1
            if replay_transport.consumed_count != len(calls):
                mismatches += 1
                logger.error(
                    "Replay consumed %d/%d recorded calls",
                    replay_transport.consumed_count,
                    len(calls),
                )

            actual_trace = trace_builder.finish()
            expected_phase_order = (session or {}).get("harnessPhaseOrder")
            if isinstance(expected_phase_order, list):
                actual_phase_order = [phase.phase for phase in actual_trace.phases]
                if actual_phase_order != expected_phase_order:
                    mismatches += 1
                    logger.error(
                        "Replay phase order mismatch: expected=%s actual=%s",
                        expected_phase_order,
                        actual_phase_order,
                    )
            expected_final_action = (session or {}).get("finalAction")
            if expected_final_action and expected_final_action != final_action:
                mismatches += 1
                logger.error(
                    "Replay final action mismatch: expected=%s actual=%s",
                    expected_final_action,
                    final_action,
                )
            expected_view_types = (session or {}).get("contextViewTypes")
            if isinstance(expected_view_types, list):
                actual_view_types = [
                    str(item.get("type"))
                    for item in actual_trace.context_views
                    if isinstance(item, dict)
                ]
                if actual_view_types != expected_view_types:
                    mismatches += 1
                    logger.error(
                        "Replay ContextView sequence mismatch: expected=%s actual=%s",
                        expected_view_types,
                        actual_view_types,
                    )

        return results, mismatches
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def cmd_replay(path: Path, *, strict: bool = False) -> int:
    """Offline harness replay: serve recorded calls via ReplayTransport.

    This drives the ReplayTransport through the recorded sequence, optionally
    calling run_harness_step() to exercise the full state machine.
    No LLM, no network — only deterministic tool-call replay from recording.

    In strict mode, tool name, argument hash, and order must all match exactly
    or replay fails.
    """
    session, calls = _load_recording(path)
    if not calls:
        logger.warning("No calls to replay")
        return 0

    logger.info("Replaying %d calls from %s (strict=%s)", len(calls), path, strict)

    results, mismatches = asyncio.run(_replay_harness(session, calls, strict=strict))

    # Summary
    print(f"\nReplay summary: {len(calls)} calls, {mismatches} mismatches")
    for r in results:
        status = "✓" if r.get("ok") else "✗"
        print(f"  {status} [{r['step']}] {r['tool']}  out_hash={r.get('out_hash', '?')[:12]}")

    if mismatches:
        logger.error("%d/%d calls had mismatches", mismatches, len(calls))
        return 1
    logger.info("All %d calls replayed successfully", len(calls))
    return 0


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay CLI — inspect, check, or replay recorded tool-call sessions (offline, no model)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_parser = sub.add_parser("inspect", help="List recorded calls in a JSONL file (no model)")
    inspect_parser.add_argument("recording", type=Path, help="Path to recording JSONL file")

    check_parser = sub.add_parser("check", help="Exact hash check (no model, no network)")
    check_parser.add_argument("recording", type=Path, help="Path to recording JSONL file")

    replay_parser = sub.add_parser("replay", help="Offline sequential replay via ReplayTransport (no model)")
    replay_parser.add_argument("recording", type=Path, help="Path to recording JSONL file")
    replay_parser.add_argument("--strict", action="store_true", help="Fail hard on any mismatch")

    args = parser.parse_args()

    if args.command == "inspect":
        cmd_inspect(args.recording)
    elif args.command == "check":
        sys.exit(cmd_check(args.recording))
    elif args.command == "replay":
        sys.exit(cmd_replay(
            args.recording,
            strict=args.strict,
        ))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
