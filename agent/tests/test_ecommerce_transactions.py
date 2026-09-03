import asyncio

import httpx

from app.domains.ecommerce import transactions
from app.domains.ecommerce.transactions import (
    TRANSACTION_AUTH_PROVIDER_UNAVAILABLE,
    bind_transaction_context,
    cancel_order_tool,
    create_order_tool,
    create_payment_tool,
    execute_confirmed_transaction,
    preview_cancel_order_tool,
    preview_order_tool,
    preview_payment_tool,
    query_order_status_tool,
)
from app.llm import route_tool_schemas, select_tool_schemas


def _deny_all_io(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("HTTP must not be constructed")

    class ForbiddenRedis:
        def __getattr__(self, name):
            raise AssertionError(f"Redis must not be reached: {name}")

    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)
    monkeypatch.setattr(transactions, "_redis_client", ForbiddenRedis())


def test_raw_authorization_session_and_body_like_values_never_mint_transaction_context(monkeypatch):
    _deny_all_io(monkeypatch)
    with bind_transaction_context(
        authorization="Bearer raw-unverified-token",
        session_id="browser-session-1",
        task_id="body-controlled-task-id",
        user_message="确认下单",
    ) as context:
        assert context is None
        trace = asyncio.run(preview_order_tool(1001, 1))
    assert trace.ok is False
    assert trace.detail["code"] == TRANSACTION_AUTH_PROVIDER_UNAVAILABLE


def test_every_direct_preview_or_write_is_stable_provider_denial_before_io(monkeypatch):
    _deny_all_io(monkeypatch)
    calls = (
        lambda: preview_order_tool(1001, 1),
        lambda: preview_cancel_order_tool("order-1"),
        lambda: preview_payment_tool("order-1"),
        lambda: query_order_status_tool("759be5bc-5763-4639-af10-2481428ccc3f"),
        create_order_tool,
        cancel_order_tool,
        create_payment_tool,
    )
    for call in calls:
        trace = asyncio.run(call())
        assert trace.ok is False
        assert trace.detail["code"] == TRANSACTION_AUTH_PROVIDER_UNAVAILABLE


def test_execute_without_capability_or_action_has_no_unbound_local_error(monkeypatch):
    _deny_all_io(monkeypatch)
    for action in (None, "create_order", "cancel_order", "create_payment"):
        trace = asyncio.run(execute_confirmed_transaction(action))
        assert trace.ok is False
        assert trace.detail["code"] == TRANSACTION_AUTH_PROVIDER_UNAVAILABLE


def test_transaction_intent_has_read_only_shopping_menu_in_legacy_and_harness_router():
    expected = {
        "search_products", "get_product_details", "compare_products",
        "rerank_products_in_scope",
    }
    message = "我想购买这台手机并下单"
    assert {item["function"]["name"] for item in select_tool_schemas(message)} == expected
    assert {item["function"]["name"] for item in route_tool_schemas(message).schemas} == expected
