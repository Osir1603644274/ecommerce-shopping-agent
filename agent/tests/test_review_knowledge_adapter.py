import unittest

from app.knowledge import (
    build_review_retrieval_trace,
    review_to_chunk,
    review_to_citation,
    reviews_to_citations,
)


class ReviewKnowledgeAdapterTests(unittest.TestCase):
    def test_review_to_chunk_preserves_source_and_metadata(self):
        review = {
            "reviewId": "review-005",
            "shopId": 3,
            "shopName": "清晨手冲咖啡",
            "text": "店里有靠窗的单人位和插座。",
            "originalText": "店里有靠窗的单人位和插座。",
            "source": "yelp",
            "language": "zh",
            "translationStatus": "translated",
            "updatedAt": "2026-07-13T10:30:00",
            "tags": ["office", "quiet"],
            "score": 0.89,
        }

        chunk = review_to_chunk(review)

        self.assertEqual(chunk.chunk_id, "review:review-005")
        self.assertEqual(chunk.source_type, "review")
        self.assertEqual(chunk.source_id, "review-005")
        self.assertEqual(chunk.title, "清晨手冲咖啡评论")
        self.assertEqual(chunk.content, "店里有靠窗的单人位和插座。")
        self.assertEqual(chunk.visibility, "public")
        self.assertEqual(chunk.metadata["shopId"], 3)
        self.assertEqual(chunk.metadata["shopName"], "清晨手冲咖啡")
        self.assertEqual(chunk.metadata["score"], 0.89)
        self.assertEqual(chunk.tags, ["office", "quiet"])
        self.assertEqual(chunk.chunk_index, 1)
        self.assertEqual(chunk.source_version, "2026-07-13T10:30:00")
        self.assertEqual(chunk.updated_at.isoformat(), "2026-07-13T10:30:00")
        self.assertEqual(len(chunk.content_hash), 64)

    def test_review_to_citation_uses_chunk_identity(self):
        citation = review_to_citation(
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座。",
                "score": "0.89",
            }
        )

        self.assertEqual(citation.chunk_id, "review:review-005")
        self.assertEqual(citation.source_type, "review")
        self.assertEqual(citation.source_id, "review-005")
        self.assertEqual(citation.quote, "店里有靠窗的单人位和插座。")
        self.assertEqual(citation.score, 0.89)

    def test_review_to_chunk_marks_translated_content_language(self):
        chunk = review_to_chunk(
            {
                "reviewId": "yelp-001",
                "shopName": "Example Shop",
                "content": "A quiet place.",
                "contentZh": "一个安静的地方。",
                "language": "en",
            }
        )

        self.assertEqual(chunk.content, "一个安静的地方。")
        self.assertEqual(chunk.language, "zh")
        self.assertEqual(chunk.metadata["sourceLanguage"], "en")

    def test_reviews_to_citations_keeps_order(self):
        citations = reviews_to_citations(
            [
                {
                    "reviewId": "review-005",
                    "shopName": "清晨手冲咖啡",
                    "text": "有插座。",
                    "score": 0.89,
                },
                {
                    "reviewId": "review-006",
                    "shopName": "清晨手冲咖啡",
                    "text": "下午安静。",
                    "score": 0.78,
                },
            ]
        )

        self.assertEqual(
            [citation.source_id for citation in citations],
            ["review-005", "review-006"],
        )

    def test_build_review_retrieval_trace_wraps_legacy_reviews(self):
        reviews = [
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座。",
                "score": 0.89,
            }
        ]

        trace = build_review_retrieval_trace(
            "哪家咖啡店适合办公？",
            reviews,
            limit=3,
            duration_ms=12.5,
            collection_name="merchant_reviews",
            filters={"typeId": 2},
        )

        self.assertEqual(trace.selected_sources, ["reviews"])
        self.assertEqual(trace.filters["visibility"], "public")
        self.assertEqual(trace.filters["typeId"], 2)
        self.assertEqual(trace.candidate_count, 1)
        self.assertEqual(trace.returned_count, 1)
        self.assertEqual(trace.citations[0].chunk_id, "review:review-005")
        self.assertEqual(trace.steps[0].name, "vector_search")
        self.assertEqual(trace.steps[0].detail["collection"], "merchant_reviews")
        self.assertEqual(trace.steps[0].detail["topK"], 3)


if __name__ == "__main__":
    unittest.main()
