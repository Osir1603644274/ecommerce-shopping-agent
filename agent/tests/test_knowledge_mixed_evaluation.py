import unittest

from app.knowledge.models import Citation, RetrievalTrace, SearchKnowledgeResult
from evaluation.knowledge_mixed_evaluation import evaluate_mixed_search_cases


def fake_result(
    selected_sources: list[str],
    citation_source_types: list[str],
) -> SearchKnowledgeResult:
    citations = [
        Citation(
            chunkId=f"{source_type}:1",
            sourceType=source_type,
            sourceId=f"{source_type}-1",
            title=source_type,
            quote="evidence",
            score=1.0,
        )
        for source_type in citation_source_types
    ]
    return SearchKnowledgeResult(
        chunks=[],
        citations=citations,
        trace=RetrievalTrace(
            query="query",
            selectedSources=selected_sources,
            candidateCount=len(citations),
            returnedCount=len(citations),
            citations=citations,
        ),
    )


class KnowledgeMixedEvaluationTests(unittest.TestCase):
    def test_checks_router_selection_and_citation_coverage(self):
        cases = [
            {
                "id": "review",
                "question": "review question",
                "expectedSources": ["reviews"],
                "category": "reviews",
            },
            {
                "id": "mixed",
                "question": "mixed question",
                "expectedSources": ["merchant_docs", "reviews"],
                "category": "merchant_reviews",
            },
        ]

        def retrieve(question: str) -> SearchKnowledgeResult:
            if question == "review question":
                return fake_result(["reviews"], ["review"])
            return fake_result(["merchant_docs", "reviews"], ["merchant_doc", "review"])

        report = evaluate_mixed_search_cases(cases, retrieve=retrieve)

        self.assertEqual(report["caseCount"], 2)
        self.assertEqual(report["selectedExact"], 2)
        self.assertEqual(report["citationCovered"], 2)
        self.assertEqual(report["failures"], [])

    def test_reports_missing_citation_sources(self):
        cases = [
            {
                "id": "mixed",
                "question": "mixed question",
                "expectedSources": ["merchant_docs", "reviews"],
                "category": "merchant_reviews",
            }
        ]

        report = evaluate_mixed_search_cases(
            cases,
            retrieve=lambda _question: fake_result(
                ["merchant_docs", "reviews"],
                ["review"],
            ),
        )

        self.assertEqual(report["selectedExact"], 1)
        self.assertEqual(report["citationCovered"], 0)
        self.assertEqual(report["failures"][0]["missingCitationSources"], ["merchant_docs"])


if __name__ == "__main__":
    unittest.main()
