import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.orchestrator import AgentOrchestrator


def test_orchestrator_prefers_unified_harness_for_covered_task() -> None:
    decision = AgentOrchestrator().decide(
        has_task_state=True,
        harness_contract_covered=True,
    )

    assert decision.route == "unified_harness"
    assert decision.reason == "task_state_present_and_tool_contract_covered"


def test_orchestrator_names_compatibility_fallback_reason() -> None:
    orchestrator = AgentOrchestrator(legacy_fallback_enabled=True)

    missing_state = orchestrator.decide(
        has_task_state=False,
        harness_contract_covered=False,
    )
    missing_contract = orchestrator.decide(
        has_task_state=True,
        harness_contract_covered=False,
    )

    assert (missing_state.route, missing_state.reason) == (
        "compatibility_fallback",
        "task_state_missing",
    )
    assert (missing_contract.route, missing_contract.reason) == (
        "compatibility_fallback",
        "tool_contract_not_migrated",
    )


def test_orchestrator_default_does_not_silently_fallback() -> None:
    decision = AgentOrchestrator().decide(
        has_task_state=False,
        harness_contract_covered=False,
    )

    assert decision.route == "unsupported"
    assert decision.reason == "task_state_missing"


def test_orchestrator_fails_closed_when_fallback_is_disabled() -> None:
    decision = AgentOrchestrator(legacy_fallback_enabled=False).decide(
        has_task_state=True,
        harness_contract_covered=False,
    )

    assert decision.route == "unsupported"
    assert decision.reason == "tool_contract_not_migrated"


def test_public_entry_uses_unified_handler_without_legacy_loop() -> None:
    from app.llm import run_agent

    state = SimpleNamespace(task_id="task-1", task_type="local_life")
    expected = ("ok", [], [], "run-1", None)
    with (
        patch("app.llm._is_harness_contract_covered", return_value=True),
        patch("app.llm._run_unified_harness_agent", new=AsyncMock(return_value=expected)) as unified,
        patch("app.llm._run_legacy_agent", new=AsyncMock()) as legacy,
        patch("app.llm.settings.agent_orchestrator_mode", "unified"),
        patch("app.llm.settings.agent_legacy_fallback_enabled", True),
    ):
        result = asyncio.run(run_agent("find a phone", task_state=state))

    assert result == expected
    unified.assert_awaited_once()
    legacy.assert_not_awaited()


def test_public_entry_fails_closed_without_migrated_contract() -> None:
    from app.llm import run_agent

    state = SimpleNamespace(task_id="task-2", task_type="local_life")
    with (
        patch("app.llm._is_harness_contract_covered", return_value=False),
        patch("app.llm._run_unified_harness_agent", new=AsyncMock()) as unified,
        patch("app.llm._run_legacy_agent", new=AsyncMock()) as legacy,
        patch("app.llm.settings.agent_orchestrator_mode", "unified"),
        patch("app.llm.settings.agent_legacy_fallback_enabled", False),
    ):
        answer, traces, _messages, run_id, summary = asyncio.run(
            run_agent("unsupported route", task_state=state)
        )

    assert "尚未迁移" in answer
    assert traces == []
    assert run_id is None
    assert summary is None
    unified.assert_not_awaited()
    legacy.assert_not_awaited()


def test_public_entry_keeps_missing_state_fallback_explicit() -> None:
    from app.llm import run_agent

    expected = ("legacy", [], [], None, None)
    with (
        patch("app.llm._run_legacy_agent", new=AsyncMock(return_value=expected)) as legacy,
        patch("app.llm.settings.agent_orchestrator_mode", "unified"),
        patch("app.llm.settings.agent_legacy_fallback_enabled", True),
    ):
        result = asyncio.run(run_agent("介绍一下你的能力", task_state=None))

    assert result == expected
    legacy.assert_awaited_once()


def test_context_pack_failure_stops_before_harness_execution() -> None:
    from app.llm import _run_explicit_harness_agent

    state = SimpleNamespace(task_id="task-pack-fail", revision=3, active_plan=None)
    with (
        patch("app.llm.build_context_pack", new=AsyncMock(side_effect=ValueError("bad pack"))),
        patch("app.llm.run_harness_step", new=AsyncMock()) as harness_step,
        patch("app.llm._persist_trace_safely", new=AsyncMock()),
        patch("app.llm.settings.agent_context_mode", "context_pack"),
    ):
        answer, traces, _messages, run_id, summary = asyncio.run(
            _run_explicit_harness_agent(
                "find coffee",
                history=[],
                client=SimpleNamespace(),
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
            )
        )

    assert "安全停止" in answer
    assert traces == []
    assert run_id is not None
    assert summary is not None
    harness_step.assert_not_awaited()


def test_durable_exact_replay_returns_receipt_without_final_model_call() -> None:
    from app.graph import DurableRunResult
    from app.llm import _run_explicit_harness_agent

    state = SimpleNamespace(task_id="task-replay", revision=8, active_plan=None)
    replay = DurableRunResult(
        graph_state={},
        task_state=state,
        boundary="task_completed",
        mode="idempotent_replay",
        run_id="run-replay",
        thread_id="v2-task:task-replay:run-replay",
        proposal_hash="proposal-replay",
    )
    with (
        patch("app.llm.settings.agent_graph_v2_durable_enabled", True),
        patch("app.llm.settings.agent_context_mode", "legacy"),
        patch("app.graph.resolve_durable_identity", new=AsyncMock(
            return_value=("run-replay", "v2-task:task-replay:run-replay")
        )),
        patch("app.graph.run_graph_v2_durable", new=AsyncMock(return_value=replay)),
        patch("app.graph.read_terminal_response_receipt", new=AsyncMock(
            return_value={"answer": "固定终态回答", "answerHash": "a", "resultHash": "b"}
        )),
        patch("app.llm.get_tool_transport", return_value=AsyncMock()),
        patch("app.llm._generate_final_answer", new=AsyncMock()) as final_model,
        patch("app.llm._persist_trace_safely", new=AsyncMock()),
    ):
        answer, traces, messages, _run_id, summary = asyncio.run(
            _run_explicit_harness_agent(
                "256GB",
                history=[],
                client=SimpleNamespace(),
                task_state=state,
                on_answer_delta=None,
                on_task_state=None,
                session_id="session-a",
                resume={"taskId": "task-replay"},
            )
        )

    assert answer == "固定终态回答"
    assert traces == []
    assert messages == []
    assert summary is not None
    final_model.assert_not_awaited()
