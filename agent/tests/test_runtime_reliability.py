"""Failure-path coverage for unified Agent runtime reliability controls."""

import asyncio
import importlib.util
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from app.critic_queue import CriticQueue
from app.evidence_critic import CriticOutput
from app.task_state import TaskState


def _load_replay_cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "replay.py"
    spec = importlib.util.spec_from_file_location("agent_replay_cli", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_offline_replay_drives_executor_and_validator_without_llm() -> None:
    replay = _load_replay_cli()
    arguments = {"shopId": 123}
    output = {
        "tool": "get_shop_detail",
        "ok": True,
        "detail": {
            "id": 123,
            "name": "Replay Cafe",
            "type": "coffee",
            "address": "Beijing",
        },
    }
    call = {
        "stepIndex": 0,
        "toolName": "get_shop_detail",
        "arguments": arguments,
        "argumentsHash": replay._stable_args_hash(arguments),
        "argumentsKeys": ["shopId"],
        "ok": True,
        "outputHash": replay._stable_output_hash(output),
        "output": output,
    }

    results, mismatches = asyncio.run(
        replay._replay_harness(
            {"runId": "test-replay", "goal": "read shop details"},
            [call],
            True,
        )
    )

    assert mismatches == 0
    assert results[-1]["action"] == "task_completed"
    assert results[-1]["executorOutcome"] == "step_executed"
    assert results[-1]["validatorOutcome"] == "passed"


def test_new_read_and_preview_contracts_complete_one_vertical_replay() -> None:
    replay = _load_replay_cli()

    def recorded(tool, arguments, detail):
        output = {"tool": tool, "ok": True, "detail": detail}
        return {
            "stepIndex": len(calls),
            "toolName": tool,
            "arguments": arguments,
            "argumentsHash": replay._stable_args_hash(arguments),
            "argumentsKeys": sorted(arguments),
            "ok": True,
            "outputHash": replay._stable_output_hash(output),
            "output": output,
        }

    calls = []
    calls.append(recorded(
        "search_places",
        {"query": "park", "limit": 5},
        {"count": 1, "total": 1, "items": [{"id": "park-1", "name": "Park"}]},
    ))
    calls.append(recorded(
        "get_place_detail",
        {"placeId": "park-1"},
        {"place": {"id": "park-1", "name": "Park"}},
    ))
    calls.append(recorded(
        "list_shop_types",
        {},
        {"count": 2, "names": ["Coffee", "Food"]},
    ))
    calls.append(recorded(
        "recommend_shops",
        {"userId": "u-1"},
        {"count": 1, "shops": [{"shopId": 7, "shopName": "Cafe"}]},
    ))
    calls.append(recorded(
        "preview_order",
        {"productId": 9, "quantity": 2},
        {
            "status": "confirmation_required",
            "action": "create_order",
            "confirmationId": "c-order",
            "expiresAt": "2030-01-01T00:00:00Z",
            "preview": {"itemId": 9, "quantity": 2},
            "confirmationPhrase": "confirm order",
        },
    ))
    for tool, action, confirmation_id in (
        ("preview_cancel_order", "cancel_order", "c-cancel"),
        ("preview_payment", "create_payment", "c-payment"),
    ):
        calls.append(recorded(
            tool,
            {"orderId": "order-1"},
            {
                "status": "confirmation_required",
                "action": action,
                "confirmationId": confirmation_id,
                "expiresAt": "2030-01-01T00:00:00Z",
                "preview": {"order": {"id": "order-1"}},
                "confirmationPhrase": "confirm",
            },
        ))

    # Transaction previews are deliberately owned by TransactionAgent and are
    # not admitted into the low-risk shopping Harness. Replay only tools with
    # a persisted Harness contract and freeze that permission split here.
    from app.control.validation_contracts import expected_output_contracts_for_tool
    assert all(
        not expected_output_contracts_for_tool(call["toolName"])
        for call in calls[-3:]
    )
    results, mismatches = asyncio.run(
        replay._replay_harness(
            {"runId": "vertical-contracts", "goal": "offline vertical replay"},
            calls[:-3],
            True,
        )
    )

    assert mismatches == 0
    assert results[-1]["action"] == "task_completed"


def test_critic_queue_graceful_stop_drains_pending_jobs() -> None:
    class NullTraceStore:
        async def get(self, _run_id):
            return None

        async def save(self, _trace):
            return None

    async def scenario() -> CriticQueue:
        queue = CriticQueue(max_size=2)
        critic = AsyncMock(
            return_value=CriticOutput(approved=True, issues=[])
        )
        with patch("app.evidence_critic.critic_enabled", return_value=True), patch(
            "app.evidence_critic.run_evidence_critic", new=critic
        ), patch(
            "app.agent_trace.get_trace_store", return_value=NullTraceStore()
        ):
            await queue.start()
            assert await queue.enqueue("run-1", {}, []) is True
            assert await queue.enqueue("run-2", {}, []) is True
            await queue.stop(graceful=True)
            assert critic.await_count == 2
            await queue.start()
            await queue.stop(graceful=True)
        return queue

    queue = asyncio.run(scenario())
    assert queue.completed == 2


def test_critic_queue_times_out_stuck_critic_and_still_stops() -> None:
    class NullTraceStore:
        async def get(self, _run_id):
            return None

        async def save(self, _trace):
            return None

    async def stuck_critic(*_args, **_kwargs):
        await asyncio.sleep(10)

    async def scenario() -> None:
        queue = CriticQueue(max_size=1)
        with patch("app.evidence_critic.critic_enabled", return_value=True), patch(
            "app.evidence_critic.run_evidence_critic", new=stuck_critic
        ), patch(
            "app.agent_trace.get_trace_store", return_value=NullTraceStore()
        ), patch(
            "app.settings.settings.evidence_critic_timeout_seconds", 0.05
        ), patch(
            "app.settings.settings.evidence_critic_shutdown_timeout_seconds", 0.5
        ):
            await queue.start()
            assert await queue.enqueue("run-stuck", {}, []) is True
            await asyncio.wait_for(queue.stop(graceful=True), timeout=1.0)

    asyncio.run(scenario())


def test_unified_harness_enforces_one_total_request_deadline() -> None:
    from app.llm import _run_explicit_harness_agent

    state = TaskState(
        taskId="deadline-task",
        revision=1,
        status="ready",
        goal="推荐一款商品",
        taskType="ecommerce_guide",
        createdAt=datetime.now(timezone.utc),
        updatedAt=datetime.now(timezone.utc),
    )
    captured = []

    async def slow_step(*_args, **_kwargs):
        await asyncio.sleep(0.25)

    async def persist(trace):
        captured.append(trace)

    async def scenario():
        with patch("app.llm.run_harness_step", new=slow_step), patch(
            "app.llm._persist_trace_safely", new=persist
        ), patch(
            "app.llm.settings.agent_request_deadline_seconds", 0.1
        ), patch(
            "app.llm.settings.agent_context_mode", "context_pack"
        ), patch(
            "app.llm.settings.agent_graph_v2_durable_enabled", False
        ):
            return await _run_explicit_harness_agent(
                "推荐一款商品",
                history=None,
                client=MagicMock(),
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
            )

    _answer, _traces, _messages, _run_id, summary = asyncio.run(scenario())

    assert summary is not None
    assert summary.degraded is True
    assert captured[-1].final_action == "request_deadline_exceeded"
    assert "request_deadline_exceeded" in captured[-1].degraded_reasons


def test_total_deadline_also_covers_task_state_extraction() -> None:
    from app.llm import _run_unified_harness_agent

    state = TaskState(
        taskId="extraction-deadline-task",
        revision=1,
        status="ready",
        goal="推荐一款商品",
        taskType="ecommerce_guide",
        createdAt=datetime.now(timezone.utc),
        updatedAt=datetime.now(timezone.utc),
    )

    async def slow_update(*_args, **_kwargs):
        await asyncio.sleep(0.25)

    async def scenario():
        with patch("app.llm.get_client", return_value=MagicMock()), patch(
            "app.llm._update_task_state_for_unified_harness", new=slow_update
        ), patch(
            "app.llm._run_explicit_harness_agent", new=AsyncMock()
        ) as explicit, patch(
            "app.llm.settings.agent_request_deadline_seconds", 0.1
        ), patch(
            "app.llm._persist_trace_safely", new=AsyncMock()
        ):
            result = await _run_unified_harness_agent(
                "推荐一款商品",
                history=None,
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
            )
            explicit.assert_not_awaited()
            return result

    answer, traces, _messages, run_id, summary = asyncio.run(scenario())
    assert "总执行时间限制" in answer
    assert traces == []
    assert isinstance(run_id, str) and run_id.startswith("run-")
    assert summary is not None
    assert summary.final_action == "safe_stop"
    assert summary.failure_code == "task_state_update_timeout"


def test_task_state_extraction_receives_bounded_context_pack_not_raw_history() -> None:
    from app.llm import _update_task_state_for_unified_harness

    state = TaskState(
        taskId="bounded-extraction-task",
        revision=1,
        status="ready",
        goal="find a product",
        taskType="ecommerce_guide",
        createdAt=datetime.now(timezone.utc),
        updatedAt=datetime.now(timezone.utc),
    )
    raw_history = [
        {"role": "user", "content": f"history-{index}-" + ("x" * 20_000)}
        for index in range(20)
    ]
    reply = MagicMock(tool_calls=[])
    response = MagicMock(choices=[MagicMock(message=reply)])
    create = AsyncMock(return_value=response)
    client = MagicMock()
    client.chat.completions.create = create

    async def scenario():
        with patch(
            "app.llm._apply_task_state_update", new=AsyncMock(return_value=state)
        ):
            await _update_task_state_for_unified_harness(
                "current request",
                history=raw_history,
                client=client,
                task_state=state,
                on_task_state=None,
            )

    asyncio.run(scenario())
    messages = create.await_args.kwargs["messages"]
    serialized_size = sum(len(str(item.get("content", ""))) for item in messages)

    assert raw_history[0] not in messages
    assert serialized_size < 100_000


def test_confirmed_transaction_is_handed_off_without_legacy_execution() -> None:
    from app.llm import _run_deterministic_preflight

    state = TaskState(
        taskId="transaction-deadline-task",
        revision=1,
        status="ready",
        goal="confirm order",
        taskType="ecommerce_guide",
        createdAt=datetime.now(timezone.utc),
        updatedAt=datetime.now(timezone.utc),
    )
    calls = []

    async def slow_transaction(_action):
        calls.append(_action)
        raise AssertionError("legacy execute_confirmed_transaction must not run")

    async def scenario():
        loop = asyncio.get_running_loop()
        with patch(
            "app.llm.explicit_confirmation_action", return_value="create_order"
        ), patch(
            "app.domains.ecommerce.transactions.execute_confirmed_transaction",
            new=slow_transaction,
        ), patch(
            "app.llm._record_tool_progress", new=AsyncMock(return_value=state)
        ):
            return await _run_deterministic_preflight(
                "confirm",
                task_state=state,
                domain_hint="ecommerce",
                on_answer_delta=None,
                on_task_state=None,
                deadline_at=loop.time() + 0.05,
            )

    result = asyncio.run(scenario())

    assert result is not None
    answer, traces, _messages, run_id, summary = result
    assert "受信交易服务" in answer
    assert calls == []
    assert run_id is None
    assert summary is None
    assert len(traces) == 1
    assert traces[0].tool == "transaction_handoff"
    assert traces[0].detail["status"] == "awaiting_trusted_transaction_agent"


def test_replay_trace_baseline_detects_phase_order_regression() -> None:
    replay = _load_replay_cli()
    arguments = {"shopId": 123}
    output = {
        "tool": "get_shop_detail",
        "ok": True,
        "detail": {
            "id": 123,
            "name": "Replay Cafe",
            "type": "coffee",
            "address": "Beijing",
        },
    }
    call = {
        "stepIndex": 0,
        "toolName": "get_shop_detail",
        "arguments": arguments,
        "argumentsHash": replay._stable_args_hash(arguments),
        "argumentsKeys": ["shopId"],
        "ok": True,
        "outputHash": replay._stable_output_hash(output),
        "output": output,
    }

    _results, mismatches = asyncio.run(replay._replay_harness(
        {
            "runId": "baseline-regression",
            "goal": "read shop",
            "harnessPhaseOrder": ["impossible_phase"],
            "finalAction": "task_completed",
        },
        [call],
        True,
    ))

    assert mismatches >= 1


def test_replay_loader_selects_latest_run_in_appended_recording() -> None:
    replay = _load_replay_cli()
    records = [
        {"type": "session", "runId": "old", "recordedAt": "1"},
        {"type": "call", "runId": "old", "stepIndex": 0},
        {"type": "session", "runId": "new", "recordedAt": "2"},
        {"type": "call", "runId": "new", "stepIndex": 0},
        {
            "type": "trace_baseline",
            "runId": "new",
            "finalAction": "task_completed",
        },
    ]
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", encoding="utf-8", delete=False
    ) as handle:
        path = Path(handle.name)
        for record in records:
            handle.write(json.dumps(record) + "\n")
    try:
        session, calls = replay._load_recording(path)
    finally:
        path.unlink(missing_ok=True)

    assert session["runId"] == "new"
    assert session["finalAction"] == "task_completed"
    assert [call["runId"] for call in calls] == ["new"]
