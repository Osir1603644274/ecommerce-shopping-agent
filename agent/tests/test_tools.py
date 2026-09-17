import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.knowledge import Citation, KnowledgeChunk, RetrievalTrace, SearchKnowledgeResult
from app.schemas import ToolTrace
from app.tools import (
    TOOL_SCHEMAS,
    call_tool,
    recommend_shops,
    search_knowledge_tool,
    search_reviews_tool,
    search_shop_reviews_tool,
    search_shops,
    search_products_tool,
)


def test_compare_products_rejects_product_ids_over_limit():
    trace = asyncio.run(call_tool(
        "compare_products",
        {
            "productIds": [1, 2, 3, 4, 5, 6],
            "category": "phone",
            "requirements": [],
        },
    ))
    assert trace.ok is False
    assert "1 到 5" in trace.detail


def test_product_search_feature_flag_fails_closed():
    with patch("app.tools.settings.ecommerce_guide_enabled", False):
        trace = asyncio.run(search_products_tool("手机", "手机"))
    assert trace.ok is False
    assert trace.detail["code"] == "ecommerce_disabled"


class ToolDispatchTests(unittest.IsolatedAsyncioTestCase):

    async def test_dispatches_get_shop_detail(self):
        expected = ToolTrace(
            tool="get_shop_detail",
            ok=True,
            detail={"id": 3, "name": "清晨手冲咖啡", "phone": "010-8888-0003"},
        )

        with patch(
            "app.tools.get_shop_detail",
            new=AsyncMock(return_value=expected),
        ) as detail_tool:
            result = await call_tool("get_shop_detail", {"shopId": 3})

        self.assertEqual(result, expected)
        detail_tool.assert_awaited_once_with(3)

    async def test_rejects_missing_shop_id(self):
        result = await call_tool("get_shop_detail", {})

        self.assertFalse(result.ok)
        self.assertIn("shopId", result.detail)

    def test_exposes_get_shop_detail_schema(self):
        tool_names = [item["function"]["name"] for item in TOOL_SCHEMAS]

        self.assertIn("get_shop_detail", tool_names)

    def test_keeps_raw_search_reviews_internal(self):
        tool_names = [item["function"]["name"] for item in TOOL_SCHEMAS]

        self.assertNotIn("search_reviews", tool_names)
        self.assertIn("search_shop_reviews", tool_names)
        self.assertIn("search_knowledge", tool_names)

    def test_public_tool_registry_is_read_only_four_tool_shopping_surface(self):
        tool_names = {item["function"]["name"] for item in TOOL_SCHEMAS}
        self.assertTrue({
            "search_products", "get_product_details", "compare_products",
            "rerank_products_in_scope",
        }.issubset(tool_names))
        self.assertTrue(tool_names.isdisjoint({
            "preview_order", "create_order", "preview_cancel_order",
            "cancel_order", "preview_payment", "create_payment",
        }))

    def test_exposes_required_search_knowledge_query_schema(self):
        schemas = {
            item["function"]["name"]: item["function"] for item in TOOL_SCHEMAS
        }

        self.assertIn("search_knowledge", schemas)
        self.assertEqual(
            schemas["search_knowledge"]["parameters"]["required"],
            ["query"],
        )
        self.assertEqual(
            schemas["search_knowledge"]["parameters"]["properties"]["sources"]["items"]["enum"],
            ["reviews", "merchant_docs", "policy_docs"],
        )
        self.assertIn(
            "shopId",
            schemas["search_knowledge"]["parameters"]["properties"],
        )

    def test_exposes_named_shop_review_schema(self):
        schemas = {
            item["function"]["name"]: item["function"] for item in TOOL_SCHEMAS
        }

        self.assertEqual(
            schemas["search_shop_reviews"]["parameters"]["required"],
            ["query", "shopName"],
        )

    def test_exposes_recommend_shops_schema(self):
        schemas = {
            item["function"]["name"]: item["function"] for item in TOOL_SCHEMAS
        }

        self.assertIn("recommend_shops", schemas)
        self.assertEqual(
            schemas["recommend_shops"]["parameters"]["required"],
            ["userId"],
        )

    async def test_dispatches_recommend_shops(self):
        expected = ToolTrace(
            tool="recommend_shops",
            ok=True,
            detail={"userId": "demo-user-1", "count": 0, "shops": []},
        )

        with patch(
            "app.tools.recommend_shops",
            new=AsyncMock(return_value=expected),
        ) as recommend_tool:
            result = await call_tool(
                "recommend_shops",
                {"userId": "demo-user-1", "limit": 3},
            )

        self.assertEqual(result, expected)
        recommend_tool.assert_awaited_once_with("demo-user-1", 3, None, None, None)

    async def test_rejects_missing_recommend_user_id(self):
        result = await call_tool("recommend_shops", {})

        self.assertFalse(result.ok)
        self.assertIn("userId", result.detail)

    async def test_rejects_invalid_recommend_limit(self):
        result = await call_tool("recommend_shops", {"userId": "demo-user-1", "limit": "3"})

        self.assertFalse(result.ok)
        self.assertIn("limit", result.detail)

    async def test_recommend_shops_returns_backend_candidates(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "success": True,
                    "data": [
                        {
                            "shopId": 3,
                            "shopName": "清晨手冲咖啡",
                            "typeId": 2,
                            "avgPrice": 35,
                            "address": "文化广场 3 号",
                            "longitude": 116.1,
                            "latitude": 39.9,
                            "reason": "你最近关注过同类型商户",
                            "triggerShopIds": [100151],
                            "triggerShopNames": ["纸间咖啡书屋"],
                            "score": 5.0,
                            "distanceMeters": 120.5,
                        }
                    ],
                }

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                self.request_params = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, params=None):
                self.request_params = params
                return FakeResponse()

        with patch("app.tools.httpx.AsyncClient", FakeAsyncClient):
            result = await recommend_shops(" demo-user-1 ", limit=1)

        self.assertTrue(result.ok)
        self.assertEqual(result.detail["userId"], "demo-user-1")
        self.assertEqual(result.detail["count"], 1)
        self.assertEqual(result.detail["shops"][0]["shopName"], "清晨手冲咖啡")
        self.assertEqual(result.detail["shops"][0]["triggerShopNames"], ["纸间咖啡书屋"])

    async def test_dispatches_search_reviews(self):
        expected = ToolTrace(
            tool="search_reviews",
            ok=True,
            detail={"query": "哪家适合办公？", "count": 0, "reviews": []},
        )

        with patch(
            "app.tools.search_reviews_tool",
            new=AsyncMock(return_value=expected),
        ) as review_tool:
            result = await call_tool("search_reviews", {"query": "哪家适合办公？"})

        self.assertEqual(result, expected)
        review_tool.assert_awaited_once_with("哪家适合办公？", None)

    async def test_dispatches_filtered_search_reviews(self):
        expected = ToolTrace(
            tool="search_reviews",
            ok=True,
            detail={"query": "适合办公吗？", "shopId": 100011, "reviews": []},
        )

        with patch(
            "app.tools.search_reviews_tool",
            new=AsyncMock(return_value=expected),
        ) as review_tool:
            result = await call_tool(
                "search_reviews",
                {"query": "适合办公吗？", "shopId": 100011},
            )

        self.assertEqual(result, expected)
        review_tool.assert_awaited_once_with("适合办公吗？", 100011)

    async def test_dispatches_named_shop_reviews(self):
        expected = ToolTrace(
            tool="search_shop_reviews",
            ok=True,
            detail={"resolvedShop": {"id": 100011}, "reviews": []},
        )

        with patch(
            "app.tools.search_shop_reviews_tool",
            new=AsyncMock(return_value=expected),
        ) as named_review_tool:
            result = await call_tool(
                "search_shop_reviews",
                {
                    "query": "Red Hook适合办公吗？",
                    "shopName": "Red Hook Coffee & Tea",
                },
            )

        self.assertEqual(result, expected)
        named_review_tool.assert_awaited_once_with(
            "Red Hook适合办公吗？",
            "Red Hook Coffee & Tea",
        )

    async def test_named_shop_reviews_resolves_then_filters_reviews(self):
        shop_trace = ToolTrace(
            tool="search_shops",
            ok=True,
            detail={
                "shops": [
                    {
                        "id": 100011,
                        "name": "Red Hook Coffee & Tea",
                        "address": "765 S 4th St",
                    }
                ]
            },
        )
        reviews = [
            {
                "reviewId": "review-red-hook",
                "shopId": 100011,
                "shopName": "Red Hook Coffee & Tea",
                "text": "适合办公。",
                "score": 0.9,
            },
            {
                "reviewId": "review-red-hook-second",
                "shopId": 100011,
                "shopName": "Red Hook Coffee & Tea",
                "text": "下午较安静。",
                "score": 0.8,
            },
        ]
        chunks = [
            KnowledgeChunk(
                chunkId=f"review:{review['reviewId']}",
                sourceType="review",
                sourceId=review["reviewId"],
                content=review["text"],
                metadata={"shopId": review["shopId"]},
            )
            for review in reviews
        ]
        citations = [
            chunk.to_citation(score=reviews[index]["score"], quote=chunk.content)
            for index, chunk in enumerate(chunks)
        ]
        knowledge_result = SearchKnowledgeResult(
            chunks=chunks,
            citations=citations,
            trace=RetrievalTrace(
                query="Red Hook适合办公吗？",
                selectedSources=["reviews"],
                filters={"visibility": "public", "shopId": 100011},
                candidateCount=2,
                returnedCount=2,
                citations=citations,
            ),
            legacy_reviews=reviews,
        )

        with patch(
            "app.tools.search_shops",
            new=AsyncMock(return_value=shop_trace),
        ) as shop_tool, patch(
            "app.tools.query_knowledge",
            return_value=knowledge_result,
        ) as knowledge_search:
            result = await search_shop_reviews_tool(
                "Red Hook适合办公吗？",
                " Red Hook Coffee & Tea ",
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.detail["resolvedShop"]["id"], 100011)
        self.assertEqual(result.detail["reviews"][0]["shopId"], 100011)
        self.assertEqual(
            result.detail["retrievalTrace"]["filters"]["shopId"],
            100011,
        )
        self.assertEqual(
            [item["reviewId"] for item in result.detail["reviews"]],
            ["review-red-hook", "review-red-hook-second"],
        )
        self.assertEqual(
            [item["sourceId"] for item in result.detail["citations"]],
            ["review-red-hook", "review-red-hook-second"],
        )
        shop_tool.assert_awaited_once_with(name="Red Hook Coffee & Tea")
        knowledge_search.assert_called_once_with(
            "Red Hook适合办公吗？",
            ["reviews"],
            3,
            shop_id=100011,
        )
        self.assertEqual(
            result.knowledge_result["chunks"][0]["chunkId"],
            "review:review-red-hook",
        )

    async def test_named_shop_reviews_rejects_ambiguous_candidates(self):
        shop_trace = ToolTrace(
            tool="search_shops",
            ok=True,
            detail={
                "shops": [
                    {"id": 100100, "name": "Starbucks", "address": "A店"},
                    {"id": 100134, "name": "Starbucks", "address": "B店"},
                ]
            },
        )

        with patch(
            "app.tools.search_shops",
            new=AsyncMock(return_value=shop_trace),
        ), patch(
            "app.tools.query_knowledge",
        ) as knowledge_search:
            result = await search_shop_reviews_tool(
                "Starbucks适合办公吗？",
                "Starbucks",
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.detail["resolutionStatus"], "multiple_exact_matches")
        self.assertEqual(len(result.detail["candidates"]), 2)
        knowledge_search.assert_not_called()

    async def test_search_shops_passes_name_to_backend(self):
        clients = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "success": True,
                    "data": [
                        {
                            "id": 100011,
                            "name": "Red Hook Coffee & Tea",
                            "address": "765 S 4th St",
                            "avgPrice": 20,
                        }
                    ],
                }

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                self.request_params = None
                clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, params=None):
                self.request_params = params
                return FakeResponse()

        with patch("app.tools.httpx.AsyncClient", FakeAsyncClient):
            result = await search_shops(name=" Red Hook Coffee & Tea ")

        self.assertTrue(result.ok)
        self.assertEqual(clients[0].request_params, {"name": "Red Hook Coffee & Tea"})
        self.assertEqual(result.detail["shops"][0]["id"], 100011)

    async def test_search_shops_uses_place_coordinates_for_nearby_query(self):
        clients = []

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "success": True,
                    "data": [
                        {
                            "id": 100001,
                            "name": "演示咖啡店",
                            "address": "北京市朝阳区朝阳公园周边",
                            "avgPrice": 28,
                            "longitude": 116.49,
                            "latitude": 39.95,
                            "distanceMeters": 420.5,
                            "coordinateSystem": "BD-09",
                            "dataNature": "localized_demo",
                        }
                    ],
                }

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                clients.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def get(self, url, params=None):
                self.url = url
                self.params = params
                return FakeResponse()

        with patch("app.tools.httpx.AsyncClient", FakeAsyncClient):
            result = await search_shops(
                type_id=2,
                near_place_id="beijing-park-178",
                radius_meters=1500,
                limit=5,
            )

        self.assertTrue(result.ok)
        self.assertTrue(clients[0].url.endswith("/api/shops/nearby"))
        self.assertEqual(clients[0].params["typeId"], 2)
        self.assertEqual(clients[0].params["radiusMeters"], 1500)
        self.assertEqual(clients[0].params["limit"], 5)
        self.assertEqual(result.detail["nearPlaceName"], "朝阳公园")
        self.assertEqual(result.detail["shops"][0]["distanceMeters"], 420.5)
        self.assertIn("不代表真实登记商家", result.detail["dataNotice"])

    async def test_rejects_missing_search_reviews_query(self):
        result = await call_tool("search_reviews", {})

        self.assertFalse(result.ok)
        self.assertIn("query", result.detail)

    async def test_dispatches_search_knowledge(self):
        expected = ToolTrace(
            tool="search_knowledge",
            ok=True,
            detail={"query": "这家店有 WiFi 吗？", "count": 0, "chunks": []},
        )

        with patch(
            "app.tools.search_knowledge_tool",
            new=AsyncMock(return_value=expected),
        ) as knowledge_tool:
            result = await call_tool(
                "search_knowledge",
                {"query": "这家店有 WiFi 吗？", "sources": ["merchant_docs"]},
            )

        self.assertEqual(result, expected)
        knowledge_tool.assert_awaited_once_with("这家店有 WiFi 吗？", ["merchant_docs"])

    async def test_rejects_missing_search_knowledge_query(self):
        result = await call_tool("search_knowledge", {})

        self.assertFalse(result.ok)
        self.assertIn("query", result.detail)

    async def test_rejects_invalid_search_knowledge_sources(self):
        result = await call_tool(
            "search_knowledge",
            {"query": "这家店有 WiFi 吗？", "sources": "merchant_docs"},
        )

        self.assertFalse(result.ok)
        self.assertIn("sources", result.detail)

    async def test_search_reviews_tool_returns_traceable_evidence(self):
        reviews = [
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座。",
                "score": 0.89,
            },
            {
                "reviewId": "review-006",
                "shopId": 4,
                "shopName": "午后咖啡",
                "text": "工作日下午比较安静。",
                "score": 0.81,
            },
        ]

        with patch("app.tools.query_reviews", return_value=reviews) as retrieve_mock:
            result = await search_reviews_tool("  哪家咖啡店适合办公？  ")

        self.assertTrue(result.ok)
        self.assertIsNotNone(result.duration_ms)
        self.assertGreaterEqual(result.duration_ms, 0)
        self.assertEqual(result.detail["reviews"], reviews)
        self.assertEqual(
            [item["reviewId"] for item in result.detail["reviews"]],
            ["review-005", "review-006"],
        )
        self.assertEqual(result.detail["retrievalTrace"]["query"], "哪家咖啡店适合办公？")
        self.assertEqual(result.detail["retrievalTrace"]["selectedSources"], ["reviews"])
        self.assertEqual(
            [
                item["chunkId"]
                for item in result.detail["retrievalTrace"]["citations"]
            ],
            ["review:review-005", "review:review-006"],
        )
        retrieve_mock.assert_called_once_with("哪家咖啡店适合办公？", 3)

    async def test_search_reviews_tool_passes_shop_id_and_traces_filter(self):
        reviews = [
            {
                "reviewId": "yelp-review",
                "shopId": 100011,
                "shopName": "Red Hook Coffee & Tea",
                "text": "适合办公。",
                "score": 0.9,
            }
        ]

        with patch("app.tools.query_reviews", return_value=reviews) as retrieve_mock:
            result = await search_reviews_tool("适合办公吗？", 100011)

        self.assertTrue(result.ok)
        self.assertEqual(result.detail["shopId"], 100011)
        self.assertEqual(
            result.detail["retrievalTrace"]["filters"]["shopId"],
            100011,
        )
        retrieve_mock.assert_called_once_with(
            "适合办公吗？",
            3,
            shop_id=100011,
        )

    async def test_search_reviews_tool_hides_internal_failure(self):
        with patch(
            "app.tools.query_reviews",
            side_effect=ConnectionError("secret-qdrant-host:6333"),
        ):
            result = await search_reviews_tool("哪家咖啡店适合办公？")

        self.assertFalse(result.ok)
        self.assertIsNotNone(result.duration_ms)
        self.assertGreaterEqual(result.duration_ms, 0)
        self.assertEqual(result.detail, "评论检索服务暂时不可用")
        self.assertNotIn("secret", result.detail)

    async def test_search_knowledge_tool_returns_chunks_citations_and_trace(self):
        chunk = KnowledgeChunk(
            chunkId="merchant_doc:business-001:profile",
            sourceType="merchant_doc",
            sourceId="business-001",
            title="示例咖啡店商户资料",
            content="示例咖啡店 WiFi：免费。",
        )
        citation = Citation(
            chunkId=chunk.chunk_id,
            sourceType=chunk.source_type,
            sourceId=chunk.source_id,
            title=chunk.title,
            quote=chunk.content,
            score=3.0,
        )
        second_chunk = KnowledgeChunk(
            chunkId="merchant_doc:business-002:profile",
            sourceType="merchant_doc",
            sourceId="business-002",
            title="第二家咖啡店商户资料",
            content="第二家咖啡店 WiFi：免费。",
        )
        second_citation = Citation(
            chunkId=second_chunk.chunk_id,
            sourceType=second_chunk.source_type,
            sourceId=second_chunk.source_id,
            title=second_chunk.title,
            quote=second_chunk.content,
            score=2.0,
        )
        trace = RetrievalTrace(
            query="示例咖啡店有 WiFi 吗？",
            selectedSources=["merchant_docs"],
            candidateCount=2,
            returnedCount=2,
            citations=[citation, second_citation],
        )
        search_result = SearchKnowledgeResult(
            chunks=[chunk, second_chunk],
            citations=[citation, second_citation],
            trace=trace,
        )

        with patch("app.tools.query_knowledge", return_value=search_result) as search_mock:
            result = await search_knowledge_tool(
                "  示例咖啡店有 WiFi 吗？  ",
                ["merchant_docs"],
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.tool, "search_knowledge")
        self.assertIsNotNone(result.duration_ms)
        self.assertEqual(result.detail["query"], "示例咖啡店有 WiFi 吗？")
        self.assertEqual(result.detail["sources"], ["merchant_docs"])
        self.assertEqual(result.detail["count"], 2)
        self.assertEqual(
            [item["chunkId"] for item in result.detail["chunks"]],
            [
                "merchant_doc:business-001:profile",
                "merchant_doc:business-002:profile",
            ],
        )
        self.assertEqual(
            [item["sourceId"] for item in result.detail["citations"]],
            ["business-001", "business-002"],
        )
        self.assertEqual(
            [item["score"] for item in result.detail["citations"]],
            [3.0, 2.0],
        )
        self.assertEqual(
            [item["chunkId"] for item in result.knowledge_result["chunks"]],
            [
                "merchant_doc:business-001:profile",
                "merchant_doc:business-002:profile",
            ],
        )
        self.assertNotIn("legacyReviews", result.knowledge_result)
        self.assertEqual(
            result.detail["retrievalTrace"]["selectedSources"],
            ["merchant_docs"],
        )
        search_mock.assert_called_once_with("示例咖啡店有 WiFi 吗？", ["merchant_docs"], 3)

    async def test_search_knowledge_tool_passes_review_shop_filter(self):
        trace = RetrievalTrace(
            query="这家店安静吗？",
            selectedSources=["reviews"],
            filters={"visibility": "public", "shopId": 100011},
        )
        search_result = SearchKnowledgeResult(trace=trace)

        with patch("app.tools.query_knowledge", return_value=search_result) as search_mock:
            result = await search_knowledge_tool(
                "这家店安静吗？",
                ["reviews"],
                100011,
            )

        self.assertTrue(result.ok)
        self.assertEqual(result.detail["shopId"], 100011)
        self.assertEqual(result.detail["retrievalTrace"]["filters"]["shopId"], 100011)
        search_mock.assert_called_once_with(
            "这家店安静吗？",
            ["reviews"],
            3,
            shop_id=100011,
        )

    async def test_search_knowledge_tool_returns_validation_error(self):
        with patch("app.tools.query_knowledge", side_effect=ValueError("unsupported knowledge source: bad")):
            result = await search_knowledge_tool("测试问题", ["bad"])

        self.assertFalse(result.ok)
        self.assertIn("unsupported knowledge source", result.detail)

    async def test_search_knowledge_tool_hides_internal_failure(self):
        with patch(
            "app.tools.query_knowledge",
            side_effect=ConnectionError("secret-qdrant-host:6333"),
        ):
            result = await search_knowledge_tool("测试问题", ["reviews"])

        self.assertFalse(result.ok)
        self.assertEqual(result.detail, "知识检索服务暂时不可用")
        self.assertNotIn("secret", result.detail)


if __name__ == "__main__":
    unittest.main()
