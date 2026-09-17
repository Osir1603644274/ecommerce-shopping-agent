import unittest

from app.rag_content_reranker import (
    parse_content_reranker_response,
    rerank_reviews_by_content_judgments,
    rerank_reviews_with_content_judge,
)
from evaluation.rag_content_reranker_evaluation import (
    build_content_reranker_validation_report,
)


def review(review_id, shop_id, score, text=None):
    return {
        "reviewId": review_id,
        "shopId": shop_id,
        "shopName": f"shop-{shop_id}",
        "score": score,
        "text": text or review_id,
    }


class RagContentRerankerTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_normalizes_relations_and_fills_missing_candidates(self):
        reviews = [review("a", 1, 0.9), review("b", 2, 0.8)]
        content = """```json
        {
          "results": [
            {
              "index": 0,
              "relation": "support",
              "score": 5,
              "matchedConditions": ["安静"],
              "failedConditions": [],
              "reason": "直接支持"
            }
          ]
        }
        ```"""

        result = parse_content_reranker_response(content, reviews)

        self.assertEqual(result[0]["score"], 3)
        self.assertEqual(result[0]["relation"], "support")
        self.assertEqual(result[1]["relation"], "irrelevant")
        self.assertEqual(result[1]["score"], 0)

    def test_content_judgments_promote_support_and_demote_conflict(self):
        reviews = [
            review("conflict", 9, 0.9),
            review("irrelevant", 8, 0.8),
            review("support", 1, 0.7),
        ]
        judgments = [
            {
                "reviewId": "conflict",
                "relation": "conflict",
                "score": 0,
                "matchedConditions": [],
                "failedConditions": ["太吵"],
                "reason": "与安静需求相反",
            },
            {
                "reviewId": "irrelevant",
                "relation": "irrelevant",
                "score": 0,
                "matchedConditions": [],
                "failedConditions": [],
                "reason": "对象不一致",
            },
            {
                "reviewId": "support",
                "relation": "support",
                "score": 3,
                "matchedConditions": ["安静"],
                "failedConditions": [],
                "reason": "直接支持",
            },
        ]

        reranked = rerank_reviews_by_content_judgments(reviews, judgments)

        self.assertEqual(
            [item["reviewId"] for item in reranked],
            ["support", "irrelevant", "conflict"],
        )
        self.assertEqual(reranked[0]["originalRank"], 3)

    async def test_batches_candidates_without_changing_judgment_alignment(self):
        reviews = [review(str(index), index, 1 - index / 10) for index in range(5)]
        batch_sizes = []

        async def fake_judge(question, batch):
            batch_sizes.append(len(batch))
            return [
                {
                    "reviewId": item["reviewId"],
                    "relation": "support",
                    "score": 1,
                    "matchedConditions": [],
                    "failedConditions": [],
                    "reason": "支持",
                }
                for item in batch
            ]

        reranked = await rerank_reviews_with_content_judge(
            "query",
            reviews,
            judge_batch=fake_judge,
            batch_size=2,
        )

        self.assertEqual(sorted(batch_sizes), [1, 2, 2])
        self.assertEqual([item["reviewId"] for item in reranked], ["0", "1", "2", "3", "4"])

    async def test_validation_report_keeps_qrels_out_of_content_judge(self):
        cases = [
            {
                "id": "case-1",
                "question": "想找安静的地方",
                "relevanceJudgments": [
                    {
                        "shopId": 1,
                        "relevance": 3,
                        "supportingReviewIds": ["good"],
                    },
                    {
                        "shopId": 2,
                        "relevance": 1,
                        "supportingReviewIds": ["partial"],
                    },
                ],
            }
        ]

        def vector_retrieve(question, limit):
            return [review("bad", 9, 0.9, "音乐太吵"), review("good", 1, 0.8, "很安静")]

        def bm25_retrieve(question, limit):
            return [review("partial", 2, 9.0, "适合放松"), review("bad", 9, 8.0, "音乐太吵")]

        async def fake_judge(question, batch):
            self.assertEqual(question, "想找安静的地方")
            self.assertTrue(all("relevanceJudgments" not in item for item in batch))
            result = []
            for item in batch:
                relation = "conflict" if item["reviewId"] == "bad" else "support"
                result.append(
                    {
                        "reviewId": item["reviewId"],
                        "relation": relation,
                        "score": 3 if item["reviewId"] == "good" else (1 if relation == "support" else 0),
                        "matchedConditions": [],
                        "failedConditions": [],
                        "reason": relation,
                    }
                )
            return result

        report = await build_content_reranker_validation_report(
            cases,
            vector_retrieve=vector_retrieve,
            bm25_retrieve=bm25_retrieve,
            judge_batch=fake_judge,
            candidate_limit=2,
            top_k=2,
            batch_size=2,
        )

        reranker = report["methods"]["llmContentReranker"]["metrics"]
        self.assertEqual(reranker["meanRecallAt2"], 1.0)
        self.assertEqual(reranker["mrr"], 1.0)
        self.assertFalse(report["methodology"]["testRead"])


if __name__ == "__main__":
    unittest.main()
