from __future__ import annotations

import json
import unittest

from app.place_data.agent_evaluation import (
    evaluate_place_agent_cases,
    load_place_agent_cases,
)
from app.schemas import ToolTrace


def _turns(calls: list[tuple[str, dict]]) -> list[dict]:
    messages = [{"role": "user", "content": "question"}]
    for index, (name, arguments) in enumerate(calls, start=1):
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"call-{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            }],
        })
    return messages


class PlaceAgentEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_dataset_has_eight_validation_and_four_unread_test_cases(self):
        cases = load_place_agent_cases()
        validation = {case["id"] for case in cases if case["split"] == "validation"}
        test = {case["id"] for case in cases if case["split"] == "test"}

        self.assertEqual(len(validation), 8)
        self.assertEqual(len(test), 4)
        self.assertTrue(validation.isdisjoint(test))

    async def test_scores_search_selection_arguments_results_and_scope_answer(self):
        cases = [{
            "id": "filter",
            "split": "validation",
            "category": "structured_filter",
            "question": "海淀区有哪些一级综合公园？",
            "expectedToolSequences": [["search_places"]],
            "expectedArguments": [{
                "district": "海淀区", "kind": "park",
                "parkLevel": "一级", "parkType": "综合公园",
            }],
            "expectedSearchTotal": 7,
            "expectedTopIds": ["beijing-park-265"],
            "forbiddenTools": ["search_knowledge"],
            "requiredAnswerAll": ["当前演示目录", "7"],
            "forbiddenAnswer": ["海淀区共有7个"],
        }]

        async def runner(_question):
            trace = ToolTrace(
                tool="search_places",
                ok=True,
                detail={
                    "total": 7,
                    "items": [{"id": "beijing-park-265"}],
                },
            )
            return (
                "当前演示目录中找到7个候选。",
                [trace],
                _turns([("search_places", {
                    "district": "海淀区", "kind": "park",
                    "parkLevel": "一级", "parkType": "综合公园",
                })]),
            )

        report = await evaluate_place_agent_cases(cases, runner=runner)

        self.assertEqual(report["completePasses"], 1)
        self.assertEqual(report["fallbackCount"], 0)
        self.assertEqual(report["metrics"]["argumentExtraction"]["accuracy"], 1.0)
        self.assertEqual(report["metrics"]["answerBoundary"]["accuracy"], 1.0)

    async def test_accepts_declared_search_detail_sequence(self):
        cases = [{
            "id": "detail",
            "split": "validation",
            "category": "search_then_detail",
            "question": "北京世界公园电话？",
            "expectedToolSequences": [["search_places", "get_place_detail"]],
            "expectedArguments": [
                {"query": "北京世界公园"},
                {"placeId": "beijing-park-433"},
            ],
            "expectedDetailId": "beijing-park-433",
        }]

        async def runner(_question):
            return (
                "电话是83613681。",
                [
                    ToolTrace(tool="search_places", ok=True, detail={"items": []}),
                    ToolTrace(
                        tool="get_place_detail",
                        ok=True,
                        detail={"place": {"id": "beijing-park-433"}},
                    ),
                ],
                _turns([
                    ("search_places", {"query": "北京世界公园"}),
                    ("get_place_detail", {"placeId": "beijing-park-433"}),
                ]),
            )

        report = await evaluate_place_agent_cases(cases, runner=runner)

        self.assertEqual(report["completePasses"], 1)
        self.assertEqual(report["metrics"]["detailResult"]["accuracy"], 1.0)

    async def test_fallback_execution_does_not_count_as_model_selection(self):
        cases = [{
            "id": "fallback",
            "split": "validation",
            "category": "empty_scope",
            "question": "平谷区有哪些公园？",
            "expectedToolSequences": [["search_places"]],
        }]

        async def runner(_question):
            return (
                "未找到。",
                [ToolTrace(tool="search_places", ok=True, detail={"total": 0, "items": []})],
                [{"role": "user", "content": _question}],
            )

        report = await evaluate_place_agent_cases(cases, runner=runner)

        self.assertEqual(report["completePasses"], 0)
        self.assertEqual(report["fallbackCount"], 1)
        self.assertFalse(report["details"][0]["selectionCorrect"])


if __name__ == "__main__":
    unittest.main()
