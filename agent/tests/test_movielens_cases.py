import unittest

from recommendation.movielens import build_cases, split_cases


class MovieLensCasesTests(unittest.TestCase):
    def test_build_cases_splits_history_and_targets_by_time(self):
        ratings = [
            {"userId": 1, "movieId": 10, "rating": 3.0, "action": "neutral_rating", "score": 2, "timestamp": 1},
            {"userId": 1, "movieId": 11, "rating": 4.0, "action": "positive_rating", "score": 4, "timestamp": 2},
            {"userId": 1, "movieId": 12, "rating": 2.0, "action": "low_rating", "score": 1, "timestamp": 3},
            {"userId": 1, "movieId": 13, "rating": 5.0, "action": "positive_rating", "score": 5, "timestamp": 4},
            {"userId": 1, "movieId": 14, "rating": 4.5, "action": "positive_rating", "score": 5, "timestamp": 5},
            {"userId": 1, "movieId": 15, "rating": 4.0, "action": "positive_rating", "score": 4, "timestamp": 6},
        ]

        cases = build_cases(
            ratings,
            min_history=3,
            max_history=10,
            target_rating_threshold=4.0,
            max_cases=None,
        )

        self.assertEqual(len(cases), 1)
        self.assertEqual([item["itemId"] for item in cases[0]["history"]], [10, 11, 12, 13])
        self.assertEqual([item["itemId"] for item in cases[0]["targetInteractions"]], [14, 15])
        self.assertEqual(cases[0]["relevantItemIds"], [14, 15])
        self.assertEqual(cases[0]["sourceItemType"], "movie")

    def test_split_cases_creates_deterministic_non_overlapping_partitions(self):
        cases = [{"caseId": f"case-{index}"} for index in range(10)]

        first = split_cases(cases, train_ratio=0.6, validation_ratio=0.2, random_seed=7)
        second = split_cases(cases, train_ratio=0.6, validation_ratio=0.2, random_seed=7)

        self.assertEqual(first, second)
        self.assertEqual(first["counts"], {"train": 6, "validation": 2, "test": 2, "total": 10})
        train_ids = {case["caseId"] for case in first["train"]}
        validation_ids = {case["caseId"] for case in first["validation"]}
        test_ids = {case["caseId"] for case in first["test"]}
        self.assertFalse(train_ids & validation_ids)
        self.assertFalse(train_ids & test_ids)
        self.assertFalse(validation_ids & test_ids)
        self.assertEqual(train_ids | validation_ids | test_ids, {case["caseId"] for case in cases})


if __name__ == "__main__":
    unittest.main()