"""Durable V2 revision-reconciliation tests (DAY2 — requirement #6/#8).

``_reconcile_revision`` classifies checkpoint vs live TaskState revision on a
restart: ``ok`` (equal / fresh), ``fast_forward`` (live is ahead ONLY by
same-run progress the durable runner can attest — the v2RunMarker lifecycle
marker or an authentic v2ExecReceipt), ``diverged`` (unrelated drift — fail
closed, a stale checkpoint must never resume over a newer TaskState).

Unit tests pin the classification matrix (equal / live-older / marker / receipt /
foreign run / foreign thread / stale receipt).  The integration test proves the
fail-closed end-to-end: after the dangerous-window fault a FOREIGN actor removes
the attestations and advances the live TaskState, and the restart returns
``state_diverged`` with ZERO additional tools and no business write.
"""
from __future__ import annotations

import hashlib
import json
import asyncio
import shutil
import socket
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app import task_state
from app.agent_trace import TraceBuilder
from app.control.planning import (
    PlanArgumentSource,
    PlanStep,
    TaskPlan,
    transition_plan_step_status,
)
from app.graph.checkpoint import GraphV2CheckpointSaver
from app.graph.nodes import _hydrate_task_state, _reconcile_revision
from app.graph.nodes.executor import ExecutorFaultInjected, set_executor_fault_hook
from app.graph.resume import (
    build_thread_id,
    run_graph_v2_durable,
    session_owner_hash,
)
from app.graph.runtime import GraphV2Runtime
from app.graph.tool_inbox_v2 import ToolInbox, ToolInboxSlot, sha256
from app.schemas import ToolTrace
from app.tool_execution_v2 import ToolInboxCallerV2
from app.executor import (
    _mark_durable_inbox,
    build_executor_step_context,
    claim_executor_step,
    recover_expired_executor_claim,
)
from app.settings import settings
from app.task_state import (
    TaskState,
    TaskStateCreateRequest,
    TaskStatePatchRequest,
    create_task_state,
    get_task_state,
    update_task_state,
)
from app.tools import TOOL_SCHEMAS
from tests.two_stage_ranking_fixtures import two_stage_search_detail

# ── self-contained Redis fake (not FakeRedis: not in the DAY2 allowlist) ──────


class InFileRedis:
    """Minimal async plain-Redis fake with the full surface the durable stack
    touches: strings/lists/hashes/zsets + ``eval`` dispatching on ``numkeys``
    (TaskState CAS = 1 key; saver CAS = 3 keys).  Per-key TTL is recorded so
    tests can assert checkpoint keys carry the 7-day TTL."""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, int]] = {}
        self.ttls: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.strings.pop(key, None)

    async def set(self, key: str, value: str, **kwargs) -> bool:
        self.strings[key] = value
        ex = kwargs.get("ex") or kwargs.get("EX")
        if ex:
            self.ttls[key] = int(ex)
        return True

    @staticmethod
    def _slice(values: list[str], start: int, end: int) -> list[str]:
        length = len(values)
        start = max(length + start, 0) if start < 0 else start
        end = length + end if end < 0 else end
        if start >= length or start > end:
            return []
        return values[start : end + 1]

    async def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self._slice(self.lists.get(key, []), start, end)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.lists[key] = self._slice(self.lists.get(key, []), start, end)

    async def hset(self, key: str, field: str, value: str) -> int:
        created = field not in self.hashes.setdefault(key, {})
        self.hashes[key][field] = value
        return int(created)

    async def hsetnx(self, key: str, field: str, value: str) -> int:
        fields = self.hashes.setdefault(key, {})
        if field in fields:
            return 0
        fields[field] = value
        return 1

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        fields = self.hashes.setdefault(key, {})
        current = int(fields.get(field, 0))
        fields[field] = str(current + amount)
        return current + amount

    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        values = self.sorted_sets.setdefault(key, {})
        new_members = sum(member not in values for member in mapping)
        values.update(mapping)
        return new_members

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        members = [
            member
            for member, _ in sorted(
                self.sorted_sets.get(key, {}).items(), key=lambda item: item[1]
            )
        ]
        return self._slice(members, start, end)

    async def zrem(self, key: str, *members: str) -> int:
        values = self.sorted_sets.get(key, {})
        removed = sum(values.pop(member, None) is not None for member in members)
        return removed

    async def expire(self, key: str, seconds: int) -> bool:
        exists = (
            key in self.strings
            or key in self.lists
            or key in self.hashes
            or key in self.sorted_sets
        )
        if exists:
            self.ttls[key] = int(seconds)
        return exists

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.strings.pop(key, None) is not None)
            deleted += int(self.lists.pop(key, None) is not None)
            deleted += int(self.hashes.pop(key, None) is not None)
            deleted += int(self.sorted_sets.pop(key, None) is not None)
            self.ttls.pop(key, None)
        return deleted

    async def scan_iter(self, match: str = "*", count: int = 10):
        import re

        pattern = re.compile("^" + match.replace("*", ".*") + "$")
        for key in (
            set(self.strings)
            | set(self.lists)
            | set(self.hashes)
            | set(self.sorted_sets)
        ):
            if pattern.match(key):
                yield key

    async def eval(self, script: str, numkeys: int, *args):
        if "GRAPH_V2_PUT_WRITES" in script and numkeys == 1:
            return self._put_writes(*args)
        if numkeys == 1 and len(args) == 4:
            return self._task_state_cas(*args)
        if numkeys == 3 and len(args) == 7:
            return self._saver_cas(*args)
        raise NotImplementedError(
            f"InFileRedis cannot eval numkeys={numkeys} args={len(args)}"
        )

    def _put_writes(self, key: str, ttl_raw: str, count_raw: str, *args):
        fields = self.hashes.setdefault(key, {})
        call_ord = int(fields.get("__ord", 0)) + 1
        fields["__ord"] = str(call_ord)
        for within in range(int(count_raw)):
            mode, field, encoded = args[within * 3 : within * 3 + 3]
            envelope = json.loads(encoded)
            envelope["o"] = f"{call_ord:05d}:{within:05d}"
            rewritten = json.dumps(envelope, separators=(",", ":"))
            if mode != "nx" or field not in fields:
                fields[field] = rewritten
        self.ttls[key] = int(ttl_raw)
        return call_ord

    def _task_state_cas(self, key: str, expected_raw: str, payload: str, ttl: str):
        raw = self.strings.get(key)
        if raw is None:
            return [-1, -1]
        actual = int(json.loads(raw)["revision"])
        expected = int(expected_raw)
        if actual != expected:
            return [0, actual]
        self.strings[key] = payload
        self.ttls[key] = int(ttl)
        return [1, expected + 1]

    def _saver_cas(
        self,
        cp_key: str,
        latest_key: str,
        step_key: str,
        envelope: str,
        checkpoint_id: str,
        step_raw: str,
        ttl_raw: str,
    ):
        self.strings[cp_key] = envelope
        self.ttls[cp_key] = int(ttl_raw)
        cur = self.strings.get(step_key)
        step = int(step_raw)
        if cur is None or step >= int(cur):
            self.strings[latest_key] = checkpoint_id
            self.strings[step_key] = step_raw
            self.ttls[latest_key] = int(ttl_raw)
            self.ttls[step_key] = int(ttl_raw)
            return 1
        return 0


# ── shared fixtures ───────────────────────────────────────────────────────────


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


def _get_product_details_schema() -> dict:
    for spec in TOOL_SCHEMAS:
        if spec["function"]["name"] == "get_product_details":
            return spec
    raise AssertionError("TOOL_SCHEMAS must ship get_product_details")


def _fake_tool(name: str, arguments: dict):
    if name == "search_products":
        return ToolTrace(
            tool=name, ok=True, detail=two_stage_search_detail([101, 102])
        )
    if name == "get_product_details":
        return ToolTrace(
            tool=name, ok=True,
            detail={"productIds": [101, 102], "products": [{"id": 101}, {"id": 102}]},
        )
    raise AssertionError(f"unexpected tool dispatch: {name}")


def _used_phone_create_request() -> TaskStateCreateRequest:
    return TaskStateCreateRequest(
        goal="想找 iOS 二手机。",
        task_type="ecommerce_guide",
        session_id="session-a",
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


def _plan(based_on_revision: int) -> TaskPlan:
    return TaskPlan(
        plan_id="plan-reconcile",
        based_on_revision=based_on_revision,
        steps=[
            PlanStep(
                step_id="step-1",
                description="搜索二手手机",
                tool_name="search_products",
                arguments={"query": "iphone"},
                argument_sources={"query": PlanArgumentSource(kind="task_goal")},
                expected_output={"productIds": ["101", "102"]},
            )
        ],
    )


# ── tests ────────────────────────────────────────────────────────────────────


class GraphV2RevisionReconcileTests(unittest.IsolatedAsyncioTestCase):
    """Unit matrix for ``_reconcile_revision``."""

    def setUp(self):
        self.redis = InFileRedis()
        task_state._client = self.redis
        task_state._task_locks.clear()
        task_state._session_locks.clear()

    async def asyncSetUp(self) -> None:
        """The attestation positive lane must use an authoritative Redis inbox.

        TaskState remains intentionally backed by the small in-file store so the
        revision matrix stays focused.  Its receipt projection is accepted only
        when this independent Redis ledger returns the exact SUCCEEDED receipt.
        """
        executable = shutil.which("redis-server")
        if executable is None:
            self.skipTest("redis-server is required for durable Inbox attestation tests")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self._inbox_port = probe.getsockname()[1]
        self._inbox_process = subprocess.Popen(
            [executable, "--port", str(self._inbox_port), "--save", "", "--appendonly", "no"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        import redis.asyncio as redis_async

        self._inbox_client = redis_async.Redis(host="127.0.0.1", port=self._inbox_port)
        for _ in range(40):
            try:
                await self._inbox_client.ping()
                break
            except Exception:  # noqa: BLE001 - owned Redis startup polling
                await asyncio.sleep(0.05)
        else:
            self.fail("owned Redis did not start")
        self._inbox = ToolInbox(self._inbox_client, lease_ms=1_000, ttl_ms=5_000)

        async def caller(name: str, _arguments: dict, _context: object) -> ToolTrace:
            return ToolTrace(tool=name, ok=True, durationMs=1, detail={"items": []})

        self._durable_boundary = ToolInboxCallerV2(inbox=self._inbox, caller=caller)

    async def asyncTearDown(self) -> None:
        await self._inbox_client.aclose()
        self._inbox_process.terminate()
        try:
            self._inbox_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._inbox_process.kill()
            self._inbox_process.wait(timeout=3)

    def _reconcile_runtime(self, *, run_id="run-x", thread_id=None) -> GraphV2Runtime:
        return GraphV2Runtime(
            user_message="test",
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda s: [
                _search_products_schema(), _get_product_details_schema(),
            ],
            tool_caller=AsyncMock(side_effect=_fake_tool),
            trace_builder=TraceBuilder(run_id, mode="context_pack"),
            projector=None,
            max_transitions=8,
            task_id="task-x",
            run_id=run_id,
            thread_id=thread_id or build_thread_id("task-x", run_id),
            session_owner_hash=session_owner_hash("session-a"),
            durable=True,
            durable_tool_boundary=self._durable_boundary,
        )

    async def _live_with_attestation(
        self, attestation: str | None, *, run_id="run-x", thread_id=None
    ) -> TaskState:
        """Build a TaskState carrying a persisted plan + one attestation.

        The plan is written first (revision 3), the attestation last
        (revision 4), so the receipt/``revision`` self-consistency invariant
        (``receipt.revision == live.revision``) holds exactly.
        """
        created = await create_task_state(_used_phone_create_request())
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision,
                actor="agent",
                status="ready",
            ),
        )
        planned = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                active_plan=_plan(based_on_revision=ready.revision),
            ),
        )
        patch: dict[str, object] = {}
        if attestation == "marker":
            patch["v2RunMarker"] = {
                "runId": run_id,
                "threadId": thread_id or build_thread_id("task-x", run_id),
                "stateRevision": planned.revision + 1,
                "planId": "plan-reconcile",
                "sessionOwnerHash": session_owner_hash("session-a"),
                "payloadSha256": hashlib.sha256("test".encode("utf-8")).hexdigest(),
            }
            patch["v2UserMessage"] = "test"
        elif attestation == "receipt":
            # A TaskState receipt is merely a projection.  Manufacture the
            # projection by actually completing the matching Redis Inbox slot.
            arguments = {"query": "iphone"}
            slot = ToolInboxSlot.create(
                task_id=planned.task_id,
                plan_id="plan-reconcile",
                step_id="step-1",
                state_revision=planned.revision,
                tool_name="search_products",
                canonical_args_sha256=sha256(arguments),
            )
            delivery = await self._durable_boundary.execute(
                slot=slot,
                run_id=run_id,
                thread_id=thread_id or build_thread_id("task-x", run_id),
                session_owner_hash=session_owner_hash("session-a"),
                tool_name="search_products",
                arguments=arguments,
            )
            # Model the only accepted durable chain: claim, PREPARED,
            # IN_FLIGHT, result projection, then receipt projection.  The
            # final projection revision is what prevents an old receipt from
            # authorizing a later unrelated OCC write.
            executing = await update_task_state(
                planned.task_id,
                TaskStatePatchRequest(
                    expectedRevision=planned.revision,
                    actor="agent",
                    status="executing",
                    activePlan=transition_plan_step_status(
                        planned.active_plan, "step-1", "executing"
                    ),
                ),
            )
            prepared = await update_task_state(
                executing.task_id,
                TaskStatePatchRequest(
                    expectedRevision=executing.revision,
                    actor="agent",
                    domain_state_patch={"recoveryChain": "prepared"},
                ),
            )
            inflight = await update_task_state(
                prepared.task_id,
                TaskStatePatchRequest(
                    expectedRevision=prepared.revision,
                    actor="agent",
                    domain_state_patch={"recoveryChain": "in-flight"},
                ),
            )
            persisted = await update_task_state(
                inflight.task_id,
                TaskStatePatchRequest(
                    expectedRevision=inflight.revision,
                    actor="agent",
                    status="ready",
                    activePlan=transition_plan_step_status(
                        inflight.active_plan, "step-1", "executed"
                    ),
                ),
            )
            receipt_hash = hashlib.sha256(
                json.dumps(
                    delivery.receipt,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return await update_task_state(
                persisted.task_id,
                TaskStatePatchRequest(
                    expectedRevision=persisted.revision,
                    actor="agent",
                    domain_state_patch={
                        "v2ExecReceipt": delivery.receipt,
                        "v2ExecReceiptProjection": {
                            "receiptHash": receipt_hash,
                            "projectionRevision": persisted.revision + 1,
                        },
                    },
                ),
            )
        elif attestation == "pseudo-receipt":
            # This has a plausible TaskState shape but no Redis SUCCEEDED
            # record.  It must never fast-forward a stale checkpoint.
            patch["v2ExecReceipt"] = {
                "taskId": planned.task_id,
                "runId": run_id,
                "threadId": thread_id or build_thread_id("task-x", run_id),
                "sessionOwnerHash": session_owner_hash("session-a"),
                "planId": "plan-reconcile",
                "stepId": "step-1",
                "toolName": "search_products",
                "stateRevision": planned.revision,
                "inputHash": "a" * 64,
                "resultHash": "b" * 64,
                "executionId": "c" * 64,
                "logicalSlotKey": "d" * 64,
                "fence": 1,
                "inboxStatus": "SUCCEEDED",
            }
        elif attestation == "foreign-marker-run":
            patch["v2RunMarker"] = {
                "runId": "run-other",
                "threadId": build_thread_id("task-x", "run-other"),
            }
        elif attestation == "foreign-marker-thread":
            patch["v2RunMarker"] = {
                "runId": run_id,
                "threadId": build_thread_id("task-x", "run-other"),
            }
        if patch:
            attested = await update_task_state(
                planned.task_id,
                TaskStatePatchRequest(
                    expectedRevision=planned.revision,
                    actor="agent",
                    domain_state_patch=patch,
                ),
            )
            return attested
        return planned

    async def test_reconcile_ok_when_equal(self):
        live = await self._live_with_attestation("marker")
        deps = self._reconcile_runtime()
        self.assertEqual(
            await _reconcile_revision({"graph_revision": live.revision}, deps, live),
            "ok",
        )

    async def test_reconcile_ok_when_no_checkpoint_revision(self):
        live = await self._live_with_attestation("marker")
        deps = self._reconcile_runtime()
        self.assertEqual(
            await _reconcile_revision({}, deps, live), "ok"
        )

    async def test_reconcile_ok_when_not_durable(self):
        live = await self._live_with_attestation(None)
        deps = self._reconcile_runtime()
        deps = GraphV2Runtime(
            **{**deps.__dict__, "durable": False}
        )
        self.assertEqual(
            await _reconcile_revision(
                {"graph_revision": live.revision - 1}, deps, live
            ),
            "ok",
        )

    async def test_reconcile_diverged_when_live_older(self):
        live = await self._live_with_attestation("marker")
        deps = self._reconcile_runtime()
        self.assertEqual(
            await _reconcile_revision({"graph_revision": live.revision + 1}, deps, live),
            "diverged",
        )

    async def test_reconcile_fast_forward_via_same_run_marker(self):
        live = await self._live_with_attestation("marker")
        deps = self._reconcile_runtime()
        self.assertEqual(
            await _reconcile_revision({"graph_revision": live.revision - 1}, deps, live),
            "fast_forward",
        )

    async def test_same_run_marker_without_exact_revision_plan_payload_fails_closed(self):
        live = await self._live_with_attestation("marker")
        raw = dict(live.domain_state["v2RunMarker"])
        for field, value in (("stateRevision", live.revision - 1), ("planId", "other-plan"), ("payloadSha256", "a" * 64), ("sessionOwnerHash", "b" * 16)):
            with self.subTest(field=field):
                marker = dict(raw)
                marker[field] = value
                tampered = live.model_copy(
                    update={"domain_state": {**live.domain_state, "v2RunMarker": marker}}
                )
                self.assertEqual(
                    await _reconcile_revision(
                        {"graph_revision": live.revision - 1},
                        self._reconcile_runtime(),
                        tampered,
                    ),
                    "diverged",
                )

    async def test_reconcile_diverged_when_marker_foreign_run(self):
        live = await self._live_with_attestation("foreign-marker-run")
        deps = self._reconcile_runtime()
        self.assertEqual(
            await _reconcile_revision({"graph_revision": live.revision - 1}, deps, live),
            "diverged",
        )

    async def test_reconcile_diverged_when_marker_foreign_thread(self):
        live = await self._live_with_attestation("foreign-marker-thread")
        deps = self._reconcile_runtime()
        self.assertEqual(
            await _reconcile_revision({"graph_revision": live.revision - 1}, deps, live),
            "diverged",
        )

    async def test_reconcile_fast_forward_via_authentic_exec_receipt(self):
        live = await self._live_with_attestation("receipt")
        deps = self._reconcile_runtime()
        self.assertEqual(
            await _reconcile_revision({"graph_revision": live.revision - 1}, deps, live),
            "fast_forward",
        )

    async def test_reconcile_diverged_for_taskstate_only_pseudo_receipt(self):
        live = await self._live_with_attestation("pseudo-receipt")
        self.assertEqual(
            await _reconcile_revision(
                {"graph_revision": live.revision - 1}, self._reconcile_runtime(), live
            ),
            "diverged",
        )

    async def test_reconcile_diverged_when_receipt_foreign_run(self):
        live = await self._live_with_attestation("receipt")
        deps = self._reconcile_runtime(run_id="run-other")
        # The receipt attests run-x, but this process is run-other: fail closed.
        self.assertEqual(
            await _reconcile_revision({"graph_revision": live.revision - 1}, deps, live),
            "diverged",
        )

    async def test_reconcile_diverged_when_old_receipt_is_followed_by_occ_drift(self):
        """An old receipt cannot attest an unrelated later TaskState write."""
        live = await self._live_with_attestation("receipt")
        drifted = await update_task_state(
            live.task_id,
            TaskStatePatchRequest(
                expectedRevision=live.revision,
                actor="system",
                domain_state_patch={"foreignDrift": "must-not-fast-forward"},
            ),
        )
        self.assertEqual(
            await _reconcile_revision(
                {"graph_revision": live.revision - 1},
                self._reconcile_runtime(),
                drifted,
            ),
            "diverged",
        )

    async def test_hydrate_rejects_checkpoint_task_id_that_differs_from_runtime(self):
        deps = self._reconcile_runtime()
        with patch("app.graph.nodes.get_task_state", new=AsyncMock()) as read_live:
            with self.assertRaisesRegex(RuntimeError, "checkpoint task_id"):
                await _hydrate_task_state({"task_id": "task-other"}, deps)
        read_live.assert_not_awaited()

    async def test_succeeded_inbox_recovers_exact_step_without_second_tool_call(self):
        created = await create_task_state(_used_phone_create_request())
        ready = await update_task_state(
            created.task_id,
            TaskStatePatchRequest(
                expectedRevision=created.revision, actor="agent", status="ready"
            ),
        )
        run_id = "run-recover"
        thread_id = build_thread_id(ready.task_id, run_id)
        marked = await update_task_state(
            ready.task_id,
            TaskStatePatchRequest(
                expectedRevision=ready.revision,
                actor="agent",
                domain_state_patch={
                    "v2RunMarker": {
                        "runId": run_id,
                        "threadId": thread_id,
                        "sessionOwnerHash": session_owner_hash("session-a"),
                    },
                },
            ),
        )
        recovery_plan = _plan(marked.revision).model_copy(
            update={
                "steps": [
                    _plan(marked.revision).steps[0].model_copy(
                        update={"expected_output": {"requiresProductCandidates": True}}
                    )
                ]
            }
        )
        planned = await update_task_state(
            marked.task_id,
            TaskStatePatchRequest(
                expectedRevision=marked.revision,
                actor="agent",
                active_plan=recovery_plan,
            ),
        )
        context = build_executor_step_context(planned)
        claimed = await claim_executor_step(planned, context, durable=True)
        arguments = {"query": "iphone"}
        slot = ToolInboxSlot.create(
            task_id=claimed.task_id,
            plan_id=context.plan_id,
            step_id=context.step.step_id,
            state_revision=context.expected_revision,
            tool_name=context.step.tool_name,
            canonical_args_sha256=sha256(arguments),
        )
        prepared = await _mark_durable_inbox(
            claimed,
            context,
            status="PREPARED",
            canonical_args_sha256=slot.canonical_args_sha256,
        )
        crossings: list[str] = []

        async def recovery_caller(
            name: str, call_arguments: dict, _context: object
        ) -> ToolTrace:
            crossings.append(name)
            return _fake_tool(name, call_arguments)

        boundary = ToolInboxCallerV2(inbox=self._inbox, caller=recovery_caller)
        delivery = await boundary.execute(
            slot=slot,
            run_id=run_id,
            thread_id=thread_id,
            session_owner_hash=session_owner_hash("session-a"),
            tool_name="search_products",
            arguments=arguments,
        )
        inflight = await _mark_durable_inbox(
            prepared,
            context,
            status="IN_FLIGHT",
            canonical_args_sha256=slot.canonical_args_sha256,
        )
        expired = inflight.model_copy(
            update={
                "domain_state": {
                    **inflight.domain_state,
                    "executorLease": {
                        **inflight.domain_state["executorLease"],
                        "expiresAt": "2000-01-01T00:00:00+00:00",
                    },
                }
            }
        )
        recovered = await recover_expired_executor_claim(
            expired, durable_inbox=self._inbox
        )
        self.assertEqual(recovered.active_plan.steps[0].status, "executed")
        self.assertEqual(recovered.domain_state["v2ExecReceipt"], delivery.receipt)
        self.assertEqual(
            recovered.domain_state["v2ExecReceiptProjection"]["projectionRevision"],
            recovered.revision,
        )
        self.assertEqual(crossings, ["search_products"])


class GraphV2RevisionConflictIntegrationTests(GraphV2RevisionReconcileTests):
    """End-to-end fail-closed proofs through ``run_graph_v2_durable``."""

    def _saver(self) -> GraphV2CheckpointSaver:
        return GraphV2CheckpointSaver(serde=JsonPlusSerializer())

    def _durable_kwargs(
        self,
        task_id: str,
        tool_caller: AsyncMock,
        trace: TraceBuilder,
        *,
        restart=False,
    ) -> dict:
        async def tool_caller_v2(name: str, arguments: dict, _context: object) -> ToolTrace:
            return await tool_caller(name, arguments)

        return dict(
            task_id=task_id,
            session_id="session-a",
            restart=restart,
            user_message="想找 iOS 二手机。",
            client=_fake_client(AsyncMock()),
            model="test-model",
            resolve_tool_schemas=lambda s: [
                _search_products_schema(), _get_product_details_schema(),
            ],
            tool_caller=tool_caller,
            tool_caller_v2=tool_caller_v2,
            tool_inbox=self._inbox,
            trace_builder=trace,
            max_transitions=8,
        )

    @staticmethod
    def _search_call_count(tool_caller: AsyncMock) -> int:
        return len(
            [c for c in tool_caller.await_args_list if c.args[0] == "search_products"]
        )

    async def test_foreign_drift_restart_fails_closed_state_diverged_zero_tools(self):
        """Unrelated drift (attestations removed + revision advanced by a foreign
        actor) must fail closed: ``state_diverged`` boundary, no tool, no write."""
        def fault(step_id: str):
            raise ExecutorFaultInjected(step_id)

        previous_fault = settings.agent_graph_v2_fault_point
        set_executor_fault_hook(fault)
        settings.agent_graph_v2_fault_point = "after_executor_receipt"
        try:
            created = await create_task_state(_used_phone_create_request())
            ready = await update_task_state(
                created.task_id,
                TaskStatePatchRequest(
                    expectedRevision=created.revision,
                    actor="agent",
                    status="ready",
                ),
            )
            tool_caller = AsyncMock(side_effect=_fake_tool)
            fresh = await run_graph_v2_durable(
                **self._durable_kwargs(
                    ready.task_id, tool_caller,
                    TraceBuilder("run-fault", mode="context_pack"),
                ),
                checkpointer=self._saver(),
            )
            self.assertEqual(fresh.boundary, "fault_injected")
            self.assertEqual(self._search_call_count(tool_caller), 1)

            # A foreign actor strips the same-run attestations and advances the
            # live TaskState (e.g. a different orchestrator adopts the task).
            live_before = await get_task_state(ready.task_id)
            drift = await update_task_state(
                ready.task_id,
                TaskStatePatchRequest(
                    expectedRevision=live_before.revision,
                    actor="system",
                    domain_state_patch={
                        "v2RunMarker": None,
                        "v2ExecReceipt": None,
                        "driftNote": "adopted-by-new-orchestrator",
                    },
                ),
            )
            self.assertGreater(drift.revision, live_before.revision)
            domain = drift.domain_state or {}
            self.assertNotIn("v2RunMarker", domain)
            self.assertNotIn("v2ExecReceipt", domain)

            restarted = await run_graph_v2_durable(
                **self._durable_kwargs(
                    ready.task_id, tool_caller,
                    TraceBuilder("run-restart", mode="context_pack"),
                    restart=True,
                ),
                checkpointer=self._saver(),
            )
            self.assertEqual(restarted.boundary, "state_diverged")
            self.assertEqual(restarted.mode, "restart")
            self.assertEqual(restarted.terminal_outcome, "STATE_DIVERGED")
            self.assertEqual(restarted.thread_id, fresh.thread_id)
            # Fail closed: no additional tool, and the graph wrote nothing.
            self.assertEqual(self._search_call_count(tool_caller), 1)
            self.assertEqual(restarted.revision, drift.revision)
        finally:
            settings.agent_graph_v2_fault_point = previous_fault
            set_executor_fault_hook(None)


if __name__ == "__main__":
    unittest.main()
