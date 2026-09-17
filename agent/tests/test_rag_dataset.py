import json
import unittest
from pathlib import Path

from app.rag import CORPUS_PATH as APP_CORPUS_PATH
from app.rag import EVAL_PATH as APP_EVAL_PATH
from app.rag import evaluate_retrieval

RAG_DIRECTORY = Path(__file__).resolve().parents[1] / "rag"
CORPUS_PATH = RAG_DIRECTORY / "corpus" / "merchant_reviews.json"
EVAL_PATH = RAG_DIRECTORY / "eval" / "retrieval_cases.json"
VALID_DIFFICULTIES = {"basic", "hard"}
VALID_CHALLENGE_TYPES = {
    "audience_constraint",
    "budget_constraint",
    "environment_preference",
    "facility_constraint",
    "implicit_intent",
    "multi_constraint",
    "negative_constraint",
    "service_constraint",
    "time_constraint",
}
SHOP_IDS_BY_CATEGORY = {
    "food": {1, 2, 9},
    "coffee": {3, 7, 8},
    "cinema": {4, 10, 11},
    "hotel": {5, 12, 13},
    "fitness": {6, 14, 15},
}


class RagDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reviews = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        cls.cases = json.loads(EVAL_PATH.read_text(encoding="utf-8"))

    def test_review_ids_are_unique(self):
        review_ids = [review["id"] for review in self.reviews]

        self.assertEqual(len(review_ids), len(set(review_ids)))

    def test_each_category_contains_three_shops_and_ninety_reviews(self):
        for category, shop_ids in SHOP_IDS_BY_CATEGORY.items():
            category_reviews = [
                review for review in self.reviews if review["shopId"] in shop_ids
            ]
            review_count_by_shop = {
                shop_id: sum(
                    review["shopId"] == shop_id for review in category_reviews
                )
                for shop_id in shop_ids
            }

            with self.subTest(category=category):
                self.assertEqual(len(category_reviews), 90)
                self.assertEqual(set(review_count_by_shop.values()), {30})

    def test_app_paths_do_not_depend_on_working_directory(self):
        self.assertEqual(APP_CORPUS_PATH, CORPUS_PATH)
        self.assertEqual(APP_EVAL_PATH, EVAL_PATH)

    def test_each_case_points_to_existing_evidence(self):
        known_review_ids = {review["id"] for review in self.reviews}
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["question"].strip())
                self.assertTrue(case["expectedPoints"])
                self.assertTrue(case["relevantReviewIds"])
                self.assertTrue(set(case["relevantReviewIds"]).issubset(known_review_ids))

    def test_basic_eval_set_still_covers_original_six_shops(self):
        evidence_ids = {
            review_id
            for case in self.cases
            if case["difficulty"] == "basic"
            for review_id in case["relevantReviewIds"]
        }
        covered_shop_ids = {
            review["shopId"] for review in self.reviews if review["id"] in evidence_ids
        }

        self.assertGreaterEqual(len(self.cases), 12)
        self.assertTrue({1, 2, 3, 4, 5, 6}.issubset(covered_shop_ids))

    def test_hard_eval_set_covers_every_category_with_new_reviews(self):
        review_by_id = {review["id"]: review for review in self.reviews}
        hard_cases = [case for case in self.cases if case["difficulty"] == "hard"]
        hard_evidence_ids = {
            review_id
            for case in hard_cases
            for review_id in case["relevantReviewIds"]
        }
        self.assertEqual(len(hard_cases), 15)
        for category, category_shop_ids in SHOP_IDS_BY_CATEGORY.items():
            category_cases = [
                case
                for case in hard_cases
                if any(
                    review_by_id[review_id]["shopId"] in category_shop_ids
                    for review_id in case["relevantReviewIds"]
                )
            ]
            with self.subTest(category=category):
                self.assertEqual(len(category_cases), 3)
        self.assertTrue(
            hard_evidence_ids.isdisjoint(
                {f"review-{index:03d}" for index in range(1, 13)}
            )
        )

    def test_each_case_has_valid_difficulty_and_challenge_types(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["difficulty"], VALID_DIFFICULTIES)
                self.assertTrue(case["challengeTypes"])
                self.assertTrue(
                    set(case["challengeTypes"]).issubset(VALID_CHALLENGE_TYPES)
                )

    def test_hit_rate_counts_any_expected_evidence(self):
        def fake_retrieve(question: str, limit: int):
            if "火锅" in question:
                return [{"reviewId": "review-001"}]
            return [{"reviewId": "not-a-match"}]

        report = evaluate_retrieval(fake_retrieve, limit=1)

        self.assertEqual(report["topK"], 1)
        self.assertEqual(report["hits"], 1)
        self.assertAlmostEqual(report["hitRate"], 1 / len(self.cases))
        self.assertEqual(report["hitsAt1"], 1)
        self.assertAlmostEqual(report["hitAt1Rate"], 1 / len(self.cases))
        self.assertAlmostEqual(report["mrr"], 1 / len(self.cases))
        self.assertIn("timing", report)
        self.assertGreaterEqual(report["timing"]["totalMs"], 0)
        self.assertGreaterEqual(report["timing"]["avgMs"], 0)
        self.assertGreaterEqual(report["timing"]["p50Ms"], 0)
        self.assertGreaterEqual(report["timing"]["p95Ms"], 0)
        self.assertGreaterEqual(report["details"][0]["durationMs"], 0)
        self.assertEqual(report["details"][0]["difficulty"], "basic")
        self.assertIn("multi_constraint", report["details"][0]["challengeTypes"])
        self.assertEqual(report["byDifficulty"]["basic"]["total"], 12)
        self.assertEqual(report["byDifficulty"]["basic"]["hits"], 1)
        self.assertIn("timing", report["byDifficulty"]["basic"])
        self.assertEqual(report["byDifficulty"]["hard"]["total"], 15)
        self.assertEqual(report["byDifficulty"]["hard"]["hits"], 0)
        multi_constraint_count = sum(
            "multi_constraint" in case["challengeTypes"] for case in self.cases
        )
        self.assertEqual(
            report["byChallengeType"]["multi_constraint"]["total"],
            multi_constraint_count,
        )
        self.assertEqual(
            report["byChallengeType"]["multi_constraint"]["hits"], 1
        )

    def test_hit_at_1_and_mrr_expose_relevant_review_at_second_place(self):
        def fake_retrieve(question: str, limit: int):
            if "火锅" in question:
                return [
                    {"reviewId": "distractor"},
                    {"reviewId": "review-001"},
                ]
            return [{"reviewId": "not-a-match"}]

        report = evaluate_retrieval(fake_retrieve, limit=3)

        self.assertEqual(report["hits"], 1)
        self.assertEqual(report["hitsAt1"], 0)
        self.assertAlmostEqual(report["mrr"], 0.5 / len(self.cases))
        self.assertEqual(report["details"][0]["firstRelevantRank"], 2)
        self.assertEqual(report["details"][0]["reciprocalRank"], 0.5)


if __name__ == "__main__":
    unittest.main()
