from __future__ import annotations

import asyncio
import pytest

from agent.app.guide_state import (
    GuideRequirement, GuideTurn, GuideV1, ShoppingState, ensure_guide_v1,
    ensure_shopping_state,
    from_catalog_search, transition,
)


def requirement(value: str, *, priority: str = "hard") -> GuideRequirement:
    return GuideRequirement(facet="商品", operator="eq", value=value,
                            priority=priority, source="user")


def test_refine_undo_new_and_cancel_preserve_only_current_semantics():
    start = GuideV1(query="杯子", retrievalQuery="杯子", requirements=[requirement("杯子")])
    refined = transition(start, GuideTurn(route="catalog", action="refine", query="玻璃杯",
                                           requirements=[requirement("玻璃杯")]))
    assert refined.query == "玻璃杯"
    assert len(refined.history) == 1
    undone = transition(refined, GuideTurn(route="catalog", action="undo"))
    assert undone.query == "杯子"
    assert undone.requirements == start.requirements
    assert not undone.history
    new = transition(refined, GuideTurn(route="catalog", action="new", query="耳机",
                                       requirements=[requirement("耳机")]))
    assert not new.history
    assert transition(new, GuideTurn(route="catalog", action="cancel")) == GuideV1()


def test_product_question_cannot_rewrite_search_requirements():
    current = GuideV1(query="杯子", requirements=[requirement("杯子")])
    turn = GuideTurn(route="product", action="inspect", query="恶意覆盖",
                     requirements=[requirement("手机")], numbers=[1])
    assert transition(current, turn) == current


def test_numeric_and_negative_requirement_contract():
    with pytest.raises(ValueError):
        GuideRequirement(facet="容量", operator="gte", value="888", priority="hard", source="user")
    with pytest.raises(ValueError):
        GuideRequirement(facet="品牌", operator="not_in", value="苹果", priority="hard", source="user")
    from agent.app.guide_state import catalog_projection
    budget = GuideRequirement(facet="预算", operator="lte", value=3000, unit="元",
                              priority="hard", source="user")
    projected = catalog_projection(GuideV1(query="手机", requirements=[budget]))
    assert projected["requirements"][0]["value"] == "3000元以内"


def test_catalog_migration_preserves_history_and_rejects_invalid_scope():
    from agent.app.catalog_service import fingerprint

    scope = {"commerceAuthority": False, "groups": []}
    scope["scopeId"] = fingerprint({k: v for k, v in scope.items() if k != "scopeId"})
    raw = {"query": "杯子", "scope": scope,
           "requirements": [{"facet": "商品", "mode": "require", "value": "杯子", "terms": []}],
           "history": [{"query": "碗", "scope": None, "requirements": []}]}
    migrated = from_catalog_search(raw)
    assert migrated is not None and migrated.query == "杯子"
    assert migrated.history[0].query == "碗"
    assert migrated.requirements[0].source == "migration:catalogSearch"
    raw["scope"]["scopeId"] = "forged"
    with pytest.raises(ValueError, match="document_scope_integrity_failed"):
        from_catalog_search(raw)


def test_old_display_scope_is_not_trusted_without_task_bound_evidence(monkeypatch):
    from agent.app import guide_interpreter, task_state
    from agent.app.catalog_service import fingerprint
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="stale-scope-session"))
        value = {"query": "杯子", "groups": [], "commerceAuthority": False}
        scope = {**value, "scopeId": fingerprint(value)}
        migrated = await ensure_guide_v1(state, catalog_search={
            "query": "杯子", "requirements": [], "history": [], "scope": scope})
        shopping = await ensure_shopping_state(migrated)
        view = guide_interpreter._catalog_workspace_view(shopping, {"catalogSearch": {
            "query": "杯子", "requirements": [], "history": [], "scope": scope}})
        assert migrated.domain_state["guideV1Migration"]["needsRequery"] is True
        assert view["catalogSearch"]["scope"] is None

    asyncio.run(scenario())


def test_migration_writes_one_revision_and_is_idempotent(monkeypatch):
    from agent.app import task_state
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="找一个杯子", sessionId="guide-test-session",
        ))
        migrated = await ensure_guide_v1(state, catalog_search={"query": "玻璃杯", "requirements": []})
        assert migrated.revision == state.revision + 1
        assert migrated.domain_state["guideV1"]["query"] == "玻璃杯"
        again = await ensure_guide_v1(migrated, catalog_search={"query": "错误的旧需求"})
        assert again.revision == migrated.revision
        assert again.domain_state["guideV1"]["query"] == "玻璃杯"

    asyncio.run(scenario())


def test_migration_preserves_unconvertible_legacy_condition_and_marks_clarification(monkeypatch):
    from agent.app import task_state
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="legacy-partial-session"))
        migrated = await ensure_guide_v1(state, catalog_search={
            "query": "杯子", "requirements": [
                {"facet": "商品", "mode": "require", "value": "杯子", "terms": []},
                {"facet": "预算", "mode": "unknown", "value": "3000以内"}],
            "history": []})
        assert migrated.domain_state["guideV1"]["requirements"][0]["value"] == "杯子"
        marker = migrated.domain_state["guideV1Migration"]
        assert marker["needsClarification"] is True
        assert marker["unconverted"][0]["value"] == "3000以内"

    asyncio.run(scenario())


def test_shopping_guide_migration_keeps_typed_legacy_requirement():
    from agent.app.guide_state import from_shopping_guide

    migrated = from_shopping_guide({
        "mode": "recommend", "category": "phone", "useCases": [],
        "requirements": [{"key": "price_minor", "operator": "lte", "value": 300000,
                          "unit": "CNY_MINOR", "priority": "hard", "source": "user"}],
        "candidateIds": [], "comparedIds": [], "evidenceStatus": "missing",
    }, goal="找手机")
    assert migrated is not None
    assert migrated.requirements[0].value == "手机"
    assert migrated.requirements[1].operator == "lte"
    assert migrated.requirements[1].value == 3000
    assert migrated.requirements[1].unit == "元"


def test_interpreter_commits_one_model_decision(monkeypatch):
    from agent.app import guide_interpreter, task_state
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    calls = []

    async def fake_plan(message, workspace, *, repair_reason=None):
        calls.append((message, workspace["catalogSearch"]["query"]))
        return {
            "route": "catalog", "action": "search", "query": "玻璃杯",
            "retrievalQuery": "玻璃杯", "requirements": [
                {"facet": "商品", "mode": "require", "value": "玻璃杯", "terms": ["玻璃杯"]}
            ], "numbers": [], "question": "", "followup": "none",
            "accessory": "", "referenceModel": "",
        }, {"model": "test", "usage": {"total_tokens": 8}}

    monkeypatch.setattr(guide_interpreter, "plan_turn", fake_plan)

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="玻璃杯", sessionId="interpreter-session",
        ))
        updated, plan, receipt = await guide_interpreter.interpret_and_commit(
            "玻璃杯", state, turn_id="turn-1",
        )
        assert len(calls) == 1
        assert updated.domain_state["shopping"]["query"] == "玻璃杯"
        assert updated.domain_state["shopping"]["history"] == []
        assert "guideV1" not in updated.domain_state
        assert updated.domain_state["guideTurnDecision"]["turnId"] == "turn-1"
        assert plan["route"] == "catalog" and receipt["model"] == "test"
        repeated, _, _ = await guide_interpreter.interpret_and_commit(
            "玻璃杯", updated, turn_id="turn-1",
        )
        assert repeated.revision == updated.revision and len(calls) == 1

    asyncio.run(scenario())


def test_shopping_migration_priority_and_expiry(monkeypatch):
    from datetime import datetime, timezone
    from agent.app import guide_state, task_state
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="当前输入", sessionId="priority-session",
            domainState={"guideV1": GuideV1(query="旧需求", retrievalQuery="旧需求")
                .model_dump(by_alias=True, mode="json")}))
        migrated = await ensure_shopping_state(state, catalog_search={"query": "更旧的目录", "requirements": []})
        assert migrated.domain_state["shopping"]["query"] == "旧需求"
        assert migrated.domain_state["shoppingMigration"]["source"] == "guideV1"
        assert (await ensure_shopping_state(migrated)).revision == migrated.revision

        old = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="归档旧输入", sessionId="expired-session"))
        monkeypatch.setattr(guide_state, "LEGACY_READ_UNTIL", datetime(2020, 1, 1, tzinfo=timezone.utc))
        expired = await ensure_shopping_state(old, catalog_search={"query": "不能复活", "requirements": []})
        assert expired.domain_state["shopping"]["query"] == ""
        assert expired.domain_state["shoppingMigration"]["source"] == "legacy_expired"

    with guide_state.bind_guide_write():
        asyncio.run(scenario())


def test_first_shopping_turn_does_not_create_fictitious_undo_history():
    fresh = ShoppingState()
    first = transition(fresh, GuideTurn(route="catalog", action="search", query="玻璃杯"))
    assert first.history == []
    assert transition(first, GuideTurn(route="catalog", action="undo")) == first


def test_interpreter_repairs_invalid_structure_once(monkeypatch):
    from agent.app import guide_interpreter, task_state
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    reasons = []

    async def fake_plan(_message, _workspace, *, repair_reason=None):
        reasons.append(repair_reason)
        if repair_reason is None:
            raise ValueError("missing select_search_action")
        return {"route": "catalog", "action": "search", "query": "杯子",
                "requirements": [{"facet": "商品", "mode": "require", "value": "杯子", "terms": []}]}, {}

    monkeypatch.setattr(guide_interpreter, "plan_turn", fake_plan)

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="repair-session",
        ))
        updated, _, _ = await guide_interpreter.interpret_and_commit("杯子", state, turn_id="repair-1")
        assert len(reasons) == 2 and reasons[0] is None and "missing" in reasons[1]
        assert updated.domain_state["shopping"]["query"] == "杯子"

    asyncio.run(scenario())


def test_second_invalid_interpretation_stops_without_semantic_write(monkeypatch):
    from agent.app import guide_interpreter, task_state
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    attempts = []

    async def invalid(_message, _workspace, *, repair_reason=None):
        attempts.append(repair_reason)
        raise ValueError("invalid model payload")

    monkeypatch.setattr(guide_interpreter, "plan_turn", invalid)

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="invalid-guide-session"))
        migrated = await ensure_shopping_state(state)
        with pytest.raises(guide_interpreter.GuideInterpretationError):
            await guide_interpreter.interpret_and_commit("找杯子", migrated, turn_id="invalid-1")
        latest = await task_state.get_task_state(state.task_id)
        assert latest.revision == migrated.revision
        assert "guideTurnDecision" not in latest.domain_state
        assert len(attempts) == 2

    asyncio.run(scenario())


def test_concurrent_guide_turns_do_not_overwrite_each_other(monkeypatch):
    from agent.app import guide_interpreter, task_state
    from agent.app.task_state import TaskStateCreateRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()
    arrived = 0
    ready = asyncio.Event()

    async def plan(message, _workspace, *, repair_reason=None):
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            ready.set()
        await ready.wait()
        return {"route": "catalog", "action": "new", "query": message,
                "retrievalQuery": message, "requirements": [], "numbers": [],
                "question": "", "followup": "none"}, {}

    monkeypatch.setattr(guide_interpreter, "plan_turn", plan)

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="concurrent-guide-session"))
        migrated = await ensure_shopping_state(state)
        results = await asyncio.gather(
            guide_interpreter.interpret_and_commit("找杯子", migrated, turn_id="concurrent-1"),
            guide_interpreter.interpret_and_commit("找耳机", migrated, turn_id="concurrent-2"),
            return_exceptions=True)
        assert sum(isinstance(result, guide_interpreter.GuideRevisionConflictError)
                   for result in results) == 1
        latest = await task_state.get_task_state(migrated.task_id)
        assert latest.domain_state["shopping"]["query"] in {"找杯子", "找耳机"}

    asyncio.run(scenario())


def test_generic_task_patch_cannot_write_canonical_guide(monkeypatch):
    from agent.app import task_state
    from agent.app.task_state import TaskStateCreateRequest, TaskStatePatchRequest
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="protected-guide-session"))
        with pytest.raises(ValueError, match="server_owned_transition"):
            await task_state.update_task_state(state.task_id, TaskStatePatchRequest(
                expectedRevision=state.revision, actor="agent",
                domainStatePatch={"guideV1": {"query": "伪造"}}))
        assert (await task_state.get_task_state(state.task_id)).revision == state.revision

    asyncio.run(scenario())


def test_guide_evidence_is_bound_to_task_revision_and_scope_hash(monkeypatch):
    from agent.app import task_state
    from agent.app.guide_evidence import publish_scope, read_scope
    from agent.app.task_state import TaskStateCreateRequest
    from agent.app.catalog_service import fingerprint
    from tests.fake_redis import FakeRedis

    monkeypatch.setattr(task_state, "_client", FakeRedis())
    task_state._task_locks.clear()

    async def scenario():
        state = await task_state.create_task_state(TaskStateCreateRequest(
            taskType="ecommerce_guide", goal="杯子", sessionId="evidence-session",
        ))
        scope = {"query": "杯子", "groups": [], "commerceAuthority": False}
        scope["scopeId"] = fingerprint(scope)
        published = await publish_scope(state, scope, run_id="run-1")
        assert published.revision == state.revision + 1
        assert await read_scope(published) == scope
        tampered = published.model_copy(update={"domain_state": {
            **published.domain_state,
            "guideEvidenceRef": {**published.domain_state["guideEvidenceRef"], "scopeSha256": "wrong"},
        }})
        with pytest.raises(ValueError, match="reference mismatch"):
            await read_scope(tampered)

    asyncio.run(scenario())
