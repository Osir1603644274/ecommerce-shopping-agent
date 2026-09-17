import unittest

from evaluation.rag_shop_aggregation_evaluation import (
    build_shop_aggregation_validation_report,
    load_shop_aggregation_validation_cases,
)


def review(review_id, shop_id, score):
    return {
        "reviewId": review_id,
        "shopId": shop_id,
        "shopName": f"Shop {shop_id}",
        "text": review_id,
        "score": score,
    }


class RagShopAggregationEvaluationTests(unittest.TestCase):
    def test_validation_cases_have_three_candidates_and_no_shop_name_in_query(self):
        cases = load_shop_aggregation_validation_cases()

        self.assertEqual(len(cases), 4)
        for case in cases:
            self.assertEqual(len(case["candidateShopIds"]), 3)
            self.assertIn(case["expectedTopShopId"], case["candidateShopIds"])

    def test_compares_modes_and_thresholds_with_same_evidence_budget(self):
        cases = [
            {
                "id": "case-1",
                "question": "which shop",
                "candidateShopIds": [1, 2, 3],
                "expectedTopShopId": 1,
                "relevantReviewIds": ["relevant"],
            }
        ]

        def global_retrieve(question, limit, *, source=None, shop_ids=None):
            self.assertEqual(limit, 6)
            self.assertEqual(shop_ids, [1, 2, 3])
            return [
                review("relevant", 1, 0.8),
                review("shop-2", 2, 0.7),
                review("shop-3-low", 3, 0.3),
            ]

        def grouped_retrieve(
            question,
            shop_ids,
            *,
            reviews_per_shop=None,
            source=None,
        ):
            self.assertEqual(reviews_per_shop, 2)
            return [
                {"shopId": 1, "reviews": [review("relevant", 1, 0.8)]},
                {"shopId": 2, "reviews": [review("shop-2", 2, 0.7)]},
                {"shopId": 3, "reviews": [review("shop-3-low", 3, 0.3)]},
            ]

        report = build_shop_aggregation_validation_report(
            cases,
            global_retrieve=global_retrieve,
            grouped_retrieve=grouped_retrieve,
            thresholds=(None, 0.5),
        )

        self.assertEqual(len(report["configurations"]), 4)
        for mode in ("global", "grouped"):
            no_threshold = next(
                config
                for config in report["configurations"]
                if config["mode"] == mode and config["scoreThreshold"] is None
            )
            thresholded = next(
                config
                for config in report["configurations"]
                if config["mode"] == mode and config["scoreThreshold"] == 0.5
            )
            self.assertEqual(no_threshold["metrics"]["relevantReviewHits"], 1)
            self.assertEqual(no_threshold["metrics"]["targetShopHitsAt1"], 1)
            self.assertEqual(no_threshold["metrics"]["averageShopCoverage"], 1.0)
            self.assertEqual(thresholded["metrics"]["averageShopCoverage"], 2 / 3)
            self.assertEqual(thresholded["metrics"]["averageEvidenceCount"], 2.0)
            self.assertEqual(thresholded["metrics"]["noEvidenceCandidateCount"], 1)
            self.assertEqual(thresholded["metrics"]["filterViolationCount"], 0)


if __name__ == "__main__":
    unittest.main()
