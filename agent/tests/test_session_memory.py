import unittest

from app import session_memory
from app.session_memory import (
    MAX_SESSIONS,
    MAX_SESSION_TURNS,
    MAX_TASK_HISTORIES_PER_SESSION,
    clear_all_sessions,
    clear_session,
    get_history,
    save_turn,
)
from tests.fake_redis import FakeRedis


class SessionMemoryTests(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        session_memory._client = FakeRedis()
        await clear_all_sessions()

    async def test_sessions_are_isolated(self):
        await save_turn("session-a", [{"role": "user", "content": "我想找咖啡店"}])
        await save_turn("session-b", [{"role": "user", "content": "我想找酒店"}])

        self.assertEqual((await get_history("session-a"))[0]["content"], "我想找咖啡店")
        self.assertEqual((await get_history("session-b"))[0]["content"], "我想找酒店")

    async def test_keeps_only_recent_turns(self):
        for number in range(MAX_SESSION_TURNS + 2):
            await save_turn(
                "session-a",
                [{"role": "user", "content": f"第 {number} 轮"}],
            )

        history = await get_history("session-a")

        self.assertEqual(len(history), MAX_SESSION_TURNS)
        self.assertEqual(history[0]["content"], "第 2 轮")

    async def test_returns_copy_of_history(self):
        await save_turn("session-a", [{"role": "user", "content": "原始内容"}])

        history = await get_history("session-a")
        history[0]["content"] = "被修改"

        self.assertEqual((await get_history("session-a"))[0]["content"], "原始内容")

    async def test_task_histories_are_isolated_within_one_session(self):
        await save_turn(
            "session-a",
            [{"role": "user", "content": "骑车去公园"}],
            "task-riding",
        )
        await save_turn(
            "session-a",
            [{"role": "user", "content": "选一台电脑"}],
            "task-shopping",
        )

        riding = await get_history("session-a", "task-riding")
        shopping = await get_history("session-a", "task-shopping")

        self.assertEqual([message["content"] for message in riding], ["骑车去公园"])
        self.assertEqual([message["content"] for message in shopping], ["选一台电脑"])

    async def test_can_migrate_legacy_session_history_to_one_task(self):
        await save_turn(
            "session-a",
            [{"role": "user", "content": "旧版历史"}],
        )

        migrated = await get_history(
            "session-a",
            "task-current",
            migrate_legacy=True,
        )

        self.assertEqual(migrated[0]["content"], "旧版历史")
        self.assertEqual(await get_history("session-a"), [])
        self.assertEqual(
            (await get_history("session-a", "task-current"))[0]["content"],
            "旧版历史",
        )

    async def test_clear_session_removes_every_task_history(self):
        await save_turn("session-a", [{"role": "user", "content": "A"}], "task-a")
        await save_turn("session-a", [{"role": "user", "content": "B"}], "task-b")

        await clear_session("session-a")

        self.assertEqual(await get_history("session-a", "task-a"), [])
        self.assertEqual(await get_history("session-a", "task-b"), [])

    async def test_limits_task_history_count_within_one_session(self):
        for number in range(MAX_TASK_HISTORIES_PER_SESSION + 1):
            await save_turn(
                "session-a",
                [{"role": "user", "content": str(number)}],
                f"task-{number}",
            )

        self.assertEqual(await get_history("session-a", "task-0"), [])
        newest = await get_history(
            "session-a",
            f"task-{MAX_TASK_HISTORIES_PER_SESSION}",
        )
        self.assertEqual(newest[0]["content"], str(MAX_TASK_HISTORIES_PER_SESSION))

    async def test_limits_total_session_count(self):
        for number in range(MAX_SESSIONS + 1):
            await save_turn(
                f"session-{number}",
                [{"role": "user", "content": str(number)}],
            )

        self.assertEqual(await get_history("session-0"), [])
        self.assertEqual((await get_history(f"session-{MAX_SESSIONS}"))[0]["content"], str(MAX_SESSIONS))
