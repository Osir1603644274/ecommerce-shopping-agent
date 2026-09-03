import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.llm import run_agent
from app.schemas import ToolTrace


CAPABILITY_ROUTER_EVAL_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "capability_router_cases.json"
)

AgentRunner = Callable[
    [str],
    Awaitable[tuple[str, list[ToolTrace], list[dict]]],
]


def load_capability_router_cases(
    path: Path = CAPABILITY_ROUTER_EVAL_PATH,
) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _model_tool_calls(turn_messages: list[dict]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in turn_messages:
        if message.get("role") != "assistant":
            continue
        for tool_call in message.get("tool_calls", []):
            function = tool_call.get("function", {})
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = None
            calls.append(
                {
                    "name": function.get("name"),
                    "arguments": arguments,
                }
            )
    return calls


def _matching_trace(
    traces: list[ToolTrace],
    expected_tool: str | None,
) -> ToolTrace | None:
    if expected_tool is None:
        return None
    return next((trace for trace in traces if trace.tool == expected_tool), None)


def _metric_summary(
    details: list[dict[str, Any]],
    field: str,
) -> dict[str, Any]:
    applicable = [detail for detail in details if detail[field] is not None]
    passed = sum(detail[field] for detail in applicable)
    return {
        "applicable": len(applicable),
        "passed": passed,
        "accuracy": passed / len(applicable) if applicable else None,
    }


async def evaluate_capability_router_cases(
    cases: list[dict[str, Any]],
    *,
    split: str = "validation",
    runner: AgentRunner = run_agent,
) -> dict[str, Any]:
    selected_cases = [case for case in cases if case.get("split") == split]
    if not selected_cases:
        raise ValueError(f"no capability router cases found for split: {split}")

    details: list[dict[str, Any]] = []
    for case in selected_cases:
        answer, traces, turn_messages = await runner(case["question"])
        model_calls = _model_tool_calls(turn_messages)
        model_tool_names = [call["name"] for call in model_calls]
        executed_tool_names = [trace.tool for trace in traces]
        expected_tools = list(case["expectedTools"])
        primary_tool = expected_tools[0] if expected_tools else None
        matching_call = next(
            (call for call in model_calls if call["name"] == primary_tool),
            None,
        )
        matching_trace = _matching_trace(traces, primary_tool)
        detail_payload = (
            matching_trace.detail
            if matching_trace is not None and isinstance(matching_trace.detail, dict)
            else {}
        )

        tool_selection_correct = model_tool_names == expected_tools
        forbidden_tools = set(case.get("forbiddenTools", []))
        forbidden_tool_free = not forbidden_tools.intersection(executed_tool_names)

        argument_correct: bool | None = None
        expected_shop_name = case.get("expectedShopName")
        if expected_shop_name is not None:
            arguments = matching_call.get("arguments") if matching_call else None
            actual_shop_name = (
                arguments.get("shopName")
                if isinstance(arguments, dict)
                else None
            )
            argument_correct = (
                isinstance(actual_shop_name, str)
                and actual_shop_name.strip().casefold()
                == str(expected_shop_name).strip().casefold()
            )

        sources_correct: bool | None = None
        if "expectedSources" in case:
            sources_correct = detail_payload.get("sources") == case["expectedSources"]

        resolution_correct: bool | None = None
        if "expectedResolutionStatus" in case:
            expected_status = case["expectedResolutionStatus"]
            actual_status = detail_payload.get("resolutionStatus")
            resolution_correct = actual_status == expected_status
            if resolution_correct and "expectedShopId" in case:
                resolved_shop = detail_payload.get("resolvedShop", {})
                resolution_correct = (
                    isinstance(resolved_shop, dict)
                    and resolved_shop.get("id") == case["expectedShopId"]
                )

        review_purity_correct: bool | None = None
        if "expectedShopId" in case:
            reviews = detail_payload.get("reviews", [])
            review_purity_correct = (
                isinstance(reviews, list)
                and bool(reviews)
                and all(
                    isinstance(review, dict)
                    and review.get("shopId") == case["expectedShopId"]
                    for review in reviews
                )
            )

        clarification_correct: bool | None = None
        if "expectedClarificationAny" in case:
            clarification_correct = any(
                fragment in answer
                for fragment in case["expectedClarificationAny"]
            )

        checks = [
            tool_selection_correct,
            forbidden_tool_free,
            argument_correct,
            sources_correct,
            resolution_correct,
            review_purity_correct,
            clarification_correct,
        ]
        complete = all(check for check in checks if check is not None)
        details.append(
            {
                "caseId": case["id"],
                "category": case["category"],
                "question": case["question"],
                "expectedTools": expected_tools,
                "modelToolNames": model_tool_names,
                "executedToolNames": executed_tool_names,
                "usedFallback": bool(executed_tool_names) and not bool(model_tool_names),
                "toolSelectionCorrect": tool_selection_correct,
                "forbiddenToolFree": forbidden_tool_free,
                "argumentCorrect": argument_correct,
                "sourcesCorrect": sources_correct,
                "resolutionCorrect": resolution_correct,
                "reviewPurityCorrect": review_purity_correct,
                "clarificationCorrect": clarification_correct,
                "complete": complete,
                "answer": answer,
            }
        )

    complete_passes = sum(detail["complete"] for detail in details)
    by_category: dict[str, dict[str, Any]] = {}
    for detail in details:
        category = detail["category"]
        group = by_category.setdefault(
            category,
            {"total": 0, "completePasses": 0, "accuracy": 0.0},
        )
        group["total"] += 1
        group["completePasses"] += int(detail["complete"])
    for group in by_category.values():
        group["accuracy"] = group["completePasses"] / group["total"]

    metric_fields = {
        "toolSelection": "toolSelectionCorrect",
        "argumentExtraction": "argumentCorrect",
        "sourceConstraint": "sourcesCorrect",
        "entityResolution": "resolutionCorrect",
        "reviewPurity": "reviewPurityCorrect",
        "clarification": "clarificationCorrect",
    }
    return {
        "methodology": {
            "split": split,
            "testRead": split == "test",
            "selectionSource": "model-emitted tool calls from turn_messages",
            "fallbackCountsAsSelection": False,
        },
        "caseCount": len(details),
        "completePasses": complete_passes,
        "completeAccuracy": complete_passes / len(details),
        "metrics": {
            name: _metric_summary(details, field)
            for name, field in metric_fields.items()
        },
        "byCategory": dict(sorted(by_category.items())),
        "fallbackCount": sum(detail["usedFallback"] for detail in details),
        "failures": [detail for detail in details if not detail["complete"]],
        "details": details,
    }


async def evaluate_capability_router(
    *,
    split: str = "validation",
    path: Path = CAPABILITY_ROUTER_EVAL_PATH,
    runner: AgentRunner = run_agent,
) -> dict[str, Any]:
    return await evaluate_capability_router_cases(
        load_capability_router_cases(path),
        split=split,
        runner=runner,
    )
