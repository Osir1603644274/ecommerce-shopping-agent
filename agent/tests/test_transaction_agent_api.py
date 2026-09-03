import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api import transaction_agent as api
from app.api.transaction_agent import OrderPreviewHandoffRequest, PaymentPreviewHandoffRequest
from app.domains.ecommerce.models import CandidateScope
from app.schemas import ToolTrace
from app.task_state import TaskState
from app.transaction_agent import runtime


def _state() -> TaskState:
    scope = CandidateScope(
        scopeId="scope-1",
        taskId="task-1",
        sourceRevision=3,
        sourcePlanId="plan-1",
        sourceStepId="step-1",
        category="phone",
        candidatePoolIds=[1001, 1002],
        rankedItemIds=[1001, 1002],
        visibleProductIds=[1001],
        requirementsSnapshot=[],
        brandAvoidancesSnapshot=[],
        evidenceRefs=["evidence-1"],
        createdAt="2026-08-31T00:00:00Z",
    )
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-1",
        taskType="ecommerce_guide",
        sessionId="session-1",
        status="ready",
        revision=7,
        goal="购买当前候选手机",
        domainState={"candidateScope": scope.model_dump(by_alias=True, mode="json")},
        createdAt=now,
        updatedAt=now,
    )


def test_preview_handoff_accepts_only_server_visible_candidate(monkeypatch):
    async def load(_session_id):
        return _state()

    async def dispatch(product_id, quantity, coupon):
        bound = runtime._REQUEST.get()
        assert bound is not None
        assert bound.task_revision == 7
        assert bound.candidate_scope_id == "scope-1"
        assert (product_id, quantity, coupon) == (1001, 1, None)
        return ToolTrace(
            tool="preview_order",
            ok=True,
            detail={"status": "confirmation_required", "confirmationPhrase": "确认下单"},
        )

    monkeypatch.setattr(api, "get_session_task_state", load)
    monkeypatch.setattr(api, "dispatch_order_preview", dispatch)
    result = asyncio.run(api.preview_order_handoff(
        OrderPreviewHandoffRequest(sessionId="session-1", productId=1001, quantity=1),
        authorization="Bearer signed-token",
    ))
    assert result["success"] is True


def test_preview_handoff_rejects_product_outside_visible_scope_before_dispatch(monkeypatch):
    async def load(_session_id):
        return _state()

    async def forbidden(*_args):
        raise AssertionError("TransactionAgent dispatch must not run")

    monkeypatch.setattr(api, "get_session_task_state", load)
    monkeypatch.setattr(api, "dispatch_order_preview", forbidden)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.preview_order_handoff(
            OrderPreviewHandoffRequest(sessionId="session-1", productId=1002, quantity=1),
            authorization="Bearer signed-token",
        ))
    assert exc.value.status_code == 409


def test_payment_preview_binds_current_task_and_scope(monkeypatch):
    async def load(_session_id):
        return _state()

    async def dispatch(order_id):
        bound = runtime._REQUEST.get()
        assert bound is not None
        assert bound.task_revision == 7
        assert bound.candidate_scope_id == "scope-1"
        assert order_id == "order-1"
        return ToolTrace(
            tool="preview_payment",
            ok=True,
            detail={"status": "confirmation_required", "confirmationPhrase": "确认发起支付"},
        )

    monkeypatch.setattr(api, "get_session_task_state", load)
    monkeypatch.setattr(api, "dispatch_payment_preview", dispatch)
    result = asyncio.run(api.preview_payment_handoff(
        PaymentPreviewHandoffRequest(sessionId="session-1", orderId="order-1"),
        authorization="Bearer signed-token",
    ))

    assert result["success"] is True
    assert result["executionPath"][-1]["status"] == "stored"
