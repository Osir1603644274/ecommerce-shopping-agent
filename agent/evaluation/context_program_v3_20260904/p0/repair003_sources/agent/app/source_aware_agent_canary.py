from __future__ import annotations

from typing import Any


RAG_TOOL_NAMES = {"search_knowledge", "search_shop_reviews"}


def _tool_traces(response: dict[str, Any]) -> list[dict[str, Any]]:
    traces = response.get("tool_trace", [])
    return [item for item in traces if isinstance(item, dict)] if isinstance(traces, list) else []


def _retrieval_trace(tool_trace: dict[str, Any]) -> dict[str, Any]:
    detail = tool_trace.get("detail")
    if not isinstance(detail, dict):
        return {}
    trace = detail.get("retrievalTrace")
    return trace if isinstance(trace, dict) else {}


def _runtime_route(retrieval_trace: dict[str, Any]) -> dict[str, Any]:
    steps = retrieval_trace.get("steps", [])
    if not isinstance(steps, list):
        return {}
    for step in steps:
        if isinstance(step, dict) and step.get("name") == "runtime_search_route":
            detail = step.get("detail")
            return detail if isinstance(detail, dict) else {}
    return {}


def _retrieval_step_detail(
    retrieval_trace: dict[str, Any],
    step_name: str,
) -> dict[str, Any]:
    steps = retrieval_trace.get("steps", [])
    if not isinstance(steps, list):
        return {}
    for step in steps:
        if isinstance(step, dict) and step.get("name") == step_name:
            detail = step.get("detail")
            return detail if isinstance(detail, dict) else {}
    return {}


def _citations(tool_trace: dict[str, Any]) -> list[dict[str, Any]]:
    detail = tool_trace.get("detail")
    if not isinstance(detail, dict):
        return []
    citations = detail.get("citations", [])
    return [item for item in citations if isinstance(item, dict)] if isinstance(citations, list) else []


def _citation_ids(citations: list[dict[str, Any]]) -> list[str]:
    return [
        str(item.get("chunkId"))
        for item in citations
        if item.get("chunkId") is not None
    ]


def _citation_shop_ids(citations: list[dict[str, Any]]) -> list[int]:
    shop_ids: list[int] = []
    for citation in citations:
        metadata = citation.get("metadata")
        if not isinstance(metadata, dict):
            continue
        shop_id = metadata.get("shopId")
        if isinstance(shop_id, int):
            shop_ids.append(shop_id)
    return shop_ids


def evaluate_agent_canary_case(
    case: dict[str, Any],
    response: dict[str, Any],
    *,
    http_status: int,
    warning_latency_ms: float = 3000.0,
    stop_latency_ms: float = 5000.0,
) -> dict[str, Any]:
    traces = _tool_traces(response)
    tool_names = [str(item.get("tool")) for item in traces]
    expected_tools = case.get("expectedTools", [])
    expected_tool_sequences = case.get("expectedToolSequences")
    if not isinstance(expected_tool_sequences, list):
        expected_tool_sequences = [expected_tools]
    forbidden_tools = set(case.get("forbiddenTools", []))
    request_trace = response.get("trace") if isinstance(response.get("trace"), dict) else {}
    answer = response.get("answer") if isinstance(response.get("answer"), str) else ""

    checks: dict[str, bool] = {
        "httpStatusOk": http_status == 200,
        "requestStatusOk": request_trace.get("status") == "ok",
        "toolsAllowedSequence": any(
            tool_names == sequence
            for sequence in expected_tool_sequences
            if isinstance(sequence, list)
        ),
        "forbiddenToolIsolation": not any(name in forbidden_tools for name in tool_names),
        "toolsSucceeded": bool(traces) and all(item.get("ok") is True for item in traces),
    }
    warnings: list[str] = []
    retrieval_summary: dict[str, Any] | None = None

    if case.get("expectsRetrieval"):
        retrieval_tools = [item for item in traces if item.get("tool") in RAG_TOOL_NAMES]
        retrieval_traces = [
            (item, _retrieval_trace(item))
            for item in retrieval_tools
            if _retrieval_trace(item)
        ]
        checks["singleRetrievalTrace"] = len(retrieval_traces) == 1
        if retrieval_traces:
            tool_trace, retrieval_trace = retrieval_traces[0]
            route = _runtime_route(retrieval_trace)
            citations = _citations(tool_trace)
            trace_citations = retrieval_trace.get("citations", [])
            if not isinstance(trace_citations, list):
                trace_citations = []
            expected_sources = case.get("expectedSources", [])
            expected_source_type = case.get("expectedSourceType")
            duration_ms = retrieval_trace.get("durationMs")
            duration_value = float(duration_ms) if isinstance(duration_ms, (int, float)) else None

            checks.update(
                {
                    "sourceAwareEffective": (
                        route.get("requestedMode") == "source_aware"
                        and route.get("effectiveMode") == "source_aware"
                        and route.get("fallback") is False
                    ),
                    "selectedSourcesExact": retrieval_trace.get("selectedSources") == expected_sources,
                    "returnedEvidence": bool(citations),
                    "citationSourceTypeExact": bool(citations)
                    and all(item.get("sourceType") == expected_source_type for item in citations),
                    "citationsAligned": bool(citations)
                    and _citation_ids(citations) == _citation_ids(
                        [item for item in trace_citations if isinstance(item, dict)]
                    ),
                    "withinStopLatency": duration_value is not None
                    and duration_value <= stop_latency_ms,
                }
            )
            expected_review_mode = case.get("expectedReviewMode")
            hybrid_route = _retrieval_step_detail(
                retrieval_trace,
                "review_hybrid_route",
            )
            step_names = [
                step.get("name")
                for step in retrieval_trace.get("steps", [])
                if isinstance(step, dict)
            ]
            if isinstance(expected_review_mode, str):
                checks["reviewModeExact"] = (
                    hybrid_route.get("requestedMode") == "hybrid"
                    and hybrid_route.get("effectiveMode") == expected_review_mode
                    and hybrid_route.get("fallback") is False
                )
                if expected_review_mode == "hybrid":
                    checks["hybridStagesPresent"] = all(
                        name in step_names
                        for name in ("vector_recall", "bm25_recall", "hybrid_fusion")
                    )
            if duration_value is not None and duration_value > warning_latency_ms:
                warnings.append(
                    f"retrieval latency {duration_value:.2f}ms exceeds warning line "
                    f"{warning_latency_ms:.2f}ms"
                )

            expected_shop_id = case.get("expectedShopId")
            if isinstance(expected_shop_id, int):
                detail = tool_trace.get("detail") if isinstance(tool_trace.get("detail"), dict) else {}
                resolved_shop = detail.get("resolvedShop")
                filters = retrieval_trace.get("filters")
                filter_shop_ids = filters.get("shopIds") if isinstance(filters, dict) else None
                citation_shop_ids = _citation_shop_ids(citations)
                checks.update(
                    {
                        "resolvedShopExact": isinstance(resolved_shop, dict)
                        and resolved_shop.get("id") == expected_shop_id,
                        "shopFilterExact": filter_shop_ids == [expected_shop_id],
                        "citationShopExact": bool(citation_shop_ids)
                        and all(shop_id == expected_shop_id for shop_id in citation_shop_ids),
                    }
                )

            expected_source_id = case.get("expectedSourceId")
            if isinstance(expected_source_id, str):
                checks["expectedSourcePresent"] = any(
                    item.get("sourceId") == expected_source_id for item in citations
                )

            retrieval_summary = {
                "tool": tool_trace.get("tool"),
                "selectedSources": retrieval_trace.get("selectedSources"),
                "runtimeRoute": route,
                "filters": retrieval_trace.get("filters"),
                "durationMs": duration_value,
                "citationCount": len(citations),
                "citationIds": _citation_ids(citations),
                "citationSourceTypes": [item.get("sourceType") for item in citations],
                "citationShopIds": _citation_shop_ids(citations),
                "reviewHybridRoute": hybrid_route,
            }
        else:
            checks.update(
                {
                    "sourceAwareEffective": False,
                    "selectedSourcesExact": False,
                    "returnedEvidence": False,
                    "citationSourceTypeExact": False,
                    "citationsAligned": False,
                    "withinStopLatency": False,
                }
            )
    else:
        checks["noRagTool"] = not any(name in RAG_TOOL_NAMES for name in tool_names)
        checks["noRetrievalTrace"] = not any(_retrieval_trace(item) for item in traces)

    required_terms = case.get("requiredAnswerTerms", [])
    forbidden_terms = case.get("forbiddenAnswerTerms", [])
    if required_terms:
        checks["requiredAnswerTerms"] = all(term in answer for term in required_terms)
    if forbidden_terms:
        checks["forbiddenAnswerTermsAbsent"] = not any(term in answer for term in forbidden_terms)

    passed = all(checks.values())
    return {
        "caseId": case.get("id"),
        "question": case.get("question"),
        "passed": passed,
        "checks": checks,
        "failedChecks": [name for name, value in checks.items() if not value],
        "warnings": warnings,
        "toolNames": tool_names,
        "answer": answer,
        "requestTrace": request_trace,
        "retrieval": retrieval_summary,
    }


def build_agent_canary_report(
    cases: list[dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    base_url: str,
    stopped_early: bool,
) -> dict[str, Any]:
    passed_count = sum(item.get("passed") is True for item in results)
    warning_count = sum(len(item.get("warnings", [])) for item in results)
    complete = len(results) == len(cases)
    passed = complete and passed_count == len(cases)
    return {
        "experiment": "RAG-PROD-01 stage 2d non-sealed Agent canary matrix",
        "methodology": {
            "sealedDataRead": False,
            "baseUrl": base_url,
            "isolatedCanaryPortRequired": True,
            "defaultAgentChanged": False,
            "hybridEnabled": False,
            "warningLatencyMs": 3000.0,
            "stopLatencyMs": 5000.0,
        },
        "summary": {
            "passed": passed,
            "complete": complete,
            "stoppedEarly": stopped_early,
            "cases": len(cases),
            "executed": len(results),
            "passedCases": passed_count,
            "failedCases": len(results) - passed_count,
            "warnings": warning_count,
            "eligibleForNextStageDiscussion": passed,
        },
        "details": results,
        "failures": [item for item in results if not item.get("passed")],
    }
