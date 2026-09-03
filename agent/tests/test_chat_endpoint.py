import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import _initial_chat_domain_state, _task_relation_direct_answer, app
from app.llm import DEFERRED_TASK_RETENTION_REASON
from app.schemas import ToolTrace
from app.agent_trace import TraceSummary
from app.domains.ecommerce import ShoppingGuideState, ShoppingRequirement
from app.domains.ecommerce.fast_response import set_preview_candidate_cache
from app import session_memory, task_state
from app.task_state import TaskRelationDecision
from tests.fake_redis import FakeRedis
from tests.two_stage_ranking_fixtures import two_stage_search_detail


def _preview_candidate(row_id, selection="full_match"):
    return {
        "id": row_id,
        "product": {
            "id": row_id,
            "title": f"候选手机 {row_id}",
            "brand": "xiaomi",
            "priceStatus": "verified",
            "snapshotPriceMinor": 189900,
            "currency": "CNY",
        },
        "facts": {
            "specifications": {
                "os": "android",
                "screen_originality": "original",
            }
        },
        "checks": [],
        "selectionType": selection,
        "evidenceRefs": [f"product:{row_id}:title"],
    }


def test_deferred_task_retention_has_deterministic_acknowledgement():
    decision = TaskRelationDecision(
        relation="continue_current",
        targetTaskId="headphones-task",
        reason=DEFERRED_TASK_RETENTION_REASON,
        confidence=1.0,
    )
    state = type("State", (), {"goal": "通勤降噪耳机"})()

    answer = _task_relation_direct_answer(decision, state)

    assert answer == (
        "好的，之前的手机任务会继续保留；当前不切换。"
        "等你说“继续手机”时，我再恢复它。"
    )


def _seed_used_phone_task(session_id: str) -> str:
    """Persist a turn-1 used-phone task (camera use case already extracted)."""
    guide = ShoppingGuideState(
        mode="recommend",
        category="phone",
        useCases=["camera_title_claim"],
        requirements=[
            ShoppingRequirement(
                key="screen_originality",
                operator="not_in",
                value=["non_original"],
                unit="enum",
                priority="hard",
                source="user",
            )
        ],
    )

    async def _run() -> str:
        state = await task_state.create_task_state(
            task_state.TaskStateCreateRequest(
                task_type="ecommerce_guide",
                goal="拍照好用的手机",
                session_id=session_id,
                domain_state={
                    "origin": "chat",
                    "turnCount": 1,
                    "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
                },
            )
        )
        client = task_state._client
        await client.set(task_state._session_task_key(session_id), state.task_id)
        await client.zadd(
            task_state._session_tasks_key(session_id), {state.task_id: 1}
        )
        return state.task_id

    return asyncio.run(_run())


def _seed_validator_bound_phone_task(session_id: str) -> str:
    """Persist a used-phone task whose Validator boundary has released a Top-3
    product display (the ``第一个和第二个哪个好`` compare target)."""
    from datetime import datetime, timezone

    from app.domains.ecommerce.ranking_contract import normalize_search_products_detail
    from app.executor import NormalizedStepOutput
    from app.planning import PlanStep, TaskPlan
    from app.validator import StepValidationResult, ValidatorResult
    from tests.two_stage_ranking_fixtures import two_stage_search_detail

    ids = [5989522, 1092185, 5304970]
    values = normalize_search_products_detail(
        two_stage_search_detail(ids),
        requirements=[],
        category="手机",
    ).normalized_values()
    output = NormalizedStepOutput(
        taskId="task-bound-compare",
        planId="plan-search",
        stepId="step-search",
        values=values,
    )
    plan = TaskPlan(
        planId="plan-search",
        basedOnRevision=2,
        status="completed",
        steps=[PlanStep(
            stepId="step-search",
            description="search",
            toolName="search_products",
            arguments={"query": "ios手机"},
            argumentSources={"query": {"kind": "task_goal"}},
            expectedOutput={"requiresProductCandidates": True},
            status="executed",
        )],
    )
    expected_summary = {
        "candidatePoolCount": len(values["candidatePoolIds"]),
        "rankedItemCount": len(values["rankedItemIds"]),
        "candidatePoolIds": values["candidatePoolIds"],
        "rankedItemIds": values["rankedItemIds"],
        "productIds": values["productIds"],
        "evidenceRefs": values["evidenceRefs"],
        **values["candidateSupport"],
    }
    validation = ValidatorResult(
        outcome="passed",
        taskId="task-bound-compare",
        planId="plan-search",
        basedOnRevision=2,
        stepResults=[StepValidationResult(
            stepId="step-search",
            outcome="satisfied",
            expectedOutput={"requiresProductCandidates": True},
            evidenceSummary={"requiresProductCandidates": expected_summary},
        )],
    )
    guide = ShoppingGuideState(
        mode="recommend",
        category="phone",
        useCases=["camera_title_claim"],
        requirements=[],
    )
    now = datetime.now(timezone.utc)
    state = task_state.TaskState(
        taskId="task-bound-compare",
        taskType="ecommerce_guide",
        sessionId=session_id,
        status="ready",
        revision=3,
        goal="ios手机",
        activePlan=plan,
        domainState={
            "shoppingGuide": guide.model_dump(by_alias=True, mode="json"),
            "validationResult": validation.model_dump(by_alias=True, mode="json"),
            "stepOutputs": {"step-search": output.model_dump(
                by_alias=True, mode="json"
            )},
        },
        createdAt=now,
        updatedAt=now,
    )

    async def _run() -> str:
        client = task_state._client
        await client.set(
            task_state._state_key(state.task_id),
            state.model_dump_json(by_alias=True),
        )
        await client.set(task_state._session_task_key(session_id), state.task_id)
        await client.zadd(
            task_state._session_tasks_key(session_id), {state.task_id: 1}
        )
        return state.task_id

    return asyncio.run(_run())


class ChatEndpointTests(unittest.TestCase):

    def setUp(self):
        fake_redis = FakeRedis()
        session_memory._client = fake_redis
        task_state._client = fake_redis
        task_state._task_locks.clear()
        task_state._session_locks.clear()
        set_preview_candidate_cache(None)
        self.client = TestClient(app)

    def test_chat_page_can_expand_review_sources_and_scores(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertIn("查看检索来源与相似度", response.text)
        self.assertIn("review.score", response.text)
        self.assertIn("renderTrace(bubble, data.tool_trace)", response.text)
        self.assertIn("renderRequestTrace(bubble, data.trace)", response.text)
        self.assertIn("renderBackendFlow(bubble, data)", response.text)
        self.assertIn("attributeBadgeClass(item)", response.text)
        self.assertIn(
            'item.key === "screen_originality" && item.value === "non_original"',
            response.text,
        )
        self.assertIn("单步调试：关", response.text)
        self.assertIn("advanceDebugTurn", response.text)
        self.assertIn("/agent/debug-turns", response.text)
        self.assertIn("当前单步流程", response.text)
        self.assertIn("renderEmbeddedDebugFlow", response.text)
        self.assertIn("panel.appendChild(renderEmbeddedDebugFlow(turn))", response.text)
        self.assertIn("step-debug-track", response.text)
        self.assertIn("requestFullscreen", response.text)
        self.assertIn("turn.nextStage === stage", response.text)
        self.assertIn('node.setAttribute("aria-current", "step")', response.text)
        self.assertNotIn('id="agent-flow-link"', response.text)
        self.assertNotIn("流程图已移到独立页面", response.text)
        self.assertNotIn('target="_blank"', response.text)
        self.assertIn("publishDebugFlow", response.text)
        self.assertIn("agent-debug-flow-current", response.text)
        self.assertIn("debug-turn-updated", response.text)
        self.assertIn("appendDebugContextIdentity", response.text)
        self.assertIn("appendDebugTransportIdentity", response.text)
        self.assertIn("toolTransportIdentity", response.text)
        self.assertIn("transport-pill", response.text)
        self.assertIn("step-debug-context-identity", response.text)
        self.assertIn("Context Policy：", response.text)
        self.assertIn("Context Skill：", response.text)
        self.assertIn("not-created", response.text)
        self.assertIn("pauseReceipt: receipt", response.text)
        self.assertIn("后台发生了什么", response.text)
        self.assertIn("TaskState", response.text)
        self.assertIn("Planner 规划", response.text)
        self.assertIn("Validator 校验", response.text)
        self.assertIn("debug-panel", response.text)
        self.assertIn("citationsFromToolTrace", response.text)
        self.assertIn("updateDebugPanel(data)", response.text)
        self.assertIn("resetDebugPanel()", response.text)
        self.assertIn("bubble.replaceChildren(renderMarkdown", response.text)
        self.assertIn("appendMarkdownTable", response.text)
        self.assertIn("markdown-table-wrap", response.text)
        self.assertIn("TaskManager · ", response.text)
        self.assertIn('event.type === "task_relation"', response.text)
        self.assertIn("/agent/chat-llm/stream", response.text)
        self.assertIn("readStreamingAnswer", response.text)
        self.assertIn(
            "referenceContext: activeReferenceContextRequest(),\n            resume,",
            response.text,
        )
        self.assertIn("refreshActiveReferenceContext", response.text)
        self.assertIn("completeData.referenceContext", response.text)
        self.assertIn('data-domain-hint="ecommerce"', response.text)
        self.assertIn("领域：商品导购", response.text)
        self.assertNotIn('setDomainHint("local_life")', response.text)
        self.assertNotIn("ecommerce-context", response.text)
        self.assertNotIn("generic-context", response.text)
        self.assertIn("历史 RAG：已归档", response.text)
        self.assertIn("电商导购 Agent", response.text)
        self.assertIn("推荐一台 iOS、优先原装屏的二手手机", response.text)
        self.assertNotIn("推荐一台 3000 元以内的二手 iPhone", response.text)

    def test_iphone_prompt_initializes_phone_guide_without_literal_phone_word(self):
        state = _initial_chat_domain_state(
            "推荐一台 3000 元以内的二手 iPhone",
            task_type="ecommerce_guide",
            origin="chat",
        )

        self.assertEqual(state["shoppingGuide"]["category"], "phone")

    def test_auto_domain_routes_product_request_to_ecommerce_task(self):
        captured_task_types = []

        async def fake_run_agent(
            message,
            history=None,
            on_answer_delta=None,
            **kwargs,
        ):
            captured_task_types.append(kwargs["task_state"].task_type)
            await on_answer_delta("开始商品导购")
            return (
                "开始商品导购",
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "开始商品导购"},
                ],
            )

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=fake_run_agent,
        ):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "推荐一台手机",
                    "sessionId": "session-auto-ecommerce",
                },
            )

        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        complete = next(event["data"] for event in events if event["type"] == "complete")
        self.assertEqual(captured_task_types, ["ecommerce_guide"])
        self.assertEqual(complete["taskState"]["taskType"], "ecommerce_guide")
        self.assertEqual(
            complete["taskState"]["domainState"]["shoppingGuide"],
            {
                "mode": "recommend",
                "category": "phone",
                "useCases": [],
                "requirements": [],
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            },
        )

    def test_started_new_task_ignores_stale_reference_context(self):
        captured_reference_contexts = []

        async def fake_run_agent(
            message,
            history=None,
            on_answer_delta=None,
            **kwargs,
        ):
            captured_reference_contexts.append(kwargs.get("reference_context"))
            await on_answer_delta("新任务已开始")
            return (
                "新任务已开始",
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "新任务已开始"},
                ],
                None,
                None,
            )

        resolver = AsyncMock(side_effect=AssertionError(
            "a new task must not resolve a prior task's reference context"
        ))
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent", new=fake_run_agent,
        ), patch("app.main.resolve_reference_context", new=resolver):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "新任务：推荐三款安卓二手手机",
                    "sessionId": "session-new-task-stale-reference",
                    "referenceContext": {"handle": "A" * 43},
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured_reference_contexts, [None])
        resolver.assert_not_awaited()
        self.assertIn("新任务已开始", response.text)

    def test_auto_domain_routes_find_used_phone_to_phone_guide(self):
        captured = []

        async def fake_run_agent(message, history=None, on_answer_delta=None, **kwargs):
            captured.append(kwargs["task_state"])
            await on_answer_delta("开始检索二手机")
            return (
                "开始检索二手机",
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "开始检索二手机"},
                ],
                None,
                None,
            )

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent", new=fake_run_agent,
        ):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "想找 iOS 二手机。",
                    "sessionId": "session-auto-used-phone",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].task_type, "ecommerce_guide")
        self.assertEqual(
            captured[0].domain_state["shoppingGuide"]["category"], "phone"
        )

    def test_local_life_wording_stays_in_primary_ecommerce_domain(self):
        captured_task_types = []

        async def fake_run_agent(message, history=None, **kwargs):
            captured_task_types.append(kwargs["task_state"].task_type)
            return (
                "请描述你要购买的商品。",
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "请描述你要购买的商品。"},
                ],
                None,
                None,
            )

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=fake_run_agent,
        ):
            response = self.client.post(
                "/agent/chat-llm",
                json={
                    "message": "附近哪里买降噪耳机？",
                    "sessionId": "session-ambiguous-domain",
                },
            )

        payload = response.json()
        self.assertEqual(payload["taskRelation"]["relation"], "start_new")
        self.assertEqual(payload["taskState"]["taskType"], "ecommerce_guide")
        self.assertEqual(captured_task_types, ["ecommerce_guide"])
        self.assertEqual(payload["tool_trace"], [])

    def test_chat_llm_stream_returns_deltas_and_complete_payload(self):
        captured = {}

        async def fake_run_agent(message, history=None, on_answer_delta=None, **_kwargs):
            self.assertEqual(message, "请流式回答")
            self.assertEqual(history, [])
            captured["task_state"] = _kwargs["task_state"]
            await on_answer_delta("第一段")
            await on_answer_delta("，第二段")
            answer = "第一段，第二段"
            return (
                answer,
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": answer},
                ],
                None,
                None,
            )

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=fake_run_agent,
        ):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={"message": "请流式回答"},
                headers={"Accept": "text/event-stream"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[0]["type"], "status")
        self.assertEqual(
            [event["delta"] for event in events if event["type"] == "delta"],
            ["第一段", "，第二段"],
        )
        complete = next(event["data"] for event in events if event["type"] == "complete")
        self.assertEqual(complete["answer"], "第一段，第二段")
        self.assertEqual(complete["trace"]["route"], "/agent/chat-llm/stream")
        self.assertEqual(complete["trace"]["status"], "ok")
        self.assertIsNone(captured["task_state"].session_id)
        self.assertEqual(
            captured["task_state"].domain_state["origin"],
            "ephemeral_chat",
        )

    def test_json_and_stream_expose_only_safe_memory_load_summary(self):
        from app.api.memory_bff import MemoryRunResolution
        from app.memory.v3_runtime import empty_memory_run_binding

        summary = {
            "reason": "available_empty",
            "durationMs": 1.25,
            "eligible": True,
            "attempted": True,
            "projectionOutcome": "available",
            "retainedCount": 0,
            "truncated": False,
        }
        resolution = MemoryRunResolution(
            empty_memory_run_binding("phone", "used-phone-439-09807c773ce6"),
            summary,
        )
        received_bindings = []

        async def fake_run(message, history=None, on_answer_delta=None, **kwargs):
            received_bindings.append(kwargs.get("memory_run_binding"))
            if on_answer_delta is not None:
                await on_answer_delta("完成")
            return "完成", [], [
                {"role": "user", "content": message},
                {"role": "assistant", "content": "完成"},
            ], None, None

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.resolve_memory_run_for_browser_session",
            new=AsyncMock(return_value=resolution),
        ), patch("app.main.run_agent", new=fake_run):
            regular = self.client.post(
                "/agent/chat-llm", json={"message": "找一台二手手机"}
            )
            streamed = self.client.post(
                "/agent/chat-llm/stream",
                json={"message": "找一台二手手机"},
                headers={"Accept": "text/event-stream"},
            )

        self.assertEqual(regular.json()["trace"]["memoryLoad"], summary)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in streamed.text.splitlines() if line.startswith("data: ")
        ]
        complete = next(event["data"] for event in events if event["type"] == "complete")
        self.assertEqual(complete["trace"]["memoryLoad"], summary)
        self.assertEqual(received_bindings, [resolution.binding, resolution.binding])
        serialized = regular.text + streamed.text
        for forbidden in ("accessToken", "ownerBinding", "entryId", "normalizedValue"):
            self.assertNotIn(forbidden, serialized)

    def test_memory_load_summary_survives_agent_error(self):
        from app.api.memory_bff import MemoryRunResolution
        from app.memory.v3_runtime import empty_memory_run_binding

        summary = {
            "reason": "projection_unavailable",
            "durationMs": 2.0,
            "eligible": True,
            "attempted": True,
            "projectionOutcome": "unavailable",
            "retainedCount": 0,
            "truncated": False,
        }
        resolution = MemoryRunResolution(
            empty_memory_run_binding("phone", "used-phone-439-09807c773ce6"),
            summary,
        )
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.resolve_memory_run_for_browser_session",
            new=AsyncMock(return_value=resolution),
        ), patch(
            "app.main.run_agent", new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            response = self.client.post(
                "/agent/chat-llm", json={"message": "找一台二手手机"}
            )

        self.assertEqual(response.json()["trace"]["memoryLoad"], summary)

    def test_memory_bound_task_rejects_durable_restart_and_pause(self):
        session_id = "memory-nondurable-owner"
        task_id = _seed_used_phone_task(session_id)
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.settings.agent_graph_v2_durable_enabled", True,
        ), patch(
            "app.main.settings.agent_control_runtime", "react_v1",
        ), patch(
            "app.main.settings.agent_react_live_enabled", True,
        ), patch(
            "app.main.claim_task_durable_mode",
            new=AsyncMock(return_value=False),
        ), patch("app.main.run_agent", new=AsyncMock()) as run_agent:
            restart = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "继续",
                    "sessionId": session_id,
                    "restartTaskId": task_id,
                },
            )
            pause = self.client.post(f"/agent/sessions/{session_id}/pause")

        self.assertEqual(restart.status_code, 200)
        self.assertEqual(restart.json()["trace"]["status"], "error")
        self.assertIn("不能进入暂停或恢复流程", restart.json()["answer"])
        self.assertEqual(pause.status_code, 409)
        run_agent.assert_not_awaited()

    def test_chat_llm_stream_emits_task_manager_decision(self):
        async def fake_run_agent(
            message,
            history=None,
            on_answer_delta=None,
            **_kwargs,
        ):
            await on_answer_delta("已创建任务")
            return (
                "已创建任务",
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "已创建任务"},
                ],
            )

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=fake_run_agent,
        ):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={"message": "我想骑车去公园", "sessionId": "session-sse-task"},
                headers={"Accept": "text/event-stream"},
            )

        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        relation_event = next(
            event for event in events if event["type"] == "task_relation"
        )
        self.assertEqual(relation_event["decision"]["relation"], "start_new")
        complete = next(event["data"] for event in events if event["type"] == "complete")
        self.assertEqual(complete["taskRelation"]["relation"], "start_new")

    def test_chat_llm_stream_restores_only_the_resumed_task_history(self):
        histories = []

        async def fake_run_agent(
            message,
            history=None,
            on_answer_delta=None,
            **_kwargs,
        ):
            histories.append(history)
            answer = f"回答：{message}"
            await on_answer_delta(answer)
            return (
                answer,
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": answer},
                ],
                None,
                None,
            )

        relation_mock = AsyncMock()
        headers = {"Accept": "text/event-stream"}
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=fake_run_agent,
        ), patch(
            "app.main.classify_task_relation",
            new=relation_mock,
        ):
            first = self.client.post(
                "/agent/chat-llm/stream",
                json={"message": "我想骑车去公园", "sessionId": "session-sse-switch"},
                headers=headers,
            )
            first_events = [
                json.loads(line.removeprefix("data: "))
                for line in first.text.splitlines()
                if line.startswith("data: ")
            ]
            riding_task_id = next(
                event["data"]["taskState"]["taskId"]
                for event in first_events
                if event["type"] == "complete"
            )

            relation_mock.return_value = TaskRelationDecision(
                relation="start_new",
                reason="切换到电脑导购任务",
                confidence=0.99,
            )
            self.client.post(
                "/agent/chat-llm/stream",
                json={"message": "先选一台电脑", "sessionId": "session-sse-switch"},
                headers=headers,
            )

            relation_mock.return_value = TaskRelationDecision(
                relation="resume_previous",
                targetTaskId=riding_task_id,
                reason="恢复骑行任务",
                confidence=0.99,
            )
            self.client.post(
                "/agent/chat-llm/stream",
                json={"message": "还是继续骑行吧", "sessionId": "session-sse-switch"},
                headers=headers,
            )

        self.assertEqual(histories[0], [])
        self.assertEqual(histories[1], [])
        self.assertEqual(
            histories[2],
            [
                {"role": "user", "content": "我想骑车去公园"},
                {"role": "assistant", "content": "回答：我想骑车去公园"},
            ],
        )

    def test_chat_page_contains_recommendation_panel(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("猜你喜欢", response.text)
        self.assertIn("loadRecommendations()", response.text)
        self.assertIn("formatRecommendationReason(shop)", response.text)
        self.assertIn("/agent/recommendations/shops?", response.text)
        self.assertIn("/static/assets/recommendations/coffee.svg", response.text)

    def test_recommendation_static_asset_is_served(self):
        response = self.client.get("/static/assets/recommendations/coffee.svg")

        self.assertEqual(response.status_code, 200)
        self.assertIn("image/svg+xml", response.headers["content-type"])

    def test_recommendations_endpoint_returns_cards_for_page(self):
        recommend_trace = ToolTrace(
            tool="recommend_shops",
            ok=True,
            duration_ms=18.5,
            detail={
                "userId": "demo-user-1",
                "count": 1,
                "shops": [
                    {
                        "shopId": 3,
                        "shopName": "清晨手冲咖啡",
                        "typeId": 2,
                        "avgPrice": 35,
                        "address": "文化广场 3 号",
                        "reason": "你最近关注过同类型商户",
                        "triggerShopIds": [1, 2],
                        "triggerShopNames": ["清晨手冲咖啡", "纸间咖啡书屋"],
                        "score": 5.0,
                        "distanceMeters": 120.5,
                    }
                ],
            },
        )

        with patch(
            "app.main.recommend_shops",
            new=AsyncMock(return_value=recommend_trace),
        ) as recommend_tool:
            response = self.client.get(
                "/agent/recommendations/shops",
                params={"userId": "demo-user-1", "limit": 3},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["userId"], "demo-user-1")
        self.assertEqual(body["shops"][0]["shopName"], "清晨手冲咖啡")
        self.assertEqual(body["shops"][0]["typeId"], 2)
        self.assertEqual(body["shops"][0]["triggerShopNames"], ["清晨手冲咖啡", "纸间咖啡书屋"])
        self.assertEqual(body["toolTrace"]["tool"], "recommend_shops")
        self.assertEqual(body["trace"]["route"], "/agent/recommendations/shops")
        self.assertEqual(body["trace"]["toolNames"], ["recommend_shops"])
        recommend_tool.assert_awaited_once_with("demo-user-1", 3, None, None, None)

    def test_recommendations_endpoint_returns_503_when_backend_fails(self):
        with patch(
            "app.main.recommend_shops",
            new=AsyncMock(
                return_value=ToolTrace(
                    tool="recommend_shops",
                    ok=False,
                    detail="商户推荐服务暂时不可用",
                )
            ),
        ):
            response = self.client.get("/agent/recommendations/shops")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "商户推荐服务暂时不可用")

    def test_chat_calls_shop_type_tool_with_keyword(self):
        with patch("app.main.list_shop_types", new_callable=AsyncMock) as shop_type_tool:
            shop_type_tool.return_value = ToolTrace(
                tool="list_shop_types",
                ok=True,
                detail={
                    "keyword": "咖啡",
                    "count": 1,
                    "names": ["咖啡"],
                },
            )

            response = self.client.post(
                "/agent/chat",
                json={"message": "有没有咖啡相关分类？"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("咖啡", body["answer"])
        self.assertEqual(body["tool_trace"][0]["tool"], "list_shop_types")
        self.assertEqual(body["tool_trace"][0]["detail"]["keyword"], "咖啡")
        shop_type_tool.assert_awaited_once_with("咖啡")

    def test_chat_calls_search_shops_tool(self):
        with patch("app.main.search_shops", new_callable=AsyncMock) as search_tool:
            search_tool.return_value = ToolTrace(
                tool="search_shops",
                ok=True,
                detail={
                    "typeId": None,
                    "count": 1,
                    "shops": [
                        {"name": "巷子口火锅", "address": "人民路 12 号", "avgPrice": 88},
                    ],
                },
            )

            response = self.client.post(
                "/agent/chat",
                json={"message": "附近有什么火锅店？"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("巷子口火锅", body["answer"])
        self.assertEqual(body["tool_trace"][0]["tool"], "search_shops")
        # 问题里没有写死的分类名（“火锅”不是分类名），所以 typeId 传 None，查全部
        search_tool.assert_awaited_once_with(None)

    def test_chat_search_shops_passes_type_id_for_category(self):
        with patch("app.main.search_shops", new_callable=AsyncMock) as search_tool:
            search_tool.return_value = ToolTrace(
                tool="search_shops",
                ok=True,
                detail={
                    "typeId": 1,
                    "count": 2,
                    "shops": [
                        {"name": "巷子口火锅", "address": "人民路 12 号", "avgPrice": 88},
                        {"name": "深夜食堂烧烤", "address": "建设街 45 号", "avgPrice": 65},
                    ],
                },
            )

            response = self.client.post(
                "/agent/chat",
                json={"message": "附近有什么美食？"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("美食", body["answer"])
        self.assertIn("巷子口火锅", body["answer"])
        # “美食”被翻译成 typeId=1 传给工具
        search_tool.assert_awaited_once_with(1)

    def test_chat_returns_tool_failure_message(self):
        with patch("app.main.list_shop_types", new_callable=AsyncMock) as shop_type_tool:
            shop_type_tool.return_value = ToolTrace(
                tool="list_shop_types",
                ok=False,
                detail="后端连接失败",
            )

            response = self.client.post(
                "/agent/chat",
                json={"message": "现在有哪些商户分类？"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("调用失败", body["answer"])
        self.assertFalse(body["tool_trace"][0]["ok"])

    def test_chat_llm_returns_request_trace(self):
        review_trace = ToolTrace(
            tool="search_reviews",
            ok=True,
            duration_ms=123.45,
            detail={
                "query": "哪家咖啡店适合办公？",
                "count": 1,
                "reviews": [{"reviewId": "review-005", "score": 0.89}],
            },
        )
        turn = [
            {"role": "user", "content": "哪家咖啡店适合办公？"},
            {"role": "assistant", "content": "推荐清晨手冲咖啡。[review-005]"},
        ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=("推荐清晨手冲咖啡。[review-005]", [review_trace], turn, None, None)),
        ):
            response = self.client.post(
                "/agent/chat-llm",
                json={"message": "哪家咖啡店适合办公？"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["trace"]["route"], "/agent/chat-llm")
        self.assertEqual(body["trace"]["status"], "ok")
        self.assertEqual(body["trace"]["toolCount"], 1)
        self.assertEqual(body["trace"]["toolNames"], ["search_reviews"])
        self.assertEqual(body["tool_trace"][0]["durationMs"], 123.45)
        self.assertEqual(body["trace"]["toolDurationMs"], 123.45)
        self.assertEqual(body["trace"]["reviewIds"], ["review-005"])
        self.assertGreaterEqual(body["trace"]["totalDurationMs"], 0)
        self.assertGreaterEqual(body["trace"]["agentDurationMs"], 0)
        self.assertFalse(body["trace"]["slow"])
        self.assertEqual(body["trace"]["slowThresholdMs"], 3000.0)
        self.assertEqual(body["trace"]["transportStatus"], "response_generated")
        self.assertEqual(body["trace"]["toolStatus"], "ok")
        self.assertEqual(body["trace"]["failureClass"], "none")
        self.assertEqual(body["trace"]["candidateCount"], 0)

    def test_chat_llm_trace_separates_tool_failure_from_request_success(self):
        failed = ToolTrace(
            tool="search_products",
            ok=False,
            detail={"code": "backend_unavailable", "candidateIds": [999]},
        )
        turn = [
            {"role": "user", "content": "找 iOS 二手机"},
            {"role": "assistant", "content": "检索暂时不可用。"},
        ]
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=(
                "检索暂时不可用。", [failed], turn, "run-tool-failed", None,
            )),
        ):
            response = self.client.post(
                "/agent/chat-llm", json={"message": "找 iOS 二手机"}
            )

        self.assertEqual(response.status_code, 200)
        trace = response.json()["trace"]
        self.assertEqual(trace["status"], "ok")
        self.assertEqual(trace["transportStatus"], "response_generated")
        self.assertEqual(trace["toolStatus"], "failed")
        self.assertEqual(trace["failureClass"], "tool_failure")
        self.assertEqual(trace["candidateCount"], 0)
        self.assertEqual(trace["runId"], "run-tool-failed")

    def test_chat_llm_trace_counts_only_strict_successful_candidate_ids(self):
        successful = ToolTrace(
            tool="search_products", ok=True,
            detail=two_stage_search_detail([101, 102, 103]),
        )
        turn = [
            {"role": "user", "content": "找 iOS 二手机"},
            {"role": "assistant", "content": "找到了。"},
        ]
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=(
                "找到了。", [successful], turn, "run-search-ok", None,
            )),
        ):
            response = self.client.post(
                "/agent/chat-llm", json={"message": "找 iOS 二手机"}
            )

        trace = response.json()["trace"]
        self.assertEqual(trace["toolStatus"], "ok")
        self.assertEqual(trace["candidateCount"], 3)
        self.assertEqual(trace["runId"], "run-search-ok")

    def test_chat_llm_trace_rejects_duplicate_candidate_ledger(self):
        malformed = ToolTrace(
            tool="search_products", ok=True,
            detail={"candidateIds": [101, 101]},
        )
        turn = [
            {"role": "user", "content": "找 iOS 二手机"},
            {"role": "assistant", "content": "候选账本非法。"},
        ]
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=(
                "候选账本非法。", [malformed], turn, "run-malformed", None,
            )),
        ):
            response = self.client.post(
                "/agent/chat-llm", json={"message": "找 iOS 二手机"}
            )

        trace = response.json()["trace"]
        self.assertEqual(trace["toolStatus"], "ok")
        self.assertEqual(trace["candidateCount"], 0)

    def test_chat_llm_trace_reports_mixed_and_not_called_separately(self):
        ok = ToolTrace(
            tool="search_products",
            ok=True,
            detail=two_stage_search_detail([101]),
        )
        failed = ToolTrace(tool="get_product_details", ok=False, detail="backend down")
        turn = [
            {"role": "user", "content": "找 iOS 二手机"},
            {"role": "assistant", "content": "详情暂时不可用。"},
        ]
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=(
                "详情暂时不可用。", [ok, failed], turn, "run-mixed", None,
            )),
        ):
            mixed_response = self.client.post(
                "/agent/chat-llm", json={"message": "找 iOS 二手机"}
            )
        mixed_trace = mixed_response.json()["trace"]
        self.assertEqual(mixed_trace["toolStatus"], "mixed")
        self.assertEqual(mixed_trace["failureClass"], "tool_failure")
        self.assertEqual(mixed_trace["candidateCount"], 1)

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=("无需工具。", [], turn, None, None)),
        ):
            empty_response = self.client.post(
                "/agent/chat-llm", json={"message": "你好"}
            )
        empty_trace = empty_response.json()["trace"]
        self.assertEqual(empty_trace["toolStatus"], "not_called")
        self.assertEqual(empty_trace["failureClass"], "none")
        self.assertEqual(empty_trace["candidateCount"], 0)

    def test_chat_llm_trace_reports_pre_tool_agent_failure(self):
        summary = TraceSummary(
            runId="run-pre-tool-failed",
            agentStatus="failed",
            finalAction="safe_stop",
            failureCode="task_state_update_timeout",
            degraded=True,
            phases=[{
                "phase": "pre_harness",
                "outcome": "safe_stopped",
                "durationMs": 12.5,
            }],
        )
        turn = [
            {"role": "user", "content": "找 iOS 二手机"},
            {"role": "assistant", "content": "已安全停止。"},
        ]
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=(
                "已安全停止。", [], turn, "run-pre-tool-failed", summary,
            )),
        ):
            response = self.client.post(
                "/agent/chat-llm", json={"message": "找 iOS 二手机"}
            )

        trace = response.json()["trace"]
        self.assertEqual(trace["status"], "ok")
        self.assertEqual(trace["toolStatus"], "not_called")
        self.assertEqual(trace["agentStatus"], "failed")
        self.assertEqual(trace["agentFinalAction"], "safe_stop")
        self.assertEqual(trace["agentFailureCode"], "task_state_update_timeout")
        self.assertEqual(trace["failureClass"], "agent_failure")
        self.assertEqual(response.json()["traceSummary"]["phases"], [{
            "phase": "pre_harness",
            "outcome": "safe_stopped",
            "durationMs": 12.5,
        }])

    def test_chat_llm_stream_complete_preserves_tool_failure_outcome(self):
        failed = ToolTrace(
            tool="search_products", ok=False,
            detail={"code": "backend_unavailable"},
        )
        turn = [
            {"role": "user", "content": "找 iOS 二手机"},
            {"role": "assistant", "content": "检索暂时不可用。"},
        ]
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=(
                "检索暂时不可用。", [failed], turn, "run-sse-failed", None,
            )),
        ):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={"message": "找 iOS 二手机"},
                headers={"Accept": "text/event-stream"},
            )

        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        complete = next(event["data"] for event in events if event["type"] == "complete")
        self.assertEqual(complete["trace"]["toolStatus"], "failed")
        self.assertEqual(complete["trace"]["failureClass"], "tool_failure")
        self.assertEqual(complete["trace"]["runId"], "run-sse-failed")

    def test_chat_llm_extracts_review_ids_from_search_knowledge_citations(self):
        knowledge_trace = ToolTrace(
            tool="search_knowledge",
            ok=True,
            duration_ms=234.56,
            detail={
                "query": "哪家咖啡店适合办公？",
                "sources": ["reviews"],
                "count": 1,
                "citations": [
                    {
                        "sourceId": "review-005",
                        "sourceType": "review",
                        "title": "清晨手冲咖啡评论",
                        "quote": "店里有靠窗的单人位和插座。",
                        "score": 0.89,
                        "metadata": {
                            "reviewId": "review-005",
                            "shopName": "清晨手冲咖啡",
                        },
                    }
                ],
            },
        )
        turn = [
            {"role": "user", "content": "哪家咖啡店适合办公？"},
            {"role": "assistant", "content": "推荐清晨手冲咖啡。[review-005]"},
        ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=("推荐清晨手冲咖啡。[review-005]", [knowledge_trace], turn, None, None)),
        ):
            response = self.client.post(
                "/agent/chat-llm",
                json={"message": "哪家咖啡店适合办公？"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["trace"]["toolNames"], ["search_knowledge"])
        self.assertEqual(body["trace"]["reviewIds"], ["review-005"])
        self.assertEqual(body["trace"]["toolDurationMs"], 234.56)

    def test_chat_llm_does_not_treat_merchant_citations_as_review_ids(self):
        merchant_trace = ToolTrace(
            tool="search_knowledge",
            ok=True,
            detail={
                "sources": ["merchant_docs"],
                "citations": [
                    {
                        "sourceId": "merchant-source-id",
                        "sourceType": "merchant_doc",
                        "metadata": {"shopId": 100001},
                    }
                ],
            },
        )

        with patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=("商户资料回答", [merchant_trace], [], None, None)),
        ):
            response = self.client.post(
                "/agent/chat-llm",
                json={"message": "这家店有 WiFi 吗？"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["trace"]["reviewIds"], [])

    def test_chat_llm_extracts_review_ids_from_named_shop_reviews(self):
        named_review_trace = ToolTrace(
            tool="search_shop_reviews",
            ok=True,
            duration_ms=210.0,
            detail={
                "resolvedShop": {"id": 100011, "name": "Red Hook Coffee & Tea"},
                "reviews": [
                    {"reviewId": "review-red-hook", "shopId": 100011}
                ],
            },
        )

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(
                return_value=(
                    "适合办公。[review-red-hook]",
                    [named_review_trace],
                    [],
                    None,
                    None,
                )
            ),
        ):
            response = self.client.post(
                "/agent/chat-llm",
                json={"message": "Red Hook Coffee & Tea适合办公吗？"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["trace"]["toolNames"], ["search_shop_reviews"])
        self.assertEqual(body["trace"]["reviewIds"], ["review-red-hook"])

    def test_chat_llm_writes_request_trace_log(self):
        review_trace = ToolTrace(
            tool="search_reviews",
            ok=True,
            duration_ms=123.45,
            detail={
                "query": "哪家咖啡店适合办公？",
                "count": 1,
                "reviews": [{"reviewId": "review-005", "score": 0.89}],
            },
        )
        turn = [
            {"role": "user", "content": "哪家咖啡店适合办公？"},
            {"role": "assistant", "content": "推荐清晨手冲咖啡。[review-005]"},
        ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=("推荐清晨手冲咖啡。[review-005]", [review_trace], turn, None, None)),
        ), self.assertLogs("uvicorn.error", level="INFO") as logs:
            response = self.client.post(
                "/agent/chat-llm",
                json={"message": "哪家咖啡店适合办公？"},
            )

        self.assertEqual(response.status_code, 200)
        log_text = "\n".join(logs.output)
        self.assertIn("request_trace", log_text)
        self.assertIn("/agent/chat-llm", log_text)
        self.assertIn("search_reviews", log_text)
        self.assertIn("review-005", log_text)
        self.assertIn('"toolDurationMs":123.45', log_text)
        self.assertNotIn("哪家咖啡店适合办公", log_text)

    def test_chat_llm_marks_slow_request_when_threshold_exceeded(self):
        turn = [
            {"role": "user", "content": "哪家咖啡店适合办公？"},
            {"role": "assistant", "content": "推荐清晨手冲咖啡。"},
        ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.SLOW_REQUEST_THRESHOLD_MS",
            0.0,
        ), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=("推荐清晨手冲咖啡。", [], turn, None, None)),
        ), self.assertLogs("uvicorn.error", level="INFO") as logs:
            response = self.client.post(
                "/agent/chat-llm",
                json={"message": "哪家咖啡店适合办公？"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["trace"]["slow"])
        self.assertEqual(body["trace"]["slowThresholdMs"], 0.0)
        self.assertEqual(body["trace"]["bottleneck"], "agent")
        self.assertIn('"slow":true', "\n".join(logs.output))
        self.assertIn('"bottleneck":"agent"', "\n".join(logs.output))

    def test_chat_llm_reuses_history_for_same_session(self):
        first_turn = [
            {"role": "user", "content": "推荐两家咖啡店"},
            {"role": "assistant", "content": "第一家 A，第二家 B。"},
        ]
        second_turn = [
            {"role": "user", "content": "第二家呢？"},
            {"role": "assistant", "content": "第二家是 B。"},
        ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(
                side_effect=[
                    ("第一家 A，第二家 B。", [], first_turn, None, None),
                    ("第二家是 B。", [], second_turn, None, None),
                ]
            ),
        ) as run_agent_mock, patch(
            "app.main.classify_task_relation",
            new=AsyncMock(
                return_value=TaskRelationDecision(
                    relation="continue_current",
                    reason="用户继续询问同一推荐结果",
                    confidence=0.99,
                )
            ),
        ):
            first_response = self.client.post(
                "/agent/chat-llm",
                json={"message": "推荐两家咖啡店", "sessionId": "session-a"},
            )
            second_response = self.client.post(
                "/agent/chat-llm",
                json={"message": "第二家呢？", "sessionId": "session-a"},
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(
            run_agent_mock.await_args_list[1].kwargs["history"],
            first_turn,
        )
        first_task = first_response.json()["taskState"]
        second_task = second_response.json()["taskState"]
        self.assertEqual(first_task["taskId"], second_task["taskId"])
        self.assertEqual(second_response.json()["trace"]["taskId"], first_task["taskId"])

        task_response = self.client.get("/agent/sessions/session-a/task")
        self.assertEqual(task_response.status_code, 200)
        self.assertEqual(task_response.json()["taskId"], first_task["taskId"])

    def test_chat_llm_can_pause_new_task_and_resume_previous_task(self):
        turn = lambda message, answer: [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ]
        run_agent_mock = AsyncMock(
            side_effect=[
                ("先确认骑行需求。", [], turn("我想骑车去公园", "先确认骑行需求。"), None, None),
                ("开始选择电脑。", [], turn("先帮我选一台电脑", "开始选择电脑。"), None, None),
                ("继续之前的骑行任务。", [], turn("算了，还是今天去骑行吧", "继续之前的骑行任务。"), None, None),
            ]
        )
        relation_mock = AsyncMock()

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=run_agent_mock,
        ), patch(
            "app.main.classify_task_relation",
            new=relation_mock,
        ):
            first = self.client.post(
                "/agent/chat-llm",
                json={"message": "我想骑车去公园", "sessionId": "session-switch"},
            )
            riding_task_id = first.json()["taskState"]["taskId"]

            relation_mock.return_value = TaskRelationDecision(
                relation="start_new",
                reason="用户明确提出独立的电脑导购目标",
                confidence=0.98,
            )
            second = self.client.post(
                "/agent/chat-llm",
                json={"message": "先帮我选一台电脑", "sessionId": "session-switch"},
            )
            shopping_task_id = second.json()["taskState"]["taskId"]

            relation_mock.return_value = TaskRelationDecision(
                relation="resume_previous",
                targetTaskId=riding_task_id,
                reason="用户明确回到之前暂停的骑行目标",
                confidence=0.99,
            )
            third = self.client.post(
                "/agent/chat-llm",
                json={"message": "算了，还是今天去骑行吧", "sessionId": "session-switch"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(third.status_code, 200)
        self.assertNotEqual(riding_task_id, shopping_task_id)
        self.assertEqual(second.json()["taskRelation"]["relation"], "start_new")
        self.assertEqual(third.json()["taskRelation"]["relation"], "resume_previous")
        self.assertEqual(third.json()["taskState"]["taskId"], riding_task_id)
        self.assertEqual(
            self.client.get(f"/agent/tasks/{shopping_task_id}").json()["status"],
            "paused",
        )
        self.assertEqual(
            self.client.get("/agent/sessions/session-switch/task").json()["taskId"],
            riding_task_id,
        )
        recent_tasks = self.client.get("/agent/sessions/session-switch/tasks").json()
        self.assertEqual(
            [state["taskId"] for state in recent_tasks],
            [riding_task_id, shopping_task_id],
        )
        self.assertEqual(relation_mock.await_count, 2)
        self.assertEqual(run_agent_mock.await_args_list[0].kwargs["history"], [])
        self.assertEqual(run_agent_mock.await_args_list[1].kwargs["history"], [])
        self.assertEqual(
            run_agent_mock.await_args_list[2].kwargs["history"],
            turn("我想骑车去公园", "先确认骑行需求。"),
        )

    def test_chat_llm_ambiguous_relation_asks_without_mutating_task(self):
        turn = [
            {"role": "user", "content": "我想骑车去公园"},
            {"role": "assistant", "content": "请补充地点。"},
        ]
        run_agent_mock = AsyncMock(return_value=("请补充地点。", [], turn, None, None))
        relation_mock = AsyncMock(
            return_value=TaskRelationDecision(
                relation="ambiguous",
                reason="无法判断用户说的换一个是换公园还是开始新任务",
                clarificationQuestion="你是想换一个公园，还是开始一件新的事情？",
                confidence=0.4,
            )
        )

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=run_agent_mock,
        ), patch(
            "app.main.classify_task_relation",
            new=relation_mock,
        ):
            first = self.client.post(
                "/agent/chat-llm",
                json={"message": "我想骑车去公园", "sessionId": "session-ambiguous"},
            )
            second = self.client.post(
                "/agent/chat-llm",
                json={"message": "换一个吧", "sessionId": "session-ambiguous"},
            )

        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            second.json()["answer"],
            "你是想换一个公园，还是开始一件新的事情？",
        )
        self.assertEqual(second.json()["taskRelation"]["relation"], "ambiguous")
        self.assertEqual(
            second.json()["taskState"]["taskId"],
            first.json()["taskState"]["taskId"],
        )
        self.assertEqual(run_agent_mock.await_count, 1)

    def test_chat_llm_does_not_share_history_between_sessions(self):
        turn = [
            {"role": "user", "content": "推荐咖啡店"},
            {"role": "assistant", "content": "推荐 A。"},
        ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(side_effect=[("推荐 A。", [], turn, None, None), ("你好。", [], turn, None, None)]),
        ) as run_agent_mock:
            self.client.post(
                "/agent/chat-llm",
                json={"message": "推荐咖啡店", "sessionId": "session-a"},
            )
            self.client.post(
                "/agent/chat-llm",
                json={"message": "你好", "sessionId": "session-b"},
            )

        self.assertEqual(run_agent_mock.await_args_list[1].kwargs["history"], [])

    def test_delete_session_clears_history(self):
        turn = [
            {"role": "user", "content": "推荐咖啡店"},
            {"role": "assistant", "content": "推荐 A。"},
        ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent",
            new=AsyncMock(return_value=("推荐 A。", [], turn, None, None)),
        ) as run_agent_mock:
            first_response = self.client.post(
                "/agent/chat-llm",
                json={"message": "推荐咖啡店", "sessionId": "session-a"},
            )
            delete_response = self.client.delete("/agent/sessions/session-a")
            second_response = self.client.post(
                "/agent/chat-llm",
                json={"message": "还记得吗？", "sessionId": "session-a"},
            )

        self.assertEqual(delete_response.status_code, 200)
        self.assertTrue(delete_response.json()["cleared"])
        self.assertEqual(run_agent_mock.await_args_list[1].kwargs["history"], [])
        self.assertEqual(
            self.client.get("/agent/sessions/session-a/task").status_code,
            200,
        )
        self.assertNotEqual(
            first_response.json()["taskState"]["taskId"],
            second_response.json()["taskState"]["taskId"],
        )


    def test_chat_llm_stream_emits_provisional_preview_before_complete(self):
        """Handoff §4.3/§4.5: fast continuation bypasses the TaskManager and a
        provisional ``preview`` event lands before the slow final answer."""
        session_id = "session-fast-preview"
        _seed_used_phone_task(session_id)

        preview_search = AsyncMock(
            return_value=ToolTrace(
                tool="search_products",
                ok=True,
                detail={
                    "candidateIds": [101, 102, 103],
                    "candidates": [
                        _preview_candidate(101),
                        _preview_candidate(102),
                        _preview_candidate(103),
                    ],
                },
            )
        )
        final_trace = ToolTrace(
            tool="search_products",
            ok=True,
            detail=two_stage_search_detail([102, 103]),
        )

        async def fake_run_agent(message, history=None, on_answer_delta=None, **_kwargs):
            answer = "最终推荐：保留 102 和 103。"
            await on_answer_delta(answer)
            turn = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ]
            return answer, [final_trace], turn, "run-final", None

        def _events(response):
            return [
                json.loads(line.removeprefix("data: "))
                for line in response.text.splitlines()
                if line.startswith("data: ")
            ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.settings.used_phone_fast_preview_enabled", True,
        ), patch("app.main.run_agent", new=fake_run_agent), patch(
            "app.main.search_products_tool", new=preview_search,
        ):
            first = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "屏幕不要非原装的",
                    "sessionId": session_id,
                    "domainHint": "ecommerce",
                },
                headers={"Accept": "text/event-stream"},
            )
            second = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "屏幕不要非原装的",
                    "sessionId": session_id,
                    "domainHint": "ecommerce",
                },
                headers={"Accept": "text/event-stream"},
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        first_events = _events(first)
        second_events = _events(second)

        # Event order: status → task_relation → preview → delta → complete.
        preview = next(event for event in first_events if event["type"] == "preview")
        self.assertEqual(preview["type"], "preview")
        self.assertEqual(preview["status"], "provisional")
        first_types = [event["type"] for event in first_events]
        self.assertLess(
            first_types.index("task_relation"), first_types.index("preview")
        )
        self.assertLess(first_types.index("preview"), first_types.index("complete"))
        self.assertLess(first_types.index("preview"), first_types.index("delta"))

        # Cold preview: real search tool, projected Top-3 full_match candidates.
        self.assertEqual(preview["trace"]["cacheStatus"], "cold")
        self.assertEqual(preview["trace"]["analyzerStatus"], "full")
        self.assertTrue(preview["trace"]["taskManagerBypass"])
        self.assertTrue(preview["trace"]["taskStateBypass"])
        self.assertEqual(
            preview["trace"]["previewCandidateIds"], ["101", "102", "103"]
        )
        self.assertEqual(
            [item["product"]["id"] for item in preview["guideResult"]["products"]],
            ["101", "102", "103"],
        )
        self.assertEqual(preview["guideResult"]["status"], "provisional")
        self.assertTrue(preview["guideResult"]["hasCompleteMatch"])
        self.assertEqual(preview["recognizedUseCases"], ["camera_title_claim"])
        self.assertIn(
            {
                "key": "screen_originality",
                "operator": "not_in",
                "value": ["non_original"],
                "unit": "enum",
                "priority": "hard",
                "source": "user",
            },
            preview["recognizedRequirements"],
        )

        # The relation was resolved deterministically, no TaskManager model.
        self.assertEqual(
            first_events[first_types.index("task_relation")]["decision"]["relation"],
            "continue_current",
        )
        complete = next(event["data"] for event in first_events if event["type"] == "complete")
        self.assertEqual(
            complete["taskRelation"]["reason"],
            "fast deterministic used-phone continuation",
        )
        self.assertEqual(complete["trace"]["candidateCount"], 2)

        # Warm preview: identical second turn reuses the deep-copy cache entry.
        warm_preview = next(
            event for event in second_events if event["type"] == "preview"
        )
        self.assertEqual(warm_preview["trace"]["cacheStatus"], "warm")
        self.assertEqual(
            warm_preview["trace"]["previewCandidateIds"], ["101", "102", "103"]
        )

    def test_chat_llm_stream_omits_preview_when_fast_preview_disabled(self):
        session_id = "session-fast-disabled"
        _seed_used_phone_task(session_id)

        async def fake_run_agent(message, history=None, on_answer_delta=None, **_kwargs):
            answer = "按完整流程推荐。"
            await on_answer_delta(answer)
            turn = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ]
            return answer, [], turn, None, None

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.settings.used_phone_fast_preview_enabled", False,
        ), patch("app.main.run_agent", new=fake_run_agent):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "屏幕不要非原装的",
                    "sessionId": session_id,
                    "domainHint": "ecommerce",
                },
                headers={"Accept": "text/event-stream"},
            )

        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertNotIn("preview", [event["type"] for event in events])

    def test_chat_llm_stream_omits_preview_when_safety_gate_fails_closed(self):
        """Handoff §4.6: an unbound budget number suppresses the bypass and the
        preview, and the normal TaskManager path answers instead."""
        session_id = "session-fast-unsafe"
        _seed_used_phone_task(session_id)
        relation_mock = AsyncMock(
            return_value=TaskRelationDecision(
                relation="continue_current",
                reason="模型路径继续当前任务",
                confidence=0.99,
            )
        )

        async def fake_run_agent(message, history=None, on_answer_delta=None, **_kwargs):
            answer = "已记录预算。"
            await on_answer_delta(answer)
            turn = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ]
            return answer, [], turn, None, None

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.settings.used_phone_fast_preview_enabled", True,
        ), patch("app.main.run_agent", new=fake_run_agent), patch(
            "app.main.classify_task_relation", new=relation_mock,
        ):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "预算3000",
                    "sessionId": session_id,
                    "domainHint": "ecommerce",
                },
                headers={"Accept": "text/event-stream"},
            )

        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        event_types = [event["type"] for event in events]
        self.assertNotIn("preview", event_types)
        relation_event = next(
            event for event in events if event["type"] == "task_relation"
        )
        # The relation came from the mocked model, not the fast bypass.
        self.assertEqual(
            relation_event["decision"]["reason"], "模型路径继续当前任务"
        )

    def test_chat_llm_stream_mixed_unresolved_cues_never_preview(self):
        """Repair 1 over the API: every mixed unresolved-cue turn fails closed —
        no preview event and the task relation comes from the TaskManager model,
        never the fast bypass."""
        relation_mock = AsyncMock(
            return_value=TaskRelationDecision(
                relation="continue_current",
                reason="模型路径继续当前任务",
                confidence=0.99,
            )
        )

        async def fake_run_agent(
            message, history=None, on_answer_delta=None, **_kwargs
        ):
            answer = "需要你明确一下。"
            await on_answer_delta(answer)
            turn = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ]
            return answer, [], turn, None, None

        for index, message in enumerate([
            "不要苹果，第一个呢",
            "不要苹果，第二个",
            "不要苹果，换一个",
            "原装屏，这个怎么样",
            "不要苹果，这个也不要",
            "不要苹果，贴膜也不要",
            "不要非原装屏，另外那个也不要",
            "我要原装屏但也不要原装屏",
        ]):
            with self.subTest(message=message):
                session_id = f"session-mixed-failclosed-{index}"
                _seed_used_phone_task(session_id)
                with patch(
                    "app.main.settings.deepseek_api_key", "test-key",
                ), patch(
                    "app.main.settings.used_phone_fast_preview_enabled", True,
                ), patch("app.main.run_agent", new=fake_run_agent), patch(
                    "app.main.classify_task_relation", new=relation_mock,
                ):
                    response = self.client.post(
                        "/agent/chat-llm/stream",
                        json={
                            "message": message,
                            "sessionId": session_id,
                            "domainHint": "ecommerce",
                        },
                        headers={"Accept": "text/event-stream"},
                    )

                self.assertEqual(response.status_code, 200)
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                event_types = [event["type"] for event in events]
                self.assertNotIn("preview", event_types)
                relation_event = next(
                    event for event in events if event["type"] == "task_relation"
                )
                # The relation came from the mocked model, not the fast bypass.
                self.assertEqual(
                    relation_event["decision"]["reason"], "模型路径继续当前任务"
                )

    def test_chat_llm_stream_active_task_risk_input_no_preview(self):
        """Safety E2E (Codex repair item 3): establish a valid phone
        shoppingGuide in the same session, then inject risk input — the
        unconsumed-negation-cue and unbound-ordinal surfaces from the repair.
        The analyzer must fail closed: no preview event and the task relation
        comes from the TaskManager model, never the fast deterministic bypass."""
        relation_mock = AsyncMock(
            return_value=TaskRelationDecision(
                relation="continue_current",
                reason="模型路径继续当前任务",
                confidence=0.99,
            )
        )

        async def fake_run_agent(
            message, history=None, on_answer_delta=None, **_kwargs
        ):
            answer = "需要你明确一下。"
            await on_answer_delta(answer)
            turn = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ]
            return answer, [], turn, None, None

        for index, message in enumerate([
            # Codex defect #1 — unconsumed later negation cue masked by a valid
            # earlier brand target across coordination boundaries.
            "不要苹果而且贴膜也不要",
            "不要苹果但贴膜也不要",
            "不要苹果和贴膜也不要",
            # Codex defect #2 — unified ordinal grammar (Arabic/Chinese).
            "不要苹果，前3个哪个好",
            "前3个哪个好",
            "头两个哪个好",
        ]):
            with self.subTest(message=message):
                session_id = f"session-active-risk-{index}"
                _seed_used_phone_task(session_id)
                with patch(
                    "app.main.settings.deepseek_api_key", "test-key",
                ), patch(
                    "app.main.settings.used_phone_fast_preview_enabled", True,
                ), patch("app.main.run_agent", new=fake_run_agent), patch(
                    "app.main.classify_task_relation", new=relation_mock,
                ):
                    response = self.client.post(
                        "/agent/chat-llm/stream",
                        json={
                            "message": message,
                            "sessionId": session_id,
                            "domainHint": "ecommerce",
                        },
                        headers={"Accept": "text/event-stream"},
                    )

                self.assertEqual(response.status_code, 200)
                events = [
                    json.loads(line.removeprefix("data: "))
                    for line in response.text.splitlines()
                    if line.startswith("data: ")
                ]
                event_types = [event["type"] for event in events]
                self.assertNotIn("preview", event_types)
                relation_event = next(
                    event for event in events if event["type"] == "task_relation"
                )
                # The relation came from the mocked model, not the fast bypass.
                self.assertEqual(
                    relation_event["decision"]["reason"], "模型路径继续当前任务"
                )

    def test_chat_llm_stream_bound_compare_no_search_no_preview(self):
        """Ordinal regression over the API: a Validator-passed Top-3 display
        then ``第一个和第二个哪个好`` — no preview event, ``search_products`` is
        never re-run, and the complete path carries the compare trace."""
        session_id = "session-bound-compare"
        _seed_validator_bound_phone_task(session_id)
        preview_search = AsyncMock(return_value=None)
        compare_trace = ToolTrace(
            tool="compare_products",
            ok=True,
            durationMs=1.0,
            detail={
                "rankedFinalists": [5989522, 1092185],
                "hasCompleteMatch": True,
            },
        )

        async def fake_run_agent(
            message, history=None, on_answer_delta=None, **_kwargs
        ):
            answer = "前两件对比完成。"
            await on_answer_delta(answer)
            turn = [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ]
            return answer, [compare_trace], turn, "run-compare", None

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.settings.used_phone_fast_preview_enabled", True,
        ), patch("app.main.run_agent", new=fake_run_agent), patch(
            "app.main.search_products_tool", new=preview_search,
        ):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "第一个和第二个哪个好",
                    "sessionId": session_id,
                    "domainHint": "ecommerce",
                },
                headers={"Accept": "text/event-stream"},
            )

        self.assertEqual(response.status_code, 200)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        event_types = [event["type"] for event in events]
        # Bound comparison carries no provisional product search.
        self.assertNotIn("preview", event_types)
        # The relation was resolved deterministically (fast bypass), never a
        # TaskManager model call.
        relation_event = next(
            event for event in events if event["type"] == "task_relation"
        )
        self.assertEqual(
            relation_event["decision"]["reason"],
            "fast deterministic used-phone continuation",
        )
        # search_products_tool was never invoked for this compare turn.
        self.assertEqual(preview_search.await_count, 0)
        complete = next(
            event["data"] for event in events if event["type"] == "complete"
        )
        self.assertEqual(
            [trace["tool"] for trace in complete.get("tool_trace", [])],
            ["compare_products"],
        )


    def test_durable_cross_session_resume_never_reads_history_or_publishes_task(self):
        task_id = _seed_used_phone_task("durable-owner-a")
        run_agent_mock = AsyncMock()
        payload = {
            "taskId": task_id,
            "runId": "run-real-a",
            "threadId": f"v2-task:{task_id}:run-real-a",
            "revision": 3,
            "proposalHash": "0123456789abcdef",
            "answer": "256GB",
        }
        with patch("app.main.settings.agent_graph_v2_durable_enabled", True), patch(
            "app.main.settings.deepseek_api_key", "test-key"
        ), patch("app.main.run_agent", new=run_agent_mock):
            response = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "256GB",
                    "sessionId": "durable-attacker-b",
                    "resume": payload,
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("无权继续", body["answer"])
        self.assertIsNone(body["taskState"])
        self.assertIsNone(body["guideResult"])
        run_agent_mock.assert_not_awaited()
        self.assertNotIn(
            f"session:durable-attacker-b:task:{task_id}", task_state._client.lists
        )

    def test_durable_cross_session_restart_never_invokes_runner_or_writes_history(self):
        task_id = _seed_used_phone_task("durable-owner-a")
        run_agent_mock = AsyncMock()
        with patch("app.main.settings.agent_graph_v2_durable_enabled", True), patch(
            "app.main.settings.deepseek_api_key", "test-key"
        ), patch("app.main.run_agent", new=run_agent_mock):
            response = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "继续任务",
                    "sessionId": "durable-attacker-b",
                    "restartTaskId": task_id,
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("无权继续", body["answer"])
        self.assertIsNone(body["taskState"])
        run_agent_mock.assert_not_awaited()
        self.assertNotIn(
            f"session:durable-attacker-b:task:{task_id}", task_state._client.lists
        )

    def test_durable_exact_replay_skips_history_load_and_turn_persistence(self):
        import hashlib

        from app.graph.runtime import CONTROL_POLICY_REVISIONS

        session_id = "durable-replay-owner"
        task_id = _seed_used_phone_task(session_id)
        run_id = "run-exact-replay"
        proposal_hash = "proposal-exact-replay"
        answer = "这是第一次完成后的固定回答。"
        payload = {
            "taskId": task_id,
            "runId": run_id,
            "threadId": f"v2-task:{task_id}:{run_id}",
            "revision": 3,
            "proposalHash": proposal_hash,
            "answer": "256GB",
        }

        async def _mark_resolved() -> None:
            current = await task_state.get_task_state(task_id)
            await task_state.update_task_state(
                task_id,
                task_state.TaskStatePatchRequest(
                    expectedRevision=current.revision,
                    actor="agent",
                    domain_state_patch={
                        "v2PendingClarification": {
                            "taskId": task_id,
                            "runId": run_id,
                            "threadId": f"v2-task:{task_id}:{run_id}",
                            "proposalHash": proposal_hash,
                            "answerHash": hashlib.sha256(b"256GB").hexdigest()[:16],
                            "sessionOwnerHash": hashlib.sha256(
                                session_id.encode("utf-8")
                            ).hexdigest()[:16],
                            "controlPolicy": "react_v1",
                            "policyRevision": CONTROL_POLICY_REVISIONS["react_v1"],
                            "status": "resolved",
                        }
                    },
                ),
            )

        asyncio.run(_mark_resolved())
        summary = TraceSummary(runId=run_id, finalAction="idempotent_resume_replay")
        run_agent_mock = AsyncMock(return_value=(answer, [], [], run_id, summary))
        with patch("app.main.settings.agent_graph_v2_durable_enabled", True), patch(
            "app.main.settings.deepseek_api_key", "test-key"
        ), patch("app.main.run_agent", new=run_agent_mock), patch(
            "app.main.get_history", new=AsyncMock()
        ) as history, patch("app.main.save_turn", new=AsyncMock()) as save_turn:
            response = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "256GB",
                    "sessionId": session_id,
                    "resume": payload,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], answer)
        self.assertEqual(response.json()["trace"]["toolCount"], 0)
        history.assert_not_awaited()
        save_turn.assert_not_awaited()

    def test_durable_resume_resolves_browser_reference_before_runner(self):
        """A clarification resume must keep the card focus selected in the UI.

        The browser first receives a durable clarification, then the user may
        click one of the still-visible cards before answering.  The resume
        endpoint must resolve that server-owned ReferenceContext and pass the
        result to the same Agent boundary used by the streaming endpoint.
        """
        from app.reference_context import ResolvedReferenceContext

        session_id = "durable-reference-owner"
        task_id = _seed_used_phone_task(session_id)
        resolved = ResolvedReferenceContext(
            task_id=task_id,
            task_revision=1,
            scope_id="scope-reference",
            scope_source_revision=1,
            presentation_mode="compact",
            presentation_ids=(101, 102, 103),
            compact_product_ids=(101, 102, 103),
            expanded_product_ids=(101, 102, 103),
            compared_product_ids=(),
            previous_batch_product_ids=(),
            focused_product_id=102,
        )
        resolver = AsyncMock(return_value=resolved)
        publisher = AsyncMock(return_value={
            "products": [],
            "referenceContext": {
                "schemaVersion": "shopping-reference-context-public-v1",
                "handle": "C" * 43,
                "presentationMode": "compact",
            },
        })
        refresher = AsyncMock(return_value={
            "schemaVersion": "shopping-reference-context-public-v1",
            "handle": "D" * 43,
            "presentationMode": "compact",
            "scopeId": "scope-reference",
            "taskRevision": 5,
        })
        captured = {}

        async def observed_run_agent(message, history=None, **kwargs):
            captured["reference_context"] = kwargs.get("reference_context")
            return (
                "已按点击商品继续。",
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "已按点击商品继续。"},
                ],
                "run-reference-resume",
                TraceSummary(runId="run-reference-resume", finalAction="respond"),
            )

        with patch("app.main.settings.agent_graph_v2_durable_enabled", True), patch(
            "app.main.settings.deepseek_api_key", "test-key"
        ), patch("app.main.resolve_reference_context", new=resolver), patch(
            "app.main.run_agent", new=observed_run_agent
        ), patch(
            "app.main.refresh_reference_context", new=refresher
        ), patch(
            "app.main._publish_browser_guide_result", new=publisher
        ), patch("app.main.claim_task_durable_mode", new=AsyncMock(return_value=True)):
            response = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "比较这个和第三个",
                    "sessionId": session_id,
                    "referenceContext": {
                        "handle": "A" * 43,
                        "presentationMode": "compact",
                        "focusedProductId": "102",
                    },
                    "resume": {
                        "taskId": task_id,
                        "runId": "run-reference-pending",
                        "threadId": f"v2-task:{task_id}:run-reference-pending",
                        "revision": 1,
                        "proposalHash": "proposal-reference",
                        "answer": "比较这个和第三个",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "已按点击商品继续。")
        resolver.assert_awaited_once()
        self.assertIs(captured["reference_context"], resolved)
        publisher.assert_awaited_once()
        self.assertEqual(
            response.json()["guideResult"]["referenceContext"]["handle"],
            "D" * 43,
        )
        self.assertEqual(response.json()["referenceContext"]["handle"], "D" * 43)
        refresher.assert_awaited_once()

    def test_durable_resume_rejects_stale_browser_reference_before_runner(self):
        from app.reference_context import ReferenceContextError

        session_id = "durable-reference-stale-owner"
        task_id = _seed_used_phone_task(session_id)
        runner = AsyncMock()
        resolver = AsyncMock(
            side_effect=ReferenceContextError("reference_context_stale_task")
        )

        with patch("app.main.settings.agent_graph_v2_durable_enabled", True), patch(
            "app.main.settings.deepseek_api_key", "test-key"
        ), patch("app.main.resolve_reference_context", new=resolver), patch(
            "app.main.run_agent", new=runner
        ), patch("app.main.claim_task_durable_mode", new=AsyncMock(return_value=True)):
            response = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "比较这个和第三个",
                    "sessionId": session_id,
                    "referenceContext": {
                        "handle": "B" * 43,
                        "presentationMode": "compact",
                        "focusedProductId": "102",
                    },
                    "resume": {
                        "taskId": task_id,
                        "runId": "run-reference-stale",
                        "threadId": f"v2-task:{task_id}:run-reference-stale",
                        "revision": 1,
                        "proposalHash": "proposal-reference-stale",
                        "answer": "比较这个和第三个",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("商品展示已过期", response.json()["answer"])
        resolver.assert_awaited_once()
        runner.assert_not_awaited()

    def test_durable_resume_refreshes_reference_without_new_guide_cards(self):
        from app.reference_context import ResolvedReferenceContext

        session_id = "durable-reference-refresh-owner"
        task_id = _seed_used_phone_task(session_id)
        resolved = ResolvedReferenceContext(
            task_id=task_id,
            task_revision=1,
            scope_id="scope-reference",
            scope_source_revision=1,
            presentation_mode="compact",
            presentation_ids=(101, 102, 103),
            compact_product_ids=(101, 102, 103),
            expanded_product_ids=(101, 102, 103),
            compared_product_ids=(102, 103),
            previous_batch_product_ids=(),
            focused_product_id=102,
        )
        resolver = AsyncMock(return_value=resolved)
        refresher = AsyncMock(return_value={
            "schemaVersion": "shopping-reference-context-public-v1",
            "handle": "D" * 43,
            "presentationMode": "compact",
            "taskRevision": 5,
        })

        async def observed_run_agent(message, history=None, **kwargs):
            return (
                "已按已有属性给出建议。",
                [],
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "已按已有属性给出建议。"},
                ],
                "run-reference-refresh",
                TraceSummary(runId="run-reference-refresh", finalAction="respond"),
            )

        with patch("app.main.settings.agent_graph_v2_durable_enabled", True), patch(
            "app.main.settings.deepseek_api_key", "test-key"
        ), patch("app.main.resolve_reference_context", new=resolver), patch(
            "app.main.refresh_reference_context", new=refresher
        ), patch("app.main.run_agent", new=observed_run_agent), patch(
            "app.main._publish_browser_guide_result", new=AsyncMock(return_value=None)
        ), patch("app.main.claim_task_durable_mode", new=AsyncMock(return_value=True)):
            response = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "根据已有属性告诉我怎么选",
                    "sessionId": session_id,
                    "referenceContext": {
                        "handle": "A" * 43,
                        "presentationMode": "compact",
                        "focusedProductId": "102",
                    },
                    "resume": {
                        "taskId": task_id,
                        "runId": "run-reference-refresh-pending",
                        "threadId": f"v2-task:{task_id}:run-reference-refresh-pending",
                        "revision": 1,
                        "proposalHash": "proposal-reference-refresh",
                        "answer": "根据已有属性告诉我怎么选",
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json().get("guideResult"))
        self.assertEqual(response.json()["referenceContext"]["handle"], "D" * 43)
        refresher.assert_awaited_once()

    def test_durable_endpoint_reports_stage_level_model_call_attribution(self):
        session_id = "durable-attribution-owner"
        task_id = _seed_used_phone_task(session_id)

        async def observed_run_agent(*_args, **_kwargs):
            from app.llm import _observe_llm_call

            _observe_llm_call("task_state", 5.0)
            _observe_llm_call("react_decision", 7.5)
            _observe_llm_call("final_answer", 11.0)
            summary = TraceSummary(
                runId="run-durable-attribution",
                finalAction="respond",
            )
            return "完成。", [], [], "run-durable-attribution", summary

        with patch("app.main.settings.agent_graph_v2_durable_enabled", True), patch(
            "app.main.settings.deepseek_api_key", "test-key"
        ), patch("app.main.run_agent", new=observed_run_agent), patch(
            "app.main.get_history", new=AsyncMock(return_value=[])
        ), patch("app.main.save_turn", new=AsyncMock()):
            response = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "继续任务",
                    "sessionId": session_id,
                    "restartTaskId": task_id,
                },
            )

        self.assertEqual(response.status_code, 200)
        trace = response.json()["trace"]
        self.assertEqual(
            trace["modelCallCounts"],
            {"task_state": 1, "react_decision": 1, "final_answer": 1},
        )
        self.assertEqual(
            trace["llmDurationByStageMs"],
            {"task_state": 5.0, "react_decision": 7.5, "final_answer": 11.0},
        )

    def test_durable_two_http_turns_keep_previous_candidate_scope_for_rerank(self):
        """A second durable HTTP turn must plan against the first turn's scope.

        This deliberately crosses the same request boundary as production:
        TaskState extraction happens before each call, then the durable graph
        builds its ContextPack/Planner input from the rehydrated state.  The
        first call establishes a scope with a full search; the second call is a
        scope-preserving candidate-domain follow-up and must not search again.
        """
        from tests.test_candidate_scope import _rerank_detail
        from tests.test_graph_v2_interrupt_resume import InFileRedis, InFileToolInbox
        from app.domains.ecommerce.used_phone_attributes import (
            USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
            USED_PHONE_ATTRIBUTE_RULESET_VERSION,
        )

        # The normal endpoint fixture intentionally has a small TaskState-only
        # Redis fake.  This production-neighbor test crosses the durable
        # checkpoint boundary too, so give both stores the same full fake.
        durable_redis = InFileRedis()
        session_memory._client = durable_redis
        task_state._client = durable_redis
        task_state._task_locks.clear()
        task_state._session_locks.clear()

        calls: list[tuple[str, dict]] = []

        def search_detail_with_all_hard_facts() -> dict:
            detail = two_stage_search_detail([101, 102, 103])
            for candidate in detail["candidates"]:
                product_id = candidate["id"]
                ref = f"product:{product_id}:attribute:motherboard_repair"
                candidate["attributes"].append({
                    "key": "motherboard_repair",
                    "rawValue": "主板未维修",
                    "normalizedText": "not_repaired",
                    "evidenceField": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                    "extractionMethod": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
                })
                candidate["checks"].append({
                    "key": "motherboard_repair",
                    "operator": "eq",
                    "expected": "not_repaired",
                    "unit": "enum",
                    "priority": "hard",
                    "source": "user",
                    "status": "pass",
                    "actual": "not_repaired",
                    "evidenceRef": ref,
                })
                candidate["evidenceRefs"].append(ref)
                detail["evidence"].append({
                    "ref": ref,
                    "field": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                    "method": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
                    "rawValue": "主板未维修",
                })
            evidence_by_ref = {
                item["ref"]: item for item in detail["evidence"]
            }
            ordered_refs = [
                ref
                for candidate in detail["candidates"]
                for ref in candidate["evidenceRefs"]
            ]
            detail["evidenceRefs"] = ordered_refs
            detail["evidence"] = [evidence_by_ref[ref] for ref in ordered_refs]
            detail["citationTrace"]["evidenceRefCount"] = len(ordered_refs)
            return detail

        async def fake_call_tool(
            name: str, arguments: dict, *, execution_context=None
        ):
            calls.append((name, dict(arguments)))
            if name == "search_products":
                return ToolTrace(
                    tool=name,
                    ok=True,
                    detail=search_detail_with_all_hard_facts(),
                )
            if name == "rerank_products_in_scope":
                input_ids = list(arguments["productIds"])
                return ToolTrace(
                    tool=name,
                    ok=True,
                    detail=_rerank_detail(
                        arguments["scopeId"],
                        list(reversed(input_ids)),
                        input_ids=input_ids,
                    ),
                )
            raise AssertionError(f"unexpected durable tool: {name}")

        async def fake_final_answer(*_args, **_kwargs):
            return "已完成本轮候选排序。"

        with patch("app.main.settings.agent_control_runtime", "fixed_v1"), patch(
            "app.main.settings.agent_graph_v2_durable_enabled", True
        ), patch(
            "app.main.settings.deepseek_api_key", "test-key"
        ), patch(
            "app.graph.resume.ToolInbox", return_value=InFileToolInbox()
        ), patch("app.tools.call_tool", new=fake_call_tool), patch(
            "app.llm._generate_final_answer", new=fake_final_answer
        ):
            first = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "iOS、主板未维修的二手手机。",
                    "sessionId": "durable-scope-owner",
                    "domainHint": "ecommerce",
                },
            )
            second = self.client.post(
                "/agent/chat-llm-durable",
                json={
                    "message": "这其中哪一个拍照效果最好",
                    "sessionId": "durable-scope-owner",
                    "domainHint": "ecommerce",
                },
            )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            [name for name, _ in calls],
            ["search_products", "rerank_products_in_scope"],
            msg=f"first={first.text} second={second.text}",
        )
        self.assertEqual(
            [trace["tool"] for trace in second.json()["tool_trace"]],
            ["rerank_products_in_scope"],
        )
        self.assertNotIn(
            "search_products",
            [trace["tool"] for trace in second.json()["tool_trace"]],
        )
        first_scope = first.json()["taskState"]["domainState"]["candidateScope"]
        second_scope = second.json()["taskState"]["domainState"]["candidateScope"]
        self.assertEqual(second_scope["scopeId"], first_scope["scopeId"])

    def test_runtime_status_and_independent_flow_page_show_react_default(self):
        with patch("app.main.settings.agent_control_runtime", "react_v1"), patch(
            "app.main.settings.agent_react_live_enabled", True
        ), patch("app.main.settings.agent_graph_v2_durable_enabled", True):
            status = self.client.get("/agent/runtime-status")
        flow = self.client.get("/agent-flow")

        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["controlPolicy"], "react_v1")
        self.assertTrue(status.json()["durableCheckpoint"])
        self.assertEqual(flow.status_code, 200)
        self.assertIn("Durable Bounded ReAct", flow.text)
        self.assertIn("Operator Pause", flow.text)
        self.assertIn("TaskState CAS", flow.text)
        self.assertIn("Validator→Policy", flow.text)
        self.assertIn("竖向可读版", flow.text)
        self.assertIn("实时单步状态", flow.text)
        self.assertIn("applyLiveTurn", flow.text)
        self.assertIn("agent-debug-flow-current", flow.text)
        self.assertIn("normalizeLiveTurn", flow.text)
        self.assertIn("turn.revision>=liveTurn.revision", flow.text)
        self.assertIn("本轮已取消", flow.text)
        self.assertIn('data-edge="executor:validator"', flow.text)
        self.assertIn("当前全系统边界", flow.text)
        self.assertIn("ReferenceContext", flow.text)
        self.assertIn("TransactionAgent", flow.text)
        self.assertIn("Java 内部事务只在交易 Demo 中展示服务端完成回执", flow.text)

    def test_commerce_demo_page_exposes_bounded_end_to_end_journey(self):
        page = self.client.get("/commerce-demo")

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.headers["cache-control"], "no-store, max-age=0")
        self.assertIn("电商导购 Agent · 全链路 Demo", page.text)
        self.assertIn("/transaction-agent/orders/preview", page.text)
        self.assertIn("/transaction-agent/payments/preview", page.text)
        self.assertIn("确认下单", page.text)
        self.assertIn("确认发起支付", page.text)
        self.assertIn("Java 事务内部只展示已完成回执", page.text)

    def test_pause_endpoint_is_session_owned_and_idempotent(self):
        from app.graph.resume import (
            build_thread_id,
            session_owner_hash,
            write_task_cursor,
        )

        session_id = "pause-owner"
        task_id = _seed_used_phone_task(session_id)
        run_id = "run-pause-http"
        thread_id = build_thread_id(task_id, run_id)

        asyncio.run(write_task_cursor(
            task_id,
            run_id=run_id,
            thread_id=thread_id,
            revision=1,
            session_owner_hash_value=session_owner_hash(session_id),
            control_policy="react_v1",
        ))
        with patch("app.main.settings.agent_control_runtime", "react_v1"), patch(
            "app.main.settings.agent_react_live_enabled", True
        ), patch("app.main.settings.agent_graph_v2_durable_enabled", True):
            first = self.client.post(f"/agent/sessions/{session_id}/pause")
            second = self.client.post(f"/agent/sessions/{session_id}/pause")
            loaded = self.client.get(f"/agent/sessions/{session_id}/pause")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["requestId"], second.json()["requestId"])
        self.assertEqual(loaded.json()["state"], "pause_requested")
        self.assertNotIn("sessionOwnerHash", loaded.json())

    def test_stream_passes_session_identity_into_agent_runtime(self):
        seen: dict[str, str | None] = {}

        async def fake_run_agent(message, history=None, on_answer_delta=None, **kwargs):
            seen["sessionId"] = kwargs.get("session_id")
            answer = "完成。"
            await on_answer_delta(answer)
            return answer, [], [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ], "run-stream-owner", None

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.run_agent", new=fake_run_agent
        ):
            response = self.client.post(
                "/agent/chat-llm/stream",
                json={
                    "message": "推荐一台手机",
                    "sessionId": "stream-owner",
                    "domainHint": "ecommerce",
                },
                headers={"Accept": "text/event-stream"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["sessionId"], "stream-owner")


if __name__ == "__main__":
    unittest.main()
