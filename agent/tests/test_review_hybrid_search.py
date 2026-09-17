import unittest
from unittest.mock import Mock

from app.knowledge.models import KnowledgeChunk, RetrievalTrace, SearchKnowledgeResult
from app.knowledge.review_hybrid_search import search_review_hybrid


def _vector_result() -> SearchKnowledgeResult:
    chunks = [
        KnowledgeChunk(
            chunkId="review:r1",
            sourceType="review",
            sourceId="r1",
            content="适合安静办公",
            metadata={"reviewId": "r1", "shopId": 7, "shopName": "目标店"},
        ),
        KnowledgeChunk(
            chunkId="review:r2",
            sourceType="review",
            sourceId="r2",
            content="咖啡不错",
            metadata={"reviewId": "r2", "shopId": 7, "shopName": "目标店"},
        ),
    ]
    citations = [
        chunks[0].to_citation(score=0.9),
        chunks[1].to_citation(score=0.8),
    ]
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=citations,
        trace=RetrievalTrace(
            query="适合办公",
            selectedSources=["reviews"],
            candidateCount=2,
            returnedCount=2,
            citations=citations,
        ),
    )


class ReviewHybridSearchTests(unittest.TestCase):
    def test_rejects_zero_candidate_limit(self):
        with self.assertRaisesRegex(ValueError, "candidate_limit must be positive"):
            search_review_hybrid("quiet cafe", candidate_limit=0)

    def test_fuses_independent_recall_and_preserves_shop_filter(self):
        vector_retrieve = Mock(return_value=_vector_result())
        bm25_retrieve = Mock(
            return_value=[
                {
                    "reviewId": "r1",
                    "shopId": 7,
                    "shopName": "目标店",
                    "text": "适合安静办公",
                    "score": 8.0,
                },
                {
                    "reviewId": "r3",
                    "shopId": 7,
                    "shopName": "目标店",
                    "text": "有插座",
                    "score": 4.0,
                },
            ]
        )

        result = search_review_hybrid(
            "适合办公",
            3,
            shop_ids=[7],
            candidate_limit=5,
            vector_weight=0.2,
            bm25_weight=0.8,
            vector_retrieve=vector_retrieve,
            bm25_retrieve=bm25_retrieve,
        )

        vector_retrieve.assert_called_once_with(
            "适合办公",
            sources=["reviews"],
            limit=5,
            shop_ids=[7],
            excluded_review_sources=("seed",),
        )
        bm25_retrieve.assert_called_once_with("适合办公", 5, shop_ids=[7])
        self.assertEqual(result.trace.filters["shopIds"], [7])
        self.assertEqual(result.trace.filters["excludedReviewSources"], ["seed"])
        self.assertEqual(result.trace.candidate_count, 3)
        self.assertEqual(result.chunks[0].source_id, "r1")
        self.assertEqual(
            [chunk.chunk_id for chunk in result.chunks],
            [citation.chunk_id for citation in result.citations],
        )
        self.assertEqual(
            [step.name for step in result.trace.steps],
            ["vector_recall", "bm25_recall", "hybrid_fusion"],
        )
        self.assertTrue(
            all(chunk.metadata["shopId"] == 7 for chunk in result.chunks)
        )

    def test_empty_shop_filter_stays_empty(self):
        vector_retrieve = Mock(
            return_value=SearchKnowledgeResult(
                trace=RetrievalTrace(
                    query="query",
                    selectedSources=["reviews"],
                )
            )
        )
        bm25_retrieve = Mock(return_value=[])

        result = search_review_hybrid(
            "query",
            shop_ids=[],
            candidate_limit=3,
            vector_retrieve=vector_retrieve,
            bm25_retrieve=bm25_retrieve,
        )

        self.assertEqual(result.chunks, [])
        self.assertEqual(result.trace.filters["shopIds"], [])
        bm25_retrieve.assert_called_once_with("query", 3, shop_ids=[])


if __name__ == "__main__":
    unittest.main()
