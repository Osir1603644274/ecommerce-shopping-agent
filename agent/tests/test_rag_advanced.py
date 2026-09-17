import unittest
from unittest.mock import AsyncMock, Mock

from app.knowledge.models import KnowledgeChunk, RetrievalTrace, SearchKnowledgeResult
from app.rag_advanced import answer_with_advanced_rag_observed


def hybrid_result() -> SearchKnowledgeResult:
    chunks = [
        KnowledgeChunk(
            chunkId="review:good",
            sourceType="review",
            sourceId="good",
            content="店里安静，有插座，适合带电脑办公。",
            metadata={
                "reviewId": "good",
                "shopId": 7,
                "shopName": "目标咖啡店",
                "fusionScore": 0.92,
            },
        ),
        KnowledgeChunk(
            chunkId="review:noise",
            sourceType="review",
            sourceId="noise",
            content="晚间音乐声音较大。",
            metadata={
                "reviewId": "noise",
                "shopId": 7,
                "shopName": "目标咖啡店",
                "fusionScore": 0.71,
            },
        ),
    ]
    citations = [
        chunks[0].to_citation(score=0.92),
        chunks[1].to_citation(score=0.71),
    ]
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=citations,
        trace=RetrievalTrace(
            query="适合办公的咖啡店",
            selectedSources=["reviews"],
            candidateCount=2,
            returnedCount=2,
            citations=citations,
        ),
    )


class AdvancedRagTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_single_llm_rerank_and_builds_exact_cited_answer(self):
        retrieve = Mock(return_value=hybrid_result())

        async def rerank(_question, reviews):
            return [
                {
                    **reviews[0],
                    "contentRelation": "support",
                    "contentScore": 3,
                },
                {
                    **reviews[1],
                    "contentRelation": "conflict",
                    "contentScore": 0,
                },
            ]

        answer, sources, metrics, pipeline = await answer_with_advanced_rag_observed(
            "适合办公的咖啡店",
            hybrid_retrieve=retrieve,
            content_rerank=rerank,
        )

        retrieve.assert_called_once_with(
            "适合办公的咖啡店",
            30,
            candidate_limit=30,
        )
        self.assertIn("目标咖啡店", answer)
        self.assertIn("店里安静，有插座，适合带电脑办公。", answer)
        self.assertIn("晚间音乐声音较大。", answer)
        self.assertIn("[good]", answer)
        self.assertIn("[noise]", answer)
        self.assertEqual([item["reviewId"] for item in sources], ["good", "noise"])
        self.assertEqual(pipeline["effectiveMode"], "advanced")
        self.assertEqual(
            pipeline["citationAudit"]["deterministicValidRecommendationRate"],
            1.0,
        )
        self.assertIn("llmDurationMs", metrics)
        self.assertNotIn("generationDurationMs", metrics)
        self.assertIn("deterministic_evidence_answer", pipeline["stages"])

    async def test_returns_no_evidence_without_calling_llm_when_recall_is_empty(self):
        empty = SearchKnowledgeResult(
            trace=RetrievalTrace(query="无结果", selectedSources=["reviews"])
        )
        rerank = AsyncMock()

        answer, sources, _metrics, pipeline = await answer_with_advanced_rag_observed(
            "无结果",
            hybrid_retrieve=Mock(return_value=empty),
            content_rerank=rerank,
        )

        self.assertIn("证据", answer)
        self.assertEqual(sources, [])
        self.assertEqual(pipeline["candidateReviewCount"], 0)
        rerank.assert_not_awaited()

    async def test_content_judge_failure_returns_hybrid_evidence(self):
        answer, sources, metrics, pipeline = await answer_with_advanced_rag_observed(
            "适合办公的咖啡店",
            hybrid_retrieve=Mock(return_value=hybrid_result()),
            content_rerank=AsyncMock(side_effect=ValueError("bad model output")),
        )

        self.assertIn("内容判断暂时不可用", answer)
        self.assertIn("[good]", answer)
        self.assertEqual([item["reviewId"] for item in sources], ["good", "noise"])
        self.assertEqual(pipeline["effectiveMode"], "hybrid_evidence_fallback")
        self.assertTrue(pipeline["fallback"])
        self.assertIn("llmDurationMs", metrics)


if __name__ == "__main__":
    unittest.main()
