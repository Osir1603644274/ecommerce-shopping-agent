"""V2 Record→Replay shadow tests — DAY-1 no-side-effect determinism.

The shadow re-executes the authoritative V1 run's observable business flow
through the V2 graph against a STRICT replay transport, a captured LLM reply
log, and an ISOLATED task-state store.  V2 makes zero live tool calls, writes
zero business state to the real Redis store, and every divergence is
fail-closed: the official answer always remains the V1 result.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app import graph as graph_mod
from app import task_state
from app.agent_trace import TraceBuilder
from app.graph import (
    CaptureLLMClient,
    ReplayLLMClient,
    build_isolated_task_state_client,
    build_v1_reference,
    isolate_task_state_store,
    read_recorded_tool_names,
    run_graph_v2_shadow,
)
from app.graph.nodes import _node_event, node_code_source
from app.harness import build_validated_guide_result, run_harness_step
from app.planner import PLANNER_SUBMISSION_TOOL_NAME
from app.react_graph import ReActGraphRuntime, run_controlled_react_graph
from app.schemas import ToolTrace
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    update_task_state,
)
from app.tool_transport import RecordTransport, ReplayTransport
from app.tools import TOOL_SCHEMAS
from tests.fake_redis import FakeRedis
from tests.two_stage_ranking_fixtures import two_stage_search_detail

_OS_HARD_REQ = {
    "key": "os", "operator": "eq", "value": "ios",
    "unit": "enum", "priority": "hard", "source": "user",
}


def _fake_client(create_mock: AsyncMock):
    return SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=create_mock),
        )
    )


def _search_products_schema() -> dict:
    for spec in TOOL_SCHEMAS:
        if spec["function"]["name"] == "search_products":
            return spec
    raise AssertionError("TOOL_SCHEMAS must ship search_products")


async def _fake_tool(name: str, arguments: dict):
    if name == "search_products":
        return ToolTrace(
            tool=name, ok=True, detail=two_stage_search_detail([101, 102])
        )
    raise AssertionError(f"unexpected tool dispatch: {name}")


def _planner_ask_user_reply():
    """A REAL model-reply shape (needs_user_input → planner LLM path)."""
    payload = {"outcome": "needs_user_input", "question": "你指的是哪一家星河咖啡？"}
    call = SimpleNamespace(
        id="planner-call",
        function=SimpleNamespace(
            name=PLANNER_SUBMISSION_TOOL_NAME,
            arguments=json.dumps(payload, ensure_ascii=False),
        ),
    )
    message = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _used_phone_create_request() -> TaskStateCreateRequest:
    return TaskStateCreateRequest(
        goal="想找 iOS 二手机。",
        task_type="ecommerce_guide",
        domain_state={
            "shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "useCases": [],
                "requirements": [_OS_HARD_REQ],
                "candidateIds": [],
                "comparedIds": [],
                "evidenceStatus": "missing",
            }
        },
    )


class GraphV2ShadowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Each IsolatedAsyncioTestCase runs on a fresh event loop; the ContextVar
        # override is task-local, so a clean FakeRedis per test is sufficient.
        task_state._client = FakeRedis()
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    def tearDown(self):
        self._tmp.cleanup()
        task_state._client = FakeRedis()

    def _tmp_dir(self) -> Path:
        return Path(self._tmp.name)

    async def _ready_used_phone_state(self) -> TaskState:
        created = await create_task_state(_used_phone_create_request())
        return await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )

    async def _record_v1_search_run(self):
        """Run the authoritative V1 graph against the real store.

        Returns (v1_graph_state, initial_state, recording_path, capture_client,
        v1_trace) — everything the shadow needs to re-execute deterministically.
        """
        ready = await self._ready_used_phone_state()
        recording_path = self._tmp_dir() / "recording.jsonl"
        transport = RecordTransport(
            output_path=recording_path, run_id="shadow-v1"
        )

        async def _record_caller(tool_name: str, arguments: dict):
            return await transport(tool_name, arguments, _fake_tool)

        capture = CaptureLLMClient(_fake_client(AsyncMock()))
        v1_trace = TraceBuilder("shadow-v1", mode="context_pack")
        v1_runtime = ReActGraphRuntime(
            user_message=ready.goal,
            client=capture,
            model="test-model",
            resolve_tool_schemas=lambda s: [_search_products_schema()],
            step_runner=run_harness_step,
            tool_caller=_record_caller,
            trace_builder=v1_trace,
            projector=None,
            max_transitions=8,
        )
        v1_state = await run_controlled_react_graph(ready, v1_runtime)
        await transport.finalize(v1_trace.finish())
        return v1_state, ready, recording_path, capture, v1_trace

    def _v1_reference(self, v1_state, ready, recording_path):
        return build_v1_reference(
            final_action=v1_state["action"],
            transition_limit_reached=v1_state["transition_limit_reached"],
            final_task_state=v1_state["task_state"],
            guide_result=build_validated_guide_result(v1_state["task_state"]),
            tool_names=read_recorded_tool_names(recording_path),
            phase_order=["planner", "executor", "validator"],
            request_id="shadow-e2e-v1",
        )

    async def _run_shadow(
        self,
        *,
        initial_task_state,
        v1_reference,
        recording_path,
        captured,
        evidence_dir,
    ):
        isolated = FakeRedis()
        replay = ReplayTransport(replay_path=recording_path, strict=True)
        return await run_graph_v2_shadow(
            initial_task_state=initial_task_state,
            v1_reference=v1_reference,
            v2_client=ReplayLLMClient(captured),
            replay_transport=replay,
            isolated_client=isolated,
            shadow_trace_builder=TraceBuilder("shadow-v2", mode="graph_v2_shadow"),
            user_message=initial_task_state.goal,
            model="test-model",
            resolve_tool_schemas=lambda s: [_search_products_schema()],
            system_policies=None,
            max_transitions=8,
            max_replans=3,
            evidence_dir=evidence_dir,
            projector=None,
            recording_path=recording_path,
            request_id="shadow-e2e-v2",
        )

    async def test_isolated_client_honors_db_over_url_path(self):
        """The web-path isolation client must pin DB 15 even when the
        configured Redis URL carries a ``/0`` (or any) path suffix.

        ``redis.from_url(url, db=15)`` lets the URL path override the keyword
        arg — a subtle landmine that would make the "isolated" store the real
        DB 0.  Guard it directly (no network: connection pools are lazy).
        """
        for url in (
            "redis://127.0.0.1:16380/0",
            "redis://127.0.0.1:16380/",
            "redis://127.0.0.1:16380",
            "redis://user:pass@127.0.0.1:16380/2",
        ):
            client = build_isolated_task_state_client(redis_url=url)
            kwargs = client.connection_pool.connection_kwargs
            self.assertEqual(kwargs.get("db"), 15, url)

    async def test_shadow_replays_v1_with_zero_live_side_effects(self):
        v1_state, ready, recording_path, capture, v1_trace = (
            await self._record_v1_search_run()
        )
        v1_reference = self._v1_reference(v1_state, ready, recording_path)

        # Snapshot the REAL store before the shadow — the shadow must not
        # write a single byte of business state into it.
        real_store = task_state._client
        key = task_state._state_key(ready.task_id)
        real_before = await real_store.get(key)

        evidence_dir = self._tmp_dir() / "evidence"
        outcome = await self._run_shadow(
            initial_task_state=ready,
            v1_reference=v1_reference,
            recording_path=recording_path,
            captured=capture.captured,
            evidence_dir=evidence_dir,
        )

        # The shadow matched V1 exactly on every comparable check.
        self.assertIsNone(outcome["shadowError"])
        self.assertTrue(outcome["matched"])
        for name, ok in outcome["comparison"]["checks"].items():
            self.assertTrue(ok, name)

        v2 = outcome["v2Summary"]
        self.assertEqual(v2["toolCallCount"], 1)
        self.assertEqual(v2["toolConsumedCount"], 1)
        self.assertEqual(v2["requestedToolNames"], ["search_products"])
        self.assertEqual(v2["nodeSequence"], ["entry", "planner", "executor", "validator"])
        # The deterministic ecommerce plan needed no LLM reply — and even so
        # every captured reply was consumed (0 == total, nothing outstanding).
        self.assertEqual(v2["llmTotalCount"], 0)
        self.assertEqual(v2["llmConsumedCount"], 0)

        # Zero live tool calls: strict replay consumed the whole recording
        # (any attempt to reach a live tool would have raised and set an error).
        self.assertEqual(
            v2["toolCallCount"], v2["toolConsumedCount"]
        )

        # Zero business-writes: the real Redis store is byte-identical.
        self.assertEqual(await real_store.get(key), real_before)

        # The evidence bundle is complete and honest.
        manifest = json.loads(
            Path(outcome["manifestPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["matched"], True)
        self.assertIsNone(manifest["shadowError"])
        for artifact in ("node_events.jsonl", "v1_reference.json",
                         "v2_summary.json", "diff.json"):
            self.assertTrue((evidence_dir / artifact).exists())
            self.assertIn(artifact, manifest["artifactSha256"])

    async def test_shadow_replay_miss_fails_closed(self):
        v1_state, ready, recording_path, capture, _ = await self._record_v1_search_run()
        # Craft a recording whose replay exhausts immediately: a session header
        # and NO tool calls.  V2's executor still requests search_products, the
        # strict transport has nothing to replay, and the shadow must fail
        # closed — never raising, never touching the real store, never
        # reproducing (and thereby masking) the V1 answer.
        empty_recording = self._tmp_dir() / "recording-empty.jsonl"
        with open(empty_recording, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "session", "runId": "shadow-miss"}) + "\n")

        real_store = task_state._client
        key = task_state._state_key(ready.task_id)
        real_before = await real_store.get(key)

        v1_reference = self._v1_reference(v1_state, ready, empty_recording)

        evidence_dir = self._tmp_dir() / "evidence-miss"
        outcome = await self._run_shadow(
            initial_task_state=ready,
            v1_reference=v1_reference,
            recording_path=empty_recording,
            captured=capture.captured,
            evidence_dir=evidence_dir,
        )

        # The divergence is detected by the comparison, not swallowed silently:
        # the replay consumed zero of the one requested tool call, so
        # allToolCallsConsumed fails and the terminal action diverges from the
        # official V1 task_completed.  The shadow itself completed without
        # crashing (a tool-call miss degrades to a bounded stop_turn).
        self.assertIsNone(outcome["shadowError"])
        self.assertFalse(outcome["matched"])
        self.assertFalse(outcome["comparison"]["checks"]["allToolCallsConsumed"])
        self.assertNotEqual(
            outcome["v2Summary"]["finalAction"], v1_state["action"]
        )
        self.assertEqual(outcome["v2Summary"]["toolCallCount"], 1)
        self.assertEqual(outcome["v2Summary"]["toolConsumedCount"], 0)
        # The official V1 outcome was not mutated by the failed shadow.
        self.assertEqual(await real_store.get(key), real_before)
        manifest = json.loads(
            Path(outcome["manifestPath"]).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["matched"], False)

    async def test_capture_then_replay_llm_client_is_deterministic(self):
        """The captured LLM reply replays into the same tool-call decision."""
        inner = _fake_client(AsyncMock(return_value=_planner_ask_user_reply()))
        capture = CaptureLLMClient(inner)
        response = await capture.chat.completions.create(model="test-model")
        self.assertEqual(len(capture.captured), 1)

        replay = ReplayLLMClient(capture.captured)
        replayed = await replay.chat.completions.create(model="test-model")
        call = replayed.choices[0].message.tool_calls[0]
        self.assertEqual(call.function.name, PLANNER_SUBMISSION_TOOL_NAME)
        self.assertEqual(replay.consumed_count, 1)
        self.assertEqual(replay.total_count, 1)
        # Exhaustion is fail-closed: a second reply that V1 never produced
        # raises instead of hallucinating a deterministic answer.
        with self.assertRaises(IndexError):
            await replay.chat.completions.create(model="test-model")

    async def test_shadow_deterministic_across_repeated_runs(self):
        v1_state, ready, recording_path, capture, _ = (
            await self._record_v1_search_run()
        )
        v1_reference = self._v1_reference(v1_state, ready, recording_path)

        outcomes = []
        for i in range(2):
            outcome = await self._run_shadow(
                initial_task_state=ready,
                v1_reference=v1_reference,
                recording_path=recording_path,
                captured=capture.captured,
                evidence_dir=self._tmp_dir() / f"evidence-repeat-{i}",
            )
            self.assertTrue(outcome["matched"])
            outcomes.append(outcome)

        # Same business summary, same diff, same redacted node sequence.
        # planId is fresh per-run identity (uuid) and deliberately not part of
        # the comparable contract — the comparison only binds planToolNames.
        for outcome in outcomes:
            outcome["v2Summary"].pop("planId", None)
        self.assertEqual(outcomes[0]["v2Summary"], outcomes[1]["v2Summary"])
        self.assertEqual(
            outcomes[0]["comparison"], outcomes[1]["comparison"]
        )

        def _project_events(manifest_path: str) -> list[dict]:
            events_dir = Path(manifest_path).parent
            events = []
            for line in (events_dir / "node_events.jsonl").read_text(
                encoding="utf-8"
            ).splitlines():
                event = json.loads(line)
                # durationMs is wall-clock; redactedStateHash embeds the
                # per-run plan id.  Both are intentionally excluded.
                event.pop("durationMs", None)
                event.pop("redactedStateHash", None)
                events.append(event)
            return events

        self.assertEqual(
            _project_events(outcomes[0]["manifestPath"]),
            _project_events(outcomes[1]["manifestPath"]),
        )

    # ── Concurrency-safe isolation (CODEX Attempt 001 HOLD) ──────────────────
    #
    # The shadow must scope the isolated client to its OWN coroutine.  A
    # concurrent authoritative request (a separate asyncio task, its context
    # captured before the override) must keep reading the real store for the
    # whole window; nothing may swap a process-global ``task_state._client``.

    async def test_concurrent_authoritative_request_sees_real_client_during_shadow(self):
        real = task_state._client
        isolated = FakeRedis()
        shadow_entered = asyncio.Event()
        results: dict[str, object] = {}

        async def authoritative():
            # This task exists BEFORE the shadow override, exactly like an
            # in-flight V1 request in the server.  It must see the real client.
            await shadow_entered.wait()
            results["concurrent"] = task_state._get_client()

        task = asyncio.create_task(authoritative())
        async with task_state.override_task_state_client(isolated):
            results["shadow"] = task_state._get_client()
            shadow_entered.set()
            await task

        self.assertIs(results["shadow"], isolated)
        self.assertIs(results["concurrent"], real)
        self.assertIs(task_state._get_client(), real)

    async def test_override_unwinds_on_exception_nested_and_consecutive(self):
        real = task_state._client
        iso1 = FakeRedis()
        iso2 = FakeRedis()

        async with task_state.override_task_state_client(iso1):
            self.assertIs(task_state._get_client(), iso1)
        self.assertIs(task_state._get_client(), real)

        with self.assertRaises(RuntimeError):
            async with task_state.override_task_state_client(iso1):
                raise RuntimeError("boom")
        self.assertIs(task_state._get_client(), real)

        async with task_state.override_task_state_client(iso1):
            async with task_state.override_task_state_client(iso2):
                self.assertIs(task_state._get_client(), iso2)
            self.assertIs(task_state._get_client(), iso1)
        self.assertIs(task_state._get_client(), real)

        async with task_state.override_task_state_client(iso1):
            pass
        async with task_state.override_task_state_client(iso2):
            pass
        self.assertIs(task_state._get_client(), real)

        # A task started after the override window never sees a leaked override.
        seen: dict[str, object] = {}
        async def late_reader():
            seen["late"] = task_state._get_client()
        await asyncio.create_task(late_reader())
        self.assertIs(seen["late"], real)

    async def test_shadow_isolation_scopes_real_store_from_concurrent_reader(self):
        """End-to-end: while the V2 graph runs under ``isolate_task_state_store``,
        a pre-existing authoritative reader still reads the real TaskState value.
        """
        ready = await self._ready_used_phone_state()
        real_key = task_state._state_key(ready.task_id)
        await task_state._client.set(real_key, "v1-value")

        isolated = FakeRedis()
        shadow_entered = asyncio.Event()
        results: dict[str, object] = {}

        async def authoritative_reader():
            await shadow_entered.wait()
            results["seen_during_shadow"] = await task_state._get_client().get(
                real_key
            )

        reader = asyncio.create_task(authoritative_reader())
        async with isolate_task_state_store(isolated):
            shadow_entered.set()
            # The graph itself sees only the isolated store.
            self.assertIs(task_state._get_client(), isolated)
            await reader
        self.assertEqual(results["seen_during_shadow"], "v1-value")
        self.assertIs(task_state._get_client(), task_state._client)

    # ── Node-event codeSource must be real repository files ──────────────────

    def test_node_event_code_sources_resolve_to_real_repo_files(self):
        repo_root = Path(__file__).resolve().parent.parent.parent
        for node in ("entry", "planner", "executor", "validator", "replanner"):
            source = node_code_source(node)
            self.assertTrue(
                (repo_root / source).is_file(),
                f"{node} event points at missing file {source}",
            )
            event = _node_event(
                node, "start", current_state=None, entered_because="regression"
            )
            self.assertEqual(event["codeSource"], source)

    def test_dual_import_tree_override_boundary(self):
        """Two import trees mean two ContextVars; production uses exactly one.

        The suite runs ``app.*`` while the server runs ``agent.app.*`` — distinct
        module objects in one process, each with its own override ContextVar.
        An override set on one tree must not reach the other, and the server
        never mixes the trees (uvicorn loads ``agent.app.main``).  This locks the
        boundary so a future mixed-tree import cannot split TaskState reads
        across two live overrides.
        """
        import sys
        root = str(Path(__file__).resolve().parent.parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        import app.task_state as a
        import agent.app.task_state as b

        self.assertIsNot(a, b)
        self.assertIsNot(a._TASK_STATE_CLIENT_OVERRIDE, b._TASK_STATE_CLIENT_OVERRIDE)

        a._client = FakeRedis()
        b._client = FakeRedis()
        iso_a = FakeRedis()

        async def probe():
            async with a.override_task_state_client(iso_a):
                self.assertIs(a._get_client(), iso_a)
                self.assertIs(b._get_client(), b._client)

        asyncio.run(probe())
        self.assertIs(a._get_client(), a._client)
        self.assertIs(b._get_client(), b._client)
