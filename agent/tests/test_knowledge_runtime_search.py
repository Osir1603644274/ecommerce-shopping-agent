import unittest
from unittest.mock import patch

from app.knowledge import (
    Citation,
    KnowledgeChunk,
    RetrievalTrace,
    SearchKnowledgeResult,
    search_knowledge,
)
from app.settings import settings


def _source_aware_review_result() -> SearchKnowledgeResult:
    chunk = KnowledgeChunk(
        chunkId="review:review-001",
        sourceType="review",
        sourceId="review-001",
        content="适合安静办公。",
        metadata={
            "reviewId": "review-001",
            "shopId": 100011,
            "shopName": "Red Hook Coffee & Tea",
            "sourceLanguage": "zh",
        },
    )
    citation = Citation(
        chunkId=chunk.chunk_id,
        sourceType=chunk.source_type,
        sourceId=chunk.source_id,
        quote=chunk.content,
        score=0.91,
    )
    return SearchKnowledgeResult(
        chunks=[chunk],
        citations=[citation],
        trace=RetrievalTrace(
            query="适合办公吗？",
            selectedSources=["reviews"],
            filters={"visibility": "public", "shopIds": [100011]},
            candidateCount=1,
            returnedCount=1,
            citations=[citation],
        ),
    )


def _legacy_result() -> SearchKnowledgeResult:
    result = _source_aware_review_result()
    result.legacy_reviews = [
        {
            "reviewId": "legacy-review-001",
            "shopId": 100011,
            "text": "旧链路评论",
            "score": 0.88,
        }
    ]
    return result


class KnowledgeRuntimeSearchTests(unittest.TestCase):
    def test_default_off_calls_legacy_without_changing_trace(self):
        legacy_result = _legacy_result()

        with patch.object(settings, "knowledge_source_aware_enabled", False), patch(
            "app.knowledge.runtime_search.search_knowledge_legacy",
            return_value=legacy_result,
        ) as legacy_search, patch(
            "app.knowledge.runtime_search.search_knowledge_source_aware",
        ) as source_aware_search:
            result = search_knowledge(
                "适合办公吗？",
                sources=["reviews"],
                limit=3,
                shop_id=100011,
            )

        self.assertIs(result, legacy_result)
        legacy_search.assert_called_once_with(
            "适合办公吗？",
            ["reviews"],
            3,
            shop_id=100011,
        )
        source_aware_search.assert_not_called()
        self.assertFalse(
            any(step.name == "runtime_search_route" for step in result.trace.steps)
        )

    def test_enabled_calls_source_aware_and_adapts_review_detail(self):
        source_aware_result = _source_aware_review_result()

        with patch.object(settings, "knowledge_source_aware_enabled", True), patch(
            "app.knowledge.runtime_search.search_knowledge_source_aware",
            return_value=source_aware_result,
        ) as source_aware_search, patch(
            "app.knowledge.runtime_search.search_knowledge_legacy",
        ) as legacy_search:
            result = search_knowledge(
                "适合办公吗？",
                sources=["reviews"],
                limit=3,
                shop_id=100011,
            )

        source_aware_search.assert_called_once_with(
            "适合办公吗？",
            ["reviews"],
            3,
            shop_ids=[100011],
        )
        legacy_search.assert_not_called()
        self.assertEqual(result.legacy_reviews[0]["reviewId"], "review-001")
        self.assertEqual(result.legacy_reviews[0]["shopId"], 100011)
        self.assertEqual(result.legacy_reviews[0]["text"], "适合安静办公。")
        self.assertEqual(result.legacy_reviews[0]["score"], 0.91)
        route_step = result.trace.steps[0]
        self.assertEqual(route_step.name, "runtime_search_route")
        self.assertEqual(
            route_step.detail,
            {
                "requestedMode": "source_aware",
                "effectiveMode": "source_aware",
                "fallback": False,
            },
        )

    def test_enabled_failure_falls_back_to_legacy_and_records_reason(self):
        legacy_result = _legacy_result()

        with patch.object(settings, "knowledge_source_aware_enabled", True), patch(
            "app.knowledge.runtime_search.search_knowledge_source_aware",
            side_effect=RuntimeError("qdrant unavailable"),
        ), patch(
            "app.knowledge.runtime_search.search_knowledge_legacy",
            return_value=legacy_result,
        ) as legacy_search:
            result = search_knowledge(
                "适合办公吗？",
                sources=["reviews"],
                limit=3,
                shop_id=100011,
            )

        legacy_search.assert_called_once_with(
            "适合办公吗？",
            ["reviews"],
            3,
            shop_id=100011,
        )
        route_step = result.trace.steps[0]
        self.assertEqual(route_step.name, "runtime_search_route")
        self.assertEqual(route_step.detail["requestedMode"], "source_aware")
        self.assertEqual(route_step.detail["effectiveMode"], "legacy")
        self.assertTrue(route_step.detail["fallback"])
        self.assertEqual(route_step.detail["errorType"], "RuntimeError")

    def test_invalid_request_does_not_trigger_fallback(self):
        with patch.object(settings, "knowledge_source_aware_enabled", True), patch(
            "app.knowledge.runtime_search.search_knowledge_source_aware",
        ) as source_aware_search, patch(
            "app.knowledge.runtime_search.search_knowledge_legacy",
        ) as legacy_search:
            with self.assertRaises(ValueError):
                search_knowledge(
                    "适合办公吗？",
                    sources=["policy_docs"],
                    shop_id=100011,
                )

        source_aware_search.assert_not_called()
        legacy_search.assert_not_called()


if __name__ == "__main__":
    unittest.main()
