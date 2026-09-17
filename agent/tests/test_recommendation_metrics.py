import unittest

from recommendation.metrics import (
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    relevance_from_target_interactions,
)


class RecommendationMetricsTests(unittest.TestCase):
    def test_precision_recall_and_reciprocal_rank(self):
        recommended = [3, 11, 8, 7, 5]
        relevant = [7, 11]

        self.assertAlmostEqual(precision_at_k(recommended, relevant, 3), 1 / 3)
        self.assertAlmostEqual(recall_at_k(recommended, relevant, 3), 1 / 2)
        self.assertAlmostEqual(reciprocal_rank(recommended, relevant), 1 / 2)

    def test_ndcg_uses_rank_and_graded_relevance(self):
        relevance = {7: 3.0, 11: 5.0}

        better = ndcg_at_k([11, 7, 3], relevance, 3)
        worse = ndcg_at_k([7, 11, 3], relevance, 3)

        self.assertGreater(better, worse)
        self.assertAlmostEqual(better, 1.0)

    def test_relevance_from_target_interactions_keeps_strongest_score(self):
        relevance = relevance_from_target_interactions(
            [
                {"itemId": 7, "score": 3},
                {"itemId": 7, "score": 5},
                {"itemId": 11, "score": 4},
            ]
        )

        self.assertEqual(relevance, {7: 5.0, 11: 4.0})


if __name__ == "__main__":
    unittest.main()