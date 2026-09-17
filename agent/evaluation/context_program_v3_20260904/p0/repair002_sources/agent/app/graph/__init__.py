"""V2 control-plane graph: public surface + the no-side-effect shadow.

Public surface re-exports the graph pieces so consumers import from one place:
``GraphV2State`` / ``GraphV2NodeEvent`` (redacted event source), ``GraphV2Runtime``
(static dependencies), the explicit nodes and routers, and ``run_graph_v2``.

The shadow (``run_graph_v2_shadow``) is the Day-1 no-side-effect Record→Replay
harness.  It re-executes the authoritative V1 run's observable business flow
through the V2 graph against:

* a STRICT replay tool transport — V2 makes zero live tool calls and must
  consume V1's recorded tool calls in exactly the recorded order;
* a captured LLM reply log — V2's Planner/Replanner model calls return the
  exact replies V1 received, so the re-execution is deterministic;
* an ISOLATED task-state store — V2 persists business state only into a seeded
  scratch store; the real Redis store is never touched.

Every divergence is fail-closed: the shadow records a diff and the official
answer always remains the V1 result.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from .. import task_state
from ..agent_trace import TraceBuilder
from ..context_view import ContextProjector
from ..harness import build_validated_guide_result
from ..settings import settings
from ..task_state import TaskState
from ..tool_transport import ReplayTransport
from .builder import (
    CONTROLLED_GRAPH_V2,
    GRAPH_V2_NAME,
    build_graph_v2,
    build_graph_v2_durable,
    run_graph_v2,
)
from .checkpoint import GraphV2CheckpointSaver
from .nodes import entry_node
from .nodes.clarification import clarification_node
from .nodes.executor import executor_node
from .nodes.planner import planner_node
from .nodes.replanner import replanner_node
from .nodes.validator import validator_node
from .resume import (
    DurableResumeRejected,
    DurableRunResult,
    ResumePayload,
    build_thread_id,
    is_exact_resolved_resume,
    parse_thread_id,
    read_task_cursor,
    read_terminal_response_receipt,
    resolve_durable_identity,
    run_graph_v2_durable,
    write_task_cursor,
    write_terminal_response_receipt,
)
from .routers import (
    route_after_clarification,
    route_after_entry,
    route_after_entry_durable,
    route_after_executor,
    route_after_executor_durable,
    route_after_planner,
    route_after_planner_durable,
    route_after_replanner,
    route_after_replanner_durable,
    route_after_validator,
    route_after_validator_durable,
)
from .runtime import GraphV2Runtime
from .state import (
    CONTINUE_ACTIONS,
    GraphV2NodeEvent,
    GraphV2State,
    redacted_state_hash,
    redacted_state_snapshot,
)

logger = logging.getLogger(__name__)

__all__ = [
    # graph surface
    "GRAPH_V2_NAME",
    "CONTROLLED_GRAPH_V2",
    "build_graph_v2",
    "run_graph_v2",
    "GraphV2State",
    "GraphV2NodeEvent",
    "GraphV2Runtime",
    "CONTINUE_ACTIONS",
    "redacted_state_hash",
    "redacted_state_snapshot",
    "entry_node",
    "planner_node",
    "executor_node",
    "validator_node",
    "replanner_node",
    "route_after_entry",
    "route_after_planner",
    "route_after_executor",
    "route_after_validator",
    "route_after_replanner",
    # durable Day-2 surface
    "build_graph_v2_durable",
    "GraphV2CheckpointSaver",
    "clarification_node",
    "route_after_entry_durable",
    "route_after_planner_durable",
    "route_after_executor_durable",
    "route_after_validator_durable",
    "route_after_replanner_durable",
    "route_after_clarification",
    "run_graph_v2_durable",
    "ResumePayload",
    "DurableRunResult",
    "DurableResumeRejected",
    "build_thread_id",
    "parse_thread_id",
    "read_task_cursor",
    "write_task_cursor",
    "is_exact_resolved_resume",
    "read_terminal_response_receipt",
    "write_terminal_response_receipt",
    "resolve_durable_identity",
    # shadow
    "CaptureLLMClient",
    "ReplayLLMClient",
    "isolate_task_state_store",
    "build_isolated_task_state_client",
    "seed_isolated_task_state",
    "guide_result_identity",
    "build_v1_reference",
    "build_v2_summary",
    "compare_v1_v2_shadow",
    "run_graph_v2_shadow",
    "read_recorded_tool_names",
]


# ── Guide-result identity ─────────────────────────────────────────────────────


def guide_result_identity(guide_result: Any) -> dict[str, Any] | None:
    """Stable identity projection of a validated guide result.

    The guide result has no UUID, so equality is defined by the contract
    version, category, the ORDERED product ids, the ranked-item count and the
    complete-match flag.  Everything else (titles, attributes, evidence refs,
    price disclosures) is presentation, not business truth.
    """
    if not isinstance(guide_result, dict):
        return None
    products = guide_result.get("products") or []
    return {
        "contractVersion": guide_result.get("contractVersion"),
        "category": guide_result.get("category"),
        "productIds": [
            item.get("product", {}).get("id")
            for item in products
            if isinstance(item, dict)
        ],
        "rankedItemCount": guide_result.get("rankedItemCount"),
        "hasCompleteMatch": guide_result.get("hasCompleteMatch"),
    }


# ── LLM capture / replay (deterministic shadow) ──────────────────────────────


def _extract_reply(response: Any) -> dict[str, Any] | None:
    """Reduce an OpenAI chat-completion response to its replayable decision.

    Only the first choice's tool-call request and content are kept — never the
    full messages, never prompts, never credentials.
    """
    try:
        message = response.choices[0].message
    except Exception:  # noqa: BLE001 - non-conforming response
        return None
    tool_calls: list[dict[str, Any]] = []
    for call in list(getattr(message, "tool_calls", None) or []):
        fn = getattr(call, "function", None)
        if fn is None:
            continue
        raw = getattr(fn, "arguments", None) or ""
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            args = {"__raw__": str(raw)[:2000]}
        tool_calls.append({"name": getattr(fn, "name", ""), "arguments": args})
    return {
        "toolCalls": tool_calls,
        "content": getattr(message, "content", None),
    }


def _replay_response(reply: dict[str, Any]) -> SimpleNamespace:
    """Reconstruct a planner/replanner-consumable response from a capture."""
    tool_calls = [
        SimpleNamespace(
            id=f"call-shadow-{i}",
            type="function",
            function=SimpleNamespace(
                name=item.get("name", ""),
                arguments=json.dumps(
                    item.get("arguments", {}), ensure_ascii=False
                ),
            ),
        )
        for i, item in enumerate(reply.get("toolCalls", []))
    ]
    message = SimpleNamespace(
        tool_calls=tool_calls if tool_calls else None,
        content=reply.get("content"),
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message, index=0)])


class _ShadowCompletions:
    def __init__(self, owner: "CaptureLLMClient | ReplayLLMClient") -> None:
        self._owner = owner

    async def create(self, **_kwargs: Any) -> Any:
        return await self._owner._create()


class _ShadowChat:
    def __init__(self, owner: "CaptureLLMClient | ReplayLLMClient") -> None:
        self.completions = _ShadowCompletions(owner)


class CaptureLLMClient:
    """Wrap the authoritative V1 LLM client and record every chat response.

    The recorded replies are the deterministic specification for the shadow:
    the V2 graph replays them via :class:`ReplayLLMClient` so its Planner and
    Replanner produce exactly the tool-call requests V1 produced.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.captured: list[dict[str, Any]] = []

    @property
    def chat(self) -> _ShadowChat:
        return _ShadowChat(self)

    async def _create(self, **kwargs: Any) -> Any:
        response = await self._inner.chat.completions.create(**kwargs)
        reply = _extract_reply(response)
        if reply is not None:
            self.captured.append(reply)
        return response


class ReplayLLMClient:
    """Deterministic client that serves V1's captured replies to the V2 shadow."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self._replies = list(replies)
        self._index = 0

    @property
    def chat(self) -> _ShadowChat:
        return _ShadowChat(self)

    @property
    def consumed_count(self) -> int:
        return self._index

    @property
    def total_count(self) -> int:
        return len(self._replies)

    async def _create(self, **_kwargs: Any) -> SimpleNamespace:
        if self._index >= len(self._replies):
            raise IndexError(
                f"Shadow LLM replay exhausted: consumed {self._index} of "
                f"{len(self._replies)} captured replies — V2 diverged from V1."
            )
        reply = self._replies[self._index]
        self._index += 1
        return _replay_response(reply)


# ── Isolated task-state store ────────────────────────────────────────────────

# The shadow must never touch the real Redis TaskState store.  It scopes an
# isolated scratch client to the shadow coroutine (``task_state``'s ContextVar
# override), seeds that store with the initial TaskState snapshot, runs the V2
# graph, and unwinds the override — always, even on error.  Concurrent
# authoritative requests run in separate asyncio tasks and keep reading the
# real client, so no process-global is ever swapped.


def _strip_redis_db_path(url: str) -> str:
    """Drop the db index encoded in a redis:// URL path.

    ``redis.from_url("redis://host/0", db=15)`` connects to DB **0** — the URL
    path overrides the ``db=`` keyword — so a ``/N``-suffixed ``settings.redis_url``
    silently defeats the isolation DB.  Strip the path (the db index for TCP
    URLs) so the explicit ``db=`` argument below is honored.  Unix-socket URLs
    keep their path, which names the socket, not a db.
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    if parsed.scheme in ("redis", "rediss") and parsed.path and parsed.path != "/":
        return urlunparse(parsed._replace(path=""))
    return url


def build_isolated_task_state_client(*, redis_url: str | None = None, db: int = 15):
    """Build a scratch Redis client for shadow isolation (web path).

    Uses a separate logical DB so the shadow's writes never collide with the
    real store.  Tests inject a FakeRedis instead of going through here.
    """
    import redis.asyncio as redis

    base_url = _strip_redis_db_path(redis_url or settings.redis_url)
    return redis.from_url(base_url, db=db, decode_responses=True)


async def seed_isolated_task_state(client: Any, state: TaskState) -> None:
    """Seed the isolated store with the initial durable snapshot.

    ``update_task_state`` refuses to update a missing task, so the isolated
    store must contain the exact starting revision before the V2 graph runs.
    """
    key = task_state._state_key(state.task_id)
    payload = state.model_dump_json(by_alias=True)
    await client.set(
        key, payload, ex=task_state.TASK_STATE_TTL_SECONDS
    )
    logger.debug("seeded isolated task-state key=%s revision=%d", key, state.revision)


@asynccontextmanager
async def isolate_task_state_store(client: Any):
    """Scope the TaskState store to an isolated scratch client, coroutine-local.

    Uses ``task_state``'s ContextVar override so only this shadow task (and its
    descendants) reads ``client``; concurrent authoritative requests in other
    asyncio tasks are untouched.  The override unwinds on normal and exceptional
    exit alike and never leaks into a later task.
    """
    async with task_state.override_task_state_client(client):
        yield


async def _noop_live(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("strict shadow replay never calls live tools")


# ── V1 reference & V2 summary ────────────────────────────────────────────────


def read_recorded_tool_names(path: Path) -> list[str]:
    """Read the ordered tool-name sequence from a RecordTransport JSONL file."""
    names: list[str] = []
    if not path.exists():
        return names
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            # RecordedCall lines carry no "type" key; only the session header
            # and the trace baseline are structural metadata to skip.
            if record.get("type") not in ("session", "trace_baseline"):
                names.append(record.get("toolName"))
    return names


def build_v1_reference(
    *,
    final_action: str,
    transition_limit_reached: bool,
    final_task_state: TaskState,
    guide_result: Any,
    tool_names: list[str],
    phase_order: list[str],
    request_id: str = "",
) -> dict[str, Any]:
    """Project the authoritative V1 run into a comparable reference dict."""
    plan = final_task_state.active_plan
    domain = final_task_state.domain_state
    validation = domain.get("validationResult") if isinstance(domain, dict) else None
    return {
        "requestId": request_id,
        "finalAction": final_action,
        "transitionLimitReached": bool(transition_limit_reached),
        "revision": final_task_state.revision,
        "status": final_task_state.status,
        "planId": plan.plan_id if plan is not None else None,
        "planStatus": plan.status if plan is not None else None,
        "planToolNames": [step.tool_name for step in plan.steps] if plan else [],
        "validatorOutcome": (
            validation.get("outcome") if isinstance(validation, dict) else None
        ),
        "guideResultIdentity": guide_result_identity(guide_result),
        "toolNames": list(tool_names),
        "phaseOrder": list(phase_order),
    }


def build_v2_summary(
    *,
    graph_state: GraphV2State,
    tool_call_count: int,
    tool_consumed_count: int,
    requested_tool_names: list[str],
    llm_consumed_count: int,
    llm_total_count: int,
) -> dict[str, Any]:
    """Project the V2 shadow run into the same comparable shape as V1."""
    events = graph_state.get("node_events", [])
    final_state = graph_state.get("task_state")
    plan = final_state.active_plan if final_state is not None else None
    domain = final_state.domain_state if final_state is not None else {}
    validation = domain.get("validationResult") if isinstance(domain, dict) else None
    return {
        "nodeSequence": [
            event.get("nodeName")
            for event in events
            if event.get("phase") == "start"
        ],
        "nodeEventCount": len(events),
        "transitionCount": graph_state.get("transition_count"),
        "replanCount": graph_state.get("replan_count"),
        "finalAction": graph_state.get("action"),
        "terminalOutcome": graph_state.get("terminal_outcome"),
        "degradedReason": graph_state.get("degraded_reason"),
        "transitionLimitReached": bool(graph_state.get("transition_limit_reached")),
        "lastRouteDecision": graph_state.get("last_route_decision"),
        "revision": final_state.revision if final_state is not None else None,
        "status": final_state.status if final_state is not None else None,
        "planId": plan.plan_id if plan is not None else None,
        "planStatus": plan.status if plan is not None else None,
        "planToolNames": [step.tool_name for step in plan.steps] if plan else [],
        "validatorOutcome": (
            validation.get("outcome") if isinstance(validation, dict) else None
        ),
        "guideResultIdentity": (
            guide_result_identity(build_validated_guide_result(final_state))
            if final_state is not None
            else None
        ),
        "toolCallCount": tool_call_count,
        "toolConsumedCount": tool_consumed_count,
        "requestedToolNames": list(requested_tool_names),
        "llmConsumedCount": llm_consumed_count,
        "llmTotalCount": llm_total_count,
    }


def compare_v1_v2_shadow(
    v1_reference: dict[str, Any],
    v2_summary: dict[str, Any],
) -> dict[str, Any]:
    """Compare the V1 reference against the V2 shadow run.

    Returns ``{"matched": bool, "checks": {...}, "diff": {...}}``.  A replay
    mismatch or exception is captured by the caller and reported as a diff —
    the shadow never mutates the official V1 outcome.
    """
    v1_tool_names = list(v1_reference.get("toolNames") or [])
    v2_tool_names = list(v2_summary.get("requestedToolNames") or [])
    checks: dict[str, Any] = {
        "toolNameSequenceMatch": v1_tool_names == v2_tool_names,
        "terminalActionMatch": (
            v1_reference.get("finalAction") == v2_summary.get("finalAction")
        ),
        "transitionLimitMatch": (
            v1_reference.get("transitionLimitReached")
            == v2_summary.get("transitionLimitReached")
        ),
        "revisionMatch": (
            v1_reference.get("revision") == v2_summary.get("revision")
        ),
        "planToolNamesMatch": (
            v1_reference.get("planToolNames") == v2_summary.get("planToolNames")
        ),
        "validatorOutcomeMatch": (
            v1_reference.get("validatorOutcome")
            == v2_summary.get("validatorOutcome")
        ),
        "guideResultIdentityMatch": (
            v1_reference.get("guideResultIdentity")
            == v2_summary.get("guideResultIdentity")
        ),
        "allToolCallsConsumed": (
            v2_summary.get("toolConsumedCount") == v2_summary.get("toolCallCount")
        ),
        "llmReplayConsumed": (
            v2_summary.get("llmTotalCount") == 0
            or v2_summary.get("llmConsumedCount") == v2_summary.get("llmTotalCount")
        ),
        "notDegraded": not bool(v2_summary.get("degradedReason")),
    }
    # Diff shows the ACTUAL compared values per check (V1 reference field vs
    # V2 summary field) so evidence diffs are self-explanatory, not null placeholders.
    _V1_FIELD = {
        "toolNameSequenceMatch": "toolNames",
        "terminalActionMatch": "finalAction",
        "transitionLimitMatch": "transitionLimitReached",
        "revisionMatch": "revision",
        "planToolNamesMatch": "planToolNames",
        "validatorOutcomeMatch": "validatorOutcome",
        "guideResultIdentityMatch": "guideResultIdentity",
        "allToolCallsConsumed": "toolCallCount",
        "llmReplayConsumed": "llmTotalCount",
        "notDegraded": "degradedReason",
    }
    _V2_FIELD = {
        "toolNameSequenceMatch": "requestedToolNames",
        "terminalActionMatch": "finalAction",
        "transitionLimitMatch": "transitionLimitReached",
        "revisionMatch": "revision",
        "planToolNamesMatch": "planToolNames",
        "validatorOutcomeMatch": "validatorOutcome",
        "guideResultIdentityMatch": "guideResultIdentity",
        "allToolCallsConsumed": "toolCallCount",
        "llmReplayConsumed": "llmTotalCount",
        "notDegraded": "degradedReason",
    }
    diff = {
        key: {
            "v1": v1_reference.get(_V1_FIELD[key]),
            "v2": v2_summary.get(_V2_FIELD[key]),
        }
        for key, value in checks.items()
        if value is False
    }
    return {"matched": all(checks.values()), "checks": checks, "diff": diff}


# ── Evidence writing ─────────────────────────────────────────────────────────


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_shadow_evidence(
    *,
    evidence_dir: Path,
    request_id: str,
    v1_reference: dict[str, Any],
    v2_summary: dict[str, Any],
    comparison: dict[str, Any],
    node_events: list[dict[str, Any]],
    recording_path: Path | None,
    shadow_error: str | None = None,
) -> Path:
    """Persist the shadow evidence bundle and return the manifest path."""
    evidence_dir.mkdir(parents=True, exist_ok=True)

    def _write(name: str, payload: Any) -> None:
        with open(evidence_dir / name, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str)
            )

    with open(evidence_dir / "node_events.jsonl", "w", encoding="utf-8") as fh:
        for event in node_events:
            fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    _write("v1_reference.json", v1_reference)
    _write("v2_summary.json", v2_summary)
    _write("diff.json", comparison)

    artifact_shas: dict[str, str] = {}
    for name in ("node_events.jsonl", "v1_reference.json", "v2_summary.json", "diff.json"):
        artifact_shas[name] = _sha256_file(evidence_dir / name)
    recording_sha: str | None = None
    if recording_path is not None and recording_path.exists():
        recording_sha = _sha256_file(recording_path)

    manifest: dict[str, Any] = {
        "requestId": request_id,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "graphName": GRAPH_V2_NAME,
        "matched": comparison.get("matched"),
        "shadowError": shadow_error,
        "recordingPath": str(recording_path) if recording_path else None,
        "recordingSha256": recording_sha,
        "artifactSha256": artifact_shas,
    }
    manifest_path = evidence_dir / "manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(manifest, ensure_ascii=False, indent=2, default=str)
        )
    return manifest_path


# ── Shadow runner ────────────────────────────────────────────────────────────


async def run_graph_v2_shadow(
    *,
    initial_task_state: TaskState,
    v1_reference: dict[str, Any],
    v2_client: Any,
    replay_transport: ReplayTransport,
    isolated_client: Any,
    shadow_trace_builder: TraceBuilder,
    user_message: str,
    model: str,
    resolve_tool_schemas: Callable[[TaskState], list[dict[str, Any]]],
    system_policies: dict[str, Any] | None,
    max_transitions: int,
    max_replans: int,
    evidence_dir: Path,
    projector: ContextProjector | None = None,
    recording_path: Path | None = None,
    request_id: str = "",
) -> dict[str, Any]:
    """Re-execute V1's recorded flow through the V2 graph with zero side effects.

    Returns a summary dict with ``matched``, ``comparison``, ``v2Summary``,
    ``evidenceDir`` and ``manifestPath``.  Never raises for a shadow
    divergence — it records the diff and returns fail-closed.
    """
    requested_tool_names: list[str] = []

    async def shadow_v2_tool_caller(tool_name: str, arguments: dict[str, Any]):
        requested_tool_names.append(tool_name)
        return await replay_transport(tool_name, arguments, _noop_live)

    runtime = GraphV2Runtime(
        user_message=user_message,
        client=v2_client,
        model=model,
        resolve_tool_schemas=resolve_tool_schemas,
        tool_caller=shadow_v2_tool_caller,
        trace_builder=shadow_trace_builder,
        projector=projector,
        max_transitions=max_transitions,
        system_policies=system_policies,
        max_replans=max_replans,
    )

    shadow_error: str | None = None
    graph_state: GraphV2State | None = None
    try:
        async with isolate_task_state_store(isolated_client):
            await seed_isolated_task_state(isolated_client, initial_task_state)
            graph_state = await run_graph_v2(initial_task_state, runtime)
    except Exception as exc:  # noqa: BLE001 - fail-closed shadow
        shadow_error = f"{type(exc).__name__}: {exc}"
        logger.warning("graph_v2 shadow divergence: %s", shadow_error)

    llm_consumed = getattr(v2_client, "consumed_count", 0)
    llm_total = getattr(v2_client, "total_count", 0)
    # toolCallCount counts the calls V2 actually requested, not the calls loaded
    # from the recording — a replay miss (fewer recorded calls than requested)
    # must surface as allToolCallsConsumed == False, never as a trivial match.
    requested_call_count = len(requested_tool_names)
    consumed_call_count = getattr(replay_transport, "consumed_count", 0)
    if graph_state is None:
        v2_summary = {
            "nodeSequence": [],
            "finalAction": None,
            "toolCallCount": requested_call_count,
            "toolConsumedCount": consumed_call_count,
            "requestedToolNames": list(requested_tool_names),
            "llmConsumedCount": llm_consumed,
            "llmTotalCount": llm_total,
            "degradedReason": "shadow_error",
        }
        comparison = {
            "matched": False,
            "checks": {"shadowError": True},
            "diff": {"shadowError": shadow_error},
        }
    else:
        v2_summary = build_v2_summary(
            graph_state=graph_state,
            tool_call_count=requested_call_count,
            tool_consumed_count=consumed_call_count,
            requested_tool_names=requested_tool_names,
            llm_consumed_count=llm_consumed,
            llm_total_count=llm_total,
        )
        comparison = compare_v1_v2_shadow(v1_reference, v2_summary)

    node_events = list(graph_state.get("node_events", [])) if graph_state else []
    manifest_path = write_shadow_evidence(
        evidence_dir=evidence_dir,
        request_id=request_id,
        v1_reference=v1_reference,
        v2_summary=v2_summary,
        comparison=comparison,
        node_events=node_events,
        recording_path=recording_path,
        shadow_error=shadow_error,
    )
    return {
        "matched": bool(comparison.get("matched")),
        "shadowError": shadow_error,
        "comparison": comparison,
        "v2Summary": v2_summary,
        "evidenceDir": str(evidence_dir),
        "manifestPath": str(manifest_path),
    }
