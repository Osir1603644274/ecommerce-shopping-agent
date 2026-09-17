import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.rag import (
    search_reviews_aggregated_by_shop,
    search_reviews_grouped_by_shop,
)


class FakeVector:
    def tolist(self):
        return [0.1, 0.2]


def scored_review(review_id, shop_id, shop_name, score):
    return SimpleNamespace(
        score=score,
        payload={
            "reviewId": review_id,
            "shopId": shop_id,
            "shopName": shop_name,
            "text": f"evidence {review_id}",
        },
    )


class RagShopAggregationTests(unittest.TestCase):
    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    def test_groups_reviews_and_ranks_by_mean_evidence_score(
        self,
        model_mock,
        client_mock,
    ):
        model_mock.return_value.embed.return_value = iter([FakeVector()])
        client_mock.return_value.query_points_groups.return_value.groups = [
            SimpleNamespace(
                id=9,
                hits=[
                    scored_review("r9-high", 9, "Shop 9", 0.9),
                    scored_review("r9-low", 9, "Shop 9", 0.1),
                ],
            ),
            SimpleNamespace(
                id=7,
                hits=[
                    scored_review("r7-a", 7, "Shop 7", 0.8),
                    scored_review("r7-b", 7, "Shop 7", 0.6),
                ],
            ),
        ]

        groups = search_reviews_grouped_by_shop(
            "哪家更安静？",
            [7, 9, 11, 7],
            reviews_per_shop=2,
            source="yelp",
        )

        self.assertEqual([group["shopId"] for group in groups], [7, 9, 11])
        self.assertAlmostEqual(groups[0]["aggregateScore"], 0.7)
        self.assertEqual(groups[0]["evidenceCount"], 2)
        self.assertEqual(groups[1]["maxScore"], 0.9)
        self.assertIsNone(groups[2]["aggregateScore"])
        self.assertEqual(groups[2]["reviews"], [])

        call = client_mock.return_value.query_points_groups.call_args.kwargs
        self.assertEqual(call["group_by"], "shopId")
        self.assertEqual(call["limit"], 3)
        self.assertEqual(call["group_size"], 2)
        conditions = {condition.key: condition for condition in call["query_filter"].must}
        self.assertEqual(conditions["source"].match.value, "yelp")
        self.assertEqual(conditions["shopId"].match.any, [7, 9, 11])

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    def test_empty_candidate_set_skips_embedding_and_qdrant(
        self,
        model_mock,
        client_mock,
    ):
        self.assertEqual(search_reviews_grouped_by_shop("query", []), [])
        model_mock.assert_not_called()
        client_mock.assert_not_called()

    def test_rejects_non_positive_reviews_per_shop(self):
        with self.assertRaisesRegex(ValueError, "reviews_per_shop must be positive"):
            search_reviews_grouped_by_shop("query", [7], reviews_per_shop=0)

    @patch("app.rag.get_qdrant_client")
    @patch("app.rag.get_embedding_model")
    def test_rejects_group_outside_candidate_set(self, model_mock, client_mock):
        model_mock.return_value.embed.return_value = iter([FakeVector()])
        client_mock.return_value.query_points_groups.return_value.groups = [
            SimpleNamespace(
                id=99,
                hits=[scored_review("unexpected", 99, "Unexpected", 0.8)],
            )
        ]

        with self.assertRaisesRegex(ValueError, "unexpected shopId 99"):
            search_reviews_grouped_by_shop("query", [7])

    @patch("app.rag.search_reviews")
    def test_global_top_k_baseline_keeps_missing_candidate_as_unknown(
        self,
        search_mock,
    ):
        search_mock.return_value = [
            {
                "reviewId": "r7",
                "shopId": 7,
                "shopName": "Shop 7",
                "text": "relevant",
                "score": 0.8,
            },
            {
                "reviewId": "r9",
                "shopId": 9,
                "shopName": "Shop 9",
                "text": "relevant",
                "score": 0.7,
            },
        ]

        groups = search_reviews_aggregated_by_shop(
            "query",
            [7, 9, 11],
            evidence_budget=6,
            evidence_per_shop=2,
            source="yelp",
        )

        self.assertEqual([group["shopId"] for group in groups], [7, 9, 11])
        self.assertEqual(groups[2]["evidenceCount"], 0)
        self.assertIsNone(groups[2]["aggregateScore"])
        search_mock.assert_called_once_with(
            "query",
            6,
            source="yelp",
            shop_ids=[7, 9, 11],
        )


if __name__ == "__main__":
    unittest.main()
