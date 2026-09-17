import unittest

from app.knowledge.models import KnowledgeChunk, RetrievalTrace, SearchKnowledgeResult
from app.knowledge_retrieval_quality import (
    evaluate_retrieval_quality_cases,
    load_retrieval_quality_cases,
    validate_retrieval_quality_cases,
)


def fake_result(chunk_ids: list[str]) -> SearchKnowledgeResult:
    chunks = [
        KnowledgeChunk(
            chunkId=chunk_id,
            sourceType=chunk_id.split(":", 1)[0],
            sourceId=chunk_id,
            content="evidence",
        )
        for chunk_id in chunk_ids
    ]
    return SearchKnowledgeResult(
        chunks=chunks,
        citations=[],
        trace=RetrievalTrace(
            query="query",
            returnedCount=len(chunks),
        ),
    )


class KnowledgeRetrievalQualityTests(unittest.TestCase):
    def test_scores_required_chunks_with_source_local_ranks(self):
        cases = [
            {
                "id": "mixed",
                "split": "validation",
                "category": "merchant_reviews",
                "question": "question",
                "expectedSources": ["merchant_docs", "reviews"],
                "relevantChunkIds": [
                    "merchant_doc:target:profile",
                    "review:target",
                ],
            }
        ]

        report = evaluate_retrieval_quality_cases(
            cases,
            retrieve=lambda _question, _sources, _limit: fake_result(
                [
                    "merchant_doc:other:profile",
                    "merchant_doc:target:profile",
                    "review:target",
                ]
            ),
        )

        self.assertEqual(report["overall"]["completeHits"], 1)
        self.assertEqual(report["overall"]["relevantChunkRecall"], 1.0)
        self.assertEqual(report["overall"]["meanRelevantReciprocalRank"], 0.75)
        self.assertEqual(report["failures"], [])

    def test_reports_partial_mixed_source_failure(self):
        cases = [
            {
                "id": "mixed",
                "split": "test",
                "category": "merchant_reviews",
                "question": "question",
                "expectedSources": ["merchant_docs", "reviews"],
                "relevantChunkIds": [
                    "merchant_doc:target:profile",
                    "review:target",
                ],
            }
        ]

        report = evaluate_retrieval_quality_cases(
            cases,
            retrieve=lambda _question, _sources, _limit: fake_result(
                ["merchant_doc:target:profile", "review:other"]
            ),
        )

        self.assertEqual(report["overall"]["completeHits"], 0)
        self.assertEqual(report["overall"]["relevantChunkRecall"], 0.5)
        self.assertEqual(len(report["failures"]), 1)

    def test_rejects_chunk_leakage_across_splits(self):
        base = {
            "category": "reviews",
            "question": "question",
            "expectedSources": ["reviews"],
            "relevantChunkIds": ["review:same"],
        }

        with self.assertRaisesRegex(ValueError, "must not cross"):
            validate_retrieval_quality_cases(
                [
                    {**base, "id": "validation", "split": "validation"},
                    {**base, "id": "test", "split": "test"},
                ]
            )

    def test_dataset_is_balanced_and_valid(self):
        cases = load_retrieval_quality_cases()

        self.assertEqual(len(cases), 16)
        for split in ("validation", "test"):
            split_cases = [case for case in cases if case["split"] == split]
            self.assertEqual(len(split_cases), 8)
            categories = {
                category: sum(
                    case["category"] == category for case in split_cases
                )
                for category in {
                    "reviews",
                    "merchant_docs",
                    "policy_docs",
                    "merchant_reviews",
                }
            }
            self.assertEqual(set(categories.values()), {2})


if __name__ == "__main__":
    unittest.main()
