import unittest
from unittest.mock import patch

from app.knowledge import (
    MERCHANT_DOC_SOURCE_NAME,
    POLICY_DOC_SOURCE_NAME,
    REVIEW_SOURCE_NAME,
    search_knowledge,
)


class KnowledgeSearchTests(unittest.TestCase):
    def test_explicit_review_source_bypasses_router_and_uses_vector_search(self):
        reviews = [
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座，下午办公很舒服。",
                "score": 0.88,
            },
            {
                "reviewId": "review-006",
                "shopId": 4,
                "shopName": "午后咖啡",
                "text": "工作日下午比较安静。",
                "score": 0.81,
            },
        ]

        with patch(
            "app.knowledge.search.route_knowledge_sources",
            side_effect=AssertionError("explicit sources should bypass router"),
        ), patch("app.knowledge.search.vector_search_reviews", return_value=reviews) as search_mock:
            result = search_knowledge(
                "哪家咖啡店适合办公？",
                sources=[REVIEW_SOURCE_NAME],
                limit=3,
            )

        search_mock.assert_called_once_with("哪家咖啡店适合办公？", 3)
        self.assertEqual(result.trace.selected_sources, [REVIEW_SOURCE_NAME])
        self.assertEqual(result.trace.steps[0].name, "source_override")
        self.assertEqual(
            [chunk.chunk_id for chunk in result.chunks],
            ["review:review-005", "review:review-006"],
        )
        self.assertEqual(
            [citation.source_id for citation in result.citations],
            ["review-005", "review-006"],
        )
        self.assertEqual(result.trace.candidate_count, 2)
        self.assertEqual(result.trace.returned_count, 2)

    def test_review_source_passes_shop_filter_without_changing_order(self):
        reviews = [
            {
                "reviewId": "review-shop-first",
                "shopId": 100011,
                "shopName": "Red Hook Coffee & Tea",
                "text": "第一条评论。",
                "score": 0.9,
            },
            {
                "reviewId": "review-shop-second",
                "shopId": 100011,
                "shopName": "Red Hook Coffee & Tea",
                "text": "第二条评论。",
                "score": 0.8,
            },
        ]

        with patch(
            "app.knowledge.search.vector_search_reviews",
            return_value=reviews,
        ) as search_mock:
            result = search_knowledge(
                "这家店安静吗？",
                sources=[REVIEW_SOURCE_NAME],
                limit=3,
                shop_id=100011,
            )

        search_mock.assert_called_once_with(
            "这家店安静吗？",
            3,
            shop_id=100011,
        )
        self.assertEqual(result.trace.filters["shopId"], 100011)
        self.assertEqual(
            [chunk.source_id for chunk in result.chunks],
            ["review-shop-first", "review-shop-second"],
        )
        self.assertEqual(result.legacy_reviews, reviews)

    def test_auto_router_selects_merchant_docs_for_wifi_question(self):
        result = search_knowledge(
            "St Honore Pastries 有 WiFi 吗？",
            limit=3,
        )

        self.assertEqual(result.trace.selected_sources, [MERCHANT_DOC_SOURCE_NAME])
        self.assertEqual(result.trace.steps[0].name, "router")
        self.assertTrue(result.chunks)
        self.assertTrue(all(chunk.source_type == "merchant_doc" for chunk in result.chunks))
        self.assertTrue(
            any("St Honore Pastries" in (chunk.title or "") for chunk in result.chunks)
        )
        self.assertGreaterEqual(result.trace.candidate_count, result.trace.returned_count)

    def test_policy_docs_keyword_search_returns_policy_chunks(self):
        result = search_knowledge(
            "个性化推荐和个人信息可以怎么处理？",
            sources=[POLICY_DOC_SOURCE_NAME],
            limit=2,
        )

        self.assertEqual(result.trace.selected_sources, [POLICY_DOC_SOURCE_NAME])
        self.assertTrue(result.chunks)
        self.assertTrue(all(chunk.source_type == "policy_doc" for chunk in result.chunks))
        self.assertTrue(
            any("个人信息" in chunk.content or "推荐" in chunk.content for chunk in result.chunks)
        )

    def test_merchant_docs_keyword_search_matches_category_synonym(self):
        result = search_knowledge(
            "\u8fd9\u5bb6\u5e97\u5c5e\u4e8e\u4ec0\u4e48\u7c7b\u522b\uff1f",
            sources=[MERCHANT_DOC_SOURCE_NAME],
            limit=3,
        )

        self.assertEqual(result.trace.selected_sources, [MERCHANT_DOC_SOURCE_NAME])
        self.assertTrue(result.chunks)
        self.assertTrue(all(chunk.source_type == "merchant_doc" for chunk in result.chunks))
        self.assertTrue(
            any("\u5206\u7c7b" in chunk.content for chunk in result.chunks)
        )

    def test_multi_source_search_combines_review_and_merchant_results(self):
        reviews = [
            {
                "reviewId": "review-013",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "工作日下午通常比较安静，插座集中在靠窗座位。",
                "score": 0.76,
            }
        ]

        with patch("app.knowledge.search.vector_search_reviews", return_value=reviews):
            result = search_knowledge(
                "St Honore Pastries 有 WiFi 吗，哪家实际适合办公？",
                sources=[MERCHANT_DOC_SOURCE_NAME, REVIEW_SOURCE_NAME],
                limit=2,
            )

        self.assertEqual(
            result.trace.selected_sources,
            [MERCHANT_DOC_SOURCE_NAME, REVIEW_SOURCE_NAME],
        )
        source_types = {chunk.source_type for chunk in result.chunks}
        self.assertIn("merchant_doc", source_types)
        self.assertIn("review", source_types)
        step_sources = {
            step.detail.get("source")
            for step in result.trace.steps
            if step.detail.get("source")
        }
        self.assertEqual(step_sources, {MERCHANT_DOC_SOURCE_NAME, REVIEW_SOURCE_NAME})

    def test_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            search_knowledge("测试问题", sources=["unknown_source"])

    def test_rejects_blank_query(self):
        with self.assertRaises(ValueError):
            search_knowledge("   ", sources=[REVIEW_SOURCE_NAME])


if __name__ == "__main__":
    unittest.main()
