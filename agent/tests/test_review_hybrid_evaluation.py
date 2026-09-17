import unittest

from app.knowledge.models import KnowledgeChunk, RetrievalTrace, SearchKnowledgeResult
from evaluation.review_hybrid_evaluation import build_review_hybrid_validation_report


def _result(review_ids: list[tuple[str, int]]) -> SearchKnowledgeResult:
    chunks = [
        KnowledgeChunk(
            chunkId=f"review:{review_id}",
            sourceType="review",
            sourceId=review_id,
            content="evidence",
            metadata={
                "reviewId": review_id,
                "shopId": shop_id,
                "shopName": f"shop-{shop_id}",
            },
        )
        for review_id, shop_id in review_ids
    ]
    citations = [chunk.to_citation(score=1.0) for chunk in chunks]
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=citations,
        trace=RetrievalTrace(
            query="query",
            selectedSources=["reviews"],
            returnedCount=len(chunks),
            citations=citations,
        ),
    )


class ReviewHybridEvaluationTests(unittest.TestCase):
    def test_gate_passes_when_hybrid_improves_quality_within_latency(self):
        prepared = []
        cases = [
            {
                "id": "one",
                "question": "quiet",
                "relevanceJudgments": [
                    {"shopId": 2, "relevance": 3, "supportingReviewIds": ["good"]},
                    {"shopId": 3, "relevance": 2, "supportingReviewIds": ["other"]},
                ],
            }
        ]

        report = build_review_hybrid_validation_report(
            cases,
            vector_retrieve=lambda _query, _limit: _result([("bad", 1)]),
            hybrid_retrieve=lambda _query, _limit: _result(
                [("good", 2), ("other", 3)]
            ),
            hybrid_prepare=lambda: prepared.append(True),
            candidate_limit=2,
            top_k=2,
        )

        self.assertTrue(report["summary"]["passed"])
        self.assertTrue(report["summary"]["eligibleForHybridAgentCanary"])
        self.assertEqual(prepared, [True])
        self.assertTrue(
            report["methodology"]["hybridInitialization"][
                "performedBeforeTimedRequests"
            ]
        )
        self.assertIn(
            "retrievalDurationMs",
            report["methods"]["reviewHybrid"]["details"][0],
        )


if __name__ == "__main__":
    unittest.main()
