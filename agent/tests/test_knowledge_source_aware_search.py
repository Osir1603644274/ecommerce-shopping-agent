import unittest
from unittest.mock import patch

from app.knowledge.models import KnowledgeChunk, RetrievalTrace, SearchKnowledgeResult
from app.knowledge.source_aware_search import search_knowledge_source_aware


def result(source: str, source_type: str) -> SearchKnowledgeResult:
    chunk = KnowledgeChunk(
        chunkId=f"{source_type}:1",
        sourceType=source_type,
        sourceId="1",
        content=f"{source} content",
    )
    return SearchKnowledgeResult(
        chunks=[chunk],
        citations=[chunk.to_citation()],
        trace=RetrievalTrace(
            query="query",
            selectedSources=[source],
            candidateCount=1,
            returnedCount=1,
        ),
    )


class KnowledgeSourceAwareSearchTests(unittest.TestCase):
    @patch("app.knowledge.source_aware_search.search_merchant_docs_lexical_index")
    @patch("app.knowledge.source_aware_search.search_knowledge_index")
    def test_uses_shop_filter_for_reviews_and_merchant_docs(
        self,
        vector_search,
        lexical_search,
    ):
        vector_search.side_effect = [
            result("merchant_docs", "merchant_doc"),
            result("reviews", "review"),
            result("policy_docs", "policy_doc"),
        ]

        combined = search_knowledge_source_aware(
            "query",
            ["merchant_docs", "reviews", "policy_docs"],
            shop_ids=[7],
        )

        self.assertEqual(len(combined.chunks), 3)
        lexical_search.assert_not_called()
        self.assertEqual(
            vector_search.call_args_list[0].kwargs,
            {
                "sources": ["merchant_docs"],
                "limit": 3,
                "shop_ids": [7],
            },
        )
        self.assertEqual(
            vector_search.call_args_list[1].kwargs,
            {
                "sources": ["reviews"],
                "limit": 3,
                "shop_ids": [7],
                "excluded_review_sources": ("seed",),
            },
        )
        self.assertEqual(
            vector_search.call_args_list[2].kwargs,
            {"sources": ["policy_docs"], "limit": 3},
        )

    @patch("app.knowledge.source_aware_search.search_review_hybrid")
    @patch("app.knowledge.source_aware_search.search_knowledge_index")
    def test_hybrid_flag_routes_reviews_to_hybrid(
        self,
        vector_search,
        hybrid_search,
    ):
        hybrid_search.return_value = result("reviews", "review")

        with patch(
            "app.knowledge.source_aware_search.settings.knowledge_review_hybrid_enabled",
            True,
        ):
            combined = search_knowledge_source_aware(
                "适合办公吗",
                ["reviews"],
                shop_ids=[7],
            )

        hybrid_search.assert_called_once_with("适合办公吗", 3, shop_ids=[7])
        vector_search.assert_not_called()
        route = next(
            step for step in combined.trace.steps if step.name == "review_hybrid_route"
        )
        self.assertEqual(route.detail["effectiveMode"], "hybrid")
        self.assertFalse(route.detail["fallback"])
        self.assertEqual(combined.trace.filters["excludedReviewSources"], ["seed"])

    @patch("app.knowledge.source_aware_search.search_review_hybrid")
    @patch("app.knowledge.source_aware_search.search_knowledge_index")
    def test_hybrid_failure_falls_back_to_source_aware_vector(
        self,
        vector_search,
        hybrid_search,
    ):
        hybrid_search.side_effect = RuntimeError("bm25 unavailable")
        vector_search.return_value = result("reviews", "review")

        with patch(
            "app.knowledge.source_aware_search.settings.knowledge_review_hybrid_enabled",
            True,
        ):
            combined = search_knowledge_source_aware(
                "适合办公吗",
                ["reviews"],
                shop_ids=[7],
            )

        vector_search.assert_called_once_with(
            "适合办公吗",
            sources=["reviews"],
            limit=3,
            shop_ids=[7],
            excluded_review_sources=("seed",),
        )
        route = next(
            step for step in combined.trace.steps if step.name == "review_hybrid_route"
        )
        self.assertEqual(route.detail["effectiveMode"], "vector")
        self.assertTrue(route.detail["fallback"])
        self.assertEqual(route.detail["errorType"], "RuntimeError")

    @patch("app.knowledge.source_aware_search.search_merchant_docs_lexical_index")
    @patch("app.knowledge.source_aware_search.search_knowledge_index")
    def test_uses_qdrant_payload_lexical_for_unresolved_merchant_query(
        self,
        vector_search,
        lexical_search,
    ):
        lexical_search.return_value = result("merchant_docs", "merchant_doc")

        combined = search_knowledge_source_aware(
            "Forin Cafe地址",
            ["merchant_docs"],
        )

        self.assertEqual(combined.chunks[0].source_type, "merchant_doc")
        lexical_search.assert_called_once_with("Forin Cafe地址", 3)
        vector_search.assert_not_called()

    def test_rejects_blank_query_and_invalid_limit(self):
        with self.assertRaisesRegex(ValueError, "query must not be blank"):
            search_knowledge_source_aware(" ", ["reviews"])
        with self.assertRaisesRegex(ValueError, "limit must be positive"):
            search_knowledge_source_aware("query", ["reviews"], limit=0)


if __name__ == "__main__":
    unittest.main()
