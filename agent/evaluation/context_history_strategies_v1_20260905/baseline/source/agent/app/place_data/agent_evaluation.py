from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..llm import run_agent
from ..schemas import ToolTrace


DEFAULT_PLACE_AGENT_CASES_PATH = (
    Path(__file__).resolve().parents[2]
    / "place_data"
    / "eval"
    / "place_agent_v1_cases.json"
)
AgentRunner = Callable[
    [str],
    Awaitable[tuple[str, list[ToolTrace], list[dict]]],
]


def load_place_agent_cases(path: Path = DEFAULT_PLACE_AGENT_CASES_PATH) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def _model_tool_calls(turn_messages: list[dict]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in turn_messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls", []):
            function = tool_call.get("function", {})
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except (json.JSONDecodeError, TypeError):
                arguments = None
            calls.append({"name": function.get("name"), "arguments": arguments})
    return calls


def _contains_expected(actual: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    if not isinstance(actual, dict):
        return False
    return all(actual.get(key) == value for key, value in expected.items())


def _metric(details: list[dict[str, Any]], key: str) -> dict[str, Any]:
    applicable = [detail for detail in details if detail[key] is not None]
    passed = sum(bool(detail[key]) for detail in applicable)
    return {
        "applicable": len(applicable),
        "passed": passed,
        "accuracy": passed / len(applicable) if applicable else None,
    }


async def evaluate_place_agent_cases(
    cases: list[dict[str, Any]],
    *,
    split: str = "validation",
    case_ids: set[str] | None = None,
    runner: AgentRunner = run_agent,
) -> dict[str, Any]:
    selected = [case for case in cases if case["split"] == split]
    if case_ids is not None:
        selected = [case for case in selected if case["id"] in case_ids]
    if not selected:
        raise ValueError(f"no place Agent cases found for split: {split}")

    details = []
    for case in selected:
        answer, traces, turn_messages = await runner(case["question"])
        model_calls = _model_tool_calls(turn_messages)
        model_names = [call["name"] for call in model_calls]
        executed_names = [trace.tool for trace in traces]
        expected_sequences = case["expectedToolSequences"]
        selection_correct = model_names in expected_sequences
        execution_correct = executed_names in expected_sequences
        forbidden = set(case.get("forbiddenTools", []))
        forbidden_free = not forbidden.intersection(executed_names)

        arguments_correct: bool | None = None
        expected_arguments = case.get("expectedArguments")
        if expected_arguments is not None:
            arguments_correct = len(model_calls) == len(expected_arguments) and all(
                _contains_expected(call.get("arguments"), expected)
                for call, expected in zip(model_calls, expected_arguments, strict=True)
            )

        search_trace = next((trace for trace in traces if trace.tool == "search_places"), None)
        search_detail = search_trace.detail if search_trace and isinstance(search_trace.detail, dict) else {}
        search_result_correct: bool | None = None
        if "expectedSearchTotal" in case or "expectedTopIds" in case:
            checks = []
            if "expectedSearchTotal" in case:
                checks.append(search_detail.get("total") == case["expectedSearchTotal"])
            if "expectedTopIds" in case:
                actual_ids = [item.get("id") for item in search_detail.get("items", [])]
                expected_ids = case["expectedTopIds"]
                checks.append(actual_ids[: len(expected_ids)] == expected_ids)
            search_result_correct = all(checks)

        detail_result_correct: bool | None = None
        if "expectedDetailId" in case:
            detail_trace = next((trace for trace in traces if trace.tool == "get_place_detail"), None)
            payload = detail_trace.detail if detail_trace and isinstance(detail_trace.detail, dict) else {}
            place = payload.get("place", {})
            detail_result_correct = isinstance(place, dict) and place.get("id") == case["expectedDetailId"]

        answer_correct: bool | None = None
        if any(key in case for key in ("requiredAnswerAll", "requiredAnswerAny", "forbiddenAnswer")):
            all_ok = all(fragment in answer for fragment in case.get("requiredAnswerAll", []))
            any_fragments = case.get("requiredAnswerAny", [])
            any_ok = not any_fragments or any(fragment in answer for fragment in any_fragments)
            forbidden_ok = not any(fragment in answer for fragment in case.get("forbiddenAnswer", []))
            answer_correct = all_ok and any_ok and forbidden_ok

        checks = [
            selection_correct,
            execution_correct,
            forbidden_free,
            arguments_correct,
            search_result_correct,
            detail_result_correct,
            answer_correct,
        ]
        complete = all(check for check in checks if check is not None)
        details.append({
            "caseId": case["id"],
            "category": case["category"],
            "question": case["question"],
            "expectedToolSequences": expected_sequences,
            "modelToolNames": model_names,
            "modelToolCalls": model_calls,
            "executedToolNames": executed_names,
            "toolTraces": [trace.model_dump(by_alias=True) for trace in traces],
            "usedFallback": bool(executed_names) and not bool(model_names),
            "selectionCorrect": selection_correct,
            "executionCorrect": execution_correct,
            "forbiddenToolFree": forbidden_free,
            "argumentsCorrect": arguments_correct,
            "searchResultCorrect": search_result_correct,
            "detailResultCorrect": detail_result_correct,
            "answerCorrect": answer_correct,
            "complete": complete,
            "answer": answer,
        })

    passed = sum(detail["complete"] for detail in details)
    metric_fields = {
        "modelToolSelection": "selectionCorrect",
        "toolExecution": "executionCorrect",
        "forbiddenToolIsolation": "forbiddenToolFree",
        "argumentExtraction": "argumentsCorrect",
        "searchResult": "searchResultCorrect",
        "detailResult": "detailResultCorrect",
        "answerBoundary": "answerCorrect",
    }
    return {
        "methodology": {
            "split": split,
            "testRead": split == "test",
            "fallbackCountsAsSelection": False,
            "catalogScope": "beijing-places-v2 demo snapshot",
        },
        "caseCount": len(details),
        "completePasses": passed,
        "completeAccuracy": passed / len(details),
        "fallbackCount": sum(detail["usedFallback"] for detail in details),
        "metrics": {name: _metric(details, field) for name, field in metric_fields.items()},
        "failures": [detail for detail in details if not detail["complete"]],
        "details": details,
    }
