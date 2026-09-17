"""Regression coverage for durable V2 context at the real dispatch boundary."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from functools import wraps
from pathlib import Path

import pytest

from app.graph.tool_inbox_v2 import ToolInboxSlot, sha256
from app.schemas import ToolTrace
from app.settings import settings
from app.tool_execution_v2 import ToolExecutionContext
from app.tool_transport import ToolExecutionContextError
from app.transport_resolver import get_tool_transport


def _async_test(function):
    @wraps(function)
    def runner(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return runner


def _context(arguments: dict[str, object]) -> ToolExecutionContext:
    args_hash = sha256(arguments)
    slot = ToolInboxSlot.create(
        task_id="task-1",
        plan_id="plan-1",
        step_id="step-1",
        state_revision=1,
        tool_name="search_products",
        canonical_args_sha256=args_hash,
    )
    return ToolExecutionContext(
        task_id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        plan_id="plan-1",
        step_id="step-1",
        state_revision=1,
        session_owner_hash="a" * 16,
        execution_id=slot.execution_id(run_id="run-1", thread_id="thread-1"),
        logical_slot_key=slot.logical_slot_key(),
        fence=1,
        canonical_args_sha256=args_hash,
    )


@_async_test
async def test_durable_context_reaches_live_dispatch_without_entering_model_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Old resolver adapters dropped this context before the internal dispatcher."""

    monkeypatch.setattr(settings, "agent_tool_transport_mode", "live")
    arguments: dict[str, object] = {"query": "q", "category": "手机"}
    context = _context(arguments)
    received: list[ToolExecutionContext | None] = []

    async def live_dispatch(
        tool_name: str,
        actual_arguments: dict[str, object],
        *,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolTrace:
        assert tool_name == "search_products"
        assert actual_arguments == arguments
        assert "execution_context" not in actual_arguments
        received.append(execution_context)
        return ToolTrace(tool=tool_name, ok=True, detail={})

    monkeypatch.setattr("app.tools.call_tool", live_dispatch)

    trace = await get_tool_transport()(  # pre-fix: wrapper rejects this keyword
        "search_products",
        arguments,
        execution_context=context,
    )

    assert trace.ok is True
    assert received == [context]


@_async_test
async def test_context_hash_mismatch_rejects_before_live_tool_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "live")
    live_calls = 0

    async def live_dispatch(*_args, **_kwargs) -> ToolTrace:
        nonlocal live_calls
        live_calls += 1
        return ToolTrace(tool="search_products", ok=True, detail={})

    monkeypatch.setattr("app.tools.call_tool", live_dispatch)
    arguments: dict[str, object] = {"query": "q", "category": "手机"}

    with pytest.raises(ToolExecutionContextError) as rejected:
        await get_tool_transport()(
            "search_products",
            {"query": "tampered", "category": "手机"},
            execution_context=_context(arguments),
        )

    assert rejected.value.code == "execution_context_arguments_mismatch"
    assert live_calls == 0


@_async_test
async def test_record_and_replay_bind_the_minimum_execution_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    arguments: dict[str, object] = {"query": "q", "category": "手机"}
    context = _context(arguments)
    received: list[ToolExecutionContext | None] = []

    async def live_dispatch(
        tool_name: str,
        actual_arguments: dict[str, object],
        *,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolTrace:
        assert actual_arguments == arguments
        received.append(execution_context)
        return ToolTrace(tool=tool_name, ok=True, detail={})

    monkeypatch.setattr("app.tools.call_tool", live_dispatch)
    recording = tmp_path / "durable-record.jsonl"
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "record")
    recorded = await get_tool_transport(recording_path=recording)(
        "search_products", arguments, execution_context=context
    )
    assert recorded.ok is True
    assert received == [context]
    call_record = json.loads(recording.read_text(encoding="utf-8").splitlines()[1])
    assert call_record["executionIdentity"] == {
        "executionId": context.execution_id,
        "logicalSlotKey": context.logical_slot_key,
        "fence": context.fence,
        "inputHash": context.canonical_args_sha256,
    }
    assert "sessionOwnerHash" not in call_record

    monkeypatch.setattr(settings, "agent_tool_transport_mode", "replay")
    replayed = await get_tool_transport(replay_path=recording)(
        "search_products", arguments, execution_context=context
    )
    assert replayed.ok is True
    with pytest.raises(ToolExecutionContextError) as rejected:
        await get_tool_transport(replay_path=recording)(
            "search_products",
            arguments,
            execution_context=replace(context, execution_id="d" * 64),
        )
    assert rejected.value.code == "execution_context_execution_mismatch"
    with pytest.raises(KeyError, match="Replay execution identity mismatch"):
        await get_tool_transport(replay_path=recording)(
            "search_products",
            arguments,
            execution_context=replace(context, fence=2),
        )


@_async_test
async def test_in_process_mcp_forwards_context_privately_to_server_dispatcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "mcp_in_process_readonly")
    monkeypatch.setattr(settings, "agent_mcp_readonly_enabled", True)
    monkeypatch.setattr(settings, "agent_mcp_transport_mode", "in_process_readonly")
    arguments: dict[str, object] = {"query": "q", "category": "手机"}
    context = _context(arguments)
    received: list[ToolExecutionContext | None] = []

    async def live_dispatch(
        tool_name: str,
        actual_arguments: dict[str, object],
        *,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolTrace:
        assert actual_arguments == arguments
        received.append(execution_context)
        return ToolTrace(tool=tool_name, ok=True, detail={})

    monkeypatch.setattr("app.tools.call_tool", live_dispatch)
    trace = await get_tool_transport()("search_products", arguments, execution_context=context)

    assert trace.ok is True
    assert received == [context]
