from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator, Literal
from urllib.parse import quote

import httpx
import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError
from redis.exceptions import RedisError

from ...schemas import ToolTrace
from ...settings import settings
from ...transaction_agent.capabilities import (
    _CapabilityDispatcher,
    _bind_transaction_capability,
    _current_transaction_capability,
)
logger = logging.getLogger(__name__)

# A Python bearer/header parser is not an authenticated identity provider.
# Transaction Agent writes remain deliberately unavailable until a trusted
# provider is wired in a later batch.
TRANSACTION_AUTH_PROVIDER_UNAVAILABLE = "transaction_auth_provider_unavailable"

TransactionAction = Literal["create_order", "cancel_order", "create_payment"]
_CONFIRMATION_PHRASES: dict[TransactionAction, frozenset[str]] = {
    "create_order": frozenset({"确认下单", "我确认下单", "确认创建订单"}),
    "cancel_order": frozenset({"确认取消订单", "我确认取消订单"}),
    "create_payment": frozenset({"确认发起支付", "我确认发起支付", "确认支付"}),
}
_ACTION_TOOL: dict[TransactionAction, str] = {
    "create_order": "create_order",
    "cancel_order": "cancel_order",
    "create_payment": "create_payment",
}
_ACTION_LABEL: dict[TransactionAction, str] = {
    "create_order": "创建订单",
    "cancel_order": "取消订单",
    "create_payment": "发起支付",
}
_TRAILING_PUNCTUATION = re.compile(r"[。！!]+$")
_ORDER_UUID = re.compile(
    r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}(?![0-9A-Fa-f])"
)
_ORDER_NUMBER = re.compile(r"(?<![A-Za-z0-9])O\d{14}[A-Za-z0-9]{12}(?![A-Za-z0-9])", re.I)
_ORDER_STATUS_CUES = ("状态", "进度", "到哪", "怎么样", "查订单", "查询订单", "查看订单")


@dataclass(frozen=True, slots=True)
class TransactionContext:
    """Per-request authority that is never placed in prompts or tool arguments."""

    access_token: str = field(repr=False)
    owner_user_id: str
    session_id: str
    task_id: str | None
    task_revision: int | None
    candidate_scope_id: str | None
    user_message: str

    @property
    def credential_fingerprint(self) -> str:
        return hashlib.sha256(self.access_token.encode("utf-8")).hexdigest()


_transaction_context: ContextVar[TransactionContext | None] = ContextVar(
    "transaction_context",
    default=None,
)
_redis_client: redis.Redis | None = None


class TransactionProposal(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    version: int = 1
    confirmation_id: str = Field(alias="confirmationId")
    action: TransactionAction
    owner_user_id: str = Field(alias="ownerUserId")
    session_id: str = Field(alias="sessionId")
    task_id: str | None = Field(default=None, alias="taskId")
    task_revision: int | None = Field(default=None, alias="taskRevision")
    candidate_scope_id: str | None = Field(default=None, alias="candidateScopeId")
    credential_fingerprint: str = Field(alias="credentialFingerprint")
    payload: dict[str, Any]
    preview: dict[str, Any]
    idempotency_key: str | None = Field(default=None, alias="idempotencyKey")
    command_digest: str = Field(alias="commandDigest")
    created_at: datetime = Field(alias="createdAt")
    expires_at: datetime = Field(alias="expiresAt")
    confirmed_at: datetime | None = Field(default=None, alias="confirmedAt")
    execution_expires_at: datetime | None = Field(
        default=None, alias="executionExpiresAt"
    )


class ConfirmationStoreUnavailable(RuntimeError):
    pass


class TransactionOutcomeUnknown(RuntimeError):
    """The write may have committed but its authoritative query is unavailable."""

    pass


@contextmanager
def bind_transaction_context(
    *,
    authorization: str | None,
    session_id: str | None,
    task_id: str | None,
    user_message: str,
) -> Iterator[TransactionContext | None]:
    """Compatibility scope that never authenticates raw request values.

    ``authorization``, ``session_id`` and ``task_id`` are intentionally ignored:
    none can mint transaction authority.  Keeping this no-op context manager
    avoids changing chat call sites while guaranteeing fail-closed tools.
    """

    context: TransactionContext | None = None
    reset_token = _transaction_context.set(context)
    try:
        yield context
    finally:
        _transaction_context.reset(reset_token)


@contextmanager
def _bind_authenticated_transaction_context(
    context: TransactionContext,
    capability: object,
) -> Iterator[TransactionContext]:
    """Bind a backend-authenticated owner under a private Transaction capability."""

    with _bind_transaction_capability(capability):
        reset_token = _transaction_context.set(context)
        try:
            yield context
        finally:
            _transaction_context.reset(reset_token)


def explicit_confirmation_action(message: str) -> TransactionAction | None:
    normalized = _TRAILING_PUNCTUATION.sub("", " ".join(message.strip().split()))
    for action, phrases in _CONFIRMATION_PHRASES.items():
        if normalized in phrases:
            return action
    return None


def is_order_status_query(message: str) -> bool:
    """Recognise an explicit read-only order-status request.

    Identity and ownership are deliberately not inferred here; the authenticated
    Java endpoint remains authoritative for both.
    """

    normalized = " ".join(message.strip().split())
    return "订单" in normalized and any(cue in normalized for cue in _ORDER_STATUS_CUES)


def extract_order_reference(message: str) -> str | None:
    """Return a strict UUID/order number from an order-status request."""

    for pattern in (_ORDER_UUID, _ORDER_NUMBER):
        match = pattern.search(message)
        if match is not None:
            return match.group(0)
    return None


def _get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def _session_digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def _proposal_key(context: TransactionContext, action: TransactionAction) -> str:
    task_digest = (
        hashlib.sha256(context.task_id.encode("utf-8")).hexdigest()[:24]
        if context.task_id
        else "missing-task"
    )
    return (
        f"agent:transaction:proposal:{_session_digest(context.session_id)}:"
        f"{task_digest}:{context.credential_fingerprint[:24]}:{action}"
    )


def _proposal_index_key(session_id: str) -> str:
    return f"agent:transaction:index:{_session_digest(session_id)}"


def _now() -> datetime:
    return datetime.now(UTC)


def _proposal_command_digest(proposal: TransactionProposal) -> str:
    payload = proposal.model_dump(
        by_alias=True,
        mode="json",
        exclude={"command_digest", "confirmed_at", "execution_expires_at"},
    )
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _finish(start: float, trace: ToolTrace) -> ToolTrace:
    return trace.model_copy(
        update={"duration_ms": round((time.perf_counter() - start) * 1000, 2)}
    )


def _error(tool: str, code: str, message: str) -> ToolTrace:
    return ToolTrace(
        tool=tool,
        ok=False,
        detail={"code": code, "message": message},
    )


def _require_context(
    tool: str,
    *,
    require_transaction_enabled: bool = True,
) -> TransactionContext | ToolTrace:
    capability = _current_transaction_capability()
    context = _transaction_context.get()
    if (
        context is None
        or capability is None
        or not _CapabilityDispatcher.allowed(tool, capability)
    ):
        # This must stay before every feature/context/Redis/backend branch.  A
        # raw Authorization header is not proof of a server-authenticated owner.
        return _error(
            tool,
            TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
            "交易操作需要可信认证 provider；未执行预览或写入。",
        )
    if require_transaction_enabled and not settings.agent_transaction_enabled:
        return _error(
            tool,
            "transaction_agent_disabled",
            "TransactionAgent 当前未启用，未执行预览或写入。",
        )
    return context


async def _save_proposal(
    context: TransactionContext,
    *,
    action: TransactionAction,
    payload: dict[str, Any],
    preview: dict[str, Any],
    idempotency_key: str | None = None,
    expires_at: datetime | None = None,
) -> TransactionProposal:
    now = _now()
    proposal = TransactionProposal(
        confirmationId=f"cfm-{secrets.token_urlsafe(12)}",
        action=action,
        ownerUserId=context.owner_user_id,
        sessionId=context.session_id,
        taskId=context.task_id,
        taskRevision=context.task_revision,
        candidateScopeId=context.candidate_scope_id,
        credentialFingerprint=context.credential_fingerprint,
        payload=payload,
        preview=preview,
        idempotencyKey=idempotency_key,
        commandDigest="0" * 64,
        createdAt=now,
        expiresAt=expires_at
        or now + timedelta(seconds=settings.transaction_confirmation_ttl_seconds),
    )
    proposal = proposal.model_copy(
        update={"command_digest": _proposal_command_digest(proposal)}
    )
    ttl = max(1, int((proposal.expires_at - now).total_seconds()))
    client = _get_redis_client()
    key = _proposal_key(context, action)
    index_key = _proposal_index_key(context.session_id)
    await client.set(key, proposal.model_dump_json(by_alias=True))
    await client.expire(key, ttl)
    await client.sadd(index_key, key)
    await client.expire(
        index_key,
        max(1, settings.transaction_confirmation_ttl_seconds),
    )
    return proposal


async def _consume_proposal(
    context: TransactionContext,
    action: TransactionAction,
) -> TransactionProposal | None:
    """Durably confirm, but do not delete, the immutable command.

    Deletion before the backend call creates the classic effect-before-receipt
    crash window.  The confirmed proposal therefore remains the recovery
    command until a terminal result is observed.  Replays cross the backend
    boundary only with the same idempotency/natural business key.
    """

    client = _get_redis_client()
    key = _proposal_key(context, action)
    raw = await client.get(key)
    if raw is None:
        return None
    try:
        proposal = TransactionProposal.model_validate_json(raw)
    except ValidationError:
        logger.error("Discarded malformed transaction proposal")
        return None
    if (
        proposal.owner_user_id != context.owner_user_id
        or proposal.session_id != context.session_id
        or proposal.task_id != context.task_id
        or proposal.task_revision != context.task_revision
        or proposal.candidate_scope_id != context.candidate_scope_id
        or proposal.credential_fingerprint != context.credential_fingerprint
        or proposal.action != action
        or proposal.command_digest != _proposal_command_digest(proposal)
    ):
        return None
    now = _now()
    if proposal.confirmed_at is None:
        if proposal.expires_at <= now:
            return None
        confirmed = proposal.model_copy(
            update={
                "confirmed_at": now,
                "execution_expires_at": now
                + timedelta(seconds=settings.transaction_execution_ttl_seconds),
            }
        )
        confirmed_raw = confirmed.model_dump_json(by_alias=True)
        ttl = max(
            1,
            int((confirmed.execution_expires_at - now).total_seconds()),
        )
        replaced = await client.eval(
            _CONFIRM_PROPOSAL_LUA,
            1,
            key,
            raw,
            confirmed_raw,
            str(ttl),
        )
        if isinstance(replaced, bytes):
            replaced = replaced.decode("utf-8")
        if not isinstance(replaced, str) or not replaced:
            return None
        try:
            proposal = TransactionProposal.model_validate_json(replaced)
        except ValidationError:
            return None
    if (
        proposal.confirmed_at is None
        or proposal.confirmed_at > proposal.expires_at
        or proposal.execution_expires_at is None
        or proposal.execution_expires_at <= now
    ):
        return None
    return proposal


async def _delete_proposal_if_same(
    context: TransactionContext,
    proposal: TransactionProposal,
) -> None:
    """Delete only the command that produced the terminal result."""

    client = _get_redis_client()
    key = _proposal_key(context, proposal.action)
    await client.eval(
        _DELETE_PROPOSAL_LUA,
        2,
        key,
        _proposal_index_key(context.session_id),
        proposal.model_dump_json(by_alias=True),
    )


async def clear_transaction_confirmations(session_id: str) -> None:
    """Revoke every pending write when a chat session is cleared."""

    if not settings.agent_transaction_enabled:
        return
    try:
        client = _get_redis_client()
        index_key = _proposal_index_key(session_id)
        proposal_keys = list(await client.smembers(index_key))
        if proposal_keys:
            await client.delete(*proposal_keys)
        await client.delete(index_key)
    except RedisError as exc:
        raise ConfirmationStoreUnavailable(
            "transaction confirmation store is unavailable"
        ) from exc


def _backend_headers(context: TransactionContext) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {context.access_token}",
        "X-Request-Id": f"agent-{secrets.token_hex(8)}",
    }


def _safe_backend_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "交易后端返回了无法解析的错误。"
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str) or not message.strip():
        return "交易后端拒绝了本次请求。"
    return message.strip()[:300]


async def _request_backend(
    context: TransactionContext,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    headers = _backend_headers(context)
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.request(
            method,
            f"{settings.backend_base_url}{path}",
            headers=headers,
            json=json_body,
        )
    if response.is_error:
        raise BackendTransactionError(
            response.status_code,
            _safe_backend_message(response),
        )
    payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise BackendTransactionError(
            502,
            "交易后端响应缺少结构化 data。",
        )
    return data


class BackendTransactionError(RuntimeError):
    def __init__(self, status_code: int, safe_message: str):
        super().__init__(safe_message)
        self.status_code = status_code
        self.safe_message = safe_message

    @property
    def retryable(self) -> bool:
        return self.status_code >= 500


async def query_order_status_tool(order_reference: str) -> ToolTrace:
    """Read one authenticated owner's order from the Java authority."""

    start = time.perf_counter()
    tool = "query_order_status"
    required = _require_context(tool, require_transaction_enabled=False)
    if isinstance(required, ToolTrace):
        return _finish(start, required)
    reference = order_reference.strip()
    if not (_ORDER_UUID.fullmatch(reference) or _ORDER_NUMBER.fullmatch(reference)):
        return _finish(
            start,
            _error(tool, "invalid_order_reference", "请提供完整的订单 ID 或订单号。"),
        )
    try:
        order = await _request_backend(required, "GET", f"/api/orders/{reference}")
        return _finish(
            start,
            ToolTrace(tool=tool, ok=True, detail={"order": order}),
        )
    except BackendTransactionError as exc:
        if exc.status_code in {403, 404}:
            return _finish(
                start,
                _error(
                    tool,
                    "order_not_found_or_forbidden",
                    "订单不存在或你无权查看该订单。",
                ),
            )
        if exc.status_code in {401}:
            return _finish(
                start,
                _error(tool, "transaction_auth_rejected", "交易身份验证失败。"),
            )
        return _finish(
            start,
            _error(tool, "transaction_backend_unavailable", exc.safe_message),
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Order status unavailable: %s", type(exc).__name__)
        return _finish(
            start,
            _error(tool, "transaction_backend_unavailable", "交易后端暂时不可用。"),
        )


def _confirmation_detail(
    proposal: TransactionProposal,
    *,
    phrase: str,
) -> dict[str, Any]:
    return {
        "status": "confirmation_required",
        "action": proposal.action,
        "confirmationId": proposal.confirmation_id,
        "expiresAt": proposal.expires_at.isoformat(),
        "preview": proposal.preview,
        "confirmationPhrase": phrase,
        "message": (
            f"请核对预览；只有在下一条消息明确回复“{phrase}”后，"
            f"才会{_ACTION_LABEL[proposal.action]}。"
        ),
    }


async def preview_order_tool(
    product_id: int,
    quantity: int,
    user_coupon_id: str | None = None,
) -> ToolTrace:
    start = time.perf_counter()
    tool = "preview_order"
    required = _require_context(tool)
    if isinstance(required, ToolTrace):
        return _finish(start, required)
    context = required
    payload = {
        "itemType": "PRODUCT",
        "itemId": product_id,
        "quantity": quantity,
        "userCouponId": user_coupon_id,
    }
    try:
        preview = await _request_backend(
            context,
            "POST",
            "/api/orders/preview",
            json_body=payload,
        )
        if (
            preview.get("itemType") != "PRODUCT"
            or preview.get("itemId") != product_id
            or preview.get("quantity") != quantity
        ):
            raise BackendTransactionError(502, "订单预览与请求参数不一致。")
        proposal = await _save_proposal(
            context,
            action="create_order",
            payload=payload,
            preview=preview,
            idempotency_key=f"agent-{secrets.token_urlsafe(24)}",
        )
        return _finish(
            start,
            ToolTrace(
                tool=tool,
                ok=True,
                detail=_confirmation_detail(proposal, phrase="确认下单"),
            ),
        )
    except BackendTransactionError as exc:
        return _finish(
            start,
            _error(tool, "order_preview_rejected", exc.safe_message),
        )
    except RedisError:
        logger.exception("Transaction confirmation store unavailable")
        return _finish(
            start,
            _error(
                tool,
                "confirmation_store_unavailable",
                "交易确认存储暂时不可用，未创建订单预览。",
            ),
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("Order preview unavailable: %s", type(exc).__name__)
        return _finish(
            start,
            _error(tool, "transaction_backend_unavailable", "交易后端暂时不可用。"),
        )


async def preview_cancel_order_tool(order_id: str) -> ToolTrace:
    return await _preview_existing_order(
        tool="preview_cancel_order",
        order_id=order_id,
        action="cancel_order",
        phrase="确认取消订单",
    )


async def preview_payment_tool(order_id: str) -> ToolTrace:
    return await _preview_existing_order(
        tool="preview_payment",
        order_id=order_id,
        action="create_payment",
        phrase="确认发起支付",
    )


async def _preview_existing_order(
    *,
    tool: str,
    order_id: str,
    action: TransactionAction,
    phrase: str,
) -> ToolTrace:
    start = time.perf_counter()
    required = _require_context(tool)
    if isinstance(required, ToolTrace):
        return _finish(start, required)
    context = required
    try:
        order = await _request_backend(context, "GET", f"/api/orders/{order_id}")
        if order.get("status") != "PENDING_PAYMENT":
            return _finish(
                start,
                _error(
                    tool,
                    "order_state_not_actionable",
                    "只有待支付订单可以执行该操作。",
                ),
            )
        proposal = await _save_proposal(
            context,
            action=action,
            payload={"orderId": order_id},
            preview={"order": order},
        )
        return _finish(
            start,
            ToolTrace(
                tool=tool,
                ok=True,
                detail=_confirmation_detail(proposal, phrase=phrase),
            ),
        )
    except BackendTransactionError as exc:
        return _finish(
            start,
            _error(tool, "order_preview_rejected", exc.safe_message),
        )
    except RedisError:
        logger.exception("Transaction confirmation store unavailable")
        return _finish(
            start,
            _error(
                tool,
                "confirmation_store_unavailable",
                "交易确认存储暂时不可用，未生成操作预览。",
            ),
        )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("%s unavailable: %s", tool, type(exc).__name__)
        return _finish(
            start,
            _error(tool, "transaction_backend_unavailable", "交易后端暂时不可用。"),
        )


async def execute_confirmed_transaction(
    expected_action: TransactionAction | None = None,
) -> ToolTrace:
    start = time.perf_counter()
    tool = _ACTION_TOOL.get(expected_action or "create_order", "create_order")
    required = _require_context(tool)
    if isinstance(required, ToolTrace):
        return _finish(start, required)
    context = required
    action = explicit_confirmation_action(context.user_message)
    if action is None or action != expected_action:
        return _finish(start, _error(
            tool,
            "exact_confirmation_required",
            "确认短语与待执行交易不一致，未执行写入。",
        ))
    try:
        proposal = await _consume_proposal(context, action)
        if proposal is None:
            return _finish(start, _error(
                tool,
                "confirmation_missing_or_expired",
                "交易预览不存在、已过期或与当前任务不匹配，未执行写入。",
            ))
        try:
            result = await _execute_with_reconciliation(context, proposal)
        except TransactionOutcomeUnknown:
            return _finish(start, _error(
                tool,
                "transaction_outcome_unknown",
                "交易结果暂时无法确认；已保留原命令和幂等键，恢复时只会对账或安全重放。",
            ))
        except BackendTransactionError as exc:
            await _delete_proposal_if_same(context, proposal)
            return _finish(start, _error(
                tool,
                "transaction_rejected",
                exc.safe_message,
            ))
        await _delete_proposal_if_same(context, proposal)
        return _finish(start, ToolTrace(
            tool=tool,
            ok=True,
            detail={
                "action": action,
                "confirmationId": proposal.confirmation_id,
                "result": result,
            },
        ))
    except RedisError:
        logger.exception("Transaction confirmation store unavailable")
        return _finish(start, _error(
            tool,
            "confirmation_store_unavailable",
            "交易确认存储暂时不可用，未执行写入。",
        ))
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("%s unavailable: %s", tool, type(exc).__name__)
        return _finish(start, _error(
            tool,
            "transaction_backend_unavailable",
            "交易后端暂时不可用。",
        ))


async def _execute_proposal(
    context: TransactionContext,
    proposal: TransactionProposal,
) -> dict[str, Any]:
    if proposal.action == "create_order":
        return await _request_backend(
            context,
            "POST",
            "/api/orders",
            json_body=proposal.payload,
            idempotency_key=proposal.idempotency_key,
        )
    order_id = proposal.payload["orderId"]
    if proposal.action == "cancel_order":
        return await _request_backend(
            context,
            "POST",
            f"/api/orders/{order_id}/cancel",
        )
    return await _request_backend(
        context,
        "POST",
        f"/api/payments/orders/{order_id}",
    )


def _ambiguous_backend_failure(exc: BaseException) -> bool:
    if isinstance(exc, BackendTransactionError):
        return exc.retryable
    return isinstance(exc, (httpx.HTTPError, ValueError, TypeError))


async def _query_proposal_effect(
    context: TransactionContext,
    proposal: TransactionProposal,
) -> dict[str, Any] | None:
    """Query the Java authority using the command's stable business key."""

    try:
        if proposal.action == "create_order":
            if not proposal.idempotency_key:
                raise TransactionOutcomeUnknown("order command has no idempotency key")
            return await _request_backend(
                context,
                "GET",
                "/api/orders/by-idempotency-key/"
                + quote(proposal.idempotency_key, safe=""),
            )
        order_id = str(proposal.payload["orderId"])
        if proposal.action == "create_payment":
            return await _request_backend(
                context,
                "GET",
                f"/api/payments/orders/{quote(order_id, safe='')}",
            )
        order = await _request_backend(
            context,
            "GET",
            f"/api/orders/{quote(order_id, safe='')}",
        )
        status = order.get("status")
        if status == "CANCELLED":
            return order
        if status == "PENDING_PAYMENT":
            return None
        raise BackendTransactionError(409, "订单当前状态不允许取消。")
    except BackendTransactionError as exc:
        if exc.status_code == 404 and proposal.action in {
            "create_order",
            "create_payment",
        }:
            return None
        raise


async def _execute_with_reconciliation(
    context: TransactionContext,
    proposal: TransactionProposal,
) -> dict[str, Any]:
    """Execute once, reconcile, then make at most one key-stable replay.

    Every supported write is either idempotency-keyed (order creation) or has
    a natural unique business key (order cancellation/payment creation).  A
    second backend crossing therefore cannot create an additional effect.
    """

    first_error: Exception | None = None
    try:
        return await _execute_proposal(context, proposal)
    except Exception as exc:
        if not _ambiguous_backend_failure(exc):
            raise
        first_error = exc

    assert first_error is not None
    last_ambiguous: Exception = first_error
    for attempt in range(2):
        try:
            observed = await _query_proposal_effect(context, proposal)
        except Exception as query_error:
            if not _ambiguous_backend_failure(query_error):
                raise
            last_ambiguous = query_error
            observed = None
        if observed is not None:
            return observed
        if attempt == 1:
            break
        try:
            return await _execute_proposal(context, proposal)
        except Exception as replay_error:
            if not _ambiguous_backend_failure(replay_error):
                # A definitive replay response still gets one final authority
                # query because the first attempt may already have committed.
                try:
                    observed = await _query_proposal_effect(context, proposal)
                except BaseException:
                    observed = None
                if observed is not None:
                    return observed
                raise
            last_ambiguous = replay_error
    raise TransactionOutcomeUnknown(type(last_ambiguous).__name__)


async def create_order_tool() -> ToolTrace:
    return await execute_confirmed_transaction("create_order")


async def cancel_order_tool() -> ToolTrace:
    return await execute_confirmed_transaction("cancel_order")


async def create_payment_tool() -> ToolTrace:
    return await execute_confirmed_transaction("create_payment")


def render_transaction_result(trace: ToolTrace) -> str:
    if not isinstance(trace.detail, dict):
        return "交易操作未完成。"
    if not trace.ok:
        return str(trace.detail.get("message", "交易操作未完成。"))
    result = trace.detail.get("result", {})
    action = trace.detail.get("action")
    if action == "create_order":
        return (
            f"订单已创建：订单号 {result.get('orderNo', '未知')}，"
            f"应付 {result.get('payableMinor', 0) / 100:.2f} 元，"
            f"状态 {result.get('status', '未知')}。"
        )
    if action == "cancel_order":
        return (
            f"订单 {result.get('orderNo', result.get('id', ''))} 已取消，"
            "库存和未使用优惠券将按后端事务规则释放。"
        )
    return (
        f"支付单已创建：支付号 {result.get('paymentNo', '未知')}，"
        f"状态 {result.get('status', '未知')}。"
        "本操作仅发起支付，不会模拟支付成功。"
    )


def render_order_status_result(trace: ToolTrace) -> str:
    if not isinstance(trace.detail, dict):
        return "订单状态查询失败。"
    if not trace.ok:
        return str(trace.detail.get("message", "订单状态查询失败。"))
    order = trace.detail.get("order")
    if not isinstance(order, dict):
        return "交易后端未返回有效订单状态。"
    status = str(order.get("status", "UNKNOWN"))
    status_labels = {
        "PENDING_PAYMENT": "待支付",
        "PAID": "已支付",
        "COMPLETED": "已完成",
        "CANCELLED": "已取消",
        "EXPIRED": "已超时关闭",
        "REFUNDING": "退款中",
        "REFUNDED": "已退款",
    }
    label = status_labels.get(status, status)
    return (
        f"订单 {order.get('orderNo', order.get('id', '未知'))} 当前状态：{label}；"
        f"应付 {order.get('payableMinor', 0) / 100:.2f} 元。"
    )


_CONFIRM_PROPOSAL_LUA = r'''
local current=redis.call('get',KEYS[1])
if not current then return '' end
if current~=ARGV[1] then return current end
redis.call('set',KEYS[1],ARGV[2],'EX',ARGV[3])
return ARGV[2]
'''

_DELETE_PROPOSAL_LUA = r'''
local current=redis.call('get',KEYS[1])
if not current or current~=ARGV[1] then return 0 end
redis.call('del',KEYS[1])
redis.call('srem',KEYS[2],KEYS[1])
return 1
'''


__all__ = [
    "TransactionContext",
    "TRANSACTION_AUTH_PROVIDER_UNAVAILABLE",
    "ConfirmationStoreUnavailable",
    "TransactionOutcomeUnknown",
    "bind_transaction_context",
    "cancel_order_tool",
    "clear_transaction_confirmations",
    "create_order_tool",
    "create_payment_tool",
    "execute_confirmed_transaction",
    "extract_order_reference",
    "explicit_confirmation_action",
    "is_order_status_query",
    "preview_cancel_order_tool",
    "preview_order_tool",
    "preview_payment_tool",
    "query_order_status_tool",
    "render_order_status_result",
    "render_transaction_result",
    "_bind_authenticated_transaction_context",
]
