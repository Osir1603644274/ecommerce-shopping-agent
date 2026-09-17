import unittest

from evaluation.rag_fuzzy_discovery_evaluation import (
    build_fuzzy_shop_discovery_validation_report,
    load_fuzzy_shop_discovery_validation_cases,
    rank_shops_from_reviews,
)


def review(review_id, shop_id, score, shop_name=None):
    return {
        "reviewId": review_id,
        "shopId": shop_id,
        "shopName": shop_name or f"shop-{shop_id}",
        "score": score,
        "text": review_id,
    }


class RagFuzzyDiscoveryEvaluationTests(unittest.TestCase):
    def test_validation_dataset_has_multiple_graded_shops_and_evidence(self):
        cases = load_fuzzy_shop_discovery_validation_cases()

        self.assertEqual(len(cases), 8)
        for case in cases:
            self.assertGreaterEqual(len(case["relevanceJudgments"]), 2)
            self.assertFalse(
                any(
                    item["shopName"] in case["question"]
                    for item in case["relevanceJudgments"]
                )
            )
            for judgment in case["relevanceJudgments"]:
                self.assertIn(judgment["relevance"], {1, 2, 3})
                self.assertTrue(judgment["supportingReviewIds"])

    def test_shop_ranking_uses_first_review_and_retains_limited_evidence(self):
        ranked = rank_shops_from_reviews(
            [
                review("a1", 1, 0.9),
                review("a2", 1, 0.8),
                review("b1", 2, 0.7),
                review("a3", 1, 0.6),
            ],
            evidence_per_shop=2,
        )

        self.assertEqual([item["shopId"] for item in ranked], [1, 2])
        self.assertEqual(ranked[0]["firstReviewRank"], 1)
        self.assertEqual(ranked[0]["reviewIds"], ["a1", "a2"])

    def test_report_scores_multiple_relevant_shops_and_fusions(self):
        cases = [
            {
                "id": "case-1",
                "question": "模糊问题",
                "relevanceJudgments": [
                    {
                        "shopId": 1,
                        "relevance": 3,
                        "supportingReviewIds": ["a"],
                    },
                    {
                        "shopId": 2,
                        "relevance": 1,
                        "supportingReviewIds": ["b"],
                    },
                ],
            }
        ]

        def vector_retrieve(question, limit):
            self.assertEqual(limit, 3)
            return [
                review("x", 9, 0.9),
                review("a", 1, 0.8),
                review("b", 2, 0.7),
            ]

        def bm25_retrieve(question, limit):
            return [
                review("b", 2, 9.0),
                review("a", 1, 8.0),
                review("y", 8, 7.0),
            ]

        report = build_fuzzy_shop_discovery_validation_report(
            cases,
            vector_retrieve=vector_retrieve,
            bm25_retrieve=bm25_retrieve,
            candidate_limit=3,
            top_k=2,
        )

        self.assertFalse(report["methodology"]["testRead"])
        self.assertEqual(set(report["methods"]), {"vector", "bm25", "hybridV02B08", "rrf"})
        self.assertEqual(report["methods"]["vector"]["metrics"]["meanRecallAt2"], 0.5)
        self.assertEqual(report["methods"]["bm25"]["metrics"]["meanRecallAt2"], 1.0)
        self.assertEqual(
            report["methods"]["vector"]["metrics"]["meanEvidenceShopRecall"],
            1.0,
        )


if __name__ == "__main__":
    unittest.main()
