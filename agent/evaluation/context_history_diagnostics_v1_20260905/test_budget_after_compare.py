import copy
import unittest
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.app.task_state import TaskState
from agent.app import task_state
from agent.app.domains.ecommerce.shopping_state_authority import select_shopping_state_authority
from agent.tests.fake_redis import FakeRedis
from .budget_after_compare import candidate_semantic_scope, load_case, price, replay


class BudgetAfterCompareTests(unittest.TestCase):
    def test_three_recorded_model_payloads_are_correct_before_canonicalization(self):
        for arm in "ABC":
            with self.subTest(arm=arm):
                result = replay(arm)
                self.assertEqual(result["originalPriceMinor"], 160000)
                self.assertEqual(result["candidatePriceMinor"], 140000)
                self.assertEqual(result["recordedPostPriceMinor"], 160000)

    def test_normal_comparison_still_cannot_invent_filters(self):
        row, arguments, _ = load_case("A")
        state = TaskState.model_validate(row["preState"])
        arguments = copy.deepcopy(arguments)
        arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"].append({
            "key": "screen_originality", "operator": "eq", "value": "original",
            "unit": "enum", "priority": "soft", "source": "inferred:dimension"})
        with patch.object(settings, "context_history_v1_enabled", True), candidate_semantic_scope():
            payload, _ = llm._build_validated_task_state_payload(state, arguments,
                message="比较这两台手机的价格和屏幕证据，预算保持1600元不变。", require_status=True)
        requirements = payload["domainStatePatch"]["shoppingGuide"]["requirements"]
        self.assertEqual(price(requirements), 160000)
        self.assertFalse(any(r["source"] == "inferred:dimension" for r in requirements))

    def test_budget_updates_use_semantic_route_not_number_extraction(self):
        row, _, _ = load_case("A")
        state = TaskState.model_validate(row["preState"])
        with patch.object(settings, "context_history_v1_enabled", True), candidate_semantic_scope():
            for message in (row["query"], "预算改为1400元，其他不变。", "预算恢复为1600元再比较这两款。"):
                with self.subTest(message=message):
                    result, observation = llm._deterministic_used_phone_task_state_decision(state, message)
                    self.assertIsNone(result)
                    self.assertIsNotNone(observation)
            self.assertIsNone(llm._explicit_phone_price_ceiling(row["query"]))

    def test_flag_off_preserves_legacy_behavior(self):
        row, _, _ = load_case("A")
        with patch.object(settings, "context_history_v1_enabled", False), candidate_semantic_scope():
            self.assertFalse(llm._requires_context_semantic_change(row["query"]))

    def test_unmodified_other_requirements_are_preserved(self):
        result = replay("A")
        before = result["originalPayload"]["domainStatePatch"]["shoppingGuide"]["requirements"]
        after = result["candidatePayload"]["domainStatePatch"]["shoppingGuide"]["requirements"]
        self.assertEqual([r for r in before if r["key"] != "price_minor"],
                         [r for r in after if r["key"] != "price_minor"])


class BudgetPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_survives_real_update_and_authority_validation(self):
        for arm in "ABC":
            with self.subTest(arm=arm):
                row, arguments, _ = load_case(arm)
                state = TaskState.model_validate(row["preState"])
                client = FakeRedis()
                await client.set(task_state._state_key(state.task_id), state.model_dump_json(by_alias=True))
                async with task_state.override_task_state_client(client):
                    with patch.object(settings, "context_history_v1_enabled", True), candidate_semantic_scope():
                        updated = await llm._apply_task_state_update(state, arguments,
                            message=row["query"], on_task_state=None, require_status=True)
                        select_shopping_state_authority(domain_state=updated.domain_state,
                            task_id=updated.task_id, task_revision=updated.revision, goal=updated.goal,
                            unknowns=updated.unknowns, pending_questions=updated.pending_questions,
                            mode=settings.shopping_state_authority)
                        self.assertEqual(price(updated.domain_state["shoppingGuide"]["requirements"]), 140000)
                        self.assertEqual(price(updated.domain_state["shoppingTaskStateV2"]["shoppingGuide"]["requirements"]), 140000)
                        self.assertEqual(next(c.value for c in updated.constraints if c.key == "price_minor"), 140000)


if __name__ == "__main__":
    unittest.main()
