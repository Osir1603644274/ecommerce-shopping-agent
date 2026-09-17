import unittest

from app.knowledge.models import KnowledgeChunk, RetrievalTrace, SearchKnowledgeResult
from evaluation.knowledge_source_aware_evaluation import build_source_aware_validation_report
from app.schemas import ToolTrace


class KnowledgeSourceAwareEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolves_entity_and_passes_shop_filter_on_validation_only(self):
        cases = [
            {
                "id": "validation-case",
                "split": "validation",
                "category": "reviews",
                "question": "Example Shop安静吗？",
                "expectedSources": ["reviews"],
                "shopEntity": {"name": "Example Shop"},
                "reviewFilter": {"source": "yelp", "shopId": 7},
                "relevantChunkIds": ["review:target"],
            },
            {
                "id": "test-case",
                "split": "test",
                "category": "reviews",
                "question": "test must not run",
                "expectedSources": ["reviews"],
                "relevantChunkIds": ["review:test"],
            },
        ]
        calls = []

        async def find_shops(_type_id, name):
            self.assertEqual(name, "Example Shop")
            return ToolTrace(
                tool="search_shops",
                ok=True,
                detail={"shops": [{"id": 7, "name": "Example Shop"}]},
            )

        def retrieve(query, sources, limit, *, shop_ids):
            calls.append((query, sources, limit, shop_ids))
            chunk = KnowledgeChunk(
                chunkId="review:target",
                sourceType="review",
                sourceId="target",
                content="安静",
            )
            return SearchKnowledgeResult(
                chunks=[chunk],
                citations=[chunk.to_citation()],
                trace=RetrievalTrace(
                    query=query,
                    selectedSources=sources,
                    returnedCount=1,
                ),
            )

        report = await build_source_aware_validation_report(
            cases,
            find_shops=find_shops,
            retrieve=retrieve,
        )

        self.assertEqual(calls, [("Example Shop安静吗？", ["reviews"], 3, [7])])
        self.assertEqual(report["entityResolution"]["correct"], 1)
        self.assertEqual(report["retrieval"]["overall"]["completeHits"], 1)
        self.assertFalse(report["methodology"]["testRead"])


if __name__ == "__main__":
    unittest.main()
