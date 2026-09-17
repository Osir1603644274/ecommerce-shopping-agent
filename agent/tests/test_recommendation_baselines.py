import unittest

from recommendation.baselines import popular_item_scores, popular_recommendations


class RecommendationBaselineTests(unittest.TestCase):
    def test_popular_item_scores_counts_positive_interactions(self):
        interactions = [
            {"itemId": 3, "rating": 5.0},
            {"itemId": 3, "rating": 4.0},
            {"itemId": 7, "rating": 4.5},
            {"itemId": 11, "rating": 2.0},
        ]

        result = popular_item_scores(interactions)

        self.assertEqual(result, {3: 2.0, 7: 1.0})

    def test_popular_recommendations_counts_positive_interactions(self):
        interactions = [
            {"itemId": 3, "rating": 5.0},
            {"itemId": 3, "rating": 4.0},
            {"itemId": 7, "rating": 4.5},
            {"itemId": 11, "rating": 2.0},
        ]

        result = popular_recommendations(interactions, limit=3)

        self.assertEqual(result, [3, 7])

    def test_popular_recommendations_uses_item_id_as_tie_breaker(self):
        interactions = [
            {"itemId": 9, "rating": 5.0},
            {"itemId": 3, "rating": 5.0},
        ]

        result = popular_recommendations(interactions, limit=2)

        self.assertEqual(result, [3, 9])


if __name__ == "__main__":
    unittest.main()