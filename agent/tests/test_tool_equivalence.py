import unittest
from unittest.mock import patch

from app.tool_equivalence import (
    build_search_knowledge_result_from_reviews,
    build_search_knowledge_tool_detail_from_reviews,
    build_search_reviews_tool_detail,
    compare_review_tool_outputs,
    extract_search_knowledge_tool_review_ids,
    extract_search_reviews_tool_review_ids,
)
from app.tools import search_knowledge_tool, search_reviews_tool


class ReviewToolEquivalenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.question = "哪家咖啡店适合下午带电脑办公？"
        self.case = {
            "id": "case-office-coffee",
            "question": self.question,
            "relevantReviewIds": ["review-005"],
        }
        self.reviews = [
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座，下午写作业或办公很舒服。",
                "score": 0.88,
            },
            {
                "reviewId": "review-006",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "背景音乐不吵，适合专注工作。",
                "score": 0.76,
            },
        ]

    def test_extracts_equivalent_review_ids_from_tool_details(self):
        legacy_detail = build_search_reviews_tool_detail(self.question, self.reviews)
        candidate_detail = build_search_knowledge_tool_detail_from_reviews(
            self.question,
            self.reviews,
        )

        self.assertEqual(
            extract_search_reviews_tool_review_ids(legacy_detail),
            ["review-005", "review-006"],
        )
        self.assertEqual(
            extract_search_knowledge_tool_review_ids(candidate_detail),
            ["review-005", "review-006"],
        )

    def test_compares_equivalent_tool_outputs(self):
        legacy_detail = build_search_reviews_tool_detail(self.question, self.reviews)
        candidate_detail = build_search_knowledge_tool_detail_from_reviews(
            self.question,
            self.reviews,
        )

        comparison = compare_review_tool_outputs(
            self.case,
            legacy_output=legacy_detail,
            candidate_output=candidate_detail,
        )

        self.assertTrue(comparison["exactMatch"])
        self.assertTrue(comparison["sameHit"])
        self.assertFalse(comparison["candidateRegression"])
        self.assertTrue(comparison["candidateSourcesOk"])

    def test_detects_candidate_mismatch(self):
        legacy_detail = build_search_reviews_tool_detail(self.question, self.reviews)
        candidate_detail = build_search_knowledge_tool_detail_from_reviews(
            self.question,
            list(reversed(self.reviews)),
        )

        comparison = compare_review_tool_outputs(
            self.case,
            legacy_output=legacy_detail,
            candidate_output=candidate_detail,
        )

        self.assertFalse(comparison["exactMatch"])
        self.assertTrue(comparison["sameHit"])
        self.assertFalse(comparison["candidateRegression"])

    async def test_actual_tool_wrappers_are_equivalent_with_mocked_retrieval(self):
        knowledge_result = build_search_knowledge_result_from_reviews(
            self.question,
            self.reviews,
        )

        with (
            patch("app.tools.query_reviews", return_value=self.reviews),
            patch("app.tools.query_knowledge", return_value=knowledge_result),
        ):
            legacy_output = await search_reviews_tool(self.question)
            candidate_output = await search_knowledge_tool(
                self.question,
                ["reviews"],
            )

        comparison = compare_review_tool_outputs(
            self.case,
            legacy_output=legacy_output,
            candidate_output=candidate_output,
        )

        self.assertTrue(legacy_output.ok)
        self.assertTrue(candidate_output.ok)
        self.assertTrue(comparison["exactMatch"])
        self.assertTrue(comparison["candidateSourcesOk"])


if __name__ == "__main__":
    unittest.main()
