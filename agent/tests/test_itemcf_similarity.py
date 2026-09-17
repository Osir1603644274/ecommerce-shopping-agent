import math
import unittest

from recommendation.itemcf import build_item_similarity_table, itemcf_recommendations


class ItemCFSimilarityTests(unittest.TestCase):
    def test_build_item_similarity_table_counts_cooccurrence_and_normalizes(self):
        cases = [
            {
                "history": [
                    {"itemId": 1, "rating": 5.0},
                    {"itemId": 2, "rating": 4.0},
                    {"itemId": 3, "rating": 3.0},
                ]
            },
            {
                "history": [
                    {"itemId": 1, "rating": 5.0},
                    {"itemId": 2, "rating": 4.5},
                    {"itemId": 4, "rating": 5.0},
                ]
            },
            {
                "history": [
                    {"itemId": 1, "rating": 5.0},
                    {"itemId": 4, "rating": 5.0},
                ]
            },
        ]

        table = build_item_similarity_table(cases, top_n=3)

        self.assertEqual(table["caseCount"], 3)
        self.assertEqual(table["contributingCaseCount"], 3)
        self.assertEqual(table["itemCount"], 3)

        item_1_neighbors = table["similarityTable"]["1"]
        self.assertEqual([item["itemId"] for item in item_1_neighbors], [2, 4])
        self.assertEqual(item_1_neighbors[0]["cooccurrence"], 2)
        self.assertAlmostEqual(item_1_neighbors[0]["similarity"], 2 / math.sqrt(3 * 2))
        self.assertEqual(item_1_neighbors[1]["cooccurrence"], 2)
        self.assertAlmostEqual(item_1_neighbors[1]["similarity"], 2 / math.sqrt(3 * 2))

    def test_itemcf_recommendations_sums_similarity_and_filters_seen_items(self):
        history = [
            {"itemId": 1, "rating": 5.0},
            {"itemId": 2, "rating": 4.0},
            {"itemId": 5, "rating": 2.0},
        ]
        similarity_table = {
            "similarityTable": {
                "1": [
                    {"itemId": 3, "similarity": 0.9},
                    {"itemId": 2, "similarity": 1.0},
                ],
                "2": [
                    {"itemId": 3, "similarity": 0.4},
                    {"itemId": 4, "similarity": 0.7},
                ],
            }
        }

        result = itemcf_recommendations(history, similarity_table, limit=2)

        self.assertEqual(result, [3, 4])

    def test_build_item_similarity_table_rejects_invalid_top_n(self):
        with self.assertRaises(ValueError):
            build_item_similarity_table([], top_n=0)

    def test_itemcf_recommendations_rejects_invalid_limit(self):
        with self.assertRaises(ValueError):
            itemcf_recommendations([], {}, limit=0)


if __name__ == "__main__":
    unittest.main()