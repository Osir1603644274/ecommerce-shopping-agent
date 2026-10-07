from __future__ import annotations

import asyncio


def test_direct_search_uses_one_interpretation_and_publishes_bound_evidence(monkeypatch):
    from agent.app import guide_execution, guide_interpreter, guide_runtime, task_state
    from agent.app.catalog_service import fingerprint
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    calls = []

    async def plan(_message, _view, *, repair_reason=None):
        calls.append("interpret")
        return {"route": "catalog", "action": "search", "query": "玻璃杯",
                "retrievalQuery": "玻璃杯", "requirements": [], "numbers": [],
                "question": "", "followup": "none"}, {"usage": {"total_tokens": 9}}

    class Search:
        async def search(self, query, **kwargs):
            calls.append("search")
            value = {"query": query, "groups": [], "commerceAuthority": False}
            return {**value, "scopeId": fingerprint(value)}

    async def answer(_message, _plan, _current):
        calls.append("answer")
        return "当前没有可展示的候选。", None

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)
    monkeypatch.setattr(guide_execution, "get_catalog_service", lambda: Search())
    monkeypatch.setattr(guide_execution, "answer_turn", answer)

    async def scenario():
        task = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="玻璃杯", sessionId="direct-guide-session"))
        updated, result = await guide_runtime.execute_guide_turn(
            "玻璃杯", task, session_id="direct-guide-session", turn_id="direct-1")
        assert result[0] == "当前没有可展示的候选。"
        assert calls == ["interpret", "search", "answer"]
        assert updated.domain_state["shopping"]["query"] == "玻璃杯"
        assert updated.domain_state["guideEvidenceRef"]["taskId"] == updated.task_id
        assert updated.domain_state["guideTurnDecision"]["modelCall"]["usage"]["total_tokens"] == 9

    asyncio.run(scenario())


def test_same_scope_can_be_published_at_a_later_task_revision(monkeypatch):
    from agent.app import task_state
    from agent.app.catalog_service import fingerprint
    from agent.app.guide_evidence import publish_scope, read_scope
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()

    async def scenario():
        task = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="same-scope-session"))
        value = {"query": "杯子", "groups": [], "commerceAuthority": False}
        scope = {**value, "scopeId": fingerprint(value)}
        first = await publish_scope(task, scope, run_id="r1")
        second = await publish_scope(first, scope, run_id="r2")
        assert await read_scope(second) == scope
        assert second.domain_state["guideEvidenceRef"]["baseRevision"] == first.revision

    asyncio.run(scenario())


def test_run_agent_uses_the_shared_guide_interpreter(monkeypatch):
    from agent.app import guide_interpreter, guide_runtime, llm, task_state
    from agent.app.settings import settings
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(settings, "catalog_workspace_enabled", True)
    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    seen = []

    async def plan(_message, _view, *, repair_reason=None):
        seen.append("parse")
        return {"route": "catalog", "action": "cancel", "query": "", "numbers": [],
                "question": "", "requirements": [], "followup": "none"}, {"usage": {"total_tokens": 7}}

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="run-guide-session"))
        answer, traces, _, _, _ = await llm.run_agent(
            "取消搜索", task_state=state, domain_hint="ecommerce", session_id="run-guide-session")
        assert answer == "已取消当前商品搜索。你可以提出新的需求。"
        assert seen == ["parse"]
        assert traces[0].tool == "answer_catalog"

    asyncio.run(scenario())


def test_refine_then_undo_restores_previous_query_without_model_claims(monkeypatch):
    from agent.app import guide_execution, guide_interpreter, guide_runtime, task_state
    from agent.app.catalog_service import fingerprint
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    decisions = [
        {"route": "catalog", "action": "search", "query": "玻璃杯", "retrievalQuery": "玻璃杯",
         "requirements": [], "numbers": [], "question": "", "followup": "none"},
        {"route": "catalog", "action": "refine", "query": "带盖玻璃杯", "retrievalQuery": "玻璃杯 带盖",
         "requirements": [], "numbers": [], "question": "", "followup": "none"},
        {"route": "catalog", "action": "undo", "query": "", "requirements": [],
         "numbers": [], "question": "", "followup": "none"},
    ]

    async def plan(_message, _view, *, repair_reason=None):
        return decisions.pop(0), {}

    class Search:
        async def search(self, query, **kwargs):
            value = {"query": query, "groups": [], "commerceAuthority": False}
            return {**value, "scopeId": fingerprint(value)}

    seen_actions = []
    async def answer(_message, _plan, _current):
        seen_actions.append(_plan["action"])
        return (("已撤销上次需求修改，当前需求：" + _current["query"] + "。\n\n")
                if _plan["action"] == "undo" else "") + "候选列表", None

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)
    monkeypatch.setattr(guide_execution, "get_catalog_service", lambda: Search())
    monkeypatch.setattr(guide_execution, "answer_turn", answer)

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="undo-guide-session"))
        for i, message in enumerate(("找玻璃杯", "要带盖", "撤销上次修改"), 1):
            state, result = await guide_runtime.execute_guide_turn(
                message, state, session_id="undo-guide-session", turn_id=f"undo-{i}")
        assert state.domain_state["shopping"]["query"] == "玻璃杯"
        assert result[0].startswith("已撤销上次需求修改，当前需求：玻璃杯")
        assert seen_actions == ["search", "refine", "undo"]
        assert not decisions

    asyncio.run(scenario())


def test_verified_listing_respects_presentation_limit():
    from agent.app.catalog_conversation import render_documents

    scope = {"groups": [{"number": i, "title": f"杯子{i}",
                         "members": [{"source": "kuaisearch", "docid": f"kuaisearch:{i}",
                                      "seller": "", "brand": ""}]}
                        for i in range(1, 21)], "presentationLimit": 6}
    rendered = render_documents(scope)
    assert "6. 杯子6" in rendered
    assert "7. 杯子7" not in rendered


def test_direct_retrieval_sends_query_when_requirements_are_empty(monkeypatch):
    from agent.app import guide_execution
    from agent.app.catalog_service import fingerprint
    seen = []

    class Search:
        async def search(self, query, **kwargs):
            seen.append((query, kwargs))
            value = {"query": query, "groups": [], "commerceAuthority": False}
            return {**value, "scopeId": fingerprint(value)}

    monkeypatch.setattr(guide_execution, "get_catalog_service", lambda: Search())
    asyncio.run(guide_execution.retrieve("当前需求", "玻璃杯", []))
    assert seen == [("当前需求", {"requirements": [], "retrieval_query": "玻璃杯"})]


def test_business_turn_reuses_committed_decision_without_second_extractor(monkeypatch):
    from types import SimpleNamespace
    from agent.app import guide_interpreter, llm, task_state
    from agent.app.settings import settings
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(settings, "catalog_workspace_enabled", True)
    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    calls = []

    async def plan(_message, _view, *, repair_reason=None):
        calls.append("parse")
        return {"route": "business", "action": "inspect", "query": "我需要售后帮助",
                "numbers": [], "question": "", "requirements": [], "followup": "none"}, {}

    async def fake_harness(_message, **kwargs):
        calls.append("harness")
        assert kwargs["guide_preinterpreted"] is True
        return "请提供订单号。", [], [], None, None

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)
    monkeypatch.setattr(llm, "_run_unified_harness_agent", fake_harness)
    monkeypatch.setattr(llm, "AgentOrchestrator", lambda **_kwargs: SimpleNamespace(
        decide=lambda **_kwargs: SimpleNamespace(route="unified_harness", reason="test")))

    async def scenario():
        task = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="我需要售后帮助", sessionId="business-guide-session"))
        result = await llm.run_agent("我需要售后帮助", task_state=task,
                                     domain_hint="ecommerce", session_id="business-guide-session")
        assert result[0] == "请提供订单号。"
        assert calls == ["parse", "harness"]

    asyncio.run(scenario())


def test_no_result_does_not_invent_a_product(monkeypatch):
    from agent.app import guide_execution, guide_interpreter, guide_runtime, task_state
    from agent.app.catalog_service import fingerprint
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()

    async def plan(_message, _view, *, repair_reason=None):
        return {"route": "catalog", "action": "search", "query": "不存在的商品",
                "retrievalQuery": "不存在的商品", "requirements": [], "numbers": [],
                "question": "", "followup": "none"}, {}

    class EmptySearch:
        async def search(self, query, **_kwargs):
            value = {"query": query, "groups": [], "commerceAuthority": False}
            return {**value, "scopeId": fingerprint(value)}

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)
    monkeypatch.setattr(guide_execution, "get_catalog_service", lambda: EmptySearch())

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="不存在的商品", sessionId="empty-guide-session"))
        _, result = await guide_runtime.execute_guide_turn(
            "不存在的商品", state, session_id="empty-guide-session", turn_id="empty-1")
        assert "没有可展示的候选" in result[0]
        assert "1." not in result[0]

    asyncio.run(scenario())
