import asyncio
import inspect
import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import session_memory, step_debug, task_state
from app.context_pack import ContextPack
from app.main import app
from app.schemas import ToolTrace
from app.settings import settings
from app.step_debug import DebugTurnStore
from tests.fake_redis import FakeRedis
from tests.two_stage_ranking_fixtures import two_stage_search_detail


def _setup_client() -> tuple[TestClient, FakeRedis]:
    fake = FakeRedis()
    session_memory._client = fake
    task_state._client = fake
    task_state._task_locks.clear()
    task_state._session_locks.clear()
    step_debug.set_debug_turn_store(DebugTurnStore(client=fake))
    step_debug.reset_debug_turn_locks()
    return TestClient(app), fake


def _step(client: TestClient, turn: dict) -> dict:
    response = client.post(
        f"/agent/debug-turns/{turn['debugTurnId']}/step",
        json={"expectedRevision": turn["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_debug_turn_advances_one_real_phase_per_click_to_validated_answer():
    client, fake = _setup_client()
    tool = AsyncMock(return_value=ToolTrace(
        tool="search_products",
        ok=True,
        durationMs=12.5,
        detail=two_stage_search_detail([101, 102, 103]),
    ))

    created = client.post(
        "/agent/debug-turns",
        json={
            "message": "想要 iOS 二手机",
            "sessionId": "session-step-debug-happy",
            "domainHint": "ecommerce",
        },
    )
    assert created.status_code == 201
    turn = created.json()
    assert turn["status"] == "queued"
    assert turn["nextStage"] == "task_manager"
    assert turn["steps"] == []
    assert turn["taskId"] is None
    context_pack_key = f"agent-debug-turn:{turn['debugTurnId']}:context-pack"
    assert asyncio.run(fake.get(context_pack_key)) is None

    expected_stages = [
        "task_manager",
        "task_state",
        "context_pack",
        "planner",
        "executor",
        "validator",
        "final_answer",
    ]
    with patch("app.tools.call_tool", new=tool):
        for expected in expected_stages:
            before_revision = turn["revision"]
            turn = _step(client, turn)
            assert turn["revision"] == before_revision + 1
            assert turn["steps"][-1]["stage"] == expected
            assert turn["steps"][-1]["outcome"] == "passed"
            assert len(turn["steps"]) == expected_stages.index(expected) + 1

    assert turn["status"] == "completed"
    assert turn["nextStage"] == "done"
    assert turn["finalAnswer"]
    assert "3 个候选" in turn["finalAnswer"]
    # Browser-facing identities are canonical decimal strings so values above
    # JavaScript's safe-integer boundary cannot be rounded.
    assert turn["guideResult"]["products"][0]["product"]["id"] == "101"
    validator_record = next(
        record for record in turn["steps"] if record["stage"] == "validator"
    )
    assert validator_record["keyState"]["validatedPresentation"] == {
        "productIds": ["101", "102", "103"],
        "productCount": 3,
        "validatorPassed": True,
    }
    task_state_record = next(
        record for record in turn["steps"] if record["stage"] == "task_state"
    )
    guide_state = task_state_record["keyState"]["shoppingGuide"]
    assert guide_state["modeLabel"] == "搜索/推荐"
    assert guide_state["requirements"] == [{
        "key": "os", "operator": "eq", "value": "ios", "unit": "enum",
        "priority": "hard", "source": "user",
    }]
    extraction = task_state_record["keyState"]["taskStateExtraction"]
    assert extraction["executionKind"] == "deterministic"
    assert extraction["modelCalled"] is False
    assert extraction["modelCallCount"] == 0
    planner_record = next(
        record for record in turn["steps"] if record["stage"] == "planner"
    )
    assert planner_record["keyState"]["executionKind"] == "deterministic"
    assert planner_record["keyState"]["modelCalled"] is False
    executor_record = next(
        record for record in turn["steps"] if record["stage"] == "executor"
    )
    step_output = executor_record["keyState"]["stepOutput"]
    assert executor_record["keyState"]["stepOutputValidationStatus"] == (
        "pending_validator"
    )
    assert step_output["taskId"] == turn["taskId"]
    assert step_output["planId"] == planner_record["keyState"]["activePlan"][
        "planId"
    ]
    assert step_output["stepId"] == "step-shopping-action"
    assert step_output["values"]["productIds"] == [101, 102, 103]
    assert step_output["values"]["candidateSupport"]["productPresentations"][0][
        "productId"
    ] == 101
    assert turn["contextPackHash"]
    assert turn["contextTokenCount"] > 0
    tool.assert_awaited_once()

    public_payload = json.dumps(turn, ensure_ascii=False)
    assert "contextPack" not in turn
    assert "systemPrompt" not in public_payload
    assert "apiKey" not in public_payload
    assert "domainState" not in public_payload
    assert '"phases"' not in public_payload
    assert '"phase_fields"' not in public_payload
    assert "agent/app/llm.py" in public_payload
    stored_pack = asyncio.run(fake.get(context_pack_key))
    assert stored_pack is not None
    assert turn["contextPackHash"] not in stored_pack
    pack = ContextPack.model_validate_json(stored_pack)
    expected_identity = {
        "contextPolicyId": pack.context_policy_id,
        "contextPolicyVersion": pack.context_policy_version,
        "contextSkillId": pack.context_skill_id,
        "contextSkillVersion": pack.context_skill_version,
    }
    for stage in ("context_pack", "planner", "executor", "validator", "final_answer"):
        record = next(item for item in turn["steps"] if item["stage"] == stage)
        assert record["keyState"]["contextIdentity"] == expected_identity


def test_debug_executor_uses_mcp_resolver_instead_of_direct_live():
    client, _fake = _setup_client()
    direct_live = AsyncMock(return_value=ToolTrace(
        tool="search_products",
        ok=True,
        detail=two_stage_search_detail([201, 202, 203]),
    ))
    resolved_live = AsyncMock(return_value=ToolTrace(
        tool="search_products",
        ok=True,
        detail=two_stage_search_detail([201, 202, 203]),
    ))
    turn = client.post(
        "/agent/debug-turns",
        json={
            "message": "想要 iOS 二手机",
            "sessionId": "session-step-debug-mcp-resolver",
            "domainHint": "ecommerce",
        },
    ).json()

    with patch.object(settings, "agent_tool_transport_mode", "mcp_in_process_readonly"), \
        patch.object(settings, "agent_mcp_readonly_enabled", True), \
        patch.object(settings, "agent_mcp_transport_mode", "in_process_readonly"), \
        patch("app.main.call_tool", new=direct_live), \
        patch("app.tools.call_tool", new=resolved_live):
        for _ in range(4):
            turn = _step(client, turn)
        assert turn["nextStage"] == "executor"
        turn = _step(client, turn)

    executor_record = turn["steps"][-1]
    assert executor_record["stage"] == "executor"
    direct_live.assert_not_awaited()
    resolved_live.assert_awaited_once()
    assert executor_record["keyState"]["toolTransportIdentity"] == {
        "mode": "mcp_in_process_readonly",
        "source": "server_resolver",
        "protocolVersion": "2026-07-28",
        "serverName": "ecommerce-readonly-gateway",
        "serverVersion": "1.0.0",
    }
    reread = client.get(
        f"/agent/debug-turns/{turn['debugTurnId']}"
    ).json()
    reread_executor = next(
        item for item in reread["steps"] if item["stage"] == "executor"
    )
    assert reread_executor["keyState"]["toolTransportIdentity"] == (
        executor_record["keyState"]["toolTransportIdentity"]
    )


def test_debug_replanner_has_no_direct_call_tool_seam():
    import app.main

    source = inspect.getsource(app.main._advance_debug_turn)
    assert "tool_caller=call_tool" not in source
    assert "get_tool_transport" in source


def test_debug_turn_stale_revision_is_rejected_without_advancing():
    client, _fake = _setup_client()
    turn = client.post(
        "/agent/debug-turns",
        json={
            "message": "想要 iOS 二手机",
            "sessionId": "session-step-debug-occ",
            "domainHint": "ecommerce",
        },
    ).json()
    stale_revision = turn["revision"]
    turn = _step(client, turn)

    response = client.post(
        f"/agent/debug-turns/{turn['debugTurnId']}/step",
        json={"expectedRevision": stale_revision},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "debug_turn_revision_conflict"
    current = client.get(
        f"/agent/debug-turns/{turn['debugTurnId']}"
    ).json()
    assert current["revision"] == turn["revision"]
    assert len(current["steps"]) == 1


def test_debug_turn_cancel_is_terminal_and_does_not_run_next_stage():
    client, _fake = _setup_client()
    turn = client.post(
        "/agent/debug-turns",
        json={
            "message": "想要 iOS 二手机",
            "sessionId": "session-step-debug-cancel",
            "domainHint": "ecommerce",
        },
    ).json()

    cancelled = client.post(
        f"/agent/debug-turns/{turn['debugTurnId']}/cancel",
        json={"expectedRevision": turn["revision"]},
    )

    assert cancelled.status_code == 200
    payload = cancelled.json()
    assert payload["status"] == "cancelled"
    assert payload["nextStage"] == "done"
    assert payload["taskId"] is None
    assert payload["steps"][0]["outcome"] == "cancelled"

    terminal = client.post(
        f"/agent/debug-turns/{turn['debugTurnId']}/step",
        json={"expectedRevision": payload["revision"]},
    )
    assert terminal.status_code == 409
    assert terminal.json()["detail"]["code"] == "debug_turn_terminal"


def test_debug_turn_rejects_transaction_confirmation_before_business_tool():
    client, _fake = _setup_client()
    tool = AsyncMock()
    turn = client.post(
        "/agent/debug-turns",
        json={
            "message": "确认下单",
            "sessionId": "session-step-debug-transaction",
            "domainHint": "ecommerce",
        },
    ).json()

    with patch("app.main.call_tool", new=tool):
        failed = _step(client, turn)

    assert failed["status"] == "failed"
    assert failed["nextStage"] == "done"
    assert failed["steps"][0]["stage"] == "task_manager"
    assert "read-only" in failed["steps"][0]["message"]
    tool.assert_not_awaited()


def test_debug_greeting_finishes_without_task_state_model_call():
    client, _fake = _setup_client()
    turn = client.post(
        "/agent/debug-turns",
        json={
            "message": "你好呀",
            "sessionId": "session-step-debug-greeting",
            "domainHint": "ecommerce",
        },
    ).json()

    turn = _step(client, turn)
    assert turn["steps"][-1]["stage"] == "task_manager"
    assert turn["nextStage"] == "final_answer"
    key_state = turn["steps"][-1]["keyState"]
    assert key_state["confidenceRange"] == [0, 1]
    assert key_state["modelCalled"] is False
    assert key_state["answerSource"] == "task_manager.deterministic_smalltalk"

    turn = _step(client, turn)
    assert turn["status"] == "completed"
    assert [item["stage"] for item in turn["steps"]] == [
        "task_manager", "final_answer",
    ]
    assert "你好呀" in turn["finalAnswer"]
    assert all("contextIdentity" not in item["keyState"] for item in turn["steps"])


def test_debug_context_identity_drift_is_controlled_before_public_save():
    client, fake = _setup_client()
    turn = client.post(
        "/agent/debug-turns",
        json={
            "message": "想要 iOS 二手机",
            "sessionId": "session-step-debug-identity-drift",
            "domainHint": "ecommerce",
        },
    ).json()
    pack = ContextPack(
        runId=turn["runId"],
        taskId="task-identity-drift",
        baseContextRevision=1,
        goal="identity test",
        taskType="ecommerce_guide",
    )
    store = step_debug.get_debug_turn_store()
    asyncio.run(
        store.save_context_pack(turn["debugTurnId"], pack.model_dump_json(by_alias=True))
    )
    model = step_debug.DebugTurn.model_validate(turn)
    record = step_debug.completed_step(
        model,
        stage="planner",
        label="Planner",
        code_file="agent/app/harness.py",
        code_function="run_planning_step",
        outcome="passed",
        duration_ms=1,
        key_state={
            "contextIdentity": {
                "contextPolicyId": "wrong-policy",
                "contextPolicyVersion": "1.0",
                "contextSkillId": "wrong-skill",
                "contextSkillVersion": "1.0",
            },
        },
    )
    asyncio.run(store.save(model.model_copy(update={"steps": [record]})))
    public = client.get(f"/agent/debug-turns/{turn['debugTurnId']}").json()
    assert public["steps"][0]["keyState"]["contextIdentity"] == {
        "status": "error",
        "code": "context_identity_mismatch",
    }


def test_debug_transport_identity_drift_is_controlled_on_readback():
    client, _fake = _setup_client()
    turn = client.post(
        "/agent/debug-turns",
        json={
            "message": "想要 iOS 二手机",
            "sessionId": "session-step-debug-transport-drift",
            "domainHint": "ecommerce",
        },
    ).json()
    store = step_debug.get_debug_turn_store()
    model = step_debug.DebugTurn.model_validate(turn)
    record = step_debug.completed_step(
        model,
        stage="executor",
        label="Executor",
        code_file="agent/app/main.py",
        code_function="_advance_debug_turn",
        outcome="passed",
        duration_ms=1,
        key_state={
            "toolTransportIdentity": {
                "mode": "mcp_in_process_readonly",
                "source": "client_settings",
            },
        },
    )
    asyncio.run(store.save(model.model_copy(update={"steps": [record]})))
    public = client.get(f"/agent/debug-turns/{turn['debugTurnId']}").json()
    assert public["steps"][0]["keyState"]["toolTransportIdentity"] == {
        "status": "error",
        "code": "transport_identity_shape_invalid",
    }
