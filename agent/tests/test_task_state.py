import asyncio
import json
import unittest

from fastapi.testclient import TestClient

from app import task_state
from app.main import app
from app.task_state import (
    TaskRelationDecision,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    TaskStateRevisionConflictError,
    TaskStateTransitionError,
    apply_session_task_relation,
    clear_session_task_state,
    create_task_state,
    get_or_create_session_task_state,
    get_session_task_state,
    list_session_task_states,
    list_task_events,
    update_task_state,
)
from tests.fake_redis import FakeRedis


class TaskStateStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def test_session_reuses_active_task_and_clear_removes_it(self):
        first, first_created = await get_or_create_session_task_state(
            "session-runtime",
            "我想骑车去公园",
        )
        second, second_created = await get_or_create_session_task_state(
            "session-runtime",
            "从北京西站出发",
        )

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.task_id, second.task_id)

        await clear_session_task_state("session-runtime")

        self.assertIsNone(await get_session_task_state("session-runtime"))
        self.assertIsNone(await task_state.get_task_state(first.task_id))

    async def test_session_can_pause_new_task_and_resume_previous_task(self):
        transitions = []

        async def collect(state, phase):
            transitions.append((phase, state.task_id, state.status))

        riding, _created = await get_or_create_session_task_state(
            "session-multi-task",
            "我想骑车去公园",
        )
        shopping = await apply_session_task_relation(
            "session-multi-task",
            "先帮我选一台电脑",
            TaskRelationDecision(
                relation="start_new",
                reason="用户明确提出了独立的电脑导购目标",
                confidence=0.98,
            ),
            on_transition=collect,
        )

        paused_riding = await task_state.get_task_state(riding.task_id)
        self.assertEqual(paused_riding.status, "paused")
        self.assertNotEqual(riding.task_id, shopping.task_id)
        self.assertEqual(
            (await get_session_task_state("session-multi-task")).task_id,
            shopping.task_id,
        )

        resumed = await apply_session_task_relation(
            "session-multi-task",
            "算了，还是今天去骑行吧",
            TaskRelationDecision(
                relation="resume_previous",
                targetTaskId=riding.task_id,
                reason="用户明确回到之前暂停的骑行目标",
                confidence=0.99,
            ),
            on_transition=collect,
        )

        paused_shopping = await task_state.get_task_state(shopping.task_id)
        self.assertEqual(paused_shopping.status, "paused")
        self.assertEqual(resumed.task_id, riding.task_id)
        self.assertEqual(resumed.status, "collecting_information")
        self.assertEqual(
            (await get_session_task_state("session-multi-task")).task_id,
            riding.task_id,
        )
        self.assertEqual(
            [state.task_id for state in await list_session_task_states("session-multi-task")],
            [riding.task_id, shopping.task_id],
        )
        self.assertEqual(
            [phase for phase, _task_id, _status in transitions],
            ["task_paused", "task_started", "task_paused", "task_resumed"],
        )

        await clear_session_task_state("session-multi-task")
        self.assertIsNone(await task_state.get_task_state(riding.task_id))
        self.assertIsNone(await task_state.get_task_state(shopping.task_id))

    async def test_session_cancel_keeps_task_history_but_clears_active_pointer(self):
        active, _created = await get_or_create_session_task_state(
            "session-cancel-task",
            "帮我选一台电脑",
        )
        cancelled = await apply_session_task_relation(
            "session-cancel-task",
            "电脑不用选了",
            TaskRelationDecision(
                relation="cancel_current",
                reason="用户明确取消当前电脑导购任务",
                confidence=0.99,
            ),
        )

        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(await get_session_task_state("session-cancel-task"))
        recent = await list_session_task_states("session-cancel-task")
        self.assertEqual([state.task_id for state in recent], [active.task_id])
        self.assertEqual(recent[0].status, "cancelled")

    async def test_create_and_patch_task_state_with_event_history(self):
        created = await create_task_state(
            TaskStateCreateRequest(
                taskType="beijing_trip",
                goal="规划一次半日出行",
                sessionId="session-task-state",
                unknowns=["origin", "destination"],
                pendingQuestions=["从哪里出发？"],
            )
        )

        self.assertTrue(created.task_id.startswith("task-"))
        self.assertEqual(created.status, "collecting_information")
        self.assertEqual(created.revision, 1)
        self.assertEqual(created.unknowns, ["origin", "destination"])

        updated = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=1,
                actor="user",
                upsertFacts=[
                    {
                        "key": "origin",
                        "value": {
                            "placeId": "beijing-place-demo",
                            "name": "北京西站",
                        },
                        "certainty": "confirmed",
                        "source": "user",
                    }
                ],
                upsertConstraints=[
                    {
                        "key": "maxTravelMinutes",
                        "operator": "lte",
                        "value": 30,
                        "source": "user",
                    },
                    {
                        "key": "transportMode",
                        "operator": "eq",
                        "value": "cycling",
                        "source": "user",
                    },
                ],
                resolveUnknowns=["origin"],
                pendingQuestions=["想去什么类型的地点？"],
                domainStatePatch={"city": "北京"},
            ),
        )

        self.assertEqual(updated.revision, 2)
        self.assertEqual(updated.facts[0].key, "origin")
        self.assertEqual(updated.facts[0].certainty, "confirmed")
        self.assertEqual(
            [constraint.key for constraint in updated.constraints],
            ["maxTravelMinutes", "transportMode"],
        )
        self.assertEqual(updated.unknowns, ["destination"])
        self.assertEqual(updated.domain_state, {"city": "北京"})

        events = await list_task_events(created.task_id)
        self.assertEqual(
            [(event.event_type, event.revision) for event in events],
            [("created", 1), ("updated", 2)],
        )
        self.assertEqual(events[-1].actor, "user")
        self.assertIn("upsertFacts", events[-1].changes)

    async def test_patch_rejects_stale_revision(self):
        created = await create_task_state(
            TaskStateCreateRequest(goal="测试 revision")
        )
        await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=1,
                addUnknowns=["origin"],
            ),
        )

        with self.assertRaises(TaskStateRevisionConflictError) as context:
            await update_task_state(
                created.task_id,
                TaskStatePatchRequest(
                    expectedRevision=1,
                    addUnknowns=["destination"],
                ),
            )

        self.assertEqual(context.exception.expected, 1)
        self.assertEqual(context.exception.actual, 2)

    async def test_two_writers_with_same_revision_only_one_commits(self):
        created = await create_task_state(
            TaskStateCreateRequest(goal="cross-process CAS")
        )
        first = TaskStatePatchRequest(
            expectedRevision=created.revision,
            domainStatePatch={"winner": "first"},
        )
        second = TaskStatePatchRequest(
            expectedRevision=created.revision,
            domainStatePatch={"winner": "second"},
        )

        outcomes = await asyncio.gather(
            update_task_state(created.task_id, first),
            update_task_state(created.task_id, second),
            return_exceptions=True,
        )

        committed = [item for item in outcomes if isinstance(item, task_state.TaskState)]
        conflicts = [
            item for item in outcomes
            if isinstance(item, TaskStateRevisionConflictError)
        ]
        self.assertEqual(len(committed), 1)
        self.assertEqual(len(conflicts), 1)
        latest = await task_state.get_task_state(created.task_id)
        self.assertEqual(latest.revision, created.revision + 1)
        self.assertIn(latest.domain_state["winner"], {"first", "second"})

    async def test_patch_rejects_invalid_status_transition(self):
        created = await create_task_state(
            TaskStateCreateRequest(goal="测试状态机")
        )

        with self.assertRaises(TaskStateTransitionError):
            await update_task_state(
                created.task_id,
                TaskStatePatchRequest(
                    expectedRevision=1,
                    status="completed",
                ),
            )

    async def test_ready_task_cannot_complete_without_completed_plan(self):
        created = await create_task_state(
            TaskStateCreateRequest(goal="测试完成状态约束")
        )
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )

        with self.assertRaises(TaskStateTransitionError):
            await update_task_state(
                ready.task_id,
                TaskStatePatchRequest(
                    expectedRevision=ready.revision,
                    actor="agent",
                    status="completed",
                ),
            )

    async def test_create_trace_reports_real_storage_steps(self):
        trace_events = []

        async def collect(event):
            trace_events.append(event)

        created = await create_task_state(
            TaskStateCreateRequest(goal="观察 TaskState 创建过程"),
            trace=collect,
        )

        self.assertEqual(created.revision, 1)
        self.assertEqual(
            [event["step"] for event in trace_events],
            [
                "request_validated",
                "state_assembled",
                "snapshot_saved",
                "snapshot_ttl_refreshed",
                "event_appended",
                "events_trimmed",
                "event_ttl_refreshed",
                "operation_completed",
            ],
        )
        self.assertEqual(
            trace_events[2]["data"]["revision"],
            1,
        )


class TaskStateEndpointTests(unittest.TestCase):
    def setUp(self):
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()
        self.client = TestClient(app)

    def test_task_state_api_create_read_patch_and_list_events(self):
        create_response = self.client.post(
            "/agent/tasks",
            json={
                "taskType": "beijing_trip",
                "goal": "骑行前往半小时内可达的公园",
                "unknowns": ["origin", "destination"],
            },
        )
        self.assertEqual(create_response.status_code, 201)
        created = create_response.json()
        task_id = created["taskId"]
        self.assertEqual(created["revision"], 1)

        read_response = self.client.get(f"/agent/tasks/{task_id}")
        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(read_response.json(), created)

        patch_response = self.client.patch(
            f"/agent/tasks/{task_id}",
            json={
                "expectedRevision": 1,
                "upsertFacts": [
                    {
                        "key": "origin",
                        "value": "北京西站",
                        "certainty": "confirmed",
                        "source": "user",
                    }
                ],
                "upsertConstraints": [
                    {
                        "key": "transportMode",
                        "operator": "eq",
                        "value": "cycling",
                        "source": "user",
                    }
                ],
                "resolveUnknowns": ["origin"],
            },
        )
        self.assertEqual(patch_response.status_code, 200)
        updated = patch_response.json()
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["unknowns"], ["destination"])

        events_response = self.client.get(
            f"/agent/tasks/{task_id}/events"
        )
        self.assertEqual(events_response.status_code, 200)
        self.assertEqual(
            [event["eventType"] for event in events_response.json()],
            ["created", "updated"],
        )

        conflict_response = self.client.patch(
            f"/agent/tasks/{task_id}",
            json={
                "expectedRevision": 1,
                "addUnknowns": ["departureTime"],
            },
        )
        self.assertEqual(conflict_response.status_code, 409)
        self.assertEqual(
            conflict_response.json()["detail"]["actualRevision"],
            2,
        )

    def test_task_state_api_returns_not_found(self):
        response = self.client.get("/agent/tasks/task-missing")

        self.assertEqual(response.status_code, 404)

    def test_public_task_api_rejects_server_owned_domain_state(self):
        create_response = self.client.post(
            "/agent/tasks",
            json={
                "taskType": "ecommerce_guide",
                "goal": "伪造候选范围",
                "domainState": {"candidateScope": {"scopeId": "forged"}},
            },
        )
        self.assertEqual(create_response.status_code, 403)

        created = self.client.post(
            "/agent/tasks",
            json={"taskType": "ecommerce_guide", "goal": "正常用户任务"},
        ).json()
        patch_response = self.client.patch(
            f"/agent/tasks/{created['taskId']}",
            json={
                "expectedRevision": 1,
                "actor": "system",
                "domainStatePatch": {
                    "candidateScope": {"scopeId": "forged"},
                },
            },
        )
        self.assertEqual(patch_response.status_code, 403)
        self.assertEqual(
            self.client.get(f"/agent/tasks/{created['taskId']}").json()["revision"],
            1,
        )

    def test_task_state_trace_endpoint_streams_steps_and_completion(self):
        response = self.client.post(
            "/agent/tasks/trace",
            json={
                "taskType": "beijing_trip",
                "goal": "观察一次真实创建请求",
                "unknowns": ["origin"],
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual(events[0]["type"], "step")
        self.assertEqual(events[0]["step"], "request_validated")
        self.assertEqual(events[-1]["type"], "complete")
        self.assertEqual(events[-1]["state"]["revision"], 1)

    def test_task_state_lab_page_is_available(self):
        response = self.client.get("/task-state-lab")

        self.assertEqual(response.status_code, 200)
        self.assertIn("TaskState 观察台", response.text)
