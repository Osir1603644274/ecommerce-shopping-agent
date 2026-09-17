import json
import unittest

from evaluation.capability_router_evaluation import (
    evaluate_capability_router_cases,
    load_capability_router_cases,
)
from app.schemas import ToolTrace


def _turn_with_tool(name: str, arguments: dict) -> list[dict]:
    return [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        },
    ]


class CapabilityRouterEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_dataset_has_isolated_validation_and_test_cases(self):
        cases = load_capability_router_cases()
        validation_ids = {case["id"] for case in cases if case["split"] == "validation"}
        test_ids = {case["id"] for case in cases if case["split"] == "test"}

        self.assertEqual(len(validation_ids), 6)
        self.assertEqual(len(test_ids), 6)
        self.assertTrue(validation_ids.isdisjoint(test_ids))

    async def test_scores_named_shop_selection_arguments_and_review_purity(self):
        cases = [
            {
                "id": "named",
                "split": "validation",
                "category": "named_experience",
                "question": "Red Hook适合办公吗？",
                "expectedTools": ["search_shop_reviews"],
                "expectedShopName": "Red Hook Coffee & Tea",
                "expectedShopId": 7,
                "expectedResolutionStatus": "resolved",
            }
        ]

        async def runner(_question):
            trace = ToolTrace(
                tool="search_shop_reviews",
                ok=True,
                detail={
                    "resolutionStatus": "resolved",
                    "resolvedShop": {"id": 7, "name": "Red Hook Coffee & Tea"},
                    "reviews": [{"reviewId": "review-1", "shopId": 7}],
                },
            )
            return (
                "适合办公。[review-1]",
                [trace],
                _turn_with_tool(
                    "search_shop_reviews",
                    {
                        "query": "Red Hook适合办公吗？",
                        "shopName": "Red Hook Coffee & Tea",
                    },
                ),
            )

        report = await evaluate_capability_router_cases(cases, runner=runner)

        self.assertEqual(report["completePasses"], 1)
        self.assertEqual(report["metrics"]["toolSelection"]["accuracy"], 1.0)
        self.assertEqual(report["metrics"]["argumentExtraction"]["accuracy"], 1.0)
        self.assertEqual(report["metrics"]["reviewPurity"]["accuracy"], 1.0)
        self.assertEqual(report["fallbackCount"], 0)

    async def test_fallback_execution_does_not_count_as_model_selection(self):
        cases = [
            {
                "id": "generic",
                "split": "validation",
                "category": "generic_experience",
                "question": "找个安静的咖啡店",
                "expectedTools": ["search_knowledge"],
                "expectedSources": ["reviews"],
            }
        ]

        async def runner(_question):
            trace = ToolTrace(
                tool="search_knowledge",
                ok=True,
                detail={"sources": ["reviews"], "citations": []},
            )
            return "暂时没有结果", [trace], [{"role": "user", "content": _question}]

        report = await evaluate_capability_router_cases(cases, runner=runner)

        self.assertEqual(report["completePasses"], 0)
        self.assertEqual(report["fallbackCount"], 1)
        self.assertFalse(report["details"][0]["toolSelectionCorrect"])
        self.assertTrue(report["details"][0]["sourcesCorrect"])

    async def test_scores_ambiguity_clarification(self):
        cases = [
            {
                "id": "ambiguous",
                "split": "validation",
                "category": "ambiguous_named_experience",
                "question": "Starbucks适合办公吗？",
                "expectedTools": ["search_shop_reviews"],
                "expectedShopName": "Starbucks",
                "expectedResolutionStatus": "multiple_exact_matches",
                "expectedClarificationAny": ["哪一家", "确认"],
            }
        ]

        async def runner(_question):
            trace = ToolTrace(
                tool="search_shop_reviews",
                ok=False,
                detail={
                    "resolutionStatus": "multiple_exact_matches",
                    "candidates": [{"id": 1}, {"id": 2}],
                },
            )
            return (
                "请确认你问的是哪一家Starbucks。",
                [trace],
                _turn_with_tool(
                    "search_shop_reviews",
                    {"query": _question, "shopName": "Starbucks"},
                ),
            )

        report = await evaluate_capability_router_cases(cases, runner=runner)

        self.assertEqual(report["completePasses"], 1)
        self.assertEqual(report["metrics"]["clarification"]["accuracy"], 1.0)


if __name__ == "__main__":
    unittest.main()
