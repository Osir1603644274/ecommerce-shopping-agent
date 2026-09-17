import unittest

from evaluation.rag_metadata_filter_evaluation import build_metadata_filter_validation_report


class RagMetadataFilterEvaluationTests(unittest.TestCase):
    def test_compares_only_filtered_validation_review_cases(self):
        cases = [
            {
                "id": "validation",
                "split": "validation",
                "category": "reviews",
                "question": "target question",
                "expectedSources": ["reviews"],
                "reviewFilter": {"source": "yelp", "shopId": 7},
                "relevantChunkIds": ["review:target"],
            },
            {
                "id": "test",
                "split": "test",
                "category": "reviews",
                "question": "must not run",
                "expectedSources": ["reviews"],
                "relevantChunkIds": ["review:test-target"],
            },
        ]
        calls = []

        def retrieve(question, limit, *, source=None, shop_id=None):
            calls.append((question, limit, source, shop_id))
            if shop_id is None:
                return [
                    {
                        "reviewId": "other",
                        "shopId": 8,
                    }
                ]
            return [
                {
                    "reviewId": "target",
                    "shopId": 7,
                }
            ]

        report = build_metadata_filter_validation_report(
            cases,
            retrieve=retrieve,
            top_k=3,
        )

        self.assertEqual(report["caseCount"], 1)
        self.assertEqual(report["unfilteredYelp"]["hits"], 0)
        self.assertEqual(report["filteredByShop"]["hits"], 1)
        self.assertEqual(report["delta"]["recoveredCaseIds"], ["validation"])
        self.assertEqual(report["filterViolations"], [])
        self.assertEqual(
            calls,
            [
                ("target question", 3, "yelp", None),
                ("target question", 3, "yelp", 7),
            ],
        )


if __name__ == "__main__":
    unittest.main()
