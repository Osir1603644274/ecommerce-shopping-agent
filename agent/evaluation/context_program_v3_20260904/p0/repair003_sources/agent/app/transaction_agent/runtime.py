"""Server-only dispatcher for the bounded ShoppingAgent -> TransactionAgent handoff."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

import httpx

from ..domains.ecommerce.transactions import (
    TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
    TransactionAction,
    TransactionContext,
    _bind_authenticated_transaction_context,
    execute_confirmed_transaction,
    preview_order_tool,
    preview_payment_tool,
    query_order_status_tool,
)
from ..schemas import ToolTrace
from ..settings import settings
from .capabilities import _issue_transaction_capability


@dataclass(frozen=True, slots=True)
class TransactionRequestContext:
    authorization: str | None = field(repr=False)
    session_id: str
    task_id: str | None
    task_revision: int | None
    candidate_scope_id: str | None
    user_message: str


@dataclass(frozen=True, slots=True)
class _AuthenticatedOwner:
    user_id: str
    roles: tuple[str, ...]
    access_token: str = field(repr=False)


class _AuthenticationFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.safe_message = message


_REQUEST: ContextVar[TransactionRequestContext | None] = ContextVar(
    "transaction_agent_request", default=None
)


@contextmanager
def bind_transaction_request(
    *,
    authorization: str | None,
    session_id: str | None,
    task_id: str | None,
    task_revision: int | None,
    candidate_scope_id: str | None,
    user_message: str,
) -> Iterator[TransactionRequestContext | None]:
    """Carry untrusted request material without granting transaction authority."""

    request = None
    if session_id:
        request = TransactionRequestContext(
            authorization=authorization,
            session_id=session_id,
            task_id=task_id,
            task_revision=task_revision,
            candidate_scope_id=candidate_scope_id,
            user_message=user_message,
        )
    token = _REQUEST.set(request)
    try:
        yield request
    finally:
        _REQUEST.reset(token)


def _bearer(authorization: str | None) -> str:
    if authorization is None or not authorization.startswith("Bearer "):
        raise _AuthenticationFailure(
            "transaction_authentication_required", "交易操作需要登录。"
        )
    token = authorization[7:]
    if not token or token != token.strip() or len(token) > 8192:
        raise _AuthenticationFailure(
            "transaction_authentication_required", "交易操作需要有效登录。"
        )
    return token


async def _authenticate(authorization: str | None) -> _AuthenticatedOwner:
    token = _bearer(authorization)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{settings.backend_base_url}/api/identity/me",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise _AuthenticationFailure(
            TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
            "交易身份服务暂时不可用。",
        ) from exc
    if response.status_code in {401, 403}:
        raise _AuthenticationFailure(
            "transaction_auth_rejected", "交易身份验证失败。"
        )
    if response.is_error:
        raise _AuthenticationFailure(
            TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
            "交易身份服务暂时不可用。",
        )
    try:
        payload = response.json()
        data = payload["data"]
        user_id = data["userId"]
        roles = data["roles"]
    except (ValueError, KeyError, TypeError) as exc:
        raise _AuthenticationFailure(
            TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
            "交易身份服务返回无效响应。",
        ) from exc
    if (
        type(user_id) is not str
        or not user_id.strip()
        or user_id != user_id.strip()
        or type(roles) is not list
        or any(type(role) is not str or not role for role in roles)
    ):
        raise _AuthenticationFailure(
            TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
            "交易身份服务返回无效身份。",
        )
    return _AuthenticatedOwner(user_id=user_id, roles=tuple(sorted(roles)), access_token=token)


def _error(tool: str, code: str, message: str) -> ToolTrace:
    return ToolTrace(tool=tool, ok=False, detail={"code": code, "message": message})


async def _dispatch(tool: str, operation, *, require_transaction_enabled: bool = True):
    request = _REQUEST.get()
    if request is None:
        return _error(
            tool,
            TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
            "缺少服务端交易交接上下文，未执行交易。",
        )
    if require_transaction_enabled and (
        request.task_id is None
        or request.task_revision is None
        or request.candidate_scope_id is None
    ):
        return _error(
            tool,
            TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
            "缺少服务端交易任务绑定，未执行交易。",
        )
    if require_transaction_enabled and not settings.agent_transaction_enabled:
        return _error(
            tool,
            "transaction_agent_disabled",
            "TransactionAgent 当前未启用，未执行交易。",
        )
    try:
        owner = await _authenticate(request.authorization)
    except _AuthenticationFailure as exc:
        return _error(tool, exc.code, exc.safe_message)
    context = TransactionContext(
        access_token=owner.access_token,
        owner_user_id=owner.user_id,
        session_id=request.session_id,
        task_id=request.task_id,
        task_revision=request.task_revision,
        candidate_scope_id=request.candidate_scope_id,
        user_message=request.user_message,
    )
    capability = _issue_transaction_capability()
    with _bind_authenticated_transaction_context(context, capability):
        return await operation()


async def dispatch_order_preview(product_id: int, quantity: int, user_coupon_id: str | None) -> ToolTrace:
    return await _dispatch(
        "preview_order",
        lambda: preview_order_tool(product_id, quantity, user_coupon_id),
    )


async def dispatch_confirmed_handoff(action: TransactionAction) -> ToolTrace:
    return await _dispatch(
        action,
        lambda: execute_confirmed_transaction(action),
    )


async def dispatch_payment_preview(order_id: str) -> ToolTrace:
    return await _dispatch(
        "preview_payment",
        lambda: preview_payment_tool(order_id),
    )


async def dispatch_order_status(order_reference: str) -> ToolTrace:
    return await _dispatch(
        "query_order_status",
        lambda: query_order_status_tool(order_reference),
        require_transaction_enabled=False,
    )


__all__ = [
    "bind_transaction_request",
    "dispatch_confirmed_handoff",
    "dispatch_order_preview",
    "dispatch_payment_preview",
    "dispatch_order_status",
]
