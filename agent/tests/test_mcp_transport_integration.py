"""Production-adjacent coverage for the explicit MCP tool transport opt-in."""

from __future__ import annotations

import json
import asyncio
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import task_state
from app.mcp_client import MCPReadonlyClient, MCPReadonlyClientError
from app.schemas import ToolTrace
from app.settings import Settings, settings
from app.task_state import (
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    update_task_state,
)
from app.tools import TOOL_SCHEMAS
from app.transport_resolver import (
    ToolTransportConfigurationError,
    get_record_transport,
    get_tool_transport,
    get_tool_transport_identity,
)
from tests.fake_redis import FakeRedis
from tests.two_stage_ranking_fixtures import two_stage_search_detail


def _async_test(function):
    @wraps(function)
    def runner(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return runner


def _set_mcp_mode(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> None:
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "mcp_in_process_readonly")
    monkeypatch.setattr(settings, "agent_mcp_readonly_enabled", enabled)
    monkeypatch.setattr(settings, "agent_mcp_transport_mode", "in_process_readonly")


def _schema_names() -> list[str]:
    return [
        schema["function"]["name"]
        for schema in TOOL_SCHEMAS
        if schema["function"]["name"] in {"search_products", "get_product_details"}
    ]


def _planner_reply() -> SimpleNamespace:
    payload = {
        "outcome": "planned",
        "steps": [
            {
                "stepId": "search-step",
                "description": "Search the product catalog",
                "toolName": "search_products",
                "arguments": {
                    "query": "找一台二手手机",
                    "category": "手机",
                },
                "argumentSources": {
                    "query": {"kind": "task_goal"},
                    "category": {
                        "kind": "task_state",
                        "reference": "facts.category",
                    },
                },
                "expectedOutput": {"requiresProductCandidates": True},
            },
            {
                "stepId": "detail-step",
                "description": "Read the selected product details",
                "toolName": "get_product_details",
                "arguments": {"productIds": [1]},
                "argumentSources": {
                    "productIds": {
                        "kind": "prior_step",
                        "reference": "search-step.productIds",
                    }
                },
                "expectedOutput": {"requiresProductDetails": True},
            },
        ],
    }
    call = SimpleNamespace(
        id="planner-call",
        function=SimpleNamespace(
            name="submit_planner_output",
            arguments=json.dumps(payload, ensure_ascii=False),
        ),
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[call]))]
    )


def _fake_client(reply: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=reply))
        )
    )


def _read_only_schemas() -> list[dict]:
    return [
        schema
        for schema in TOOL_SCHEMAS
        if schema["function"]["name"] in {"search_products", "get_product_details"}
    ]


def test_settings_rejects_unknown_transport_mode() -> None:
    assert Settings(_env_file=None).agent_tool_transport_mode == "live"
    for mode in ("live", "record", "replay", "mcp_in_process_readonly"):
        assert Settings(_env_file=None, agent_tool_transport_mode=mode).agent_tool_transport_mode == mode
    with pytest.raises(ValueError):
        Settings(_env_file=None, agent_tool_transport_mode="unknown-mode")


def test_transport_identity_contract_is_bounded_and_server_owned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolver identity is intentionally not inferred from settings/UI."""
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "live")
    transport = get_tool_transport()
    assert get_tool_transport_identity(transport) == {
        "mode": "live",
        "source": "server_resolver",
    }
    with pytest.raises(ToolTransportConfigurationError) as missing:
        get_tool_transport_identity(lambda _name, _arguments: None)
    assert missing.value.code == "transport_identity_missing"
    broken = SimpleNamespace(transport_identity={
        "mode": "live",
        "source": "server_resolver",
        "unexpected": "field",
    })
    with pytest.raises(ToolTransportConfigurationError) as shape:
        get_tool_transport_identity(broken)
    assert shape.value.code == "transport_identity_shape_invalid"


@_async_test
async def test_unknown_mode_fails_before_live_io(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "unknown-mode")
    live = AsyncMock()
    monkeypatch.setattr("app.tools.call_tool", live)

    with pytest.raises(ToolTransportConfigurationError) as exc_info:
        get_tool_transport()

    assert exc_info.value.code == "unknown_tool_transport_mode"
    assert live.await_count == 0


@pytest.mark.parametrize(
    ("enabled", "mcp_mode", "expected_code"),
    [
        (False, "in_process_readonly", "mcp_readonly_not_enabled"),
        (True, "disabled", "mcp_transport_mode_mismatch"),
        (True, "wrong", "mcp_transport_mode_mismatch"),
    ],
)
@_async_test
async def test_mcp_contradictions_fail_before_any_business_io(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    mcp_mode: str,
    expected_code: str,
) -> None:
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "mcp_in_process_readonly")
    monkeypatch.setattr(settings, "agent_mcp_readonly_enabled", enabled)
    monkeypatch.setattr(settings, "agent_mcp_transport_mode", mcp_mode)
    live = AsyncMock()
    monkeypatch.setattr("app.tools.call_tool", live)

    with pytest.raises(ToolTransportConfigurationError) as exc_info:
        get_tool_transport()

    assert exc_info.value.code == expected_code
    assert live.await_count == 0


@_async_test
async def test_mcp_search_and_detail_are_request_local_and_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mcp_mode(monkeypatch)
    live = AsyncMock(
        side_effect=[
            ToolTrace(
                tool="search_products",
                ok=True,
                detail=two_stage_search_detail([1, 2, 3]),
            ),
            ToolTrace(
                tool="get_product_details",
                ok=True,
                detail={"productIds": [1], "products": []},
            ),
        ]
    )
    monkeypatch.setattr("app.tools.call_tool", live)

    transport = get_tool_transport(run_id="mcp-run")
    assert get_tool_transport_identity(transport) == {
        "mode": "mcp_in_process_readonly",
        "source": "server_resolver",
        "protocolVersion": "2026-07-28",
        "serverName": "ecommerce-readonly-gateway",
        "serverVersion": "1.0.0",
    }
    search_trace = await transport(
        "search_products",
        {"query": "used phone", "category": "手机", "limit": 3},
    )
    detail_trace = await transport("get_product_details", {"productIds": [1]})

    assert search_trace.tool == "search_products"
    assert search_trace.ok is True
    assert detail_trace.tool == "get_product_details"
    assert detail_trace.ok is True
    assert live.await_args_list[0].args[0] == "search_products"
    assert live.await_args_list[1].args[0] == "get_product_details"
    assert live.await_count == 2


@_async_test
async def test_mcp_failure_has_no_live_fallback_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mcp_mode(monkeypatch)
    live = AsyncMock(
        return_value=ToolTrace(
            tool="search_products",
            ok=False,
            detail={"secretBackendMessage": "must not cross the boundary"},
        )
    )
    monkeypatch.setattr("app.tools.call_tool", live)
    transport = get_tool_transport()

    with pytest.raises(MCPReadonlyClientError) as exc_info:
        await transport("search_products", {"query": "used phone", "category": "手机"})

    assert exc_info.value.code == "mcp_tool_failed"
    assert live.await_count == 1


@_async_test
async def test_mcp_allowlist_rejects_forbidden_tool_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mcp_mode(monkeypatch)
    live = AsyncMock()
    monkeypatch.setattr("app.tools.call_tool", live)
    transport = get_tool_transport()

    with pytest.raises(MCPReadonlyClientError) as exc_info:
        await transport("create_payment", {"amountMinor": 1})

    assert exc_info.value.code == "mcp_tool_not_allowlisted"
    assert live.await_count == 0


@_async_test
async def test_default_live_transport_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "live")
    monkeypatch.setattr(settings, "agent_mcp_readonly_enabled", True)
    monkeypatch.setattr(settings, "agent_mcp_transport_mode", "in_process_readonly")
    live = AsyncMock(return_value=ToolTrace(tool="search_products", ok=True, detail={}))
    monkeypatch.setattr("app.tools.call_tool", live)

    trace = await get_tool_transport()("search_products", {"query": "q", "category": "手机"})

    assert trace.tool == "search_products"
    assert live.await_count == 1


@_async_test
async def test_record_replay_and_v2_shadow_never_enter_mcp(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    live = AsyncMock(return_value=ToolTrace(tool="search_products", ok=True, detail={}))
    monkeypatch.setattr("app.tools.call_tool", live)
    monkeypatch.setattr(settings, "agent_mcp_readonly_enabled", True)
    monkeypatch.setattr(settings, "agent_mcp_transport_mode", "in_process_readonly")
    mcp_constructor = AsyncMock(side_effect=AssertionError("MCP must not be entered"))
    monkeypatch.setattr("app.mcp_client.MCPReadonlyClient", mcp_constructor)

    recording_path = tmp_path / "tool_calls.jsonl"
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "record")
    record_transport = get_tool_transport(
        run_id="record-run", recording_path=recording_path
    )
    await record_transport("search_products", {"query": "q", "category": "手机"})
    assert live.await_count == 1

    replay_live = AsyncMock(side_effect=AssertionError("replay must not call live"))
    monkeypatch.setattr("app.tools.call_tool", replay_live)
    monkeypatch.setattr(settings, "agent_tool_transport_mode", "replay")
    replay_transport = get_tool_transport(replay_path=recording_path)
    replayed = await replay_transport("search_products", {"query": "q", "category": "手机"})
    assert replayed.tool == "search_products"
    assert replay_live.await_count == 0

    monkeypatch.setattr(settings, "agent_tool_transport_mode", "mcp_in_process_readonly")
    shadow_live = AsyncMock(return_value=ToolTrace(tool="search_products", ok=True, detail={}))
    monkeypatch.setattr("app.tools.call_tool", shadow_live)
    shadow = get_record_transport(tmp_path / "shadow.jsonl", "shadow-run")
    shadow_trace = await shadow("search_products", {"query": "q", "category": "手机"})
    assert shadow_trace.tool == "search_products"
    assert shadow_live.await_count == 1
    assert mcp_constructor.await_count == 0


@_async_test
async def test_harness_single_search_uses_resolved_mcp_transport_and_normalizes_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the real Planner→Executor boundary with the resolver wrapper."""

    _set_mcp_mode(monkeypatch)
    task_state._client = FakeRedis()
    task_state._task_locks.clear()
    task_state._session_locks.clear()
    live = AsyncMock(
        return_value=ToolTrace(
            tool="search_products",
            ok=True,
            detail=two_stage_search_detail([1, 2, 3]),
        )
    )
    monkeypatch.setattr("app.tools.call_tool", live)

    state = await create_task_state(
        TaskStateCreateRequest(
            taskType="ecommerce_guide",
            goal="找一台二手手机",
            facts=[
                {
                    "key": "category",
                    "value": "手机",
                    "certainty": "confirmed",
                    "source": "user",
                }
            ],
        )
    )
    ready = await update_task_state(
        state.task_id,
        TaskStatePatchRequest(
            expectedRevision=state.revision,
            actor="agent",
            status="ready",
        ),
    )
    transport = get_tool_transport(run_id=ready.task_id)

    from app.harness import run_harness_step

    result = await run_harness_step(
        ready,
        "找一台二手手机",
        _read_only_schemas(),
        client=_fake_client(_planner_reply()),
        model="test-model",
        tool_caller=transport,
    )

    assert result.action == "continue_to_executor", (
        f"planner={result.planner_result!r} executor={result.executor_result!r}"
    )
    assert result.executor_result is not None
    assert result.executor_result.outcome == "step_executed"
    assert result.executor_result.step_output is not None
    assert result.executor_result.step_output.values["candidatePoolIds"] == [1, 2, 3]
    assert result.executor_result.execution_result.tool_trace.tool == "search_products"
    assert result.executor_result.execution_result.tool_trace.ok is True
    assert live.await_count == 1
