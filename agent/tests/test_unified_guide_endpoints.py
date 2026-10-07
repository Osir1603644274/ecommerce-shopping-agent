"""The public JSON and SSE transports exercise the same interpreter contract."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_json_and_stream_share_guide_state(monkeypatch):
    from app import guide_interpreter, main, session_memory, task_state
    from app.settings import settings
    from tests.fake_redis import FakeRedis

    redis = FakeRedis()
    monkeypatch.setattr(task_state, "_client", redis)
    monkeypatch.setattr(session_memory, "_client", redis)
    task_state._task_locks.clear()
    task_state._session_locks.clear()
    monkeypatch.setattr(settings, "catalog_workspace_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    messages = []

    async def plan(message, _view, *, repair_reason=None):
        messages.append(message)
        return {"route": "catalog", "action": "cancel", "query": "", "numbers": [],
                "question": "", "requirements": [], "followup": "none"}, {"usage": {"total_tokens": 5}}

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)
    client = TestClient(main.app)
    first = client.post("/agent/chat-llm", json={"message": "取消搜索", "sessionId": "guide-endpoint-test"})
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["answer"] == "已取消当前商品搜索。你可以提出新的需求。"
    assert body["taskState"]["domainState"]["shopping"]["history"] == []
    assert "guideV1" not in body["taskState"]["domainState"]

    streamed = client.post("/agent/chat-llm/stream", json={
        "message": "取消当前搜索", "sessionId": "guide-endpoint-test"})
    assert streamed.status_code == 200, streamed.text
    events = [json.loads(line[6:]) for line in streamed.text.splitlines() if line.startswith("data: ")]
    complete = next(event["data"] for event in events if event["type"] == "complete")
    assert complete["answer"] == body["answer"]
    assert complete["taskState"]["taskId"] == body["taskState"]["taskId"]
    assert messages == ["取消搜索", "取消当前搜索"]


def test_debug_steps_use_the_same_interpreter(monkeypatch):
    from app import guide_interpreter, main, session_memory, task_state
    from app.step_debug import DebugTurnStore
    from app.settings import settings
    from tests.fake_redis import FakeRedis

    redis = FakeRedis()
    monkeypatch.setattr(task_state, "_client", redis)
    monkeypatch.setattr(session_memory, "_client", redis)
    task_state._task_locks.clear()
    task_state._session_locks.clear()
    monkeypatch.setattr(settings, "catalog_workspace_enabled", True)
    store = DebugTurnStore(redis)
    monkeypatch.setattr(main, "get_debug_turn_store", lambda: store)
    calls = []

    async def plan(message, _view, *, repair_reason=None):
        calls.append(message)
        return {"route": "catalog", "action": "cancel", "query": "", "numbers": [],
                "question": "", "requirements": [], "followup": "none"}, {}

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)
    client = TestClient(main.app)
    created = client.post("/agent/debug-turns", json={
        "message": "取消搜索", "sessionId": "debug-guide-session", "domainHint": "ecommerce"})
    assert created.status_code == 201, created.text
    turn = created.json()
    for _ in range(3):
        stepped = client.post(f"/agent/debug-turns/{turn['debugTurnId']}/step",
                              json={"expectedRevision": turn["revision"]})
        assert stepped.status_code == 200, stepped.text
        turn = stepped.json()
    assert turn["status"] == "completed"
    assert turn["finalAnswer"] == "已取消当前商品搜索。你可以提出新的需求。"
    assert calls == ["取消搜索"]


def test_durable_catalog_turn_uses_the_shared_interpreter(monkeypatch):
    from app import guide_interpreter, main, session_memory, task_state
    from app.settings import settings
    from tests.fake_redis import FakeRedis

    redis = FakeRedis()
    monkeypatch.setattr(task_state, "_client", redis)
    monkeypatch.setattr(session_memory, "_client", redis)
    task_state._task_locks.clear()
    task_state._session_locks.clear()
    monkeypatch.setattr(settings, "catalog_workspace_enabled", True)
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(settings, "agent_graph_v2_durable_enabled", True)
    calls = []

    async def plan(message, _view, *, repair_reason=None):
        calls.append(message)
        return {"route": "catalog", "action": "cancel", "query": "", "numbers": [],
                "question": "", "requirements": [], "followup": "none"}, {}

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)
    response = TestClient(main.app).post("/agent/chat-llm-durable", json={
        "message": "取消搜索", "sessionId": "durable-guide-session"})
    assert response.status_code == 200, response.text
    assert response.json()["answer"] == "已取消当前商品搜索。你可以提出新的需求。"
    assert calls == ["取消搜索"]
