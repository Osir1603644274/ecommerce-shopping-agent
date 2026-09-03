import asyncio

import httpx

from app.llm import shopping_tool_schemas
from app.tools import TOOL_SCHEMAS, call_tool
from app.transaction_agent import SHOPPING_TOOL_NAMES
from app.transaction_agent.capabilities import (
    TRANSACTION_PREVIEW_TOOL_NAMES,
    TRANSACTION_WRITE_TOOL_NAMES,
)


def test_only_read_only_shopping_menu_is_public_to_models():
    names = {item["function"]["name"] for item in shopping_tool_schemas()}
    public_names = {item["function"]["name"] for item in TOOL_SCHEMAS}
    assert names == SHOPPING_TOOL_NAMES
    assert names == {
        "search_products", "get_product_details", "compare_products",
        "rerank_products_in_scope",
    }
    assert public_names.isdisjoint(TRANSACTION_PREVIEW_TOOL_NAMES | TRANSACTION_WRITE_TOOL_NAMES)


def test_transaction_dispatch_is_provider_denied_before_transport(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("transaction transport must not be constructed")

    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenClient)
    for name in sorted(TRANSACTION_PREVIEW_TOOL_NAMES | TRANSACTION_WRITE_TOOL_NAMES):
        trace = asyncio.run(call_tool(name, {}))
        assert trace.ok is False
        assert trace.detail == {"code": "transaction_auth_provider_unavailable"}


def test_capability_factories_are_not_public_package_api():
    import app.transaction_agent as transaction_agent

    for name in (
        "TransactionCapability", "ShoppingCapability", "issue_transaction_capability",
        "issue_shopping_capability", "bind_transaction_capability",
    ):
        assert not hasattr(transaction_agent, name)
