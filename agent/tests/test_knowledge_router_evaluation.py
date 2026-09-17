import unittest

from evaluation.knowledge_router_evaluation import evaluate_router, evaluate_router_cases, load_router_cases


class KnowledgeRouterEvaluationTests(unittest.TestCase):
    def test_evaluates_router_cases(self):
        cases = [
            {
                "id": "review",
                "question": "哪家店实际比较安静，适合办公？",
                "expectedSources": ["reviews"],
                "category": "reviews",
            },
            {
                "id": "merchant",
                "question": "这家店有没有 WiFi？",
                "expectedSources": ["merchant_docs"],
                "category": "merchant_docs",
            },
            {
                "id": "mixed",
                "question": "这家店有 WiFi，而且实际适合办公吗？",
                "expectedSources": ["merchant_docs", "reviews"],
                "category": "merchant_reviews",
            },
        ]

        report = evaluate_router_cases(cases)

        self.assertEqual(report["caseCount"], 3)
        self.assertEqual(report["exactMatches"], 3)
        self.assertEqual(report["accuracy"], 1.0)
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["byCategory"]["merchant_reviews"]["accuracy"], 1.0)

    def test_router_eval_dataset_is_currently_passing(self):
        cases = load_router_cases()
        report = evaluate_router()

        self.assertGreaterEqual(len(cases), 30)
        self.assertEqual(report["caseCount"], len(cases))
        self.assertEqual(report["failures"], [])
        self.assertEqual(report["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
