import unittest

from recommendation.evaluation import (
    evaluate_hybrid,
    evaluate_hybrid_weight_grid,
    evaluate_strict_hybrid_experiment,
    evaluate_strict_three_way_hybrid_experiment,
    evaluate_itemcf,
    evaluate_popular_baseline,
    evaluate_three_way_hybrid,
    evaluate_three_way_hybrid_weight_grid,
)


class RecommendationEvaluationTests(unittest.TestCase):
    def test_evaluate_popular_baseline_filters_seen_items_and_summarizes_metrics(self):
        cases = [
            {
                "caseId": "case-1",
                "userId": "user-1",
                "history": [
                    {"itemId": 1, "rating": 5.0},
                    {"itemId": 2, "rating": 5.0},
                ],
                "targetInteractions": [{"itemId": 3, "score": 5}],
                "relevantItemIds": [3],
            },
            {
                "caseId": "case-2",
                "userId": "user-2",
                "history": [
                    {"itemId": 3, "rating": 5.0},
                    {"itemId": 3, "rating": 4.0},
                    {"itemId": 4, "rating": 5.0},
                ],
                "targetInteractions": [{"itemId": 1, "score": 4}],
                "relevantItemIds": [1],
            },
        ]

        report = evaluate_popular_baseline(cases, top_k=2, candidate_limit=10)

        self.assertEqual(report["baseline"], "global_popular")
        self.assertTrue(report["excludedSeenItems"])
        self.assertEqual(report["caseCount"], 2)
        self.assertEqual(report["details"][0]["recommendedItemIds"], [3, 4])
        self.assertEqual(report["details"][1]["recommendedItemIds"], [1, 2])
        self.assertAlmostEqual(report["metrics"]["hitRateAtK"], 1.0)
        self.assertAlmostEqual(report["metrics"]["precisionAtK"], 0.5)
        self.assertAlmostEqual(report["metrics"]["recallAtK"], 1.0)
        self.assertAlmostEqual(report["metrics"]["mrr"], 1.0)
        self.assertAlmostEqual(report["metrics"]["ndcgAtK"], 1.0)

    def test_evaluate_itemcf_scores_from_similarity_table(self):
        cases = [
            {
                "caseId": "case-1",
                "userId": "user-1",
                "history": [
                    {"itemId": 1, "rating": 5.0},
                    {"itemId": 2, "rating": 4.0},
                ],
                "targetInteractions": [{"itemId": 3, "score": 5}],
                "relevantItemIds": [3],
            },
            {
                "caseId": "case-2",
                "userId": "user-2",
                "history": [{"itemId": 4, "rating": 5.0}],
                "targetInteractions": [{"itemId": 2, "score": 4}],
                "relevantItemIds": [2],
            },
        ]
        similarity_table = {
            "algorithm": "test_itemcf",
            "similarityTable": {
                "1": [
                    {"itemId": 3, "similarity": 0.8},
                    {"itemId": 2, "similarity": 1.0},
                ],
                "2": [{"itemId": 4, "similarity": 0.7}],
                "4": [{"itemId": 2, "similarity": 0.9}],
            },
        }

        report = evaluate_itemcf(cases, similarity_table, top_k=2)

        self.assertEqual(report["baseline"], "itemcf")
        self.assertEqual(report["similarityAlgorithm"], "test_itemcf")
        self.assertTrue(report["excludedSeenItems"])
        self.assertEqual(report["caseCount"], 2)
        self.assertEqual(report["details"][0]["recommendedItemIds"], [3, 4])
        self.assertEqual(report["details"][1]["recommendedItemIds"], [2])
        self.assertAlmostEqual(report["metrics"]["hitRateAtK"], 1.0)
        self.assertAlmostEqual(report["metrics"]["precisionAtK"], 0.5)
        self.assertAlmostEqual(report["metrics"]["recallAtK"], 1.0)
        self.assertAlmostEqual(report["metrics"]["mrr"], 1.0)
        self.assertAlmostEqual(report["metrics"]["ndcgAtK"], 1.0)

    def test_evaluate_hybrid_combines_global_popular_and_itemcf(self):
        cases = [
            {
                "caseId": "case-1",
                "userId": "user-1",
                "history": [
                    {"itemId": 1, "rating": 5.0},
                    {"itemId": 2, "rating": 5.0},
                ],
                "targetInteractions": [{"itemId": 3, "score": 5}],
                "relevantItemIds": [3],
            },
            {
                "caseId": "case-2",
                "userId": "user-2",
                "history": [
                    {"itemId": 3, "rating": 5.0},
                    {"itemId": 4, "rating": 5.0},
                ],
                "targetInteractions": [{"itemId": 1, "score": 4}],
                "relevantItemIds": [1],
            },
        ]
        similarity_table = {
            "algorithm": "test_itemcf",
            "similarityTable": {
                "1": [{"itemId": 3, "similarity": 0.9}],
                "2": [{"itemId": 4, "similarity": 0.5}],
                "3": [{"itemId": 1, "similarity": 0.9}],
                "4": [{"itemId": 2, "similarity": 0.5}],
            },
        }

        report = evaluate_hybrid(cases, similarity_table, top_k=2)

        self.assertEqual(report["baseline"], "popular_itemcf_hybrid")
        self.assertEqual(report["similarityAlgorithm"], "test_itemcf")
        self.assertEqual(report["popularWeight"], 1.0)
        self.assertEqual(report["itemcfWeight"], 1.0)
        self.assertEqual(report["caseCount"], 2)
        self.assertEqual(report["details"][0]["recommendedItemIds"], [3, 4])
        self.assertEqual(report["details"][1]["recommendedItemIds"], [1, 2])
        self.assertAlmostEqual(report["metrics"]["hitRateAtK"], 1.0)


    def test_evaluate_three_way_hybrid_adds_type_preference_signal(self):
        cases = [
            {
                "caseId": "case-1",
                "userId": "user-1",
                "history": [{"itemId": 1, "typeId": 10, "rating": 5.0}],
                "targetInteractions": [{"itemId": 2, "score": 5}],
                "relevantItemIds": [2],
            }
        ]
        similarity_table = {"algorithm": "test_itemcf", "similarityTable": {}}
        items = [
            {"itemId": 1, "typeId": 10},
            {"itemId": 2, "typeId": 10},
            {"itemId": 3, "typeId": 20},
        ]
        popular_scores = {2: 1.0, 3: 100.0}

        report = evaluate_three_way_hybrid(
            cases,
            similarity_table,
            items,
            popular_weight=0.1,
            itemcf_weight=0.0,
            type_weight=1.0,
            popular_scores=popular_scores,
            top_k=2,
        )

        self.assertEqual(report["baseline"], "popular_itemcf_type_hybrid")
        self.assertEqual(report["typeWeight"], 1.0)
        self.assertEqual(report["details"][0]["recommendedItemIds"], [2, 3])
        self.assertAlmostEqual(report["metrics"]["hitRateAtK"], 1.0)


    def test_evaluate_hybrid_weight_grid_selects_best_result(self):
        cases = [
            {
                "caseId": "case-1",
                "userId": "user-1",
                "history": [
                    {"itemId": 1, "rating": 5.0},
                    {"itemId": 2, "rating": 5.0},
                ],
                "targetInteractions": [{"itemId": 3, "score": 5}],
                "relevantItemIds": [3],
            }
        ]
        similarity_table = {
            "similarityTable": {
                "1": [{"itemId": 3, "similarity": 0.9}],
                "2": [{"itemId": 4, "similarity": 0.5}],
            }
        }

        report = evaluate_hybrid_weight_grid(
            cases,
            similarity_table,
            popular_weights=(0.5, 1.0),
            itemcf_weights=(0.5, 1.0),
            top_k=2,
        )

        self.assertEqual(report["experiment"], "hybrid_weight_grid")
        self.assertEqual(report["caseCount"], 1)
        self.assertEqual(len(report["results"]), 4)
        self.assertIsNotNone(report["bestResult"])
        self.assertEqual(report["bestResult"]["metrics"]["hitRateAtK"], 1.0)


    def test_evaluate_three_way_hybrid_weight_grid_selects_best_result(self):
        cases = [
            {
                "caseId": "case-1",
                "userId": "user-1",
                "history": [{"itemId": 1, "typeId": 10, "rating": 5.0}],
                "targetInteractions": [{"itemId": 2, "score": 5}],
                "relevantItemIds": [2],
            }
        ]
        similarity_table = {"similarityTable": {}}
        items = [
            {"itemId": 1, "typeId": 10},
            {"itemId": 2, "typeId": 10},
            {"itemId": 3, "typeId": 20},
        ]

        report = evaluate_three_way_hybrid_weight_grid(
            cases,
            similarity_table,
            items,
            popular_weights=(0.0,),
            itemcf_weights=(0.0,),
            type_weights=(0.5, 1.0),
            top_k=2,
        )

        self.assertEqual(report["experiment"], "three_way_hybrid_weight_grid")
        self.assertEqual(report["caseCount"], 1)
        self.assertEqual(len(report["results"]), 2)
        self.assertEqual(report["bestResult"]["metrics"]["hitRateAtK"], 1.0)


    def test_evaluate_strict_hybrid_experiment_uses_validation_for_weight_selection(self):
        def case(case_id, user_id, history_ids, target_id):
            return {
                "caseId": case_id,
                "userId": user_id,
                "history": [
                    {"itemId": item_id, "rating": 5.0, "score": 5}
                    for item_id in history_ids
                ],
                "targetInteractions": [{"itemId": target_id, "score": 5}],
                "relevantItemIds": [target_id],
            }

        case_splits = {
            "splitMethod": "unit_test_split",
            "randomSeed": 1,
            "counts": {"train": 3, "validation": 1, "test": 1, "total": 5},
            "train": [
                case("train-1", "u1", [1, 2], 3),
                case("train-2", "u2", [1, 3], 4),
                case("train-3", "u3", [2, 4], 5),
            ],
            "validation": [case("validation-1", "u4", [1], 3)],
            "test": [case("test-1", "u5", [1], 3)],
        }

        report = evaluate_strict_hybrid_experiment(
            case_splits,
            popular_weights=(0.5, 1.0),
            itemcf_weights=(1.0,),
            top_k=2,
        )

        self.assertEqual(report["experiment"], "strict_train_validation_test_hybrid")
        self.assertEqual(report["splitCounts"], case_splits["counts"])
        self.assertEqual(report["weightSelection"]["split"], "validation")
        self.assertIn(report["weightSelection"]["bestPopularWeight"], (0.5, 1.0))
        self.assertIn("hybrid", report["test"])
        self.assertEqual(report["test"]["hybrid"]["caseCount"], 1)

    def test_evaluate_strict_three_way_hybrid_experiment_uses_validation_for_weight_selection(self):
        def case(case_id, user_id, history_items, target_id):
            return {
                "caseId": case_id,
                "userId": user_id,
                "history": [
                    {"itemId": item_id, "typeId": type_id, "rating": 5.0, "score": 5}
                    for item_id, type_id in history_items
                ],
                "targetInteractions": [{"itemId": target_id, "score": 5}],
                "relevantItemIds": [target_id],
            }

        case_splits = {
            "splitMethod": "unit_test_split",
            "randomSeed": 1,
            "counts": {"train": 3, "validation": 1, "test": 1, "total": 5},
            "train": [
                case("train-1", "u1", [(1, 10), (2, 10)], 3),
                case("train-2", "u2", [(1, 10), (3, 20)], 4),
                case("train-3", "u3", [(2, 10), (4, 20)], 5),
            ],
            "validation": [case("validation-1", "u4", [(1, 10)], 2)],
            "test": [case("test-1", "u5", [(1, 10)], 2)],
        }
        items = [
            {"itemId": 1, "typeId": 10},
            {"itemId": 2, "typeId": 10},
            {"itemId": 3, "typeId": 20},
            {"itemId": 4, "typeId": 20},
            {"itemId": 5, "typeId": 20},
        ]

        report = evaluate_strict_three_way_hybrid_experiment(
            case_splits,
            items,
            popular_weights=(0.0,),
            itemcf_weights=(0.0,),
            type_weights=(1.0,),
            top_k=2,
        )

        self.assertEqual(report["experiment"], "strict_train_validation_test_three_way_hybrid")
        self.assertEqual(report["splitCounts"], case_splits["counts"])
        self.assertEqual(report["weightSelection"]["bestTypeWeight"], 1.0)
        self.assertIn("threeWayHybrid", report["test"])
        self.assertEqual(report["test"]["threeWayHybrid"]["caseCount"], 1)

    def test_evaluate_popular_baseline_rejects_invalid_top_k(self):
        with self.assertRaises(ValueError):
            evaluate_popular_baseline([], top_k=0)

    def test_evaluate_itemcf_rejects_invalid_top_k(self):
        with self.assertRaises(ValueError):
            evaluate_itemcf([], {}, top_k=0)

    def test_evaluate_hybrid_rejects_invalid_top_k(self):
        with self.assertRaises(ValueError):
            evaluate_hybrid([], {}, top_k=0)


if __name__ == "__main__":
    unittest.main()
