import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import session_memory, task_state
from app.main import app
from app.schemas import ChatRequest, EcommerceChatRequest, ToolTrace
from tests.fake_redis import FakeRedis


@pytest.fixture(autouse=True)
def isolate_task_state_storage():
    fake_redis = FakeRedis()
    session_memory._client = fake_redis
    task_state._client = fake_redis
    task_state._task_locks.clear()
    task_state._session_locks.clear()
    yield
    task_state._task_locks.clear()
    task_state._session_locks.clear()


def test_old_chat_request_defaults_to_auto_domain():
    assert ChatRequest(message="你好").domain_hint == "auto"
    assert ChatRequest(message="附近咖啡店", domainHint="local_life").domain_hint == "local_life"


def test_primary_request_defaults_to_ecommerce_compatible_auto():
    assert EcommerceChatRequest(message="推荐手机").domain_hint == "auto"


def test_primary_chat_contract_rejects_archived_local_life_hint():
    response = TestClient(app).post(
        "/agent/chat-llm",
        json={"message": "附近有什么咖啡店？", "domainHint": "local_life"},
    )

    assert response.status_code == 422


def test_auto_domain_creates_ecommerce_task_for_local_life_wording():
    async def fake_run_agent(message, history=None, **kwargs):
        state = kwargs["task_state"]
        assert state.task_type == "ecommerce_guide"
        return "请描述商品需求。", [], [], None, None

    with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
        "app.main.run_agent", new=fake_run_agent
    ):
        response = TestClient(app).post(
            "/agent/chat-llm",
            json={"message": "附近有什么咖啡店？", "domainHint": "auto"},
        )

    assert response.status_code == 200
    assert response.json()["taskState"]["taskType"] == "ecommerce_guide"


def test_legacy_local_life_routes_are_marked_deprecated():
    schema = TestClient(app).get("/openapi.json").json()

    assert schema["paths"]["/agent/chat"]["post"]["deprecated"] is True
    assert schema["paths"]["/agent/rag-chat"]["post"]["deprecated"] is True
    assert schema["paths"]["/agent/recommendations/shops"]["get"]["deprecated"] is True


def test_stream_complete_event_does_not_publish_raw_tool_detail_as_guide_result():
    raw_guide = {
        "category": "phone",
        "products": [],
        "comparisonMatrix": [],
        "evidence": [],
        "snapshotNotice": "历史公开数据快照，非实时价格/库存。",
        "hasCompleteMatch": False,
    }

    async def fake_run_agent(
        message, history=None, on_answer_delta=None, domain_hint="auto", **_kwargs
    ):
        assert domain_hint == "ecommerce"
        await on_answer_delta("没有完全匹配。")
        trace = ToolTrace(tool="compare_products", ok=True, detail=raw_guide)
        return (
            "没有完全匹配。",
            [trace],
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "没有完全匹配。"},
            ],
            None,
            None,
        )

    with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
        "app.main.run_agent", new=fake_run_agent
    ):
        response = TestClient(app).post(
            "/agent/chat-llm/stream",
            json={"message": "推荐手机", "domainHint": "ecommerce"},
        )
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    complete = next(event["data"] for event in events if event["type"] == "complete")
    assert complete.get("guideResult") is None
    assert complete["tool_trace"][0]["tool"] == "compare_products"
