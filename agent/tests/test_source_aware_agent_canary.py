import unittest

from app.source_aware_agent_canary import (
    build_agent_canary_report,
    evaluate_agent_canary_case,
)


def _rag_response(*, fallback: bool = False, source_type: str = "review") -> dict:
    citation = {
        "chunkId": "review:one" if source_type == "review" else "merchant_doc:one",
        "sourceType": source_type,
        "sourceId": "one",
        "metadata": {"shopId": 100011} if source_type == "review" else {},
    }
    return {
        "answer": "有证据的回答",
        "tool_trace": [
            {
                "tool": "search_shop_reviews",
                "ok": True,
                "detail": {
                    "resolvedShop": {"id": 100011},
                    "citations": [citation],
                    "retrievalTrace": {
                        "selectedSources": ["reviews"],
                        "filters": {"shopIds": [100011]},
                        "durationMs": 1200.0,
                        "citations": [citation],
                        "steps": [
                            {
                                "name": "runtime_search_route",
                                "detail": {
                                    "requestedMode": "source_aware",
                                    "effectiveMode": "legacy" if fallback else "source_aware",
                                    "fallback": fallback,
                                },
                            }
                        ],
                    },
                },
            }
        ],
        "trace": {"status": "ok", "totalDurationMs": 3000.0},
    }


class SourceAwareAgentCanaryTests(unittest.TestCase):
    def test_named_review_case_checks_route_filter_and_citations(self):
        case = {
            "id": "named",
            "question": "question",
            "expectedTools": ["search_shop_reviews"],
            "forbiddenTools": ["search_shops"],
            "expectsRetrieval": True,
            "expectedSources": ["reviews"],
            "expectedSourceType": "review",
            "expectedShopId": 100011,
        }

        result = evaluate_agent_canary_case(case, _rag_response(), http_status=200)

        self.assertTrue(result["passed"])
        self.assertEqual(result["failedChecks"], [])

    def test_fallback_is_a_failure(self):
        case = {
            "id": "fallback",
            "question": "question",
            "expectedTools": ["search_shop_reviews"],
            "expectsRetrieval": True,
            "expectedSources": ["reviews"],
            "expectedSourceType": "review",
        }

        result = evaluate_agent_canary_case(
            case,
            _rag_response(fallback=True),
            http_status=200,
        )

        self.assertFalse(result["passed"])
        self.assertIn("sourceAwareEffective", result["failedChecks"])

    def test_report_requires_every_case_to_pass(self):
        cases = [{"id": "one"}, {"id": "two"}]
        report = build_agent_canary_report(
            cases,
            [{"caseId": "one", "passed": True, "warnings": []}],
            base_url="http://localhost:8001",
            stopped_early=True,
        )

        self.assertFalse(report["summary"]["passed"])
        self.assertFalse(report["summary"]["complete"])
        self.assertTrue(report["summary"]["stoppedEarly"])

    def test_non_rag_case_can_allow_multiple_valid_tool_sequences(self):
        case = {
            "id": "structured-shop",
            "question": "question",
            "expectedToolSequences": [
                ["search_shops"],
                ["list_shop_types", "search_shops"],
            ],
            "forbiddenTools": ["search_knowledge"],
            "expectsRetrieval": False,
        }
        response = {
            "answer": "找到商户",
            "tool_trace": [
                {"tool": "list_shop_types", "ok": True, "detail": {}},
                {"tool": "search_shops", "ok": True, "detail": {}},
            ],
            "trace": {"status": "ok"},
        }

        result = evaluate_agent_canary_case(case, response, http_status=200)

        self.assertTrue(result["passed"])


if __name__ == "__main__":
    unittest.main()
