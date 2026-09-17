"""Regression for a new clarification receipt after answering the old one."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent.app.graph import resume as module
from agent.app.settings import settings


class ResumeReaskTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, *, boundary, interrupted):
        task, run, owner = "task-history-reask", "run-history-reask", "session-owner"
        thread = module.build_thread_id(task, run)
        marker = {"runId": run, "threadId": thread, "sessionOwnerHash": owner,
            "controlPolicy": "react_v1", "policyRevision": module._policy_revision("react_v1")}
        live = SimpleNamespace(revision=3, domain_state={"v2RunMarker": marker})
        payload = module.ResumePayload(task_id=task, run_id=run, thread_id=thread,
            revision=3, proposal_hash="old-question-hash", answer="自用二手手机")
        parked = {"type": "clarification", "taskId": task, "threadId": thread,
            "revision": 3, "proposalHash": "old-question-hash", "question": "购买用途？"}
        graph = SimpleNamespace(aget_state=AsyncMock(return_value=SimpleNamespace(values={},
            interrupts=[SimpleNamespace(value=parked)])))
        fresh = {**parked, "revision": 6, "proposalHash": "new-question-hash", "question": "预算多少？"}
        completed = module.DurableRunResult(graph_state={}, task_state=live, boundary=boundary,
            mode="resume", run_id=run, thread_id=thread, interrupted=interrupted,
            interrupt_payload=fresh if interrupted else None,
            proposal_hash="new-question-hash" if interrupted else None)
        with patch.object(settings, "context_history_v1_enabled", True), \
             patch.object(module, "read_task_cursor", AsyncMock(return_value=marker)), \
             patch.object(module, "_invoke", AsyncMock(return_value=completed)):
            return await module._run_resume(graph=graph, saver=object(), task_id=task, payload=payload,
                live=live, runtime=SimpleNamespace(control_policy="react_v1"),
                trace_builder=object(), session_owner_hash_value=owner)

    async def test_new_interrupt_keeps_its_new_proposal_hash(self):
        result = await self.invoke(boundary="clarification", interrupted=True)
        self.assertEqual(result.proposal_hash, "new-question-hash")
        self.assertEqual(result.proposal_hash, result.interrupt_payload["proposalHash"])

    async def test_terminal_receipt_keeps_answered_proposal_hash(self):
        result = await self.invoke(boundary="task_completed", interrupted=False)
        self.assertEqual(result.proposal_hash, "old-question-hash")


if __name__ == "__main__":
    unittest.main()
