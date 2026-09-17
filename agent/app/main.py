import asyncio
import hashlib
import json
import re
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ConfigDict, Field

from .api.system import create_system_router
from .api.tasks import router as task_router
from .api.memory_bff import (
    claim_task_durable_mode,
    resolve_memory_run_for_browser_session,
    router as memory_bff_router,
)
from .api.transaction_agent import router as transaction_agent_router
from .api.commerce_demo import (
    optional_browser_authorization,
    router as commerce_demo_router,
)
from .intent import (
    extract_shop_type_id,
    extract_shop_type_keyword,
    looks_like_shop_query,
    looks_like_shop_type_query,
)
from .harness import build_validated_guide_result
from .llm import (
    DEFERRED_TASK_RETENTION_REASON,
    _deterministic_smalltalk_answer,
    ask_llm,
    begin_agent_llm_call_span,
    classify_task_relation,
    end_agent_llm_call_span,
    persist_agent_llm_call_snapshot,
    run_agent,
)
from .rag_advanced import answer_with_advanced_rag_observed, requires_advanced_rag
from .rag_answer import RagGenerationError, RagRetrievalError, answer_with_rag_observed
from .review_index_events import listen_for_review_index_events
from .review_projection_receipts import apply_review_projection
from .review_index_sync import (
    delete_review_search_indexes,
    upsert_review_search_indexes,
)
from .agent_trace import TRACE_DEBUG_HEADER, get_trace_for_debug, trace_debug_enabled, validate_debug_key
from .schemas import (
    ChatRequest,
    ChatResponse,
    EcommerceChatRequest,
    RequestTrace,
    RagChatRequest,
    RagChatResponse,
    RecommendedShop,
    ReviewVectorSyncRequest,
    ReviewVectorSyncResponse,
    SessionClearResponse,
    ShopRecommendationsResponse,
    ToolTrace,
)
from .session_memory import clear_session, get_history, save_turn
from .settings import settings
from .runtime import resolve_domain
from .domains.ecommerce import (
    ConfirmationStoreUnavailable,
    clear_transaction_confirmations,
    compiled_shopping_requirements,
    detect_product_category,
    explicit_confirmation_action,
    ShoppingGuideState,
    search_products_tool,
)
from .transaction_agent.runtime import bind_transaction_request
from .task_state import (
    TaskRelationDecision,
    TaskState,
    TaskStateCreateRequest,
    apply_session_task_relation,
    clear_session_task_state,
    create_task_state,
    get_or_create_session_task_state,
    get_session_task_state,
    get_task_state,
    list_session_task_states,
)
from .tools import call_tool, check_backend, list_shop_types, recommend_shops, search_shops
from .transport_resolver import get_tool_transport, get_tool_transport_identity
from .step_debug import (
    DebugTurn,
    DebugTurnCreateRequest,
    DebugTurnNotFoundError,
    DebugTurnRevisionConflictError,
    DebugTurnStepRequest,
    completed_step,
    debug_turn_lock,
    get_debug_turn_store,
    next_checkpoint,
)
from .web_query_intake import (
    capture_health,
    capture_web_query,
    capture_web_query_diagnostic,
    capture_web_query_observation,
    get_web_query_intake_store,
    mark_web_query_outcome,
)
from .memory_candidate_worker import (
    current_request_recipient_scope,
    run_memory_candidate_worker,
    schedule_memory_extraction,
)
from .reference_context import (
    ReferenceContextError,
    ResolvedReferenceContext,
    publish_reference_context,
    refresh_reference_context,
    resolve_reference_context,
)
from .domains.ecommerce.fast_response import (
    analyze_used_phone_fast,
    build_preview_cache_key,
    build_provisional_guide_result,
    catalog_revision,
    get_preview_candidate_cache,
)


PRIMARY_AGENT_DOMAIN_HINT = "ecommerce"


async def _publish_browser_guide_result(
    state: TaskState | None,
    *,
    session_id: str | None,
    memory_run_binding: Any | None = None,
) -> dict[str, Any] | None:
    """Build the guide and bind its exact card order to a short-lived receipt."""

    guide_result = build_validated_guide_result(
        state,
        memory_run_binding=memory_run_binding,
        memory_rerank_weight=settings.memory_rerank_lambda,
    )
    if state is None or session_id is None or not isinstance(guide_result, dict):
        return guide_result
    try:
        public_context = await publish_reference_context(
            session_id=session_id,
            state=state,
            guide_result=guide_result,
        )
    except ReferenceContextError as exc:
        # Reference publication is an optional UI affordance.  Any contract
        # mismatch suppresses the handle; it must never weaken the guide itself.
        logger.warning(
            "reference_context publication suppressed code=%s task_id=%s",
            exc.code,
            state.task_id,
        )
        return guide_result
    if public_context is not None:
        guide_result["referenceContext"] = public_context
    return guide_result


async def _publish_response_reference_context(
    state: TaskState | None,
    *,
    session_id: str | None,
    resolved: ResolvedReferenceContext | None,
    guide_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the current presentation receipt independently of card rendering."""

    if state is not None and session_id is not None and resolved is not None:
        try:
            refreshed = await refresh_reference_context(
                session_id=session_id,
                state=state,
                resolved=resolved,
            )
            if isinstance(guide_result, dict):
                guide_result["referenceContext"] = refreshed
            return refreshed
        except ReferenceContextError as exc:
            logger.warning(
                "reference_context refresh suppressed code=%s task_id=%s",
                exc.code,
                state.task_id,
            )
    if isinstance(guide_result, dict):
        published = guide_result.get("referenceContext")
        if isinstance(published, dict):
            return published
    return None


def _reference_context_flow_diagnostic(
    *,
    request_hint: Any | None,
    resolved: ResolvedReferenceContext | None,
    failure_code: str | None,
) -> dict[str, Any]:
    request_summary: dict[str, Any] = {"present": request_hint is not None}
    if request_hint is not None:
        request_summary.update({
            "handleSha256": hashlib.sha256(
                request_hint.handle.encode("ascii")
            ).hexdigest(),
            "presentationMode": request_hint.presentation_mode,
            "focusedProductId": request_hint.focused_product_id,
        })
    resolved_summary = None
    if resolved is not None:
        resolved_summary = {
            "taskId": resolved.task_id,
            "taskRevision": resolved.task_revision,
            "scopeId": resolved.scope_id,
            "scopeSourceRevision": resolved.scope_source_revision,
            "presentationMode": resolved.presentation_mode,
            "presentationIds": list(resolved.presentation_ids),
            "compactProductIds": list(resolved.compact_product_ids),
            "expandedProductIds": list(resolved.expanded_product_ids),
            "comparedProductIds": list(resolved.compared_product_ids),
            "previousBatchProductIds": list(
                resolved.previous_batch_product_ids
            ),
            "focusedProductId": resolved.focused_product_id,
        }
    return {
        "request": request_summary,
        "resolver": {
            "status": (
                "rejected" if failure_code is not None
                else "resolved" if resolved is not None
                else "not_requested"
            ),
            "failureCode": failure_code,
            "resolved": resolved_summary,
        },
    }
class DurableEcommerceChatRequest(EcommerceChatRequest):
    """Day-2 durable control-plane request (clarification resume / restart).

    ``resume`` carries the server-owned fields echoed by the client from the
    parked interrupt payload (taskId/runId/threadId/revision/proposalHash) plus
    the clarification ``answer``.  When present, this request is a continuation
    of an existing durable thread, NOT a fresh user turn.  ``restartTaskId``
    continues a durable thread that a process kill interrupted mid-window.
    Both are fail-closed validated by the durable runner (#5).
    """

    model_config = ConfigDict(
        title="电商导购对话请求（Day-2 durable）",
        populate_by_name=True,
    )

    resume: dict[str, Any] | None = Field(
        default=None,
        description=(
            "续答载荷：taskId/runId/threadId/revision/proposalHash + answer"
        ),
    )
    restart_task_id: str | None = Field(
        default=None,
        alias="restartTaskId",
        description="进程重启后继续的 durable taskId（系统内部使用）",
    )
    pause_receipt: dict[str, Any] | None = Field(
        default=None,
        alias="pauseReceipt",
        description="暂停恢复回执；必须与服务端 paused receipt 和 checkpoint 完全匹配",
    )


def _durable_session_owns_task(
    request_session_id: str | None, state: TaskState
) -> bool:
    """Return true only for a complete, server-owned durable session binding."""
    return bool(
        isinstance(request_session_id, str)
        and request_session_id
        and isinstance(state.session_id, str)
        and state.session_id
        and request_session_id == state.session_id
    )


def _durable_ownership_rejected_response(
    *, request_id: str, route: str, start: float
) -> ChatResponse:
    """Fail closed without publishing another session's TaskState or result."""
    return ChatResponse(
        answer="当前会话无权继续该持久化任务，系统已安全停止；请重新开始当前任务。",
        tool_trace=[],
        trace=_build_request_trace(
            request_id=request_id, route=route, start=start, status="error"
        ),
    )

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Build optional in-memory indexes before a flagged canary accepts traffic."""
    listener_task: asyncio.Task | None = None
    listener_stop = asyncio.Event()
    memory_worker_task: asyncio.Task | None = None
    memory_worker_stop = asyncio.Event()
    critic_queue = None
    try:
        from .api.commerce_demo import start_java_client
        await start_java_client()
        if settings.catalog_workspace_enabled:
            from .catalog_service import get_catalog_service
            await get_catalog_service().start()
            from .catalog_model_client import warm_startup
            await warm_startup()
        if settings.evidence_critic_enabled:
            from .critic_queue import get_critic_queue

            critic_queue = get_critic_queue()
            await critic_queue.start()
        if settings.knowledge_review_hybrid_enabled:
            from .rag_bm25 import prepare_review_bm25_indexes

            listener_ready = asyncio.Event()
            listener_task = asyncio.create_task(
                listen_for_review_index_events(listener_ready, listener_stop)
            )
            await asyncio.wait_for(listener_ready.wait(), timeout=10.0)
            await asyncio.to_thread(prepare_review_bm25_indexes)
        if settings.web_query_intake_enabled:
            try:
                await asyncio.to_thread(get_web_query_intake_store().stats)
                logger.info("Web query intake store initialized")
            except Exception as exc:
                # Capture remains best-effort; initialization failure is visible
                # but never prevents the Agent from serving requests.
                logger.warning(
                    "Web query intake initialization degraded: errorClass=%s",
                    type(exc).__name__,
                )
        if (
            settings.memory_bff_enabled
            and settings.memory_projection_client_enabled
        ):
            memory_worker_task = asyncio.create_task(
                run_memory_candidate_worker(memory_worker_stop)
            )
        if (
            settings.product_retrieval_mode == "hybrid"
            and settings.product_vector_backend == "local"
        ):
            try:
                from .domains.ecommerce.tools import warm_local_product_vector_cache

                warmed = await asyncio.wait_for(
                    warm_local_product_vector_cache(),
                    timeout=max(
                        float(settings.product_vector_timeout_seconds),
                        30.0,
                    ),
                )
                logger.info("Warmed local product vectors: products=%s", warmed)
            except Exception as exc:
                # Hybrid retrieval has an explicit BM25 fallback; vector warmup
                # failure must remain observable without taking health down.
                logger.warning(
                    "Local product vector warmup degraded: errorClass=%s",
                    type(exc).__name__,
                )
        yield
    finally:
        if settings.catalog_workspace_enabled:
            from .catalog_service import get_catalog_service
            get_catalog_service().close()
        from .catalog_model_client import close_client as close_catalog_model_client
        await close_catalog_model_client()
        from .api.commerce_demo import close_java_client
        await close_java_client()
        from .domains.ecommerce.tools import close_product_search_clients

        await close_product_search_clients()
        if critic_queue is not None:
            await critic_queue.stop(graceful=True)
        if listener_task is not None:
            listener_stop.set()
            listener_task.cancel()
            with suppress(asyncio.CancelledError):
                await listener_task
        if memory_worker_task is not None:
            memory_worker_stop.set()
            memory_worker_task.cancel()
            with suppress(asyncio.CancelledError):
                await memory_worker_task


app = FastAPI(
    title="电商导购 Agent 项目",
    summary="面向工程学习与求职证据的后端 + Agent 项目",
    description=(
        "正式主线是 3C 电商导购。服务以 TaskState、受控工具合同和"
        "基于 sessionId 的多轮状态驱动检索、比较与交易确认。"
    ),
    version="0.1.0",
    lifespan=lifespan,
    openapi_tags=[
        {"name": "系统接口", "description": "用于确认服务是否正常运行。"},
        {"name": "智能体接口", "description": "用于体验电商导购 Agent；旧本地生活接口已标记 deprecated。"},
        {"name": "任务状态", "description": "通用 Agent TaskState 的创建、读取与受控更新。"},
    ],
)

logger = logging.getLogger(__name__)
request_trace_logger = logging.getLogger("uvicorn.error")

# 聊天网页所在目录（app/static/index.html）
STATIC_DIR = Path(__file__).resolve().parent / "static"
SLOW_REQUEST_THRESHOLD_MS = 3000.0

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.include_router(create_system_router(STATIC_DIR))
app.include_router(task_router)
app.include_router(memory_bff_router)
app.include_router(transaction_agent_router)
app.include_router(commerce_demo_router)
from .api.commerce_workspace import router as commerce_workspace_router
from .api import commerce_controls  # Register owner-bound controls before including the router.
from .api import recommendation_workspace  # Public-history tool shares workspace identity and storage.
app.include_router(commerce_workspace_router)
from .backend_observer import BackendObserverMiddleware, router as backend_observer_router
app.add_middleware(BackendObserverMiddleware)
app.include_router(backend_observer_router)
from .api.commerce_capabilities import router as commerce_capabilities_router
app.include_router(commerce_capabilities_router)


def _new_request_id() -> str:
    return f"req-{uuid.uuid4().hex[:12]}"


def _active_transaction_scope_id(state: TaskState | None) -> str | None:
    if state is None or type(state.domain_state) is not dict:
        return None
    raw = state.domain_state.get("candidateScope")
    if type(raw) is not dict:
        return None
    if raw.get("status", "active") != "active" or raw.get("taskId") != state.task_id:
        return None
    scope_id = raw.get("scopeId")
    return scope_id if type(scope_id) is str and scope_id else None


def _memory_category_id(state: TaskState | None) -> str | None:
    if state is None or type(state.domain_state) is not dict:
        return None
    guide = state.domain_state.get("shoppingGuide")
    if type(guide) is not dict:
        return None
    category = guide.get("category")
    return category if type(category) is str else None


def _explicit_memory_recipient_scope(message: str) -> str | None:
    """Return self only when the current request explicitly identifies self.

    V13 intentionally fails closed for an omitted recipient because TaskState
    does not yet persist a recipient contract across turns.
    """
    return current_request_recipient_scope(message)


def _sse_data(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _initial_chat_domain_state(
    message: str,
    *,
    task_type: str,
    origin: str,
) -> dict[str, Any]:
    """Create server-owned domain state before any model extraction.

    Exact product identity belongs to routing, not to the LLM.  Publishing an
    empty validated guide for a detected category lets the same deterministic
    seven-field compiler used by the benchmark run through the real API path.
    Messages without a concrete category keep the minimal generic state.
    """

    state: dict[str, Any] = {"origin": origin, "turnCount": 0}
    category = detect_product_category(message) if task_type == "ecommerce_guide" else None
    if category is not None:
        state["shoppingGuide"] = {
            "mode": "recommend",
            "category": category,
            "useCases": [],
            "requirements": [],
            "candidateIds": [],
            "comparedIds": [],
            "evidenceStatus": "missing",
        }
    return state


async def _prepare_session_task(
    session_id: str,
    message: str,
    *,
    domain_hint: str = "auto",
    on_state: Callable[[TaskState, str], Awaitable[None]] | None = None,
    on_relation: Callable[[TaskRelationDecision], Awaitable[None]] | None = None,
) -> tuple[TaskState, TaskRelationDecision, bool]:
    """Select one ecommerce task before the normal planning round.

    ``auto`` remains accepted on the wire for existing clients, but the primary
    Agent no longer routes new traffic into the archived local-life domain.
    """
    effective_domain_hint = PRIMARY_AGENT_DOMAIN_HINT
    initial_routing = resolve_domain(message, domain_hint=effective_domain_hint)
    active, created = await get_or_create_session_task_state(
        session_id,
        message,
        initial_routing.task_type,
        domain_state=_initial_chat_domain_state(
            message,
            task_type=initial_routing.task_type,
            origin="chat",
        ),
    )
    routing = resolve_domain(
        message,
        domain_hint=effective_domain_hint,
        active_task_type=active.task_type,
    )
    requested_task_type = routing.task_type
    ecommerce = routing.domain_id == "ecommerce"
    if on_state is not None:
        await on_state(active, "created" if created else "loaded")

    if routing.ambiguous:
        decision = TaskRelationDecision(
            relation="ambiguous",
            targetTaskId=active.task_id,
            reason="消息同时命中本地生活与商品导购领域",
            clarificationQuestion=routing.clarification_question,
            confidence=routing.confidence,
        )
        if on_relation is not None:
            await on_relation(decision)
        return active, decision, False

    if created:
        decision = TaskRelationDecision(
            relation="start_new",
            targetTaskId=active.task_id,
            reason="当前会话没有可复用的活跃任务",
            confidence=1.0,
        )
        if on_relation is not None:
            await on_relation(decision)
        return active, decision, True

    if active.task_type != requested_task_type and (
        effective_domain_hint != "auto"
        or ecommerce
        or routing.reason == "detected_switch"
    ):
        decision = TaskRelationDecision(
            relation="start_new",
            reason="用户显式切换了对话领域",
            confidence=1.0,
        )
        if on_relation is not None:
            await on_relation(decision)
        selected = await apply_session_task_relation(
            session_id,
            message,
            decision,
            on_transition=on_state,
            task_type=requested_task_type,
            domain_state=_initial_chat_domain_state(
                message,
                task_type=requested_task_type,
                origin="chat",
            ),
        )
        return selected, decision, True

    if (
        active.task_type == "ecommerce_guide"
        and explicit_confirmation_action(message) is not None
    ):
        decision = TaskRelationDecision(
            relation="continue_current",
            targetTaskId=active.task_id,
            reason="交易确认语必须由当前电商任务的确定性安全门处理",
            confidence=1.0,
        )
        if on_relation is not None:
            await on_relation(decision)
        return active, decision, False

    # Fast used-phone continuation: when the active task is a used-phone guide
    # and the fast analysis can prove this turn continues it, resolve the
    # relation deterministically and skip the TaskManager model call.  The
    # taskId/session/revision/OCC contract is untouched — no copy or forgery of
    # TaskState, and unsafe turns fail closed back onto the model path.
    if active.task_type == "ecommerce_guide":
        fast = analyze_used_phone_fast(message, active)
        # Relation classification is read-only and independently safety-gated.
        # The preview feature flag controls only provisional UI results; it
        # must not force a redundant TaskManager model call for deterministic
        # phone continuations.
        if fast.allow_task_manager_bypass:
            decision = TaskRelationDecision(
                relation="continue_current",
                targetTaskId=active.task_id,
                reason="fast deterministic used-phone continuation",
                confidence=1.0,
            )
            if on_relation is not None:
                await on_relation(decision)
            return active, decision, False

    recent_tasks = await list_session_task_states(session_id)
    decision = await classify_task_relation(message, active, recent_tasks)
    if on_relation is not None:
        await on_relation(decision)
    selected = await apply_session_task_relation(
        session_id,
        message,
        decision,
        on_transition=on_state,
        task_type=requested_task_type,
        domain_state=_initial_chat_domain_state(
            message,
            task_type=requested_task_type,
            origin="chat",
        ),
    )
    return selected, decision, decision.relation == "start_new"


async def _prepare_ephemeral_task(
    message: str,
    *,
    domain_hint: str = "auto",
    on_state: Callable[[TaskState, str], Awaitable[None]] | None = None,
) -> TaskState:
    """Create a persisted, non-resumable TaskState for clients without sessionId.

    Omitting sessionId no longer bypasses the unified runtime. The task still has
    a stable identity/revision for ContextPack, Trace and OCC, but no session
    binding or conversation history is created.
    """

    routing = resolve_domain(message, domain_hint=PRIMARY_AGENT_DOMAIN_HINT)
    state = await create_task_state(
        TaskStateCreateRequest(
            task_type=routing.task_type,
            goal=message,
            session_id=None,
            domain_state=_initial_chat_domain_state(
                message,
                task_type=routing.task_type,
                origin="ephemeral_chat",
            ),
        )
    )
    if on_state is not None:
        await on_state(state, "created_ephemeral")
    return state


def _task_relation_direct_answer(
    decision: TaskRelationDecision,
    state: TaskState,
) -> str | None:
    if decision.relation == "ambiguous":
        return decision.clarification_question or (
            f"你是想继续“{state.goal}”，还是开始一件新的事情？"
        )
    if decision.relation == "cancel_current":
        return f"好的，已取消当前任务：“{state.goal}”。"
    if decision.reason == DEFERRED_TASK_RETENTION_REASON:
        return "好的，之前的手机任务会继续保留；当前不切换。等你说“继续手机”时，我再恢复它。"
    return None


def _extract_search_candidate_ids(
    tool_traces: list[ToolTrace] | None,
) -> list[str]:
    """Pull ranked candidate ids from the final search_products tool trace."""
    for trace in tool_traces or []:
        detail = trace.detail
        if not trace.ok or not isinstance(detail, dict):
            continue
        if trace.tool != "search_products":
            continue
        candidate_ids = detail.get("candidateIds")
        if isinstance(candidate_ids, list):
            return [str(item) for item in candidate_ids if item is not None]
    return []


async def _maybe_build_preview_event(
    request_id: str,
    message: str,
    task_state: TaskState | None,
    fast: Any | None = None,
) -> dict[str, Any] | None:
    """Build the provisional ``preview`` SSE event for a fast used-phone turn.

    Returns ``None`` when fast preview is not applicable (disabled, unsafe,
    non-phone category, empty candidate pool).  The provisional guideResult is
    projected only from rule-level ``full_match`` Top-3 candidates through the
    real two-stage ranking pool — never from the Validator boundary — and is
    labelled provisional so the final ``complete`` event replaces it in place.

    Cache semantics: ``warm`` reuses a deep copy of the same catalog-revision
    candidates; ``cold`` runs the real ``search_products_tool`` and stores a deep
    copy under a key that auto-invalidates on catalog revision changes.
    """
    if not settings.used_phone_fast_preview_enabled or task_state is None:
        return None
    if fast is None:
        fast = analyze_used_phone_fast(message, task_state)
    if not fast.allow_product_preview:
        return None
    guide = task_state.domain_state or {}
    if (guide or {}).get("shoppingGuide", {}).get("category") != "phone":
        return None

    preview_start = time.perf_counter()
    cache = get_preview_candidate_cache()
    revision = catalog_revision()
    cache_key = build_preview_cache_key(
        catalog_revision=revision,
        query=message,
        use_cases=list(fast.use_cases or []),
        requirements=list(fast.preview_requirements or []),
    )
    cache_status = "cold"
    candidates: list[dict[str, Any]] | None = cache.get(cache_key)
    if candidates is not None:
        cache_status = "warm"
    else:
        trace = await search_products_tool(
            query=message,
            category="手机",
            requirements=[
                item.model_dump(mode="json")
                for item in (fast.preview_requirements or [])
            ],
            limit=20,
        )
        if trace.ok and isinstance(trace.detail, dict):
            trace_candidates = trace.detail.get("candidates")
            if isinstance(trace_candidates, list):
                candidates = trace_candidates
                cache.set(cache_key, candidates)
    if not candidates:
        return None

    provisional = build_provisional_guide_result(candidates, limit=3)
    preview_duration_ms = (time.perf_counter() - preview_start) * 1000
    preview_candidate_ids = [
        str(item["product"]["id"])
        for item in provisional.get("products", [])
        if item.get("product", {}).get("id") is not None
    ]
    return {
        "type": "preview",
        "requestId": request_id,
        "status": "provisional",
        "message": "已识别部分要求，正在继续分析；以下为初步匹配，最终结果可能调整。",
        "recognizedRequirements": [
            requirement.model_dump(mode="json")
            for requirement in (fast.preview_requirements or [])
        ],
        "recognizedUseCases": list(fast.use_cases or []),
        "guideResult": provisional,
        "trace": {
            "route": "used_phone_fast_preview",
            "cacheStatus": cache_status,
            "cacheKey": cache_key,
            "previewStatus": "provisional",
            "previewDurationMs": round(preview_duration_ms, 3),
            "previewCandidateIds": preview_candidate_ids,
            "taskManagerBypass": bool(fast.allow_task_manager_bypass),
            "taskStateBypass": bool(fast.allow_task_state_bypass),
            "analyzerStatus": fast.status,
            "analyzerReason": fast.reason,
        },
    }


def _fast_observation_fields(
    fast: Any | None,
    span_snapshot: dict[str, Any] | None,
    *,
    task_id: str | None,
    route: str | None,
    session_id: str | None,
    preview_event: dict[str, Any] | None,
    final_candidate_ids: list[str],
    final_duration_ms: float,
) -> dict[str, Any]:
    """Project the web-query-observation row from one request's fast state."""
    model_calls = (span_snapshot or {}).get("modelCalls", {})
    durations = (span_snapshot or {}).get("llmDurationMs", {})
    preview_trace = (preview_event or {}).get("trace") or {}
    preview_ids = preview_trace.get("previewCandidateIds") or []
    final_ids = [str(item) for item in final_candidate_ids]
    removed = sorted(set(preview_ids) - set(final_ids))
    added = sorted(set(final_ids) - set(preview_ids))
    reordered = (
        [item for item in preview_ids if item in final_ids]
        != [item for item in final_ids if item in preview_ids]
    )
    fields: dict[str, Any] = {
        "task_id": task_id,
        "route": route,
        "session_id": session_id,
        "extraction_route": (fast.route if fast is not None else None),
        "extraction_reason": (fast.reason if fast is not None else None),
        "mentioned_keys": list(fast.mentioned_keys if fast is not None else []),
        "covered_keys": list(fast.covered_keys if fast is not None else []),
        "uncovered_keys": list(fast.uncovered_keys if fast is not None else []),
        "task_manager_model_calls": model_calls.get("task_manager", 0),
        "task_manager_duration_ms": round(durations.get("task_manager", 0.0), 3),
        "task_state_model_calls": model_calls.get("task_state", 0),
        "task_state_duration_ms": round(durations.get("task_state", 0.0), 3),
        "preview_eligible": int(preview_event is not None),
        "preview_status": preview_trace.get("previewStatus"),
        "preview_cache_status": preview_trace.get("cacheStatus"),
        "preview_duration_ms": preview_trace.get("previewDurationMs"),
        "preview_candidate_ids": list(preview_ids),
        "final_duration_ms": round(final_duration_ms, 3),
        "final_candidate_ids": list(final_ids),
        "preview_final_removed": list(removed),
        "preview_final_added": list(added),
        "preview_final_reordered": int(reordered),
    }
    return fields


_DEBUG_STAGE_CODE: dict[str, tuple[str, str, str]] = {
    "task_manager": (
        "TaskManager",
        "agent/app/main.py",
        "_prepare_session_task",
    ),
    "task_state": (
        "更新 TaskState",
        "agent/app/llm.py",
        "_update_task_state_for_unified_harness",
    ),
    "context_pack": (
        "构建 ContextPack",
        "agent/app/context_pack.py",
        "build_context_pack",
    ),
    "planner": (
        "Planner 规划",
        "agent/app/harness.py",
        "run_planning_step",
    ),
    "executor": (
        "Executor 执行一步",
        "agent/app/executor.py",
        "run_executor_step",
    ),
    "validator": (
        "Validator 校验",
        "agent/app/validator.py",
        "run_validator_phase",
    ),
    "replanner": (
        "Replanner 恢复",
        "agent/app/harness.py",
        "run_harness_step",
    ),
    "final_answer": (
        "生成最终回答",
        "agent/app/llm.py",
        "_render_validated_used_phone_answer",
    ),
}


def _debug_task_projection(state: TaskState | None) -> dict[str, Any]:
    """Return a bounded, user-facing checkpoint instead of raw TaskState."""

    if state is None:
        return {}
    guide = state.domain_state.get("shoppingGuide")
    guide_projection: dict[str, Any] | None = None
    if isinstance(guide, dict):
        candidate_ids = guide.get("candidateIds")
        compared_ids = guide.get("comparedIds")
        raw_requirements = guide.get("requirements")
        try:
            compiled_requirements = [
                item.model_dump(mode="json")
                for item in compiled_shopping_requirements(
                    ShoppingGuideState.model_validate(guide)
                )
            ]
        except ValueError:
            compiled_requirements = raw_requirements
        requirements = []
        if isinstance(compiled_requirements, list):
            for item in compiled_requirements[:12]:
                if not isinstance(item, dict):
                    continue
                requirements.append({
                    key: item.get(key)
                    for key in (
                        "key", "operator", "value", "unit", "priority", "source",
                    )
                })
        mode = guide.get("mode")
        guide_projection = {
            "mode": mode,
            "modeLabel": "比较" if mode == "compare" else "搜索/推荐",
            "category": guide.get("category"),
            "requirements": requirements,
            "requirementCount": len(compiled_requirements)
            if isinstance(compiled_requirements, list) else 0,
            "candidateIds": list(candidate_ids[:5])
            if isinstance(candidate_ids, list) else [],
            "candidateCount": len(candidate_ids)
            if isinstance(candidate_ids, list) else 0,
            "comparedIds": list(compared_ids)
            if isinstance(compared_ids, list) else [],
            "evidenceStatus": guide.get("evidenceStatus"),
        }
    plan_projection: dict[str, Any] | None = None
    if state.active_plan is not None:
        plan_projection = {
            "planId": state.active_plan.plan_id,
            "status": state.active_plan.status,
            "steps": [
                {
                    "stepId": step.step_id,
                    "toolName": step.tool_name,
                    "status": step.status,
                }
                for step in state.active_plan.steps
            ],
        }
    published_projection: dict[str, Any] | None = None
    guide_result = build_validated_guide_result(state)
    if isinstance(guide_result, dict):
        products = guide_result.get("products")
        if isinstance(products, list):
            product_ids = []
            for item in products:
                if not isinstance(item, dict) or not isinstance(item.get("product"), dict):
                    continue
                raw_id = item["product"].get("id")
                if type(raw_id) is int and raw_id > 0:
                    product_ids.append(str(raw_id))
                elif isinstance(raw_id, str) and re.fullmatch(r"[1-9]\d*", raw_id):
                    product_ids.append(raw_id)
            published_projection = {
                "productIds": product_ids,
                "productCount": len(product_ids),
                "validatorPassed": bool(product_ids),
            }
    extraction = state.domain_state.get("taskStateExtraction")
    extraction_projection = None
    if isinstance(extraction, dict):
        extraction_projection = {
            key: extraction.get(key)
            for key in (
                "route", "reason", "executionKind", "modelCalled",
                "modelCallCount", "llmDurationMs", "repairUsed",
                "mentionedKeys", "coveredKeys", "uncoveredKeys",
            )
            if key in extraction
        }
    return {
        "taskId": state.task_id,
        "revision": state.revision,
        "status": state.status,
        "unknowns": list(state.unknowns),
        "pendingQuestions": list(state.pending_questions),
        "shoppingGuide": guide_projection,
        "taskStateExtraction": extraction_projection,
        "activePlan": plan_projection,
        "validatedPresentation": published_projection,
    }


async def _debug_load_task(turn: DebugTurn) -> TaskState:
    if turn.task_id is None or turn.task_revision is None:
        raise ValueError("debug turn has no bound TaskState")
    state = await get_task_state(turn.task_id)
    if state is None:
        raise ValueError("debug turn TaskState no longer exists")
    if state.revision != turn.task_revision:
        raise DebugTurnRevisionConflictError(
            turn.task_revision,
            state.revision,
        )
    return state


async def _debug_load_pack(turn: DebugTurn):
    from .context_pack import ContextPack, context_pack_hash
    from .context_view import ContextProjector

    raw = await get_debug_turn_store().get_context_pack(turn.debug_turn_id)
    pack = ContextPack.model_validate_json(raw)
    if (
        pack.run_id != turn.run_id
        or pack.task_id != turn.task_id
        or context_pack_hash(pack) != turn.context_pack_hash
    ):
        raise ValueError("debug ContextPack identity/hash mismatch")
    return pack, ContextProjector(pack)


async def _advance_debug_turn(turn: DebugTurn) -> DebugTurn:
    """Execute exactly one persisted debugger stage."""

    from .context_pack import (
        build_context_pack,
        context_pack_hash,
        context_pack_token_count,
    )
    from .executor import run_executor_step
    from .harness import (
        _build_executed_steps,
        _build_validated_evidence_refs,
        _build_validated_results,
        _extract_constraint_keys_for_step,
        _extract_fact_keys_for_step,
        _extract_tool_schema_dict,
        _project_prior_step_outputs_for_step,
        _project_step_arguments,
        decide_after_execution,
        decide_after_validation,
        run_harness_step,
        run_planning_step,
    )
    from .llm import (
        _explicit_harness_tool_schemas,
        _render_validated_used_phone_answer,
        _update_task_state_for_unified_harness,
        get_client,
    )
    from .validator import run_validator_phase

    stage = turn.next_stage
    debug_policies = ({'productKnowledgeUserQuery': turn.message} if settings.product_knowledge_enabled else {})
    if stage == "done":
        return turn
    label, code_file, code_function = _DEBUG_STAGE_CODE[stage]
    started = time.perf_counter()
    state: TaskState | None = None
    next_stage = "done"
    updates: dict[str, Any] = {}
    key_state: dict[str, Any] = {}
    outcome = "passed"
    message: str | None = None
    error_code: str | None = None

    try:
        if stage == "task_manager":
            if explicit_confirmation_action(turn.message) is not None:
                raise ValueError(
                    "single-step MVP is read-only and does not execute transaction confirmations"
                )
            state, relation, _started_new = await _prepare_session_task(
                turn.session_id,
                turn.message,
                domain_hint=turn.domain_hint,
            )
            direct_answer = _task_relation_direct_answer(relation, state)
            answer_source = None
            if direct_answer is None:
                direct_answer = _deterministic_smalltalk_answer(turn.message)
                if direct_answer is not None:
                    answer_source = "task_manager.deterministic_smalltalk"
            next_stage = "final_answer" if direct_answer is not None else "task_state"
            updates.update({
                "task_id": state.task_id,
                "task_revision": state.revision,
                "relation": relation.model_dump(by_alias=True, mode="json"),
                "final_answer": direct_answer,
            })
            key_state = {
                "relation": relation.relation,
                "confidence": relation.confidence,
                "confidenceRange": [0, 1],
                "confidenceBasis": relation.reason,
                "startedNew": relation.relation == "start_new",
                "executionKind": "deterministic",
                "modelCalled": False,
                "modelCallCount": 0,
                "answerSource": answer_source,
                **_debug_task_projection(state),
            }

        elif stage == "task_state":
            state = await _debug_load_task(turn)
            history = await get_history(
                turn.session_id,
                state.task_id,
                migrate_legacy=True,
            )
            updated = await _update_task_state_for_unified_harness(
                turn.message,
                history=history,
                client=get_client(),
                task_state=state,
                on_task_state=None,
            )
            if updated.revision == state.revision:
                raise ValueError("TaskState extractor produced no persisted update")
            state = updated
            next_stage = (
                "final_answer"
                if state.pending_questions or state.status != "ready"
                else "context_pack"
            )
            updates.update({
                "task_revision": state.revision,
            })
            key_state = _debug_task_projection(state)

        elif stage == "context_pack":
            state = await _debug_load_task(turn)
            history = await get_history(
                turn.session_id,
                state.task_id,
                migrate_legacy=True,
            )
            schemas = _explicit_harness_tool_schemas(turn.message, state)
            allowed_names = [
                schema.get("function", schema).get("name", "")
                for schema in schemas
                if schema.get("function", schema).get("name")
            ]
            pack = await build_context_pack(
                state,
                allowed_tools=allowed_names,
                history=history,
                run_id=turn.run_id,
            )
            pack_hash = context_pack_hash(pack)
            token_count = context_pack_token_count(pack)
            await get_debug_turn_store().save_context_pack(
                turn.debug_turn_id,
                pack.model_dump_json(by_alias=True),
            )
            next_stage = "planner"
            updates.update({
                "context_pack_hash": pack_hash,
                "context_token_count": token_count,
                "allowed_tool_names": allowed_names,
            })
            key_state = {
                "contextPackHash": pack_hash,
                "tokenCount": token_count,
                "allowedToolNames": allowed_names,
                "baseContextRevision": pack.base_context_revision,
            }

        elif stage == "planner":
            state = await _debug_load_task(turn)
            _pack, projector = await _debug_load_pack(turn)
            schemas = _explicit_harness_tool_schemas(turn.message, state)
            flat_tools = [
                dict(schema.get("function", schema))
                for schema in schemas
                if isinstance(schema.get("function", schema), dict)
            ]
            planner_view = projector.planner_view(
                tool_names=[item.get("name", "") for item in flat_tools],
                task_status=state.status,
                user_message=turn.message,
                candidate_tool_schemas=flat_tools,
                phase_task_revision=state.revision,
                system_policies=debug_policies,
            )
            result = await run_planning_step(
                state,
                turn.message,
                schemas,
                client=get_client(),
                model=settings.deepseek_model,
                planner_view=planner_view,
                system_policies=debug_policies,
            )
            state = result.task_state
            next_stage = {
                "continue_to_executor": "executor",
                "ready_for_validation": "validator",
                "ask_user": "final_answer",
                "stop_turn": "final_answer",
            }[result.action]
            updates["task_revision"] = state.revision
            key_state = {
                "action": result.action,
                "plannerOutcome": (
                    result.planner_result.outcome
                    if result.planner_result is not None else "skipped"
                ),
                "executionKind": (
                    "deterministic"
                    if planner_view.shopping_guide_sources is not None else "model"
                ),
                "modelCalled": planner_view.shopping_guide_sources is None,
                "modelCallCount": (
                    0 if planner_view.shopping_guide_sources is not None else 1
                ),
                **_debug_task_projection(state),
            }

        elif stage == "executor":
            state = await _debug_load_task(turn)
            _pack, projector = await _debug_load_pack(turn)
            tool_caller = get_tool_transport(
                run_id=turn.run_id,
                context_pack_hash=turn.context_pack_hash,
            )
            tool_transport_identity = get_tool_transport_identity(tool_caller)
            schemas = _explicit_harness_tool_schemas(turn.message, state)
            plan = state.active_plan
            if plan is None:
                raise ValueError("Executor stage requires an active Plan")
            pending = [step for step in plan.steps if step.status == "pending"]
            if not pending:
                raise ValueError("Executor stage has no pending Plan step")
            step = pending[0]
            prior_outputs = _project_prior_step_outputs_for_step(state, plan, step)
            executor_view = projector.executor_view(
                plan_id=plan.plan_id,
                step_id=step.step_id,
                step_description=step.description,
                tool_name=step.tool_name,
                tool_schema=_extract_tool_schema_dict(step.tool_name, schemas),
                resolved_arguments=_project_step_arguments(step, prior_outputs),
                required_fact_keys=_extract_fact_keys_for_step(step),
                required_constraint_keys=_extract_constraint_keys_for_step(step),
                prior_step_outputs=_project_prior_step_outputs_for_step(
                    state,
                    plan,
                    step,
                ),
                phase_task_revision=state.revision,
                system_policies=debug_policies,
            )
            result = await run_executor_step(
                state,
                schemas,
                tool_caller=tool_caller,
                executor_view=executor_view,
                system_policies=debug_policies,
            )
            state = result.task_state
            action = decide_after_execution(result)
            next_stage = (
                "validator" if action == "ready_for_validation"
                else "executor" if action == "continue_to_executor"
                else "final_answer"
            )
            updates["task_revision"] = state.revision
            tool_trace = (
                result.execution_result.tool_trace
                if result.execution_result is not None else None
            )
            step_output = (
                result.step_output.model_dump(by_alias=True, mode="json")
                if result.step_output is not None else None
            )
            key_state = {
                "action": action,
                "executorOutcome": result.outcome,
                "executionKind": "tool",
                "modelCalled": False,
                "modelCallCount": 0,
                "tool": tool_trace.tool if tool_trace is not None else step.tool_name,
                "toolOk": tool_trace.ok if tool_trace is not None else False,
                "toolDurationMs": (
                    tool_trace.duration_ms if tool_trace is not None else None
                ),
                "toolTransportIdentity": dict(tool_transport_identity),
                "errorCode": result.error_code,
                # Show the output produced by this exact Executor step, not
                # every historical stepOutput in TaskState.  The object is the
                # bounded normalized runtime contract; raw ToolTrace.detail
                # remains outside the single-step public projection.  It is
                # explicitly pending until the next Validator checkpoint.
                "stepOutput": step_output,
                "stepOutputValidationStatus": (
                    "pending_validator" if step_output is not None else "not_produced"
                ),
                **_debug_task_projection(state),
            }

        elif stage == "validator":
            state = await _debug_load_task(turn)
            _pack, projector = await _debug_load_pack(turn)
            validator_view = projector.validator_view(
                plan=state.active_plan,
                executed_steps=_build_executed_steps(state),
                phase_task_revision=state.revision,
            )
            result, state = await run_validator_phase(
                state,
                context_view=validator_view,
            )
            action = decide_after_validation(result)
            compound = state.domain_state.get("compoundComparison")
            if (
                action == "task_completed"
                and isinstance(compound, dict)
                and compound.get("status") == "ready"
            ):
                next_stage = "context_pack"
            else:
                next_stage = (
                    "final_answer" if action in {"task_completed", "stop_turn"}
                    else "replanner"
                )
            updates["task_revision"] = state.revision
            key_state = {
                "action": action,
                "validatorOutcome": result.outcome,
                "executionKind": "deterministic",
                "modelCalled": False,
                "modelCallCount": 0,
                "errorCode": result.error_code,
                **_debug_task_projection(state),
            }

        elif stage == "replanner":
            state = await _debug_load_task(turn)
            _pack, projector = await _debug_load_pack(turn)
            tool_caller = get_tool_transport(
                run_id=turn.run_id,
                context_pack_hash=turn.context_pack_hash,
            )
            tool_transport_identity = get_tool_transport_identity(tool_caller)
            schemas = _explicit_harness_tool_schemas(turn.message, state)
            result = await run_harness_step(
                state,
                turn.message,
                schemas,
                client=get_client(),
                model=settings.deepseek_model,
                tool_caller=tool_caller,
                projector=projector,
                system_policies=debug_policies,
            )
            state = result.task_state
            next_stage = (
                "executor" if result.action == "continue_to_executor"
                else "final_answer"
            )
            updates["task_revision"] = state.revision
            key_state = {
                "action": result.action,
                "replannerOutcome": (
                    result.replanner_result.outcome
                    if result.replanner_result is not None else "skipped"
                ),
                "toolTransportIdentity": dict(tool_transport_identity),
                **_debug_task_projection(state),
            }

        elif stage == "final_answer":
            state = await _debug_load_task(turn)
            answer = turn.final_answer
            answer_source = (
                "task_manager.deterministic_direct"
                if answer is not None else None
            )
            if answer is None and state.pending_questions:
                answer = state.pending_questions[0]
                answer_source = "task_state.pending_question"
            if answer is None:
                _pack, projector = await _debug_load_pack(turn)
                validated_results = _build_validated_results(state, [])
                final_view = projector.final_answer_view(
                    validated_results=validated_results,
                    evidence_refs=_build_validated_evidence_refs(validated_results),
                    phase_task_revision=state.revision,
                )
                answer = _render_validated_used_phone_answer(state, final_view)
                if answer is not None:
                    answer_source = "deterministic.validated_renderer"
            if answer is None:
                raw_validation = state.domain_state.get("validationResult")
                if isinstance(raw_validation, dict) and raw_validation.get("errorCode"):
                    answer = (
                        "本轮结果没有通过可靠性校验："
                        + str(raw_validation["errorCode"])
                    )
                else:
                    answer = "单步执行已经结束，但当前没有可发布的已验证答案。"
                answer_source = "deterministic.safe_fallback"
            guide_result = build_validated_guide_result(state)
            await save_turn(
                turn.session_id,
                [
                    {"role": "user", "content": turn.message},
                    {"role": "assistant", "content": answer},
                ],
                state.task_id,
            )
            next_stage = "done"
            updates.update({
                "final_answer": answer,
                "guide_result": guide_result,
            })
            key_state = {
                "answerLength": len(answer),
                "answerSource": answer_source,
                "modelCalled": False,
                "modelCallCount": 0,
                "publishedGuideResult": guide_result is not None,
                **_debug_task_projection(state),
            }

        else:
            raise ValueError(f"unsupported debug stage: {stage}")

    except Exception as exc:
        logger.exception(
            "Debug turn %s failed at stage %s",
            turn.debug_turn_id,
            stage,
        )
        outcome = "failed"
        stable_code = getattr(exc, "code", None)
        error_code = stable_code or type(exc).__name__
        message = stable_code or str(exc)[:500]
        key_state = {
            **_debug_task_projection(state),
            "failure": message,
        }
        if stage in {"executor", "replanner"}:
            key_state["toolTransportIdentity"] = {
                "status": "error",
                "code": stable_code or "transport_identity_unavailable",
            }
        next_stage = "done"
        updates.update({
            "failure_code": error_code,
        })

    record = completed_step(
        turn,
        stage=stage,
        label=label,
        code_file=code_file,
        code_function=code_function,
        outcome=outcome,
        duration_ms=(time.perf_counter() - started) * 1000,
        key_state=key_state,
        message=message,
        error_code=error_code,
    )
    terminal_status = (
        "failed" if outcome == "failed"
        else "completed" if next_stage == "done"
        else "running"
    )
    return next_checkpoint(
        turn,
        status=terminal_status,
        next_stage=next_stage,
        steps=[*turn.steps, record],
        **updates,
    )


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


def _optional_ms(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _append_unique(target: list[str], value: str | None) -> None:
    if value and value not in target:
        target.append(value)


def _review_ids_from_sources(sources: list[dict[str, Any]] | None) -> list[str]:
    review_ids: list[str] = []
    for source in sources or []:
        _append_unique(review_ids, source.get("reviewId"))
    return review_ids


def _review_ids_from_tool_traces(tool_traces: list[ToolTrace] | None) -> list[str]:
    review_ids: list[str] = []
    for trace in tool_traces or []:
        if not isinstance(trace.detail, dict):
            continue
        if trace.tool in {"search_reviews", "search_shop_reviews"}:
            reviews = trace.detail.get("reviews", [])
            if not isinstance(reviews, list):
                continue
            for review in reviews:
                if isinstance(review, dict):
                    _append_unique(review_ids, review.get("reviewId"))
        if trace.tool == "search_knowledge":
            citations = trace.detail.get("citations", [])
            if not isinstance(citations, list):
                continue
            for citation in citations:
                if not isinstance(citation, dict):
                    continue
                metadata = citation.get("metadata", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                review_id = metadata.get("reviewId")
                if not review_id and citation.get("sourceType") == "review":
                    review_id = citation.get("sourceId")
                _append_unique(
                    review_ids,
                    review_id,
                )
    return review_ids


def _tool_duration_ms(tool_traces: list[ToolTrace] | None) -> float | None:
    durations = [
        trace.duration_ms
        for trace in tool_traces or []
        if trace.duration_ms is not None
    ]
    return round(sum(durations), 2) if durations else None


def _tool_status(tool_traces: list[ToolTrace] | None) -> str:
    traces = tool_traces or []
    if not traces:
        return "not_called"
    successes = sum(trace.ok is True for trace in traces)
    if successes == len(traces):
        return "ok"
    if successes == 0:
        return "failed"
    return "mixed"


def _candidate_count_from_tool_traces(
    tool_traces: list[ToolTrace] | None,
) -> int:
    """Count only a strict candidate ledger from the final successful search."""

    for trace in reversed(tool_traces or []):
        if trace.tool != "search_products" or trace.ok is not True:
            continue
        if not isinstance(trace.detail, dict):
            return 0
        from .domains.ecommerce.ranking_contract import (
            TwoStageRankingContractError,
            normalize_search_products_detail,
        )
        try:
            ranking = normalize_search_products_detail(trace.detail)
        except TwoStageRankingContractError:
            return 0
        return len(ranking.ranked_item_ids)
    return 0


def _failure_class(
    *, request_status: str, agent_status: str, tool_status: str,
) -> str:
    request_failed = request_status != "ok"
    agent_failed = agent_status == "failed"
    tool_failed = tool_status in {"failed", "mixed"}
    if request_failed and agent_failed and tool_failed:
        return "request_agent_and_tool_failure"
    if request_failed and agent_failed:
        return "request_and_agent_failure"
    if request_failed and tool_failed:
        return "request_and_tool_failure"
    if agent_failed and tool_failed:
        return "agent_and_tool_failure"
    if request_failed:
        return "request_failure"
    if agent_failed:
        return "agent_failure"
    if tool_failed:
        return "tool_failure"
    return "none"


def _classify_bottleneck(
    *,
    route: str,
    slow: bool,
    agent_duration_ms: float | None = None,
    retrieval_duration_ms: float | None = None,
    llm_duration_ms: float | None = None,
) -> str:
    if not slow:
        return "none"
    if route in {"/agent/chat-llm", "/agent/chat-llm/stream"}:
        return "agent" if agent_duration_ms is not None else "unknown"
    if route == "/agent/rag-chat":
        retrieval = retrieval_duration_ms or 0
        llm = llm_duration_ms or 0
        if retrieval <= 0 and llm <= 0:
            return "unknown"
        if llm >= retrieval * 1.5:
            return "llm"
        if retrieval >= llm * 1.5:
            return "retrieval"
        return "mixed"
    return "unknown"


def _build_request_trace(
    *,
    request_id: str,
    route: str,
    start: float,
    status: str,
    tool_traces: list[ToolTrace] | None = None,
    sources: list[dict[str, Any]] | None = None,
    metrics: dict[str, float] | None = None,
    agent_duration_ms: float | None = None,
    llm_duration_ms: float | None = None,
    task_state: TaskState | None = None,
    run_id: str | None = None,
    transport_status: str = "response_generated",
    trace_summary: Any | None = None,
    llm_span_snapshot: dict[str, Any] | None = None,
    memory_load: dict[str, Any] | None = None,
) -> RequestTrace:
    metrics = metrics or {}
    tool_names = [trace.tool for trace in tool_traces or []]
    review_ids = _review_ids_from_sources(sources)
    for review_id in _review_ids_from_tool_traces(tool_traces):
        _append_unique(review_ids, review_id)
    total_duration_ms = _elapsed_ms(start)
    slow = total_duration_ms >= SLOW_REQUEST_THRESHOLD_MS
    rounded_agent_duration_ms = _optional_ms(agent_duration_ms)
    stage_calls = dict((llm_span_snapshot or {}).get("modelCalls", {}))
    stage_failures = dict((llm_span_snapshot or {}).get("modelFailures", {}))
    stage_durations = {
        str(stage): round(float(duration), 3)
        for stage, duration in dict(
            (llm_span_snapshot or {}).get("llmDurationMs", {})
        ).items()
    }
    observed_llm_total = sum(stage_durations.values())
    rounded_llm_duration_ms = _optional_ms(
        llm_duration_ms or metrics.get("llmDurationMs") or observed_llm_total
    )
    rounded_retrieval_duration_ms = _optional_ms(metrics.get("retrievalDurationMs"))

    tool_status = _tool_status(tool_traces)
    agent_status = (
        getattr(trace_summary, "agent_status", "not_run")
        if trace_summary is not None else "not_run"
    )
    agent_final_action = (
        getattr(trace_summary, "final_action", None)
        if trace_summary is not None else None
    )
    agent_failure_code = (
        getattr(trace_summary, "failure_code", None)
        if trace_summary is not None else None
    )
    trace = RequestTrace(
        request_id=request_id,
        route=route,
        status=status,
        transport_status=transport_status,
        tool_status=tool_status,
        failure_class=_failure_class(
            request_status=status,
            agent_status=agent_status,
            tool_status=tool_status,
        ),
        candidate_count=_candidate_count_from_tool_traces(tool_traces),
        run_id=run_id,
        agent_status=agent_status,
        agent_final_action=agent_final_action,
        agent_failure_code=agent_failure_code,
        total_duration_ms=total_duration_ms,
        slow=slow,
        slow_threshold_ms=SLOW_REQUEST_THRESHOLD_MS,
        bottleneck=_classify_bottleneck(
            route=route,
            slow=slow,
            agent_duration_ms=rounded_agent_duration_ms,
            retrieval_duration_ms=rounded_retrieval_duration_ms,
            llm_duration_ms=rounded_llm_duration_ms,
        ),
        agent_duration_ms=rounded_agent_duration_ms,
        llm_duration_ms=rounded_llm_duration_ms,
        model_call_counts=stage_calls,
        model_call_failures=stage_failures,
        llm_duration_by_stage_ms=stage_durations,
        retrieval_duration_ms=rounded_retrieval_duration_ms,
        tool_duration_ms=_tool_duration_ms(tool_traces),
        tool_count=len(tool_names),
        tool_names=tool_names,
        review_ids=review_ids,
        task_id=task_state.task_id if task_state is not None else None,
        task_revision=task_state.revision if task_state is not None else None,
        memory_load=memory_load,
    )
    request_trace_logger.info(
        "request_trace %s",
        trace.model_dump_json(by_alias=True, exclude_none=True),
    )
    return trace


@app.post(
    "/agent/debug-turns",
    response_model=DebugTurn,
    status_code=201,
    tags=["智能体接口"],
    summary="创建只读 Agent 单步调试轮次",
)
async def create_debug_turn(request: DebugTurnCreateRequest) -> DebugTurn:
    """Queue one message without executing TaskManager or any business tool."""

    turn = await get_debug_turn_store().create(request)
    await capture_web_query(
        request_id=turn.request_id,
        session_id=turn.session_id,
        message=turn.message,
        route="/agent/debug-turns",
        mode="step_debug",
    )
    return turn


@app.get(
    "/agent/debug-turns/{debug_turn_id}",
    response_model=DebugTurn,
    tags=["智能体接口"],
    summary="读取 Agent 单步调试 checkpoint",
)
async def read_debug_turn(debug_turn_id: str) -> DebugTurn:
    try:
        return await get_debug_turn_store().get(debug_turn_id)
    except DebugTurnNotFoundError as exc:
        raise HTTPException(status_code=404, detail="debug turn not found") from exc


@app.post(
    "/agent/debug-turns/{debug_turn_id}/step",
    response_model=DebugTurn,
    tags=["智能体接口"],
    summary="推进一个 Agent 服务端阶段",
)
async def step_debug_turn(
    debug_turn_id: str,
    request: DebugTurnStepRequest,
) -> DebugTurn:
    store = get_debug_turn_store()
    async with debug_turn_lock(debug_turn_id):
        try:
            turn = await store.get(debug_turn_id)
        except DebugTurnNotFoundError as exc:
            raise HTTPException(status_code=404, detail="debug turn not found") from exc
        if turn.revision != request.expected_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "debug_turn_revision_conflict",
                    "expectedRevision": request.expected_revision,
                    "actualRevision": turn.revision,
                },
            )
        if turn.status in {"completed", "failed", "cancelled"}:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "debug_turn_terminal",
                    "status": turn.status,
                },
            )
        updated = await _advance_debug_turn(turn)
        await store.save(updated)
        if updated.status in {"completed", "failed", "cancelled"}:
            await mark_web_query_outcome(
                updated.request_id,
                updated.status,
                failure_code=updated.failure_code,
            )
        return updated


@app.post(
    "/agent/debug-turns/{debug_turn_id}/cancel",
    response_model=DebugTurn,
    tags=["智能体接口"],
    summary="取消尚未结束的 Agent 单步调试轮次",
)
async def cancel_debug_turn(
    debug_turn_id: str,
    request: DebugTurnStepRequest,
) -> DebugTurn:
    store = get_debug_turn_store()
    async with debug_turn_lock(debug_turn_id):
        try:
            turn = await store.get(debug_turn_id)
        except DebugTurnNotFoundError as exc:
            raise HTTPException(status_code=404, detail="debug turn not found") from exc
        if turn.revision != request.expected_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "debug_turn_revision_conflict",
                    "expectedRevision": request.expected_revision,
                    "actualRevision": turn.revision,
                },
            )
        if turn.status in {"completed", "failed", "cancelled"}:
            return turn
        stage = turn.next_stage
        label, code_file, code_function = _DEBUG_STAGE_CODE.get(
            stage,
            ("取消", "agent/app/main.py", "cancel_debug_turn"),
        )
        record = completed_step(
            turn,
            stage=stage,
            label=f"取消前：{label}",
            code_file=code_file,
            code_function=code_function,
            outcome="cancelled",
            duration_ms=0,
            key_state={"cancelledBeforeStage": stage},
            message="用户取消了本轮单步调试；尚未执行的阶段不会继续。",
        )
        updated = next_checkpoint(
            turn,
            status="cancelled",
            next_stage="done",
            steps=[*turn.steps, record],
        )
        await store.save(updated)
        await mark_web_query_outcome(updated.request_id, "cancelled")
        return updated


@app.get(
    "/agent/recommendations/shops",
    response_model=ShopRecommendationsResponse,
    tags=["智能体接口"],
    summary="猜你喜欢推荐商户",
    description=(
        "给前端“猜你喜欢/为你推荐”区域使用。Agent 服务作为浏览器与 Spring Boot "
        "推荐接口之间的轻量代理，默认使用 demo-user-1。"
    ),
    deprecated=True,
)
async def recommend_shops_for_page(
    user_id: str = Query(
        default="demo-user-1",
        alias="userId",
        min_length=1,
        max_length=64,
        description="用户编号；页面默认使用 demo-user-1",
    ),
    limit: int = Query(default=6, ge=1, le=20, description="最多返回多少家商户"),
    longitude: float | None = Query(default=None, description="可选，用户当前位置经度"),
    latitude: float | None = Query(default=None, description="可选，用户当前位置纬度"),
    radius_meters: float | None = Query(
        default=None,
        alias="radiusMeters",
        gt=0,
        description="可选，推荐半径，单位米",
    ),
) -> ShopRecommendationsResponse:
    start = time.perf_counter()
    request_id = _new_request_id()
    tool_trace = await recommend_shops(
        user_id,
        limit,
        longitude,
        latitude,
        radius_meters,
    )

    if not tool_trace.ok or not isinstance(tool_trace.detail, dict):
        _build_request_trace(
            request_id=request_id,
            route="/agent/recommendations/shops",
            start=start,
            status="error",
            tool_traces=[tool_trace],
            transport_status="response_pending",
        )
        raise HTTPException(status_code=503, detail="商户推荐服务暂时不可用")

    detail = tool_trace.detail
    shops = [
        RecommendedShop.model_validate(shop)
        for shop in detail.get("shops", [])
        if isinstance(shop, dict)
    ]
    return ShopRecommendationsResponse(
        userId=detail.get("userId", user_id),
        shops=shops,
        toolTrace=tool_trace,
        trace=_build_request_trace(
            request_id=request_id,
            route="/agent/recommendations/shops",
            start=start,
            status="ok",
            tool_traces=[tool_trace],
        ),
    )


@app.post(
    "/agent/chat",
    response_model=ChatResponse,
    tags=["智能体接口"],
    summary="智能体对话",
    description=(
        "输入一个本地生活相关问题。当前版本会识别商户/附近/优惠券等意图，"
        "并演示一次对后端服务的工具调用。"
    ),
    deprecated=True,
)
async def chat(request: ChatRequest) -> ChatResponse:
    start = time.perf_counter()
    request_id = _new_request_id()
    message = request.message.strip()
    tool_trace: list[ToolTrace] = []

    if looks_like_shop_type_query(message):
        keyword = extract_shop_type_keyword(message)
        shop_types_trace = await list_shop_types(keyword)
        tool_trace.append(shop_types_trace)
        if shop_types_trace.ok and isinstance(shop_types_trace.detail, dict):
            names = shop_types_trace.detail.get("names", [])
            if names:
                answer = f"目前匹配到这些商户分类：{'、'.join(names)}。"
            else:
                answer = "我查询了后端分类接口，但暂时没有匹配到对应分类。"
        else:
            answer = "我尝试查询商户分类，但后端工具暂时调用失败了。你可以稍后再试。"
    elif looks_like_shop_query(message):
        category = extract_shop_type_keyword(message)
        type_id = extract_shop_type_id(message)
        shops_trace = await search_shops(type_id)
        tool_trace.append(shops_trace)
        if shops_trace.ok and isinstance(shops_trace.detail, dict):
            shops = shops_trace.detail.get("shops", [])
            if shops:
                lines = [
                    f"{shop['name']}（{shop['address']}，人均 {shop['avgPrice']} 元）"
                    for shop in shops
                ]
                prefix = f"为你找到这些「{category}」商户：" if category else "为你找到这些商户："
                answer = prefix + "\n" + "\n".join(lines)
            elif category:
                answer = f"我查询了「{category}」相关商户，但暂时没有找到。"
            else:
                answer = "我查询了后端商户接口，但暂时没有找到商户。"
        else:
            answer = "我尝试查询商户，但后端工具暂时调用失败了。你可以稍后再试。"
    else:
        answer = (
            "现在我是本地生活智能体的最小版本。你可以先问我类似"
            "“帮我找附近适合两个人吃的火锅店”的问题；后续我们会逐步接入真实工具。"
        )

    return ChatResponse(
        answer=answer,
        tool_trace=tool_trace,
        trace=_build_request_trace(
            request_id=request_id,
            route="/agent/chat",
            start=start,
            status="ok",
            tool_traces=tool_trace,
        ),
    )


@app.post(
    "/agent/llm-ping",
    response_model=ChatResponse,
    tags=["智能体接口"],
    summary="测试 DeepSeek 连通性",
    description=(
        "把你的一句话直接发给 DeepSeek 大模型，返回它的普通回答（暂不带工具调用）。"
        "用来验证 Agent 能否成功连上 DeepSeek。"
    ),
)
async def llm_ping(request: ChatRequest) -> ChatResponse:
    start = time.perf_counter()
    request_id = _new_request_id()
    if not settings.deepseek_api_key:
        return ChatResponse(
            answer="还没有配置 DeepSeek API Key。请在项目根目录的 .env 文件里填写 DEEPSEEK_API_KEY，再重启 agent。",
            tool_trace=[],
            trace=_build_request_trace(
                request_id=request_id,
                route="/agent/llm-ping",
                start=start,
                status="missing_api_key",
            ),
        )

    try:
        llm_start = time.perf_counter()
        reply = await ask_llm(request.message)
        llm_duration_ms = (time.perf_counter() - llm_start) * 1000
    except Exception as exc:
        return ChatResponse(
            answer=f"调用 DeepSeek 失败：{exc}",
            tool_trace=[],
            trace=_build_request_trace(
                request_id=request_id,
                route="/agent/llm-ping",
                start=start,
                status="error",
            ),
        )

    return ChatResponse(
        answer=reply,
        tool_trace=[],
        trace=_build_request_trace(
            request_id=request_id,
            route="/agent/llm-ping",
            start=start,
            status="ok",
            llm_duration_ms=llm_duration_ms,
        ),
    )


@app.post(
    "/agent/chat-llm",
    response_model=ChatResponse,
    tags=["智能体接口"],
    summary="智能体对话（大模型版）",
    description=(
        "用 DeepSeek + Function Calling：模型自己决定要不要调工具、调哪个、参数填什么，"
        "再根据后端返回的真实数据组织回答。对照 /agent/chat（关键词规则版）感受差别。"
    ),
)
async def chat_llm(
    request: EcommerceChatRequest,
    authorization: str | None = Header(default=None),
    shopping_memory_session: str | None = Cookie(
        default=None, alias="shopping_memory_session"
    ),
    browser_authorization: str | None = Depends(optional_browser_authorization),
) -> ChatResponse:
    start = time.perf_counter()
    request_id = _new_request_id()
    task_state: TaskState | None = None
    task_relation: TaskRelationDecision | None = None
    memory_run_binding: Any | None = None
    memory_load_summary: dict[str, Any] | None = None
    reference_context: ResolvedReferenceContext | None = None
    reference_failure_answer: str | None = None
    reference_failure_code: str | None = None
    fast: Any | None = None
    final_candidate_ids: list[str] = []
    agent_duration_ms = 0.0
    begin_agent_llm_call_span(run_id=request_id)
    await capture_web_query(
        request_id=request_id,
        session_id=request.session_id,
        message=request.message,
        route="/agent/chat-llm",
        mode="chat",
    )
    if not settings.deepseek_api_key:
        span_snapshot = end_agent_llm_call_span()
        await persist_agent_llm_call_snapshot(span_snapshot)
        response = ChatResponse(
            answer="还没有配置 DeepSeek API Key。请在项目根目录的 .env 文件里填写 DEEPSEEK_API_KEY，再重启 agent。",
            tool_trace=[],
            trace=_build_request_trace(
                request_id=request_id,
                route="/agent/chat-llm",
                start=start,
                status="missing_api_key",
                llm_span_snapshot=span_snapshot,
            ),
        )
        await capture_web_query_diagnostic(
            request_id,
            session_id=request.session_id,
            task_id=None,
            route="/agent/chat-llm",
            payload=response.model_dump(by_alias=True, exclude_none=True, mode="json"),
        )
        await mark_web_query_outcome(
            request_id,
            "failed",
            failure_code="missing_api_key",
        )
        return response

    try:
        async def capture_task_state(state: TaskState, _phase: str) -> None:
            nonlocal task_state
            task_state = state

        if request.session_id:
            task_state, task_relation, started_new = await _prepare_session_task(
                request.session_id,
                request.message.strip(),
                domain_hint=request.domain_hint,
                on_state=capture_task_state,
            )
            history = (
                []
                if started_new
                else await get_history(
                    request.session_id,
                    task_state.task_id,
                    migrate_legacy=True,
                )
            )
        else:
            task_state = await _prepare_ephemeral_task(
                request.message.strip(),
                domain_hint=request.domain_hint,
                on_state=capture_task_state,
            )
            started_new = True
            history = []

        if request.reference_context is not None and not started_new:
            try:
                reference_context = await resolve_reference_context(
                    session_id=request.session_id,
                    state=task_state,
                    hint=request.reference_context,
                )
            except ReferenceContextError as exc:
                reference_failure_answer = exc.safe_answer
                reference_failure_code = exc.code

        fast = (
            analyze_used_phone_fast(request.message.strip(), task_state)
            if settings.used_phone_fast_preview_enabled
            and task_state is not None
            else None
        )

        agent_start = time.perf_counter()
        direct_answer = reference_failure_answer or (
            _task_relation_direct_answer(task_relation, task_state)
            if task_relation is not None and task_state is not None
            else None
        )
        if direct_answer is not None:
            answer = direct_answer
            tool_traces = []
            turn_messages = [
                {"role": "user", "content": request.message.strip()},
                {"role": "assistant", "content": answer},
            ]
            run_id = None
            trace_summary = None
        else:
            memory_resolution = await resolve_memory_run_for_browser_session(
                shopping_memory_session,
                category_id=_memory_category_id(task_state),
                recipient_scope=request.recipient_scope,
                catalog_revision=settings.memory_active_catalog_revision,
                task_id=task_state.task_id if task_state is not None else None,
            )
            memory_run_binding = memory_resolution.binding
            memory_load_summary = memory_resolution.summary
            task_kwargs = {
                "task_state": task_state,
                "on_task_state": capture_task_state,
                "session_id": request.session_id,
                "memory_run_binding": memory_run_binding,
                "reference_context": reference_context,
            }
            task_kwargs["domain_hint"] = PRIMARY_AGENT_DOMAIN_HINT
            effective_authorization = (
                authorization
                if isinstance(authorization, str) and authorization
                else browser_authorization
            )
            with bind_transaction_request(
                authorization=effective_authorization,
                session_id=request.session_id,
                task_id=task_state.task_id if task_state is not None else None,
                task_revision=task_state.revision if task_state is not None else None,
                candidate_scope_id=_active_transaction_scope_id(task_state),
                user_message=request.message.strip(),
            ):
                answer, tool_traces, turn_messages, run_id, trace_summary = await run_agent(
                    request.message.strip(), history=history, **task_kwargs
                )
        final_candidate_ids = _extract_search_candidate_ids(tool_traces)
        agent_duration_ms = (time.perf_counter() - agent_start) * 1000
        if request.session_id:
            await save_turn(
                request.session_id,
                turn_messages,
                task_state.task_id,
            )
    except Exception as exc:
        span_snapshot = end_agent_llm_call_span()
        await persist_agent_llm_call_snapshot(span_snapshot)
        await mark_web_query_outcome(
            request_id,
            "failed",
            failure_code=type(exc).__name__,
        )
        if settings.used_phone_fast_preview_enabled:
            await capture_web_query_observation(
                request_id,
                **_fast_observation_fields(
                    fast,
                    span_snapshot,
                    task_id=(
                        task_state.task_id
                        if task_state is not None
                        else None
                    ),
                    route="/agent/chat-llm",
                    session_id=request.session_id,
                    preview_event=None,
                    final_candidate_ids=final_candidate_ids,
                    final_duration_ms=agent_duration_ms,
                ),
            )
        response = ChatResponse(
            answer=f"调用大模型失败：{exc}",
            tool_trace=[],
            trace=_build_request_trace(
                request_id=request_id,
                route="/agent/chat-llm",
                start=start,
                status="error",
                task_state=task_state,
                llm_span_snapshot=span_snapshot,
                memory_load=memory_load_summary,
            ),
            task_state=(
                task_state.model_dump(by_alias=True, mode="json")
                if task_state is not None
                else None
            ),
            task_relation=(
                task_relation.model_dump(by_alias=True, mode="json")
                if task_relation is not None
                else None
            ),
        )
        await capture_web_query_diagnostic(
            request_id,
            session_id=request.session_id,
            task_id=task_state.task_id if task_state is not None else None,
            route="/agent/chat-llm",
            payload=response.model_dump(by_alias=True, exclude_none=True, mode="json"),
        )
        return response

    span_snapshot = end_agent_llm_call_span()
    await persist_agent_llm_call_snapshot(span_snapshot)
    guide_result = await _publish_browser_guide_result(
        task_state,
        session_id=request.session_id,
        memory_run_binding=memory_run_binding,
    )
    response_reference_context = await _publish_response_reference_context(
        task_state,
        session_id=request.session_id,
        resolved=reference_context,
        guide_result=guide_result,
    )
    response = ChatResponse(
        answer=answer,
        tool_trace=tool_traces,
        guide_result=guide_result,
        reference_context=response_reference_context,
        trace=_build_request_trace(
            request_id=request_id,
            route="/agent/chat-llm",
            start=start,
            status="ok",
            tool_traces=tool_traces,
            agent_duration_ms=agent_duration_ms,
            task_state=task_state,
            run_id=run_id,
            trace_summary=trace_summary,
            llm_span_snapshot=span_snapshot,
            memory_load=memory_load_summary,
        ),
        task_state=(
            task_state.model_dump(by_alias=True, mode="json")
            if task_state is not None
            else None
        ),
        task_relation=(
            task_relation.model_dump(by_alias=True, mode="json")
            if task_relation is not None
            else None
        ),
        run_id=run_id,
        trace_summary=trace_summary.model_dump(by_alias=True, mode="json") if trace_summary is not None else None,
    )
    if settings.used_phone_fast_preview_enabled:
        await capture_web_query_observation(
            request_id,
            **_fast_observation_fields(
                fast,
                span_snapshot,
                task_id=task_state.task_id if task_state is not None else None,
                route="/agent/chat-llm",
                session_id=request.session_id,
                preview_event=None,
                final_candidate_ids=final_candidate_ids,
                final_duration_ms=agent_duration_ms,
            ),
        )
    diagnostic_payload = response.model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )
    diagnostic_payload["referenceContextFlow"] = _reference_context_flow_diagnostic(
        request_hint=request.reference_context,
        resolved=reference_context,
        failure_code=reference_failure_code,
    )
    await capture_web_query_diagnostic(
        request_id,
        session_id=request.session_id,
        task_id=task_state.task_id if task_state is not None else None,
        route="/agent/chat-llm",
        payload=diagnostic_payload,
    )
    await mark_web_query_outcome(request_id, "completed")
    if type(shopping_memory_session) is str:
        category_id = _memory_category_id(task_state)
        if category_id is not None:
            schedule_memory_extraction(
                browser_session_id=shopping_memory_session,
                message_id=request_id,
                user_message=request.message.strip(),
                category_id=category_id,
                recipient_scope=request.recipient_scope,
            )
    return response


@app.post(
    "/agent/chat-llm-durable",
    response_model=ChatResponse,
    tags=["智能体接口"],
    summary="智能体对话（Day-2 durable 控制面）",
    description=(
        "Day-2 持久化控制面：真实 interrupt()/resume() 澄清、进程重启恢复、"
        "TaskState revision 对账、幂等续答。仅当 AGENT_GRAPH_V2_DURABLE_ENABLED=true 时启用。"
    ),
)
async def chat_llm_durable(
    request: DurableEcommerceChatRequest,
    authorization: str | None = Header(default=None),
    shopping_memory_session: str | None = Cookie(default=None, alias="shopping_memory_session"),
) -> ChatResponse:
    start = time.perf_counter()
    request_id = _new_request_id()
    route = "/agent/chat-llm-durable"
    task_state: TaskState | None = None
    task_relation: TaskRelationDecision | None = None
    reference_context: ResolvedReferenceContext | None = None
    reference_failure_answer: str | None = None
    reference_failure_code: str | None = None
    final_candidate_ids: list[str] = []
    agent_duration_ms = 0.0
    resume_payload = request.resume
    restart_task_id = request.restart_task_id
    pause_receipt = request.pause_receipt
    if pause_receipt is not None and not restart_task_id:
        raise HTTPException(
            status_code=422,
            detail="pauseReceipt 只能与 restartTaskId 一起提交",
        )
    exact_resume_replay = False
    started_new = False
    await capture_web_query(
        request_id=request_id,
        session_id=request.session_id,
        message=request.message,
        route=route,
        mode="chat",
    )
    # Durable work is never anonymous: requiring this before state selection
    # prevents an omitted sessionId from turning into a cross-session reader.
    if not request.session_id:
        await mark_web_query_outcome(
            request_id, "failed", failure_code="durable_session_missing"
        )
        return _durable_ownership_rejected_response(
            request_id=request_id, route=route, start=start
        )
    if not settings.agent_graph_v2_durable_enabled:
        await mark_web_query_outcome(
            request_id, "failed", failure_code="durable_disabled"
        )
        return ChatResponse(
            answer="当前部署未启用 Day-2 持久化控制面（AGENT_GRAPH_V2_DURABLE_ENABLED）。",
            tool_trace=[],
            trace=_build_request_trace(
                request_id=request_id,
                route=route,
                start=start,
                status="error",
            ),
        )
    if not settings.deepseek_api_key:
        await mark_web_query_outcome(
            request_id,
            "failed",
            failure_code="missing_api_key",
        )
        return ChatResponse(
            answer="还没有配置 DeepSeek API Key。请在项目根目录的 .env 文件里填写 DEEPSEEK_API_KEY，再重启 agent。",
            tool_trace=[],
            trace=_build_request_trace(
                request_id=request_id,
                route=route,
                start=start,
                status="missing_api_key",
            ),
        )

    try:
        async def capture_task_state(state: TaskState, _phase: str) -> None:
            nonlocal task_state
            task_state = state

        if resume_payload is not None:
            task_id = resume_payload.get("taskId")
            if not isinstance(task_id, str) or not task_id:
                await mark_web_query_outcome(
                    request_id, "failed", failure_code="resume_missing_task_id"
                )
                return ChatResponse(
                    answer="续答载荷缺少 taskId，系统已安全停止；请重新开始当前任务。",
                    tool_trace=[],
                    trace=_build_request_trace(
                        request_id=request_id,
                        route=route,
                        start=start,
                        status="error",
                    ),
                )
            loaded = await get_task_state(task_id)
            if loaded is None:
                await mark_web_query_outcome(
                    request_id, "failed", failure_code="resume_task_not_found"
                )
                return ChatResponse(
                    answer="当前任务状态不存在或已过期，无法继续；请重新开始当前任务。",
                    tool_trace=[],
                    trace=_build_request_trace(
                        request_id=request_id,
                        route=route,
                        start=start,
                        status="error",
                    ),
                )
            if not _durable_session_owns_task(request.session_id, loaded):
                await mark_web_query_outcome(
                    request_id, "failed", failure_code="durable_session_ownership_rejected"
                )
                return _durable_ownership_rejected_response(
                    request_id=request_id, route=route, start=start
                )
            task_state = loaded
            # A matching resolved receipt is an HTTP replay, not a new chat
            # turn. The durable runner repeats the full identity validation.
            from .graph import is_exact_resolved_resume
            exact_resume_replay = is_exact_resolved_resume(
                loaded, resume_payload, session_id=request.session_id
            )
            history = (
                await get_history(request.session_id, task_id, migrate_legacy=True)
                if request.session_id and not exact_resume_replay
                else []
            )
            begin_agent_llm_call_span(run_id=request_id)
        elif restart_task_id:
            loaded = await get_task_state(restart_task_id)
            if loaded is None:
                await mark_web_query_outcome(
                    request_id, "failed", failure_code="restart_task_not_found"
                )
                return ChatResponse(
                    answer="当前任务状态不存在或已过期，无法继续；请重新开始当前任务。",
                    tool_trace=[],
                    trace=_build_request_trace(
                        request_id=request_id,
                        route=route,
                        start=start,
                        status="error",
                    ),
                )
            if not _durable_session_owns_task(request.session_id, loaded):
                await mark_web_query_outcome(
                    request_id, "failed", failure_code="durable_session_ownership_rejected"
                )
                return _durable_ownership_rejected_response(
                    request_id=request_id, route=route, start=start
                )
            task_state = loaded
            history = (
                await get_history(request.session_id, restart_task_id, migrate_legacy=True)
                if request.session_id
                else []
            )
            begin_agent_llm_call_span(run_id=request_id)
        else:
            # Include TaskManager/TaskState classification in the same
            # per-request attribution span as the durable Harness run.
            begin_agent_llm_call_span(run_id=request_id)
            task_state, task_relation, started_new = await _prepare_session_task(
                request.session_id,
                request.message.strip(),
                domain_hint=request.domain_hint,
                on_state=capture_task_state,
            )
            history = (
                []
                if started_new
                else await get_history(
                    request.session_id,
                    task_state.task_id,
                    migrate_legacy=True,
                )
            )

        # The normal JSON/SSE endpoints and the durable continuation endpoint
        # share one ReferenceContext resolver.  A browser focus chosen after a
        # clarification is therefore bound to the same server-owned receipt
        # before the durable runner sees it.  Exact HTTP replays deliberately
        # skip this new input: they replay an already resolved terminal result.
        if (
            request.reference_context is not None
            and not started_new
            and not exact_resume_replay
        ):
            try:
                reference_context = await resolve_reference_context(
                    session_id=request.session_id,
                    state=task_state,
                    hint=request.reference_context,
                )
            except ReferenceContextError as exc:
                reference_failure_answer = exc.safe_answer
                reference_failure_code = exc.code

        if not await claim_task_durable_mode(task_state.task_id):
            await mark_web_query_outcome(
                request_id, "failed", failure_code="memory_task_non_durable"
            )
            return ChatResponse(
                answer="当前任务使用了购物记忆，为保证同一轮记忆快照不漂移，不能进入暂停或恢复流程；请新建任务继续。",
                tool_trace=[],
                trace=_build_request_trace(
                    request_id=request_id,
                    route=route,
                    start=start,
                    status="error",
                ),
                task_state=task_state.model_dump(by_alias=True, mode="json"),
            )

        agent_start = time.perf_counter()
        task_kwargs: dict[str, Any] = {
            "task_state": task_state,
            "on_task_state": capture_task_state,
            "session_id": request.session_id,
            "reference_context": reference_context,
        }
        task_kwargs["domain_hint"] = PRIMARY_AGENT_DOMAIN_HINT
        if resume_payload is not None:
            task_kwargs["resume"] = resume_payload
        if restart_task_id:
            task_kwargs["restart"] = True
            task_kwargs["pause_resume"] = pause_receipt
        if settings.memory_durable_snapshot_enabled and not exact_resume_replay:
            memory_resolution = await resolve_memory_run_for_browser_session(
                shopping_memory_session, category_id=_memory_category_id(task_state),
                recipient_scope=request.recipient_scope,
                catalog_revision=settings.memory_active_catalog_revision,
                task_id=task_state.task_id,
            )
            task_kwargs["memory_run_binding"] = memory_resolution.binding
        direct_answer = reference_failure_answer or (
            _task_relation_direct_answer(task_relation, task_state)
            if task_relation is not None and task_state is not None
            else None
        )
        if direct_answer is not None:
            answer = direct_answer
            tool_traces = []
            turn_messages = [
                {"role": "user", "content": request.message.strip()},
                {"role": "assistant", "content": answer},
            ]
            run_id = None
            trace_summary = None
        else:
            with bind_transaction_request(
                authorization=authorization,
                session_id=request.session_id,
                task_id=task_state.task_id,
                task_revision=task_state.revision,
                candidate_scope_id=_active_transaction_scope_id(task_state),
                user_message=request.message.strip(),
            ):
                answer, tool_traces, turn_messages, run_id, trace_summary = await run_agent(
                    request.message.strip(), history=history, **task_kwargs
                )
        agent_duration_ms = (time.perf_counter() - agent_start) * 1000
        if request.session_id and not exact_resume_replay:
            await save_turn(
                request.session_id,
                turn_messages,
                task_state.task_id,
            )
    except Exception as exc:
        span_snapshot = end_agent_llm_call_span()
        await persist_agent_llm_call_snapshot(span_snapshot)
        await mark_web_query_outcome(
            request_id,
            "failed",
            failure_code=type(exc).__name__,
        )
        return ChatResponse(
            answer=f"调用大模型失败：{exc}",
            tool_trace=[],
            trace=_build_request_trace(
                request_id=request_id,
                route=route,
                start=start,
                status="error",
                task_state=task_state,
                llm_span_snapshot=span_snapshot,
            ),
            task_state=(
                task_state.model_dump(by_alias=True, mode="json")
                if task_state is not None
                else None
            ),
            task_relation=(
                task_relation.model_dump(by_alias=True, mode="json")
                if task_relation is not None
                else None
            ),
        )

    span_snapshot = end_agent_llm_call_span()
    await persist_agent_llm_call_snapshot(span_snapshot)
    guide_result = (
        build_validated_guide_result(task_state)
        if exact_resume_replay
        else await _publish_browser_guide_result(
            task_state,
            session_id=request.session_id,
        )
    )
    response_reference_context = await _publish_response_reference_context(
        task_state,
        session_id=request.session_id,
        resolved=reference_context,
        guide_result=guide_result,
    )
    response = ChatResponse(
        answer=answer,
        tool_trace=tool_traces,
        guide_result=guide_result,
        reference_context=response_reference_context,
        trace=_build_request_trace(
            request_id=request_id,
            route=route,
            start=start,
            status="ok",
            tool_traces=tool_traces,
            agent_duration_ms=agent_duration_ms,
            task_state=task_state,
            run_id=run_id,
            trace_summary=trace_summary,
            llm_span_snapshot=span_snapshot,
        ),
        task_state=(
            task_state.model_dump(by_alias=True, mode="json")
            if task_state is not None
            else None
        ),
        task_relation=(
            task_relation.model_dump(by_alias=True, mode="json")
            if task_relation is not None
            else None
        ),
        run_id=run_id,
        trace_summary=trace_summary.model_dump(by_alias=True, mode="json") if trace_summary is not None else None,
    )
    diagnostic_payload = response.model_dump(
        by_alias=True,
        exclude_none=True,
        mode="json",
    )
    diagnostic_payload["referenceContextFlow"] = _reference_context_flow_diagnostic(
        request_hint=request.reference_context,
        resolved=reference_context,
        failure_code=reference_failure_code,
    )
    await capture_web_query_diagnostic(
        request_id,
        session_id=request.session_id,
        task_id=task_state.task_id if task_state is not None else None,
        route=route,
        payload=diagnostic_payload,
    )
    await mark_web_query_outcome(request_id, "completed")
    return response


@app.post(
    "/agent/chat-llm/stream",
    response_class=StreamingResponse,
    tags=["智能体接口"],
    summary="智能体对话（流式输出）",
    description=(
        "以 Server-Sent Events 增量返回 DeepSeek 回答；complete 事件保留完整回答、"
        "工具调用记录和请求 trace，旧版 /agent/chat-llm JSON 接口继续兼容。"
    ),
)
async def chat_llm_stream(
    request: EcommerceChatRequest,
    authorization: str | None = Header(default=None),
    shopping_memory_session: str | None = Cookie(
        default=None, alias="shopping_memory_session"
    ),
) -> StreamingResponse:
    start = time.perf_counter()
    request_id = _new_request_id()
    route = "/agent/chat-llm/stream"
    await capture_web_query(
        request_id=request_id,
        session_id=request.session_id,
        message=request.message,
        route=route,
        mode="chat",
    )

    async def event_stream():
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

        async def emit_delta(delta: str) -> None:
            if delta:
                await queue.put({"type": "delta", "delta": delta})

        async def produce() -> None:
            task_state: TaskState | None = None
            task_relation: TaskRelationDecision | None = None
            memory_run_binding: Any | None = None
            memory_load_summary: dict[str, Any] | None = None
            reference_context: ResolvedReferenceContext | None = None
            reference_failure_answer: str | None = None
            reference_failure_code: str | None = None
            fast: Any | None = None
            preview_event: dict[str, Any] | None = None
            final_candidate_ids: list[str] = []
            agent_duration_ms = 0.0
            begin_agent_llm_call_span(run_id=request_id)
            try:
                if not settings.deepseek_api_key:
                    response = ChatResponse(
                        answer=(
                            "还没有配置 DeepSeek API Key。请在项目根目录的 .env 文件里填写 "
                            "DEEPSEEK_API_KEY，再重启 agent。"
                        ),
                        tool_trace=[],
                        trace=_build_request_trace(
                            request_id=request_id,
                            route=route,
                            start=start,
                            status="missing_api_key",
                        ),
                    )
                else:
                    async def emit_task_state(state: TaskState, phase: str) -> None:
                        nonlocal task_state
                        task_state = state
                        await queue.put(
                            {
                                "type": "task_state",
                                "phase": phase,
                                "state": state.model_dump(
                                    by_alias=True,
                                    mode="json",
                                ),
                            }
                        )

                    async def emit_task_relation(
                        decision: TaskRelationDecision,
                    ) -> None:
                        nonlocal task_relation
                        task_relation = decision
                        await queue.put(
                            {
                                "type": "task_relation",
                                "decision": decision.model_dump(
                                    by_alias=True,
                                    mode="json",
                                ),
                            }
                        )

                    if request.session_id:
                        task_state, task_relation, started_new = (
                            await _prepare_session_task(
                                request.session_id,
                                request.message.strip(),
                                domain_hint=request.domain_hint,
                                on_state=emit_task_state,
                                on_relation=emit_task_relation,
                            )
                        )
                    else:
                        task_state = await _prepare_ephemeral_task(
                            request.message.strip(),
                            domain_hint=request.domain_hint,
                            on_state=emit_task_state,
                        )
                        started_new = True

                    if request.reference_context is not None and not started_new:
                        try:
                            reference_context = await resolve_reference_context(
                                session_id=request.session_id,
                                state=task_state,
                                hint=request.reference_context,
                            )
                        except ReferenceContextError as exc:
                            reference_failure_answer = exc.safe_answer
                            reference_failure_code = exc.code

                    fast = (
                        analyze_used_phone_fast(request.message.strip(), task_state)
                        if settings.used_phone_fast_preview_enabled
                        and task_state is not None
                        else None
                    )

                    agent_start = time.perf_counter()
                    direct_answer = reference_failure_answer or (
                        _task_relation_direct_answer(task_relation, task_state)
                        if task_relation is not None and task_state is not None
                        else None
                    )
                    if direct_answer is not None:
                        answer = direct_answer
                        tool_traces = []
                        turn_messages = [
                            {"role": "user", "content": request.message.strip()},
                            {"role": "assistant", "content": answer},
                        ]
                        await emit_delta(answer)
                        run_id = None
                        trace_summary = None
                    else:
                        # Emit the provisional preview BEFORE the session-history
                        # load: history is only needed by the full agent, and
                        # holding the preview hostage to a multi-megabyte Redis
                        # read would push the warm preview past the 1s bar and
                        # defeat 先快后完整.
                        preview_event = await _maybe_build_preview_event(
                            request_id,
                            request.message.strip(),
                            task_state,
                            fast=fast,
                        )
                        if preview_event is not None:
                            await queue.put(preview_event)
                        history = (
                            []
                            if started_new
                            else await get_history(
                                request.session_id,
                                task_state.task_id,
                                migrate_legacy=True,
                            )
                        )
                        agent_start = time.perf_counter()
                        memory_resolution = await resolve_memory_run_for_browser_session(
                            shopping_memory_session,
                            category_id=_memory_category_id(task_state),
                            recipient_scope=request.recipient_scope,
                            catalog_revision=settings.memory_active_catalog_revision,
                            task_id=task_state.task_id if task_state is not None else None,
                        )
                        memory_run_binding = memory_resolution.binding
                        memory_load_summary = memory_resolution.summary
                        task_kwargs = {
                            "task_state": task_state,
                            "on_task_state": emit_task_state,
                            "session_id": request.session_id,
                            "memory_run_binding": memory_run_binding,
                            "reference_context": reference_context,
                        }
                        task_kwargs["domain_hint"] = PRIMARY_AGENT_DOMAIN_HINT
                        with bind_transaction_request(
                            authorization=authorization,
                            session_id=request.session_id,
                            task_id=(
                                task_state.task_id
                                if task_state is not None
                                else None
                            ),
                            task_revision=(
                                task_state.revision
                                if task_state is not None
                                else None
                            ),
                            candidate_scope_id=_active_transaction_scope_id(task_state),
                            user_message=request.message.strip(),
                        ):
                            answer, tool_traces, turn_messages, run_id, trace_summary = await run_agent(
                                request.message.strip(),
                                history=history,
                                on_answer_delta=emit_delta,
                                **task_kwargs,
                            )
                    final_candidate_ids = _extract_search_candidate_ids(tool_traces)
                    agent_duration_ms = (time.perf_counter() - agent_start) * 1000
                    if request.session_id:
                        await save_turn(
                            request.session_id,
                            turn_messages,
                            task_state.task_id,
                        )
                    guide_result = await _publish_browser_guide_result(
                        task_state,
                        session_id=request.session_id,
                        memory_run_binding=memory_run_binding,
                    )
                    response_reference_context = (
                        await _publish_response_reference_context(
                            task_state,
                            session_id=request.session_id,
                            resolved=reference_context,
                            guide_result=guide_result,
                        )
                    )
                    response = ChatResponse(
                        answer=answer,
                        tool_trace=tool_traces,
                        guide_result=guide_result,
                        reference_context=response_reference_context,
                        trace=_build_request_trace(
                            request_id=request_id,
                            route=route,
                            start=start,
                            status="ok",
                            tool_traces=tool_traces,
                            agent_duration_ms=agent_duration_ms,
                            task_state=task_state,
                            run_id=run_id,
                            trace_summary=trace_summary,
                            memory_load=memory_load_summary,
                        ),
                        task_state=(
                            task_state.model_dump(by_alias=True, mode="json")
                            if task_state is not None
                            else None
                        ),
                        task_relation=(
                            task_relation.model_dump(by_alias=True, mode="json")
                            if task_relation is not None
                            else None
                        ),
                        run_id=run_id,
                        trace_summary=trace_summary.model_dump(by_alias=True, mode="json") if trace_summary is not None else None,
                    )
            except asyncio.CancelledError:
                await mark_web_query_outcome(request_id, "cancelled")
                raise
            except Exception as exc:
                logger.exception("流式智能体调用失败")
                response = ChatResponse(
                    answer=f"调用大模型失败：{exc}",
                    tool_trace=[],
                    trace=_build_request_trace(
                        request_id=request_id,
                        route=route,
                        start=start,
                        status="error",
                        task_state=task_state,
                        memory_load=memory_load_summary,
                    ),
                    task_state=(
                        task_state.model_dump(by_alias=True, mode="json")
                        if task_state is not None
                        else None
                    ),
                    task_relation=(
                        task_relation.model_dump(by_alias=True, mode="json")
                        if task_relation is not None
                        else None
                    ),
                )
            finally:
                span_snapshot = end_agent_llm_call_span()
                await persist_agent_llm_call_snapshot(span_snapshot)
                if settings.used_phone_fast_preview_enabled:
                    await capture_web_query_observation(
                        request_id,
                        **_fast_observation_fields(
                            fast,
                            span_snapshot,
                            task_id=(
                                task_state.task_id
                                if task_state is not None
                                else None
                            ),
                            route=route,
                            session_id=request.session_id,
                            preview_event=preview_event,
                            final_candidate_ids=final_candidate_ids,
                            final_duration_ms=agent_duration_ms,
                        ),
                    )

            trace_status = response.trace.status if response.trace is not None else "error"
            diagnostic_payload = response.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            )
            diagnostic_payload["referenceContextFlow"] = (
                _reference_context_flow_diagnostic(
                    request_hint=request.reference_context,
                    resolved=reference_context,
                    failure_code=reference_failure_code,
                )
            )
            await capture_web_query_diagnostic(
                request_id,
                session_id=request.session_id,
                task_id=task_state.task_id if task_state is not None else None,
                route=route,
                payload=diagnostic_payload,
            )
            await mark_web_query_outcome(
                request_id,
                "completed" if trace_status == "ok" else "failed",
                failure_code=None if trace_status == "ok" else trace_status,
            )
            if trace_status == "ok" and type(shopping_memory_session) is str:
                category_id = _memory_category_id(task_state)
                if category_id is not None:
                    schedule_memory_extraction(
                        browser_session_id=shopping_memory_session,
                        message_id=request_id,
                        user_message=request.message.strip(),
                        category_id=category_id,
                        recipient_scope=request.recipient_scope,
                    )
            await queue.put(
                {
                    "type": "complete",
                    "data": response.model_dump(
                        by_alias=True,
                        exclude_none=True,
                    ),
                }
            )
            await queue.put(None)

        yield _sse_data(
            {
                "type": "status",
                "phase": "thinking",
                "message": "正在分析问题，必要时查询数据…",
            }
        )
        producer = asyncio.create_task(produce())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield _sse_data(event)
        finally:
            if not producer.done():
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/agent/rag-chat",
    response_model=RagChatResponse,
    tags=["智能体接口"],
    summary="商户评论 RAG 问答",
    description=(
        "auto按问题选择快速Hybrid或高级内容判断；mode=advanced时显式执行Hybrid召回、"
        "内容重排、Context Selection和reviewId引用审计。"
    ),
    deprecated=True,
)
async def rag_chat(request: RagChatRequest) -> RagChatResponse:
    start = time.perf_counter()
    request_id = _new_request_id()
    effective_mode = request.mode
    if request.mode == "auto":
        effective_mode = (
            "advanced" if requires_advanced_rag(request.message) else "standard"
        )
    if not settings.deepseek_api_key:
        return RagChatResponse(
            answer="还没有配置 DeepSeek API Key。请在项目根目录的 .env 文件里填写 DEEPSEEK_API_KEY，再重启 agent。",
            sources=[],
            mode=effective_mode,
            trace=_build_request_trace(
                request_id=request_id,
                route="/agent/rag-chat",
                start=start,
                status="missing_api_key",
            ),
        )

    try:
        pipeline = None
        if effective_mode == "advanced":
            answer, sources, metrics, pipeline = (
                await answer_with_advanced_rag_observed(request.message)
            )
            pipeline = {
                **pipeline,
                "requestedMode": request.mode,
                "autoRouteReason": (
                    "preference_or_recommendation_query"
                    if request.mode == "auto"
                    else "explicit_mode"
                ),
            }
        else:
            answer, sources, metrics = await answer_with_rag_observed(request.message)
    except RagRetrievalError:
        logger.exception("RAG 评论检索失败")
        return RagChatResponse(
            answer="评论检索服务暂时不可用，请稍后重试。",
            sources=[],
            mode=effective_mode,
            trace=_build_request_trace(
                request_id=request_id,
                route="/agent/rag-chat",
                start=start,
                status="retrieval_error",
            ),
        )
    except RagGenerationError as exc:
        logger.exception("RAG 回答生成失败")
        return RagChatResponse(
            answer="已找到相关评论，但回答生成服务暂时不可用，请稍后重试。",
            sources=exc.sources,
            mode=effective_mode,
            trace=_build_request_trace(
                request_id=request_id,
                route="/agent/rag-chat",
                start=start,
                status="generation_error",
                sources=exc.sources,
                metrics=exc.metrics,
            ),
        )

    return RagChatResponse(
        answer=answer,
        sources=sources,
        mode=effective_mode,
        pipeline=pipeline,
        trace=_build_request_trace(
            request_id=request_id,
            route="/agent/rag-chat",
            start=start,
            status="ok",
            sources=sources,
            metrics=metrics,
        ),
    )


@app.delete(
    "/agent/sessions/{session_id}",
    response_model=SessionClearResponse,
    tags=["智能体接口"],
    summary="清除会话记忆",
    description="清除指定sessionId的对话历史、TaskState绑定、快照和事件。",
)
async def delete_session(session_id: str) -> SessionClearResponse:
    try:
        await clear_transaction_confirmations(session_id)
    except ConfirmationStoreUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="交易确认存储暂时不可用，会话尚未清除，请稍后重试。",
        ) from exc
    await clear_session(session_id)
    await clear_session_task_state(session_id)
    return SessionClearResponse(sessionId=session_id, cleared=True)


@app.get(
    "/agent/sessions/{session_id}/task",
    response_model=TaskState,
    tags=["任务状态"],
    summary="读取聊天会话当前绑定的TaskState",
)
async def get_session_task(session_id: str) -> TaskState:
    state = await get_session_task_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="该会话尚未绑定TaskState")
    return state


@app.get(
    "/agent/sessions/{session_id}/tasks",
    response_model=list[TaskState],
    tags=["任务状态"],
    summary="读取聊天会话最近的TaskState列表",
)
async def get_session_tasks(session_id: str) -> list[TaskState]:
    states = await list_session_task_states(session_id)
    if not states:
        raise HTTPException(status_code=404, detail="该会话尚无TaskState")
    return states


@app.post(
    "/agent/sessions/{session_id}/pause",
    tags=["智能体控制面"],
    summary="请求在下一个 durable checkpoint 边界暂停",
)
async def pause_session_run(session_id: str) -> dict[str, Any]:
    """Request cooperative pause without interrupting an in-flight tool."""

    if not (
        settings.agent_control_runtime in {"fixed_v1", "react_v1"}
        and (settings.agent_control_runtime == "fixed_v1" or settings.agent_react_live_enabled)
        and settings.agent_graph_v2_durable_enabled
    ):
        raise HTTPException(status_code=409, detail="当前部署未启用可暂停的 durable runtime")
    state = await get_session_task_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="该会话尚未绑定TaskState")
    if not _durable_session_owns_task(session_id, state):
        raise HTTPException(status_code=403, detail="当前会话无权暂停该任务")
    if not await claim_task_durable_mode(state.task_id):
        raise HTTPException(
            status_code=409,
            detail="使用购物记忆的任务暂不允许暂停或恢复，请新建任务继续",
        )

    from .graph import read_task_cursor
    from .graph.pause_control import public_pause_receipt, request_graph_pause
    from .graph.resume import session_owner_hash
    from .graph.runtime import CONTROL_POLICY_REVISIONS

    cursor = await read_task_cursor(state.task_id)
    control_policy = settings.agent_control_runtime
    expected_policy_revision = CONTROL_POLICY_REVISIONS[control_policy]
    if (
        not isinstance(cursor, dict)
        or cursor.get("sessionOwnerHash") != session_owner_hash(session_id)
        or cursor.get("controlPolicy") != control_policy
        or cursor.get("policyRevision") != expected_policy_revision
        or not isinstance(cursor.get("runId"), str)
        or not isinstance(cursor.get("threadId"), str)
    ):
        raise HTTPException(
            status_code=409,
            detail="当前任务尚未建立可暂停的 durable checkpoint 游标",
        )
    receipt = await request_graph_pause(
        task_id=state.task_id,
        session_id=session_id,
        run_id=cursor["runId"],
        thread_id=cursor["threadId"],
        control_policy=control_policy,
    )
    return public_pause_receipt(receipt) or {}


@app.get(
    "/agent/sessions/{session_id}/pause",
    tags=["智能体控制面"],
    summary="读取当前 durable checkpoint 暂停状态",
)
async def get_session_pause(session_id: str) -> dict[str, Any]:
    state = await get_session_task_state(session_id)
    if state is None:
        raise HTTPException(status_code=404, detail="该会话尚未绑定TaskState")
    if not _durable_session_owns_task(session_id, state):
        raise HTTPException(status_code=403, detail="当前会话无权读取该任务")

    from .graph.pause_control import public_pause_receipt, read_graph_pause
    from .graph.resume import session_owner_hash

    receipt = await read_graph_pause(state.task_id)
    if (
        not isinstance(receipt, dict)
        or receipt.get("sessionOwnerHash") != session_owner_hash(session_id)
    ):
        raise HTTPException(status_code=404, detail="当前任务没有暂停请求")
    return public_pause_receipt(receipt) or {}


@app.get(
    "/agent/runtime-status",
    tags=["智能体控制面"],
    summary="读取非敏感控制运行时状态",
)
async def get_agent_runtime_status() -> dict[str, Any]:
    from .graph.runtime import CONTROL_POLICY_REVISIONS

    policy = settings.agent_control_runtime
    return {
        "enteredRuntimeDefault": policy,
        "controlPolicy": policy,
        "policyRevision": CONTROL_POLICY_REVISIONS.get(policy),
        "reactLive": bool(settings.agent_react_live_enabled),
        "durableCheckpoint": bool(settings.agent_graph_v2_durable_enabled),
        "maxModelDecisionsPerTurn": settings.agent_react_v1_max_model_decisions,
        "rollbackPolicy": "fixed_v1",
    }


@app.put(
    "/internal/review-vectors/{review_id}",
    response_model=ReviewVectorSyncResponse,
    tags=["系统接口"],
    summary="新增或更新单条评论向量",
)
def sync_review_vector(
    review_id: str,
    request: ReviewVectorSyncRequest,
    projection_revision: int | None = Header(default=None, alias="X-Projection-Revision", ge=1),
) -> ReviewVectorSyncResponse:
    try:
        review = {
                "reviewId": review_id,
                "shopId": request.shop_id,
                "shopName": request.shop_name,
                "content": request.content,
                "source": request.source,
                "language": request.language,
                "contentZh": request.content_zh,
                "translationStatus": request.translation_status,
                "tags": request.tags,
            }
        apply_review_projection(review_id, projection_revision, {"operation":"upsert", "review":review},
                                lambda: upsert_review_search_indexes(review))
    except Exception as exc:
        logger.exception("评论向量 upsert 失败: reviewId=%s", review_id)
        raise HTTPException(status_code=503, detail="评论向量同步失败") from exc

    return ReviewVectorSyncResponse(
        reviewId=review_id,
        operation="upsert",
        synced=True,
    )


@app.delete(
    "/internal/review-vectors/{review_id}",
    response_model=ReviewVectorSyncResponse,
    tags=["系统接口"],
    summary="删除单条评论向量",
)
def remove_review_vector(review_id: str,
                         projection_revision: int | None = Header(default=None, alias="X-Projection-Revision", ge=1)) -> ReviewVectorSyncResponse:
    try:
        apply_review_projection(review_id, projection_revision, {"operation":"delete", "reviewId":review_id},
                                lambda: delete_review_search_indexes(review_id))
    except Exception as exc:
        logger.exception("评论向量 delete 失败: reviewId=%s", review_id)
        raise HTTPException(status_code=503, detail="评论向量同步失败") from exc

    return ReviewVectorSyncResponse(
        reviewId=review_id,
        operation="delete",
        synced=True,
    )


# ── Debug: Agent Run Trace (gated) ────────────────────────────────────────────


@app.get(
    "/internal/debug/agent-runs/{run_id}",
    tags=["系统接口"],
    summary="按 runId 查询 AgentRunTrace（需 X-Agent-Debug-Key）",
    response_description="完整的 AgentRunTrace JSON 或 401/403/404",
)
async def debug_agent_run(
    run_id: str,
    x_agent_debug_key: str | None = Header(default=None, alias=TRACE_DEBUG_HEADER),
) -> dict[str, Any]:
    if not trace_debug_enabled():
        raise HTTPException(status_code=503, detail="Debug interface is disabled")
    if not validate_debug_key(x_agent_debug_key):
        raise HTTPException(status_code=403, detail="Invalid or missing debug key")
    trace = await get_trace_for_debug(run_id, x_agent_debug_key or "")
    if trace is None:
        raise HTTPException(status_code=404, detail=f"No trace found for runId={run_id}")
    return trace.model_dump(by_alias=True, mode="json")


def _require_query_intake_debug_access(debug_key: str | None) -> None:
    if not trace_debug_enabled():
        raise HTTPException(status_code=503, detail="Debug interface is disabled")
    if not validate_debug_key(debug_key):
        raise HTTPException(status_code=403, detail="Invalid or missing debug key")


@app.get(
    "/internal/debug/web-query-intake",
    tags=["系统接口"],
    summary="导出网页真实询问留存（需 X-Agent-Debug-Key）",
)
async def export_web_query_intake(
    session_id: str | None = Query(default=None, alias="sessionId", max_length=64),
    limit: int = Query(default=1000, ge=1, le=10000),
    x_agent_debug_key: str | None = Header(default=None, alias=TRACE_DEBUG_HEADER),
) -> dict[str, Any]:
    _require_query_intake_debug_access(x_agent_debug_key)
    store = get_web_query_intake_store()
    records = await asyncio.to_thread(
        store.list_records,
        session_id=session_id,
        limit=limit,
    )
    return {
        "captureHealth": capture_health(),
        "store": await asyncio.to_thread(store.stats),
        "records": [
            record.model_dump(by_alias=True, mode="json") for record in records
        ],
    }


@app.delete(
    "/internal/debug/web-query-intake/sessions/{session_id}",
    tags=["系统接口"],
    summary="删除指定会话的网页询问留存（需 X-Agent-Debug-Key）",
)
async def delete_web_query_intake_session(
    session_id: str,
    x_agent_debug_key: str | None = Header(default=None, alias=TRACE_DEBUG_HEADER),
) -> dict[str, Any]:
    _require_query_intake_debug_access(x_agent_debug_key)
    deleted = await asyncio.to_thread(
        get_web_query_intake_store().delete_session,
        session_id,
    )
    return {"sessionId": session_id, "deletedRecords": deleted}
