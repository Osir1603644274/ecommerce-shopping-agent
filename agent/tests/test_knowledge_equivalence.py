import unittest

from app.knowledge_equivalence import compare_review_retrievers


class KnowledgeEquivalenceTests(unittest.TestCase):
    def test_compare_review_retrievers_counts_exact_match_and_regression(self):
        cases = [
            {
                "id": "case-1",
                "question": "适合办公？",
                "relevantReviewIds": ["review-1"],
            },
            {
                "id": "case-2",
                "question": "适合约会？",
                "relevantReviewIds": ["review-2"],
            },
        ]

        def baseline(question: str, limit: int):
            if "办公" in question:
                return [{"reviewId": "review-1"}]
            return [{"reviewId": "review-2"}]

        def candidate(question: str, limit: int):
            if "办公" in question:
                return [{"reviewId": "review-1"}]
            return [{"reviewId": "wrong"}]

        report = compare_review_retrievers(
            cases,
            baseline=baseline,
            candidate=candidate,
            limit=1,
        )

        self.assertEqual(report["total"], 2)
        self.assertEqual(report["exactMatches"], 1)
        self.assertEqual(report["sameHitCount"], 1)
        self.assertEqual(report["candidateRegressions"], 1)
        self.assertTrue(report["details"][1]["candidateRegression"])


if __name__ == "__main__":
    unittest.main()
