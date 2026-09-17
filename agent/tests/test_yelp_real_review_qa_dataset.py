import unittest

from app.rag_real_qa import load_yelp_real_review_qa_cases


class YelpRealReviewQADatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = load_yelp_real_review_qa_cases()

    def test_qa_cases_match_existing_real_review_cases(self):
        self.assertEqual(len(self.cases), 25)
        self.assertTrue(all(case["id"].startswith("yelp-rag-") for case in self.cases))
        self.assertTrue(all(case["source"] == "yelp" for case in self.cases))

    def test_each_case_keeps_real_evidence_and_generated_answer(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertTrue(case["evidenceText"].strip())
                self.assertTrue(case["relevantReviewIds"])
                self.assertTrue(case["expectedPoints"])
                self.assertTrue(case["expectedAnswer"].strip())
                self.assertEqual(
                    case["answerSource"],
                    "generated_from_real_yelp_evidence",
                )

    def test_expected_answers_cite_the_real_review_id(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                for review_id in case["relevantReviewIds"]:
                    self.assertIn(review_id, case["expectedAnswer"])

    def test_expected_answers_are_not_raw_evidence_copies(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                self.assertNotEqual(case["expectedAnswer"], case["evidenceText"])


if __name__ == "__main__":
    unittest.main()
