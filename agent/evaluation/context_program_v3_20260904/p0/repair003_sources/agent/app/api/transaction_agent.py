from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..domains.ecommerce.models import CandidateScope
from ..task_state import get_session_task_state
from .commerce_demo import optional_browser_authorization
from ..transaction_agent.runtime import (
    bind_transaction_request,
    dispatch_order_preview,
    dispatch_payment_preview,
)


router = APIRouter(prefix="/transaction-agent", tags=["交易履约 Agent"])


class OrderPreviewHandoffRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    session_id: str = Field(alias="sessionId", min_length=1, max_length=64)
    product_id: int = Field(alias="productId", gt=0)
    quantity: int = Field(gt=0, le=20)
    user_coupon_id: str | None = Field(default=None, alias="userCouponId", max_length=64)


class PaymentPreviewHandoffRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    session_id: str = Field(alias="sessionId", min_length=1, max_length=64)
    order_id: str = Field(alias="orderId", min_length=1, max_length=80)


def _effective_authorization(explicit: str | None, browser: object) -> str | None:
    return explicit if isinstance(explicit, str) and explicit else (
        browser if isinstance(browser, str) and browser else None
    )


async def _active_scope(session_id: str) -> tuple[object, CandidateScope]:
    state = await get_session_task_state(session_id)
    if state is None or state.session_id != session_id or state.task_type != "ecommerce_guide":
        raise HTTPException(status_code=409, detail="当前导购任务不存在或已过期")
    try:
        scope = CandidateScope.model_validate(state.domain_state.get("candidateScope"))
    except Exception as exc:
        raise HTTPException(status_code=409, detail="当前任务没有可交易候选范围") from exc
    if scope.status != "active" or scope.task_id != state.task_id:
        raise HTTPException(status_code=409, detail="当前候选范围已失效")
    return state, scope


def _raise_for_trace(trace) -> None:
    detail = trace.detail if isinstance(trace.detail, dict) else {}
    code = detail.get("code")
    status = 401 if code in {"transaction_authentication_required", "transaction_auth_rejected"} else 409
    if code in {
        "transaction_auth_provider_unavailable",
        "transaction_backend_unavailable",
        "confirmation_store_unavailable",
        "transaction_agent_disabled",
    }:
        status = 503
    raise HTTPException(status_code=status, detail=detail.get("message", "交易预览失败"))


@router.post("/orders/preview")
async def preview_order_handoff(
    request: OrderPreviewHandoffRequest,
    authorization: str | None = Header(default=None),
    browser_authorization: str | None = Depends(optional_browser_authorization),
) -> dict:
    state, scope = await _active_scope(request.session_id)
    if request.product_id not in scope.visible_product_ids:
        raise HTTPException(status_code=409, detail="商品不在当前服务端候选范围内")
    with bind_transaction_request(
        authorization=_effective_authorization(authorization, browser_authorization),
        session_id=request.session_id,
        task_id=state.task_id,
        task_revision=state.revision,
        candidate_scope_id=scope.scope_id,
        user_message="订单预览",
    ):
        trace = await dispatch_order_preview(
            request.product_id, request.quantity, request.user_coupon_id
        )
    if trace.ok:
        return {
            "success": True,
            "data": trace.detail,
            "message": "ok",
            "executionPath": [
                {"stage": "CandidateScope", "status": "visible_product_verified"},
                {"stage": "TransactionAgent", "status": "jwt_owner_verified"},
                {"stage": "Java OrderController", "status": "preview_returned"},
                {"stage": "Redis confirmation proposal", "status": "stored"},
            ],
        }
    _raise_for_trace(trace)


@router.post("/payments/preview")
async def preview_payment_handoff(
    request: PaymentPreviewHandoffRequest,
    authorization: str | None = Header(default=None),
    browser_authorization: str | None = Depends(optional_browser_authorization),
) -> dict:
    state, scope = await _active_scope(request.session_id)
    with bind_transaction_request(
        authorization=_effective_authorization(authorization, browser_authorization),
        session_id=request.session_id,
        task_id=state.task_id,
        task_revision=state.revision,
        candidate_scope_id=scope.scope_id,
        user_message="支付预览",
    ):
        trace = await dispatch_payment_preview(request.order_id)
    if trace.ok:
        return {
            "success": True,
            "data": trace.detail,
            "message": "ok",
            "executionPath": [
                {"stage": "TransactionAgent", "status": "jwt_owner_verified"},
                {"stage": "Java OrderController", "status": "pending_payment_verified"},
                {"stage": "Redis confirmation proposal", "status": "stored"},
            ],
        }
    _raise_for_trace(trace)
