"""Contract tests use injected providers/models; none measure retrieval quality."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.catalog_evidence import CatalogBinding, use_catalog_evidence_provider
from app.catalog_evidence_agent import run_catalog_evidence_agent
from app.domains.ecommerce.cross_encoder import (
    commerce_search_text, logits_to_rank_scores, use_commerce_cross_encoder,
)
from app.domains.ecommerce.tools import search_products_tool
from app.schemas import ToolTrace
from app.settings import settings
from app.tools import TOOL_SCHEMAS, call_tool


BINDING = CatalogBinding(dataRoot="F:/agent", runId="test-only-catalog", manifestSha256="a" * 64)
MODEL_SHA = "b" * 64


@pytest.fixture
def catalog_settings(monkeypatch):
    for key, value in {
        "catalog_evidence_enabled": True,
        "catalog_evidence_data_root": BINDING.data_root,
        "catalog_evidence_run_id": BINDING.run_id,
        "catalog_evidence_manifest_sha256": BINDING.manifest_sha256,
        "catalog_evidence_source": "kuaisearch",
    }.items():
        monkeypatch.setattr(settings, key, value)


def result(request):
    return {"binding": BINDING.model_dump(by_alias=True), "query": request.query,
            "source": request.source, "hits": [{
                "source": request.source, "docid": request.source + ":1",
                "text": "测试原文，不包含价格或库存", "rank": 1, "score": -3.0,
                "provenance": {"fixture": True}, "unknown": ["brand"],
            }]}


def test_disabled_tool_does_not_call_provider(monkeypatch):
    monkeypatch.setattr(settings, "catalog_evidence_enabled", False)
    provider = AsyncMock()
    with use_catalog_evidence_provider(BINDING, provider):
        trace = asyncio.run(call_tool("search_catalog_evidence", {"query": "测试", "source": "kuaisearch"}))
    assert trace.detail["code"] == "catalog_evidence_disabled"
    provider.assert_not_called()
    assert "search_catalog_evidence" not in {s["function"]["name"] for s in TOOL_SCHEMAS}


def test_catalog_preserves_source_id_text_and_missing_commerce_fields(catalog_settings):
    provider = AsyncMock(side_effect=result)
    with use_catalog_evidence_provider(BINDING, provider):
        trace = asyncio.run(call_tool("search_catalog_evidence", {"query": "测试", "source": "kuaisearch"}))
    assert trace.ok
    hit = trace.detail["hits"][0]
    assert hit["docid"] == "kuaisearch:1" and hit["score"] == -3.0
    assert hit["text"] == "测试原文，不包含价格或库存"
    assert {"brand", "verified_price", "inventory", "purchase_availability"} <= set(hit["unknown"])
    assert "candidateIds" not in trace.detail and "id" not in hit
    assert "snapshotPriceMinor" not in hit and trace.detail["commerceAuthority"] is False


@pytest.mark.parametrize("mutation", ["source", "docid", "duplicate", "rank", "run", "query", "price"])
def test_catalog_rejects_cross_source_or_fabricated_product_shape(catalog_settings, mutation):
    async def provider(request):
        value = result(request)
        if mutation == "source": value["hits"][0]["source"] = "multicpr"
        if mutation == "docid": value["hits"][0]["docid"] = "1"
        if mutation == "duplicate": value["hits"].append(deepcopy(value["hits"][0]))
        if mutation == "rank": value["hits"][0]["rank"] = 2
        if mutation == "run": value["binding"]["runId"] = "other-run"
        if mutation == "query": value["query"] = "other-query"
        if mutation == "price": value["hits"][0]["snapshotPriceMinor"] = 1
        return value
    with use_catalog_evidence_provider(BINDING, provider):
        trace = asyncio.run(call_tool("search_catalog_evidence", {"query": "测试", "source": "kuaisearch"}))
    assert not trace.ok


def test_catalog_provider_scope_resets_and_fails_without_real_provider(catalog_settings):
    with use_catalog_evidence_provider(BINDING, AsyncMock(side_effect=result)):
        assert asyncio.run(call_tool("search_catalog_evidence", {"query": "测试", "source": "kuaisearch"})).ok
    trace = asyncio.run(call_tool("search_catalog_evidence", {"query": "测试", "source": "kuaisearch"}))
    assert trace.detail["code"] == "catalog_evidence_provider_unavailable"


def selection_response(query="测试", *, tool="search_catalog_evidence", calls=True):
    selected = SimpleNamespace(id="call-fixture", function=SimpleNamespace(
        name=tool, arguments='{"query":"' + query + '","source":"kuaisearch","limit":10}'))
    return SimpleNamespace(id="fixture-selection", usage=None, choices=[SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
        content=None, tool_calls=[selected] if calls else [],
    ))])


def final_response():
    return SimpleNamespace(id="fixture-answer", usage=None, choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
        content="相关文档：kuaisearch:1；价格和库存未知。", tool_calls=None,
    ))])


def test_agent_model_selects_tool_dispatches_and_receives_real_provider_output(catalog_settings):
    create = AsyncMock(side_effect=[selection_response(), final_response()])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    provider = AsyncMock(side_effect=result)
    with use_catalog_evidence_provider(BINDING, provider):
        answer, traces, turns, _, _ = asyncio.run(run_catalog_evidence_agent(
            "测试", client=client, tool_caller=call_tool,
        ))
    assert provider.await_count == 1 and create.await_count == 2
    assert create.call_args_list[0].kwargs["tool_choice"] == "auto"
    assert "tools" not in create.call_args_list[1].kwargs
    assert create.call_args_list[1].kwargs["messages"][-1]["role"] == "tool"
    assert "测试原文" in create.call_args_list[1].kwargs["messages"][-1]["content"]
    assert traces[-1].detail["modelCallCount"] == 2
    assert traces[-1].detail["taskStateMutated"] is False
    assert answer == turns[-1]["content"]


@pytest.mark.parametrize("reply", [selection_response(calls=False), selection_response(query="改写"), selection_response(tool="search_products")])
def test_agent_never_substitutes_scripted_search_for_invalid_model_selection(catalog_settings, reply):
    create = AsyncMock(return_value=reply)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    caller = AsyncMock()
    _, traces, _, _, _ = asyncio.run(run_catalog_evidence_agent("测试", client=client, tool_caller=caller))
    caller.assert_not_called()
    assert create.await_count == 1 and not traces[-1].ok


def test_public_agent_entry_reuses_explicit_model_tool_loop(catalog_settings):
    from app import llm
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.chat.completions.create.side_effect = [selection_response(), final_response()]
    with patch.object(llm, "get_client", return_value=client), use_catalog_evidence_provider(BINDING, AsyncMock(side_effect=result)):
        _, traces, _, _, _ = asyncio.run(llm.run_agent("测试", domain_hint="catalog_evidence"))
    assert traces[-1].ok and traces[-1].detail["modelCallCount"] == 2


def test_catalog_route_rejects_stateful_shopping_context(catalog_settings):
    from app.llm import run_agent
    with pytest.raises(ValueError, match="isolated_stateless"):
        asyncio.run(run_agent("测试", domain_hint="catalog_evidence", task_state=object()))


def test_negative_ce_logits_preserve_order_and_product_id_tie_break():
    scores, order = logits_to_rank_scores({3: -2.0, 2: -1.0, 1: -1.0}, [3, 2, 1])
    assert order == [1, 2, 3]
    assert scores == {1: 1.0, 2: 2 / 3, 3: 1 / 3}


@pytest.mark.parametrize("logits", [{1: float("nan")}, {1: float("inf")}, {2: 1.0}, {1: True}])
def test_ce_invalid_scores_or_foreign_ids_rejected(logits):
    with pytest.raises(ValueError): logits_to_rank_scores(logits, [1])


def product(pid):
    return {"id": pid, "title": "测试手机 " + str(pid), "brand": "测试品牌", "source": "fixture",
            "categoryL1": "手机", "categoryL2": "手机通讯", "categoryL3": "智能手机",
            "snapshotPriceMinor": 10000, "currency": "CNY", "priceStatus": "verified",
            "lifecycleStatus": "ACTIVE", "entityVersion": 1, "availableQuantity": 1,
            "inventoryVersion": 1, "attributeText": "测试文本", "attributes": []}


class Backend:
    async def get(self, url, params=None):
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"data": {
            "channel": "elasticsearch", "products": [{"id": 1}, {"id": 2}],
        }})


def run_commerce(provider=None, *, enable=False, products=None, requirements=None):
    details = ToolTrace(tool="get_product_details", ok=True,
                        detail={"products": products or [product(1), product(2)], "productIds": [1, 2]})
    with patch("app.domains.ecommerce.tools._product_http_client", return_value=Backend()), \
         patch("app.domains.ecommerce.tools.get_product_details_tool", AsyncMock(return_value=details)), \
         patch.object(settings, "product_retrieval_mode", "elasticsearch"), \
         patch.object(settings, "product_cross_encoder_enabled", enable), \
         patch.object(settings, "product_cross_encoder_model_sha256", MODEL_SHA):
        if provider is None:
            return asyncio.run(search_products_tool("测试手机", "手机", requirements=requirements))
        with use_commerce_cross_encoder(MODEL_SHA, provider):
            return asyncio.run(search_products_tool("测试手机", "手机", requirements=requirements))


def test_commerce_ce_uses_resolved_pool_and_changes_semantic_order_only():
    async def provider(request):
        assert [pid for pid, _ in request.pairs] == [1, 2]
        assert request.pairs[0][1] == commerce_search_text(product(1))
        return {1: -5.0, 2: -1.0}
    trace = run_commerce(provider, enable=True)
    assert trace.ok, trace.detail
    assert trace.detail["candidatePoolIds"] == [1, 2]
    assert trace.detail["rankedItemIds"] == [2, 1]
    assert trace.detail["retrievalTrace"]["crossEncoder"]["scores"][0]["rawLogit"] == -1.0


def test_commerce_ce_failure_keeps_prior_score_map():
    async def provider(request): raise RuntimeError("fixture unavailable")
    baseline = run_commerce()
    failed = run_commerce(provider, enable=True)
    assert baseline.ok and failed.ok
    assert failed.detail["rankedItemIds"] == baseline.detail["rankedItemIds"]
    assert [r["scoreBreakdown"] for r in failed.detail["candidates"]] == [r["scoreBreakdown"] for r in baseline.detail["candidates"]]
    assert failed.detail["retrievalTrace"]["crossEncoder"]["fallback"] == "previous_score_map"


def test_commerce_ce_cannot_rescue_wrong_brand_or_override_budget_order():
    async def provider(request): return {1: -5.0, 2: -1.0}
    products = [product(1), {**product(2), "brand": "其他品牌"}]
    hard_brand = {"key": "brand", "operator": "eq", "value": "测试品牌", "unit": "text",
                  "priority": "hard", "source": "user"}
    trace = run_commerce(provider, enable=True, products=products, requirements=[hard_brand])
    assert trace.ok, trace.detail
    assert trace.detail["rankedItemIds"] == [1]
    products = [{**product(1), "snapshotPriceMinor": 19000}, product(2)]
    budget = {"key": "price_minor", "operator": "lte", "value": 20000, "unit": "CNY_MINOR",
              "priority": "hard", "source": "user"}
    trace = run_commerce(provider, enable=True, products=products, requirements=[budget])
    assert trace.ok, trace.detail
    assert trace.detail["rankedItemIds"] == [1, 2]


def test_commerce_ce_does_not_receive_missing_inventory_or_unverified_price():
    async def provider(request):
        assert [pid for pid, _ in request.pairs] == [1]
        return {1: -1.0}
    for missing in ({"availableQuantity": None}, {"priceStatus": "missing", "snapshotPriceMinor": None}):
        trace = run_commerce(provider, enable=True, products=[product(1), {**product(2), **missing}])
        assert trace.ok, trace.detail
        assert trace.detail["candidatePoolIds"] == [1]


def test_evidence_docid_cannot_enter_commerce_detail_dispatch():
    with patch("app.tools.get_product_details_tool", new_callable=AsyncMock) as details:
        trace = asyncio.run(call_tool("get_product_details", {"productIds": ["kuaisearch:1"]}))
    assert not trace.ok
    details.assert_not_called()
