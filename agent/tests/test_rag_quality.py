import unittest
from unittest.mock import patch

from app.bm25 import BM25Document, BM25Index, tokenize_for_bm25
from app.rag_quality import (
    analyze_yelp_retrieval_failure_cases,
    rerank_reviews_by_lexical_overlap,
)


class RagQualityTests(unittest.TestCase):
    def test_analyzes_failures_and_recovered_beyond_top_k(self):
        cases = [
            {
                "id": "case-hit",
                "question": "quiet cafe",
                "relevantReviewIds": ["r1"],
                "difficulty": "basic",
                "challengeTypes": ["environment_preference"],
                "evidenceText": "expected evidence",
            },
            {
                "id": "case-recovered",
                "question": "service cafe",
                "relevantReviewIds": ["r5"],
                "difficulty": "hard",
                "challengeTypes": ["service_constraint", "multi_constraint"],
                "evidenceText": "expected evidence",
            },
            {
                "id": "case-miss",
                "question": "late cafe",
                "relevantReviewIds": ["r9"],
                "difficulty": "hard",
                "challengeTypes": ["time_constraint"],
                "evidenceText": "expected evidence",
            },
        ]

        def retrieve(question: str, limit: int):
            mapping = {
                "quiet cafe": ["r1", "r2", "r3", "r4", "r5"],
                "service cafe": ["r1", "r2", "r3", "r4", "r5"],
                "late cafe": ["review-001", "r2", "r3", "r4", "r5"],
            }
            return [
                {
                    "reviewId": review_id,
                    "shopName": "Shop",
                    "text": f"text {review_id}",
                    "score": 1.0 / rank,
                }
                for rank, review_id in enumerate(mapping[question][:limit], start=1)
            ]

        report = analyze_yelp_retrieval_failure_cases(
            cases,
            retrieve=retrieve,
            top_k=3,
            analysis_top_k=5,
        )

        self.assertEqual(report["caseCount"], 3)
        self.assertEqual(report["hitAtTopK"], 1)
        self.assertEqual(report["failureCount"], 2)
        self.assertEqual(report["recoveredBeyondTopK"], 1)
        self.assertEqual(report["recoveredBeyondTopKCaseIds"], ["case-recovered"])
        self.assertEqual(report["failureReasons"]["top_k_too_small"], 1)
        self.assertEqual(report["failureReasons"]["semantic_miss_not_in_analysis_top_k"], 1)
        self.assertEqual(report["failureReasons"]["seed_review_interference"], 1)

    def test_lexical_rerank_can_promote_keyword_match_from_candidates(self):
        reviews = [
            {
                "reviewId": "vector-first",
                "text": "环境不错，咖啡也可以。",
                "score": 0.90,
            },
            {
                "reviewId": "keyword-match",
                "text": "这里有免费 WiFi，还有很多插座，适合带电脑办公。",
                "score": 0.89,
            },
        ]

        reranked = rerank_reviews_by_lexical_overlap(
            "有没有免费 WiFi 和插座，适合带电脑办公？",
            reviews,
            vector_weight=1.0,
            lexical_weight=1.0,
        )

        self.assertEqual(reranked[0]["reviewId"], "keyword-match")
        self.assertGreater(reranked[0]["lexicalScore"], reranked[1]["lexicalScore"])
        self.assertEqual(reranked[0]["originalRank"], 2)

    def test_bm25_tokenizes_mixed_chinese_and_english(self):
        tokens = tokenize_for_bm25("Red Hook 有 WiFi 和插座")

        self.assertIn("red", tokens)
        self.assertIn("hook", tokens)
        self.assertIn("wifi", tokens)
        self.assertIn("插座", tokens)

    def test_bm25_search_prefers_rare_exact_terms(self):
        index = BM25Index(
            [
                BM25Document(
                    doc_id="general",
                    text="咖啡 环境 不错 适合 放松",
                    payload={"reviewId": "general"},
                ),
                BM25Document(
                    doc_id="wifi",
                    text="这里有免费 WiFi 和很多插座，适合电脑办公",
                    payload={"reviewId": "wifi"},
                ),
            ]
        )

        results = index.search("有没有 WiFi 和插座", limit=2)

        self.assertEqual(results[0]["reviewId"], "wifi")
        self.assertEqual(len(results), 1)
        self.assertGreater(results[0]["score"], 0)

    def test_bm25_search_honors_hard_document_filter(self):
        index = BM25Index(
            [
                BM25Document("shop-a", "免费 WiFi 插座", {"reviewId": "shop-a"}),
                BM25Document("shop-b", "免费 WiFi 插座", {"reviewId": "shop-b"}),
            ]
        )

        results = index.search("WiFi 插座", limit=2, doc_ids={"shop-b"})

        self.assertEqual([item["reviewId"] for item in results], ["shop-b"])

    def test_bm25_inverted_index_supports_upsert_and_delete(self):
        index = BM25Index(
            [BM25Document("r1", "安静 咖啡", {"reviewId": "r1", "version": 1})]
        )

        self.assertEqual(index.search("插座"), [])
        index.upsert(
            BM25Document(
                "r1",
                "安静 插座 电脑",
                {"reviewId": "r1", "version": 2},
            )
        )
        self.assertEqual(index.search("咖啡"), [])
        self.assertEqual(index.search("插座")[0]["version"], 2)

        self.assertTrue(index.remove("r1"))
        self.assertFalse(index.remove("r1"))
        self.assertEqual(index.search("插座"), [])
        self.assertEqual(index.document_count, 0)

    def test_bm25_rejects_duplicate_document_ids(self):
        with self.assertRaisesRegex(ValueError, "duplicate BM25 document ID"):
            BM25Index(
                [
                    BM25Document("same", "安静", {}),
                    BM25Document("same", "插座", {}),
                ]
            )

    def test_bm25_search_scores_only_posting_candidates(self):
        index = BM25Index(
            [
                BM25Document(f"common-{number}", "咖啡 环境", {})
                for number in range(100)
            ]
            + [BM25Document("rare", "独特 插座", {"reviewId": "rare"})]
        )

        with patch.object(
            index,
            "_score_terms",
            wraps=index._score_terms,
        ) as score_mock:
            results = index.search("独特", limit=3)

        self.assertEqual([item["reviewId"] for item in results], ["rare"])
        self.assertEqual(score_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
