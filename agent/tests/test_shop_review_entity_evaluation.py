import unittest

from app.schemas import ToolTrace
from evaluation.shop_review_entity_evaluation import (
    build_shop_review_entity_validation_report,
)
from app.shop_entity import resolve_unique_shop


class ShopReviewEntityEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_resolver_prefers_unique_exact_match(self):
        resolved, error = resolve_unique_shop(
            [
                {"id": 1, "name": "Red Hook Bakery"},
                {"id": 2, "name": "Red Hook Coffee & Tea"},
            ],
            "red hook coffee & tea",
        )

        self.assertEqual(resolved["id"], 2)
        self.assertIsNone(error)

    def test_resolver_rejects_ambiguous_partial_matches(self):
        resolved, error = resolve_unique_shop(
            [
                {"id": 1, "name": "Starbucks A"},
                {"id": 2, "name": "Starbucks B"},
            ],
            "Starbucks",
        )

        self.assertIsNone(resolved)
        self.assertEqual(error, "ambiguous_partial_matches")

    async def test_validation_chain_resolves_before_review_search(self):
        cases = [
            {
                "id": "validation",
                "split": "validation",
                "category": "reviews",
                "question": "Red Hook适合办公吗？",
                "expectedSources": ["reviews"],
                "shopEntity": {"name": "Red Hook Coffee & Tea"},
                "reviewFilter": {"source": "yelp", "shopId": 7},
                "relevantChunkIds": ["review:target"],
            },
            {
                "id": "test",
                "split": "test",
                "category": "reviews",
                "question": "must not run",
                "expectedSources": ["reviews"],
                "relevantChunkIds": ["review:test"],
            },
        ]
        calls = []

        async def find_shops(type_id, name):
            calls.append(("shops", type_id, name))
            return ToolTrace(
                tool="search_shops",
                ok=True,
                detail={
                    "shops": [
                        {"id": 7, "name": "Red Hook Coffee & Tea"},
                    ]
                },
            )

        async def find_reviews(query, shop_id):
            calls.append(("reviews", query, shop_id))
            return ToolTrace(
                tool="search_reviews",
                ok=True,
                detail={
                    "reviews": [
                        {"reviewId": "target", "shopId": 7},
                    ]
                },
            )

        report = await build_shop_review_entity_validation_report(
            cases,
            find_shops=find_shops,
            find_reviews=find_reviews,
        )

        self.assertEqual(report["caseCount"], 1)
        self.assertEqual(report["entityResolution"]["correct"], 1)
        self.assertEqual(report["reviewRetrieval"]["hits"], 1)
        self.assertEqual(
            calls,
            [
                ("shops", None, "Red Hook Coffee & Tea"),
                ("reviews", "Red Hook适合办公吗？", 7),
            ],
        )


if __name__ == "__main__":
    unittest.main()
