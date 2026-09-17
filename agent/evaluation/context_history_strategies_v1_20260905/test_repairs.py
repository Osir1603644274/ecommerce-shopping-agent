from datetime import datetime, timezone
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from agent.app import llm
from agent.app.context_pack import build_context_pack
from agent.app.context_view import ContextProjector
from agent.app.settings import settings
from agent.app.task_state import TaskState


class Repairs(IsolatedAsyncioTestCase):
    def test_relaxation_is_not_inverted_into_a_new_hard_filter(self):
        from agent.app.domains.ecommerce.models import ShoppingGuideState
        instant = datetime.now(timezone.utc)
        old = ShoppingGuideState(category="phone", mode="recommend", requirements=[{
            "key": "battery_originality", "operator": "eq", "value": "original", "unit": "enum", "priority": "hard", "source": "user"}])
        state = TaskState(taskId="release-test", taskType="ecommerce_guide", sessionId="release-session",
            status="ready", revision=1, goal="买手机", createdAt=instant, updatedAt=instant,
            domainState={"shoppingGuide": old.model_dump(by_alias=True, mode="json")})
        proposed = ShoppingGuideState(category="phone", mode="recommend", requirements=[])
        text = "撤销电池必须原装这条硬条件；非原装电池也可以，但电池健康要求保留。"
        with patch.object(settings, "context_history_v1_enabled", True):
            args, observation = llm._deterministic_used_phone_task_state_decision(state, text)
            self.assertIsNone(args)
            self.assertEqual(observation["reason"], "semantic_withdrawal_or_acceptance")
            self.assertEqual(llm._canonicalize_explicit_used_phone_semantics(state, text, proposed).requirements, [])
            self.assertEqual(llm._canonicalize_explicit_used_phone_negation(text, proposed).requirements, [])
            self.assertFalse(llm._requires_context_semantic_change("不要取消原装电池要求"))

    def test_brand_count_is_not_a_product_display_ordinal(self):
        instant = datetime.now(timezone.utc)
        state = TaskState(taskId="ordinal-test", taskType="ecommerce_guide", sessionId="ordinal-session",
            status="ready", revision=1, goal="买手机", createdAt=instant, updatedAt=instant,
            domainState={"candidateScopeInvalidation": {"status": "invalidated", "scopeId": "old", "replacedByScopeId": "new"}})
        with patch.object(settings, "context_history_v1_enabled", True), patch.object(llm, "_trusted_validator_presentation_ids", return_value=[11, 12]):
            self.assertEqual(llm._ordinal_comparison_binding(state, "倾向小米或红米，并非只能买这两个品牌。"), ("none", None))
            self.assertEqual(llm._ordinal_comparison_binding(state, "第一款和第二款哪个好？"), ("bound", [11, 12]))
            self.assertFalse(llm._references_invalidated_candidate_scope(state, "之前那两个品牌不再是硬限制。"))
            self.assertTrue(llm._references_invalidated_candidate_scope(state, "之前那两个商品哪个好？"))

    def test_goal_bound_and_non_brand_negative_language(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            old = "选手机" * 160
            self.assertEqual(llm._append_task_goal(old, "补充条件" * 30, "追加条件"), old)
            self.assertLessEqual(len(llm._append_task_goal("买手机", "预算2000", "追加条件")), 500)
            with self.assertRaises(llm.TaskStatePayloadValidationError):
                llm._validated_model_task_patch({"goal": "字" * 501})
            for text in ("不要补造候选", "不要编造商品", "不要声称已核验库存", "不要把未知写成通过"):
                self.assertFalse(llm._has_unbound_brand_rejection(text), text)
            for text in ("不要它。", "这个品牌不喜欢", "不要这个品牌", "我不想要"):
                self.assertTrue(llm._has_unbound_brand_rejection(text), text)

    def test_non_brand_rejection_does_not_force_brand_clarification(self):
        instant = datetime.now(timezone.utc)
        state = TaskState(taskId="negative-test", taskType="ecommerce_guide", sessionId="negative-session",
            status="ready", revision=1, goal="买手机", createdAt=instant, updatedAt=instant,
            domainState={"shoppingGuide": {"category": "phone", "mode": "recommend", "requirements": []}})
        with patch.object(settings, "context_history_v1_enabled", True):
            for text in ("若不足两款，先说明原因，不要补造候选。", "不要编造商品。"):
                _, observation = llm._deterministic_used_phone_task_state_decision(state, text)
                self.assertNotEqual((observation or {}).get("reason"), "unbound_negative_target")
            arguments, observation = llm._deterministic_used_phone_task_state_decision(state, "完整任务备注。" * 100)
            self.assertIsNone(arguments)
            self.assertEqual(observation["reason"], "long_message_requires_bounded_goal")

    def test_empty_search_is_not_transport_failure(self):
        from agent.app.schemas import ToolTrace
        from .catalog_transport import ContextCatalogTransport, FrozenCatalogTransport
        transport = ContextCatalogTransport(())
        detail = {"candidatePoolIds": [1], "rankedItemIds": [], "candidates": [],
                  "rankingTrace": {"eligibleCandidateCount": 0},
                  "retrievalTrace": {"channels": {"offlineFrozenBm25": {"status": "active"}}}}
        with patch.object(FrozenCatalogTransport, "_search", return_value=ToolTrace(tool="search_products", ok=False, detail=detail)):
            result = transport._search({"query": "手机"})
            self.assertTrue(result.ok)
            self.assertEqual(result.detail, detail)
        failed = ToolTrace(tool="search_products", ok=False, detail={"code": "missing_query"})
        with patch.object(FrozenCatalogTransport, "_search", return_value=failed):
            self.assertFalse(transport._search({}).ok)

    def test_composite_budget_defers_to_model_instead_of_first_amount(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            self.assertIsNone(llm._explicit_phone_price_ceiling("硬预算是七千元加三百元机动，合计最多七千三百元，超过就不要列入。"))
            self.assertIsNone(llm._explicit_phone_price_ceiling("预算7000+300元"))
            self.assertEqual(llm._explicit_phone_price_ceiling("预算七千三百元以内"), 730000)

    def test_budget_rejection_phrase_is_not_an_unbound_brand_veto(self):
        instant = datetime.now(timezone.utc)
        state = TaskState(taskId="budget-test", taskType="ecommerce_guide", sessionId="budget-session",
            status="ready", revision=1, goal="买手机", createdAt=instant, updatedAt=instant,
            domainState={"shoppingGuide": {"category": "phone", "mode": "recommend", "requirements": []}})
        with patch.object(settings, "context_history_v1_enabled", True):
            arguments, observation = llm._deterministic_used_phone_task_state_decision(state,
                "硬预算是七千元加三百元机动，合计最多七千三百元，超过就不要列入。")
            self.assertIsNone(arguments)
            self.assertEqual(observation["route"], "model_fallback")

    def test_explicit_soft_brand(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            for query in ("倾向华为", "偏向华为", "希望华为", "华为优先"):
                with self.subTest(query=query):
                    self.assertEqual(llm._explicit_used_phone_requirements(query)["brand"].priority, "soft")
            self.assertEqual(llm._explicit_used_phone_requirements("只要华为")["brand"].priority, "hard")

    def test_negated_gaming(self):
        with patch.object(settings, "context_history_v1_enabled", True):
            for query in ("给父亲买手机，不玩游戏", "不打游戏", "不玩游戏，喜欢拍照"):
                self.assertNotEqual(llm._used_phone_text_claim_discovery(query), "gaming_title_claim")
            self.assertEqual(llm._used_phone_text_claim_discovery("要玩游戏的手机"), "gaming_title_claim")
            self.assertEqual(llm._used_phone_text_claim_discovery("不是不玩游戏"), "gaming_title_claim")

    async def test_view_preserves_preferences_without_mutating_pack(self):
        instant = datetime.now(timezone.utc)
        state = TaskState(taskId="repair-test", taskType="ecommerce_guide", sessionId="repair-session",
            status="ready", revision=1, goal="给父亲选二手机", createdAt=instant, updatedAt=instant,
            domainState={"shoppingGuide": {"category": "phone", "mode": "recommend",
                "useCases": ["user_preference:wechat_video:微信视频"],
                "brandAvoidances": [{"values": ["apple"], "strength": "hard", "source": "user"}],
                "requirements": [{"key": "brand", "operator": "eq", "value": "huawei", "unit": "text",
                                  "priority": "soft", "source": "user"}], "evidenceStatus": "missing"}})
        with patch.object(settings, "shopping_state_authority", "legacy"), patch.object(settings, "context_history_v1_enabled", True):
            pack = await build_context_pack(state)
            before = pack.model_dump(mode="json")
            view = ContextProjector(pack).final_answer_view(validated_results=[])
            self.assertIn("apple", view.answer_preferences["brandAvoidances"][0]["values"])
            self.assertTrue(view.answer_preferences["useCases"])
            self.assertTrue(view.answer_preferences["softPreferences"])
            self.assertEqual(before, pack.model_dump(mode="json"))
