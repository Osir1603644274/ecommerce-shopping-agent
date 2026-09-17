import unittest

from app.rag_context_selection import select_reranked_review_context
from evaluation.rag_context_selection_evaluation import (
    build_context_selection_validation_report,
)


def review(
    review_id,
    shop_id,
    relation,
    score=0,
    text=None,
):
    return {
        "reviewId": review_id,
        "shopId": shop_id,
        "shopName": f"shop-{shop_id}",
        "text": text or f"text-{review_id}",
        "contentRelation": relation,
        "contentScore": score,
    }


class RagContextSelectionTests(unittest.TestCase):
    def test_round_robin_support_prevents_one_shop_from_consuming_budget(self):
        ranked = [
            review("a1", 1, "support", 3),
            review("a2", 1, "support", 3),
            review("a3", 1, "support", 2),
            review("b1", 2, "support", 2),
            review("c1", 3, "support", 1),
        ]

        result = select_reranked_review_context(
            ranked,
            max_shops=3,
            max_support_per_shop=2,
            max_total_reviews=3,
        )

        self.assertEqual(
            [item["reviewId"] for item in result["selectedReviews"]],
            ["a1", "b1", "c1"],
        )

    def test_selects_conflict_only_for_shop_with_selected_support(self):
        ranked = [
            review("a-support", 1, "support", 3),
            review("b-irrelevant", 2, "irrelevant"),
            review("a-conflict", 1, "conflict"),
            review("b-conflict", 2, "conflict"),
        ]

        result = select_reranked_review_context(ranked, max_shops=2)

        self.assertEqual(
            [item["reviewId"] for item in result["selectedReviews"]],
            ["a-support", "a-conflict"],
        )
        self.assertEqual(result["shops"][0]["evidenceStatus"], "mixed")
        self.assertEqual(result["shops"][1]["evidenceStatus"], "insufficient")

    def test_deduplicates_review_ids_and_same_shop_text(self):
        ranked = [
            review("a1", 1, "support", 3, "same text"),
            review("a1", 1, "support", 3, "same text"),
            review("a2", 1, "support", 2, "same   text"),
            review("a3", 1, "support", 1, "different"),
        ]

        result = select_reranked_review_context(ranked, max_support_per_shop=3)

        self.assertEqual(
            [item["reviewId"] for item in result["selectedReviews"]],
            ["a1", "a3"],
        )

    def test_respects_review_and_character_budgets_without_truncating_source(self):
        ranked = [
            review("a", 1, "support", 3, "a" * 100),
            review("b", 2, "support", 3, "b" * 100),
            review("c", 3, "support", 3, "c" * 100),
        ]

        result = select_reranked_review_context(
            ranked,
            max_total_reviews=2,
            max_total_chars=120,
            max_chars_per_review=60,
        )

        self.assertEqual(result["selectedReviewCount"], 2)
        self.assertEqual(result["selectedCharacterCount"], 120)
        self.assertEqual(len(result["selectedReviews"][0]["contextText"]), 60)
        self.assertEqual(len(result["selectedReviews"][0]["text"]), 100)

    def test_rejects_non_positive_limits(self):
        with self.assertRaises(ValueError):
            select_reranked_review_context([], max_total_reviews=0)

    def test_evaluation_counts_false_support_in_qrel_precision_denominator(self):
        cases = [
            {
                "id": "case-1",
                "question": "安静的店",
                "relevanceJudgments": [
                    {
                        "shopId": 1,
                        "relevance": 3,
                        "supportingReviewIds": ["relevant"],
                    }
                ],
            }
        ]
        content_report = {
            "rerankerAudit": [
                {
                    "caseId": "case-1",
                    "rankedCandidates": [
                        {
                            "reviewId": "relevant",
                            "shopId": 1,
                            "shopName": "shop-1",
                            "originalHybridRank": 2,
                            "relation": "support",
                            "score": 3,
                        },
                        {
                            "reviewId": "false-support",
                            "shopId": 2,
                            "shopName": "shop-2",
                            "originalHybridRank": 1,
                            "relation": "support",
                            "score": 2,
                        },
                    ],
                }
            ]
        }
        reviews = [
            review("relevant", 1, "support", 3),
            review("false-support", 2, "support", 2),
        ]

        report = build_context_selection_validation_report(
            cases=cases,
            content_report=content_report,
            reviews=reviews,
            max_shops=2,
            max_total_reviews=2,
        )

        self.assertEqual(report["metrics"]["selectedSupportQrelPrecision"], 0.5)
        self.assertEqual(report["metrics"]["relevantTopShopExactEvidenceCoverage"], 1.0)
        self.assertFalse(report["methodology"]["shopOrderChanged"])


if __name__ == "__main__":
    unittest.main()
