from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock
from openai.types.chat import ChatCompletion

from agent.app.context_history import HistoryArchive
from agent.app.context_input import experimental_context_input, phase_context, is_experimental_context_input
from agent.app.task_state import TaskState
from .context_client import ContextClient
from .history_strategies import HistoryPolicy, HistoryStrategies


class InputBoundaryTests(IsolatedAsyncioTestCase):
    async def test_above_window_request_is_preserved_without_model_call(self):
        from .artifacts import sha
        from .history_strategies import tokens
        with tempfile.TemporaryDirectory(prefix="window-rejection-") as temporary:
            directory = Path(temporary)
            archive = HistoryArchive(directory / "raw", session_id="session", task_id="task", create=True)
            archive.append("user", "完整历史不能截断" * 1000, turn=1)
            instant = datetime.now(timezone.utc)
            state = TaskState(taskId="task", taskType="ecommerce_guide", sessionId="session", status="ready",
                revision=1, goal="选手机", createdAt=instant, updatedAt=instant)
            create = AsyncMock()
            base = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            client = ContextClient(base, arm="A_FULL_HISTORY", history=HistoryStrategies(archive, HistoryPolicy(input_budget=1000)),
                task_id="task", session_id="session", query="继续", output=directory / "inputs.jsonl",
                state_loader=AsyncMock(return_value=state))
            with experimental_context_input():
                tagged = phase_context("final_answer", {"taskId": "task"})
            with self.assertRaisesRegex(ValueError, "application_input_budget_exceeded"):
                await client.create(messages=[{"role": "system", "content": json.dumps(tagged)}])
            create.assert_not_awaited()
            record = json.loads((directory / "preflight-rejected-001.json").read_text(encoding="utf-8"))
            self.assertFalse(record["modelCalled"])
            self.assertEqual(record["requestSha256"], sha(record["request"]))
            self.assertEqual(record["applicationRequestTokens"], tokens(record["request"]))

    async def test_model_requested_lookup_is_read_only_and_charged(self):
        from .history_lookup import NAME
        with tempfile.TemporaryDirectory(prefix="gateway-lookup-") as temporary:
            directory = Path(temporary)
            archive = HistoryArchive(directory / "raw", session_id="session", task_id="task", create=True)
            original = archive.append("user", "原预算为2000元，后来才改了", turn=1)
            instant = datetime.now(timezone.utc)
            state = TaskState(taskId="task", taskType="ecommerce_guide", sessionId="session",
                status="ready", revision=1, goal="选手机", createdAt=instant, updatedAt=instant)
            response1 = ChatCompletion(id="read", object="chat.completion", created=1, model="test",
                choices=[{"index": 0, "finish_reason": "tool_calls", "message": {"role": "assistant",
                    "content": None, "tool_calls": [{"id": "read-1", "type": "function", "function": {
                        "name": NAME, "arguments": json.dumps({"operation": "turn", "selector": "1", "limit": 2})}}]}}])
            response2 = ChatCompletion(id="answer", object="chat.completion", created=1, model="test",
                choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "原预算2000元。"}}])
            create = AsyncMock(side_effect=[response1, response2])
            base = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            client = ContextClient(base, arm="B_PACK_VIEW", history=HistoryStrategies(archive, HistoryPolicy()),
                task_id="task", session_id="session", query="第1轮旧预算是什么", output=directory / "inputs.jsonl",
                state_loader=AsyncMock(return_value=state))
            with experimental_context_input():
                tagged = phase_context("final_answer", {"taskId": "task"})
            response = await client.create(messages=[{"role": "system", "content": json.dumps(tagged)}])
            self.assertIs(response, response2)
            self.assertEqual(create.await_count, 2)
            evidence = json.loads(create.call_args.kwargs["messages"][-1]["content"])
            self.assertFalse(evidence["actionAuthorized"])
            self.assertEqual(evidence["records"][0]["messageId"], original["messageId"])
            self.assertTrue((directory / "history_lookups.jsonl").exists())

    def test_default_off_and_scope_reset(self):
        source = {"goal": "phone"}
        self.assertIs(phase_context("final_answer", source), source)
        with experimental_context_input():
            self.assertEqual(phase_context("final_answer", source)["payload"], source)
        self.assertFalse(is_experimental_context_input())

    async def test_a_c_exact_request_equality_and_b_uses_phase_view(self):
        with tempfile.TemporaryDirectory(prefix="boundary-unit-") as temporary:
            directory = Path(temporary)
            archive = HistoryArchive(directory / "raw", session_id="session", task_id="task", create=True)
            archive.append("user", "第一轮：安卓是偏好，不要苹果硬过滤。" + "长消息尾部" * 80, turn=1)
            instant = datetime.now(timezone.utc)
            state = TaskState(taskId="task", taskType="ecommerce_guide", sessionId="session",
                              status="ready", revision=1, goal="选手机", createdAt=instant, updatedAt=instant)
            with experimental_context_input():
                tagged = phase_context("extraction", {"taskId": "task", "goal": "PACK_GOAL_SENTINEL",
                    "history_summaries": [{"summary": "LEGACY_CLIPPED"}], "allowedTools": []})
            request = {"messages": [{"role": "system", "content": json.dumps(tagged)}]}
            requests = {}
            for arm in ("A_FULL_HISTORY", "B_PACK_VIEW", "C_LLM_THRESHOLD"):
                create = AsyncMock(return_value=ChatCompletion(id="test", object="chat.completion", created=1,
                    model="test", choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}]))
                base = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                client = ContextClient(base, arm=arm, history=HistoryStrategies(archive, HistoryPolicy()),
                    task_id="task", session_id="session", query="当前问题", output=directory / (arm + ".jsonl"),
                    state_loader=AsyncMock(return_value=state))
                await client.create(**request)
                requests[arm] = create.call_args.kwargs
            self.assertEqual(requests["A_FULL_HISTORY"], requests["C_LLM_THRESHOLD"])
            self.assertNotIn("PACK_GOAL_SENTINEL", str(requests["A_FULL_HISTORY"]))
            self.assertNotIn("LEGACY_CLIPPED", str(requests["A_FULL_HISTORY"]))
            self.assertIn("PACK_GOAL_SENTINEL", str(requests["B_PACK_VIEW"]))
            self.assertIn("长消息尾部" * 80, requests["B_PACK_VIEW"]["messages"][0]["content"])

    async def test_untagged_model_route_fails_closed(self):
        base = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())))
        client = ContextClient(base, arm="A_FULL_HISTORY", history=None, task_id="t", session_id="s", query="q", output=None)
        with self.assertRaisesRegex(ValueError, "boundaries"):
            await client.create(messages=[{"role": "user", "content": "unaccounted phase"}])
        base.chat.completions.create.assert_not_awaited()
