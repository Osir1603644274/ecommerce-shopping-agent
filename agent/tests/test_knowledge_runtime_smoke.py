import unittest

from app.knowledge import (
    KnowledgeChunk,
    RetrievalStep,
    RetrievalTrace,
    SearchKnowledgeResult,
)
from app.knowledge_runtime_smoke import (
    SOURCE_AWARE_RUNTIME_SMOKE_CASES,
    build_source_aware_runtime_smoke_report,
    evaluate_legacy_fallback_smoke,
    evaluate_source_aware_smoke_case,
)
from app.settings import settings


def _result(
    *,
    source_type: str,
    source_id: str,
    content: str,
    retriever: str,
    effective_mode: str = "source_aware",
    fallback: bool = False,
    shop_id: int | None = None,
) -> SearchKnowledgeResult:
    chunk = KnowledgeChunk(
        chunkId=f"{source_type}:{source_id}",
        sourceType=source_type,
        sourceId=source_id,
        content=content,
        metadata={"shopId": shop_id} if shop_id is not None else {},
    )
    citation = chunk.to_citation(score=0.9)
    route_detail = {
        "requestedMode": "source_aware",
        "effectiveMode": effective_mode,
        "fallback": fallback,
    }
    if fallback:
        route_detail["errorType"] = "UnexpectedResponse"
    return SearchKnowledgeResult(
        chunks=[chunk],
        citations=[citation],
        trace=RetrievalTrace(
            query="query",
            selectedSources=["reviews"],
            returnedCount=1,
            citations=[citation],
            steps=[
                RetrievalStep(name="runtime_search_route", detail=route_detail),
                RetrievalStep(
                    name="vector_search",
                    detail={"retriever": retriever},
                ),
            ],
        ),
    )


class KnowledgeRuntimeSmokeTests(unittest.TestCase):
    def test_policy_case_fails_when_expected_policy_is_missing_from_top_k(self):
        case = next(
            item
            for item in SOURCE_AWARE_RUNTIME_SMOKE_CASES
            if item["id"] == "policy-semantic-top3"
        )
        result = _result(
            source_type="policy_doc",
            source_id="consumer-rights",
            content="退款限制应当显著提示",
            retriever="qdrant_unified_vector",
        )

        detail = evaluate_source_aware_smoke_case(
            case,
            result,
            elapsed_ms=100.0,
            latency_ceiling_ms=5000.0,
        )

        self.assertFalse(detail["passed"])
        self.assertFalse(detail["checks"]["expectedSourceInTopK"])
        self.assertFalse(detail["checks"]["requiredMatchingTerms"])

    def test_fallback_case_requires_real_legacy_route_and_retriever(self):
        result = _result(
            source_type="review",
            source_id="legacy-review",
            content="适合办公",
            retriever="qdrant_vector",
            effective_mode="legacy",
            fallback=True,
        )

        detail = evaluate_legacy_fallback_smoke(
            result,
            elapsed_ms=200.0,
            latency_ceiling_ms=8000.0,
        )

        self.assertTrue(detail["passed"])

    def test_report_temporarily_enables_source_aware_and_restores_settings(self):
        original_enabled = settings.knowledge_source_aware_enabled
        original_collection = settings.knowledge_collection_name
        calls: list[str] = []

        def retrieve(query, sources, limit, *, shop_id=None):
            self.assertTrue(settings.knowledge_source_aware_enabled)
            self.assertEqual(limit, 3)
            calls.append(settings.knowledge_collection_name)
            if settings.knowledge_collection_name.endswith("__missing_runtime_smoke"):
                return _result(
                    source_type="review",
                    source_id="legacy-review",
                    content="适合办公",
                    retriever="qdrant_vector",
                    effective_mode="legacy",
                    fallback=True,
                )
            return _result(
                source_type="review",
                source_id="source-aware-review",
                content="适合办公",
                retriever="qdrant_unified_vector",
            )

        report = build_source_aware_runtime_smoke_report(
            retrieve=retrieve,
            cases=[SOURCE_AWARE_RUNTIME_SMOKE_CASES[0]],
        )

        self.assertTrue(report["summary"]["passed"])
        self.assertEqual(settings.knowledge_source_aware_enabled, original_enabled)
        self.assertEqual(settings.knowledge_collection_name, original_collection)
        self.assertEqual(
            calls,
            [original_collection, f"{original_collection}__missing_runtime_smoke"],
        )


if __name__ == "__main__":
    unittest.main()
