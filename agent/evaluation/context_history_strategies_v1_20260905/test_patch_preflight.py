"""All PatchRequest shape errors must be caught before the repair boundary."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.app.task_state import TaskState


class PatchPreflightTests(unittest.TestCase):
    def test_fact_replace_conflict_is_repairable_before_persistence(self):
        instant = datetime.now(timezone.utc)
        state = TaskState(taskId="preflight-task", taskType="ecommerce_guide", sessionId="preflight-session",
            status="ready", revision=1, goal="买手机", createdAt=instant, updatedAt=instant,
            domainState={"shoppingGuide": {"category": "phone", "mode": "recommend", "requirements": [
                {"key": "price_minor", "operator": "lte", "value": 200000, "unit": "CNY_MINOR", "priority": "hard", "source": "user"}]}})
        arguments = {"status": "ready", "upsertFacts": [{"key": "inspection_time", "value": "工作日晚六点", "source": "user"}],
            "removeFactKeys": ["inspection_time"]}
        with patch.object(settings, "context_history_v1_enabled", True), patch.object(settings, "shopping_state_authority", "legacy"):
            with self.assertRaises(llm.TaskStatePayloadValidationError) as caught:
                llm._build_validated_task_state_payload(state, arguments, message="验机改到工作日晚六点", require_status=True)
            self.assertIn("upsert", str(caught.exception))
            # Valid replacement is an upsert only. The server does not silently
            # choose between conflicting model operations.
            payload, _ = llm._build_validated_task_state_payload(state,
                {key: value for key, value in arguments.items() if key != "removeFactKeys"},
                message="验机改到工作日晚六点", require_status=True)
            self.assertEqual(payload["upsertFacts"][0]["value"], "工作日晚六点")


if __name__ == "__main__":
    unittest.main()
