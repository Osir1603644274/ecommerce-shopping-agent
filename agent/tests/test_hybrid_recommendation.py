import unittest

from recommendation.hybrid import (
    build_item_type_lookup,
    hybrid_recommendations,
    three_way_hybrid_recommendations,
    type_preference_candidate_scores,
)


class HybridRecommendationTests(unittest.TestCase):
    def test_hybrid_recommendations_blends_popular_and_itemcf_scores(self):
        history = [
            {"itemId": 1, "rating": 5.0},
            {"itemId": 2, "rating": 3.0},
        ]
        similarity_table = {
            "similarityTable": {
                "1": [
                    {"itemId": 3, "similarity": 0.9},
                    {"itemId": 2, "similarity": 1.0},
                ]
            }
        }
        popular_scores = {
            2: 100.0,
            3: 10.0,
            4: 50.0,
        }

        result = hybrid_recommendations(
            history,
            similarity_table,
            popular_scores,
            limit=2,
        )

        self.assertEqual(result, [3, 4])

    def test_hybrid_recommendations_falls_back_to_popular_when_itemcf_has_no_candidates(self):
        history = [{"itemId": 1, "rating": 5.0}]
        similarity_table = {"similarityTable": {}}
        popular_scores = {
            1: 100.0,
            2: 50.0,
            3: 25.0,
        }

        result = hybrid_recommendations(
            history,
            similarity_table,
            popular_scores,
            limit=2,
        )

        self.assertEqual(result, [2, 3])

    def test_hybrid_recommendations_rejects_invalid_weights(self):
        with self.assertRaises(ValueError):
            hybrid_recommendations([], {}, {}, popular_weight=0, itemcf_weight=0)
        with self.assertRaises(ValueError):
            hybrid_recommendations([], {}, {}, popular_weight=-1)

    def test_type_preference_scores_use_rating_strength_and_exclude_seen_items(self):
        item_type_lookup = {
            1: 10,
            2: 10,
            3: 20,
            4: 20,
            5: 30,
        }
        history = [
            {"itemId": 1, "typeId": 10, "rating": 5.0},
            {"itemId": 3, "typeId": 20, "rating": 4.0},
            {"itemId": 5, "typeId": 30, "rating": 3.0},
        ]

        result = type_preference_candidate_scores(history, item_type_lookup)

        self.assertEqual(result, {2: 1.0, 4: 0.8})

    def test_three_way_hybrid_can_rank_type_preference_candidates(self):
        history = [{"itemId": 1, "typeId": 10, "rating": 5.0}]
        similarity_table = {"similarityTable": {}}
        popular_scores = {2: 1.0, 3: 100.0}
        item_type_lookup = {1: 10, 2: 10, 3: 20}

        result = three_way_hybrid_recommendations(
            history,
            similarity_table,
            popular_scores,
            item_type_lookup,
            popular_weight=0.1,
            itemcf_weight=0,
            type_weight=1.0,
            limit=2,
        )

        self.assertEqual(result, [2, 3])

    def test_build_item_type_lookup_skips_incomplete_items(self):
        result = build_item_type_lookup(
            [
                {"itemId": 1, "typeId": 10},
                {"itemId": 2},
                {"typeId": 20},
            ]
        )

        self.assertEqual(result, {1: 10})


if __name__ == "__main__":
    unittest.main()
