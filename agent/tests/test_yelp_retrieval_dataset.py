import json
import unittest

from app.rag import YELP_EVAL_PATH, evaluate_retrieval_cases
from recommendation.yelp_content_zh_translations import (
    YELP_CONTENT_ZH_TRANSLATIONS,
)


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


class YelpRetrievalDatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(YELP_EVAL_PATH.read_text(encoding="utf-8"))

    def test_yelp_eval_has_enough_real_cases(self):
        self.assertGreaterEqual(len(self.cases), 20)

    def test_each_case_points_to_translated_yelp_evidence(self):
        known_review_ids = set(YELP_CONTENT_ZH_TRANSLATIONS)
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["id"].startswith("yelp-rag-"))
                self.assertEqual(case["source"], "yelp")
                self.assertTrue(case["question"].strip())
                self.assertTrue(case["expectedPoints"])
                self.assertTrue(case["relevantReviewIds"])
                self.assertTrue(
                    set(case["relevantReviewIds"]).issubset(known_review_ids)
                )

    def test_evidence_text_matches_full_translation(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertEqual(len(case["relevantReviewIds"]), 1)
                review_id = case["relevantReviewIds"][0]
                self.assertEqual(
                    case["evidenceText"],
                    YELP_CONTENT_ZH_TRANSLATIONS[review_id],
                )

    def test_each_case_has_valid_difficulty_and_challenge_types(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertIn(case["difficulty"], VALID_DIFFICULTIES)
                self.assertTrue(case["challengeTypes"])
                self.assertTrue(
                    set(case["challengeTypes"]).issubset(VALID_CHALLENGE_TYPES)
                )

    def test_evaluate_retrieval_cases_can_score_yelp_cases(self):
        first_case = self.cases[0]

        def fake_retrieve(question: str, limit: int):
            if question == first_case["question"]:
                return [{"reviewId": first_case["relevantReviewIds"][0]}]
            return [{"reviewId": "not-a-match"}]

        report = evaluate_retrieval_cases(self.cases, fake_retrieve, limit=1)

        self.assertEqual(report["topK"], 1)
        self.assertEqual(report["hits"], 1)
        self.assertEqual(report["hitsAt1"], 1)
        self.assertAlmostEqual(report["hitRate"], 1 / len(self.cases))
        self.assertAlmostEqual(report["mrr"], 1 / len(self.cases))


if __name__ == "__main__":
    unittest.main()
