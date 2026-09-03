import asyncio

import httpx

from app import llm
from app.domains.ecommerce import transactions
from app.domains.ecommerce.transactions import (
    BackendTransactionError,
    TransactionContext,
    _bind_authenticated_transaction_context,
    extract_order_reference,
    is_order_status_query,
    query_order_status_tool,
    render_order_status_result,
)
from app.schemas import ToolTrace
from app.settings import settings
from app.transaction_agent import runtime
from app.transaction_agent.capabilities import _issue_transaction_capability


ORDER_ID = "759be5bc-5763-4639-af10-2481428ccc3f"
ORDER_NO = "O20260902010101ABCDEF123456"


def _context(message: str) -> TransactionContext:
    return TransactionContext(
        access_token="signed-access-token",
        owner_user_id="user-1",
        session_id="session-1",
        task_id="task-1",
        task_revision=7,
        candidate_scope_id=None,
        user_message=message,
    )


def test_order_status_intent_requires_explicit_order_and_accepts_id_or_number():
    assert is_order_status_query(f"查询订单 {ORDER_ID} 的状态") is True
    assert extract_order_reference(f"查询订单 {ORDER_ID} 的状态") == ORDER_ID
    assert extract_order_reference(f"订单号 {ORDER_NO} 进度怎么样") == ORDER_NO
    assert is_order_status_query("推荐一台手机") is False
    assert extract_order_reference("查询订单状态") is None


def test_authenticated_status_dispatch_is_read_only_when_writes_are_disabled(monkeypatch):
    monkeypatch.setattr(settings, "agent_transaction_enabled", False)

    async def authenticate(_authorization):
        return runtime._AuthenticatedOwner(
            user_id="user-1", roles=("USER",), access_token="signed-access-token"
        )

    async def backend(context, method, path, *, json_body=None, idempotency_key=None):
        assert context.owner_user_id == "user-1"
        assert method == "GET"
        assert path == f"/api/orders/{ORDER_ID}"
        assert json_body is None
        assert idempotency_key is None
        return {
            "id": ORDER_ID,
            "orderNo": ORDER_NO,
            "status": "PENDING_PAYMENT",
            "payableMinor": 199900,
        }

    monkeypatch.setattr(runtime, "_authenticate", authenticate)
    monkeypatch.setattr(transactions, "_request_backend", backend)
    with runtime.bind_transaction_request(
        authorization="Bearer signed-access-token",
        session_id="session-1",
        task_id="task-1",
        task_revision=7,
        candidate_scope_id=None,
        user_message=f"查询订单 {ORDER_ID} 的状态",
    ):
        trace = asyncio.run(runtime.dispatch_order_status(ORDER_ID))

    assert trace.ok is True
    assert "待支付" in render_order_status_result(trace)
    assert "1999.00" in render_order_status_result(trace)


def test_status_dispatch_rejects_missing_login_before_backend(monkeypatch):
    async def forbidden(_order_reference):
        raise AssertionError("order backend must not run")

    monkeypatch.setattr(runtime, "query_order_status_tool", forbidden)
    with runtime.bind_transaction_request(
        authorization=None,
        session_id="session-1",
        task_id=None,
        task_revision=None,
        candidate_scope_id=None,
        user_message=f"查询订单 {ORDER_ID} 的状态",
    ):
        trace = asyncio.run(runtime.dispatch_order_status(ORDER_ID))
    assert trace.ok is False
    assert trace.detail["code"] == "transaction_authentication_required"


def test_status_query_fails_closed_for_invalid_forbidden_missing_and_unavailable(monkeypatch):
    async def forbidden_io(*_args, **_kwargs):
        raise AssertionError("invalid order reference must not reach backend")

    with _bind_authenticated_transaction_context(
        _context("查询订单状态"), _issue_transaction_capability()
    ):
        monkeypatch.setattr(transactions, "_request_backend", forbidden_io)
        invalid = asyncio.run(query_order_status_tool("not-an-order"))
    assert invalid.detail["code"] == "invalid_order_reference"

    async def run_error(error):
        async def backend(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(transactions, "_request_backend", backend)
        with _bind_authenticated_transaction_context(
            _context(f"查询订单 {ORDER_ID} 的状态"), _issue_transaction_capability()
        ):
            return await query_order_status_tool(ORDER_ID)

    for status in (403, 404):
        trace = asyncio.run(run_error(BackendTransactionError(status, "unsafe detail")))
        assert trace.detail["code"] == "order_not_found_or_forbidden"
        assert "不存在或你无权" in trace.detail["message"]

    request = httpx.Request("GET", "http://backend/api/orders/x")
    unavailable = asyncio.run(run_error(httpx.ConnectError("down", request=request)))
    assert unavailable.detail["code"] == "transaction_backend_unavailable"


def test_chat_preflight_returns_backend_order_status_without_model(monkeypatch):
    async def dispatch(order_reference):
        assert order_reference == ORDER_ID
        return ToolTrace(
            tool="query_order_status",
            ok=True,
            detail={
                "order": {
                    "id": ORDER_ID,
                    "orderNo": ORDER_NO,
                    "status": "PAID",
                    "payableMinor": 199900,
                }
            },
        )

    monkeypatch.setattr(llm, "dispatch_order_status", dispatch)
    result = asyncio.run(llm._run_deterministic_preflight(
        f"查询订单 {ORDER_ID} 的状态",
        task_state=None,
        domain_hint="auto",
        on_answer_delta=None,
        on_task_state=None,
    ))
    assert result is not None
    answer, traces, messages, run_id, summary = result
    assert answer == f"订单 {ORDER_NO} 当前状态：已支付；应付 1999.00 元。"
    assert traces[0].tool == "query_order_status"
    assert messages[-1]["content"] == answer
    assert run_id is None
    assert summary is None
