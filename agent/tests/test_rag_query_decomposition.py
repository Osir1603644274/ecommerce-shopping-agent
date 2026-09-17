import unittest

from app.rag_query_decomposed_reranker import (
    parse_condition_judge_response,
    rerank_reviews_by_shop_condition_coverage,
    rerank_reviews_with_query_decomposition,
)
from evaluation.rag_query_decomposed_reranker_evaluation import (
    build_query_decomposed_reranker_validation_report,
)
from app.rag_query_decomposition import parse_query_decomposition_response


def review(review_id, shop_id, score, text=None):
    return {
        "reviewId": review_id,
        "shopId": shop_id,
        "shopName": f"shop-{shop_id}",
        "score": score,
        "text": text or review_id,
    }


def laptop_decomposition(question="想带电脑待几个小时，最好座位舒服"):
    return {
        "query": question,
        "summary": "适合带电脑长时间停留",
        "conditions": [
            {
                "id": "c1",
                "text": "适合使用笔记本电脑工作",
                "importance": "required",
                "polarity": "positive",
                "evidenceMode": "explicit",
            },
            {
                "id": "c2",
                "text": "允许长时间停留",
                "importance": "preferred",
                "polarity": "positive",
                "evidenceMode": "explicit",
            },
        ],
    }


class RagQueryDecompositionTests(unittest.IsolatedAsyncioTestCase):
    def test_parser_normalizes_fields_and_guarantees_required_condition(self):
        content = """{
          "summary": "安静放松",
          "conditions": [
            {"id":"quiet","text":"环境安静","importance":"optional",
             "polarity":"unknown","evidenceMode":"guess"}
          ]
        }"""

        result = parse_query_decomposition_response(content, "想安静待一会儿")

        self.assertEqual(result["conditions"][0]["importance"], "required")
        self.assertEqual(result["conditions"][0]["polarity"], "positive")
        self.assertEqual(result["conditions"][0]["evidenceMode"], "explicit")

    def test_missing_required_condition_cannot_be_support(self):
        reviews = [review("hours", 1, 0.9)]
        content = """{
          "results": [{
            "index": 0,
            "conditions": [
              {"conditionId":"c1","status":"not_mentioned","evidence":""},
              {"conditionId":"c2","status":"supports_preference","evidence":"坐了几小时"}
            ],
            "reason":"只证明久坐，没有电脑工作证据"
          }]
        }"""

        result = parse_condition_judge_response(
            content,
            laptop_decomposition(),
            reviews,
        )

        self.assertEqual(result[0]["relation"], "irrelevant")
        self.assertEqual(result[0]["score"], 0)
        self.assertIn("适合使用笔记本电脑工作", result[0]["failedConditions"])

    def test_required_conflict_is_demoted_and_complete_support_scores_three(self):
        reviews = [review("no-laptop", 1, 0.9), review("complete", 2, 0.8)]
        content = """{
          "results": [
            {"index":0,"conditions":[
              {"conditionId":"c1","status":"conflicts_preference","evidence":"禁止使用电脑"},
              {"conditionId":"c2","status":"supports_preference","evidence":"可久坐"}
            ]},
            {"index":1,"conditions":[
              {"conditionId":"c1","status":"supports_preference","evidence":"很多人带电脑工作"},
              {"conditionId":"c2","status":"supports_preference","evidence":"待了一下午"}
            ]}
          ]
        }"""

        result = parse_condition_judge_response(
            content,
            laptop_decomposition(),
            reviews,
        )

        self.assertEqual(result[0]["relation"], "conflict")
        self.assertEqual(result[1]["relation"], "support")
        self.assertEqual(result[1]["score"], 3)

    def test_shop_coverage_combines_conditions_from_multiple_reviews(self):
        decomposition = laptop_decomposition()
        reviews = [
            {
                **review("laptop", 1, 0.9),
                "originalRank": 1,
                "contentRelation": "irrelevant",
                "contentScore": 0,
                "conditionAssessments": [
                    {"conditionId": "c1", "status": "supports_preference", "evidence": "带电脑"},
                    {"conditionId": "c2", "status": "not_mentioned", "evidence": ""},
                ],
            },
            {
                **review("hours", 1, 0.8),
                "originalRank": 2,
                "contentRelation": "irrelevant",
                "contentScore": 0,
                "conditionAssessments": [
                    {"conditionId": "c1", "status": "not_mentioned", "evidence": ""},
                    {"conditionId": "c2", "status": "supports_preference", "evidence": "待一下午"},
                ],
            },
            {
                **review("noise", 2, 0.7),
                "originalRank": 3,
                "contentRelation": "irrelevant",
                "contentScore": 0,
                "conditionAssessments": [
                    {"conditionId": "c1", "status": "not_mentioned", "evidence": ""},
                    {"conditionId": "c2", "status": "not_mentioned", "evidence": ""},
                ],
            },
        ]

        ranked = rerank_reviews_by_shop_condition_coverage(decomposition, reviews)

        self.assertEqual(ranked[0]["shopId"], 1)
        self.assertEqual(ranked[0]["shopConditionRelation"], "support")
        self.assertEqual(ranked[0]["shopConditionScore"], 3)
        self.assertEqual(ranked[-1]["shopId"], 2)

    async def test_decomposes_once_and_keeps_batch_alignment(self):
        reviews = [review(str(index), index, 1 - index / 10) for index in range(5)]
        decompose_calls = []
        batch_sizes = []

        async def fake_decompose(question):
            decompose_calls.append(question)
            return laptop_decomposition(question)

        async def fake_judge(decomposition, batch):
            batch_sizes.append(len(batch))
            return [
                {
                    "reviewId": item["reviewId"],
                    "relation": "support",
                    "score": 1,
                    "matchedConditions": [],
                    "failedConditions": [],
                    "conditionAssessments": [],
                    "reason": "支持",
                }
                for item in batch
            ]

        decomposition, reranked = await rerank_reviews_with_query_decomposition(
            "query",
            reviews,
            decompose_query=fake_decompose,
            judge_batch=fake_judge,
            batch_size=2,
        )

        self.assertEqual(decompose_calls, ["query"])
        self.assertEqual(sorted(batch_sizes), [1, 2, 2])
        self.assertEqual(decomposition["query"], "query")
        self.assertEqual([item["reviewId"] for item in reranked], ["0", "1", "2", "3", "4"])

    async def test_evaluation_never_passes_qrels_to_decomposer_or_judge(self):
        cases = [
            {
                "id": "case-1",
                "question": "想带电脑工作",
                "relevanceJudgments": [
                    {
                        "shopId": 1,
                        "relevance": 3,
                        "supportingReviewIds": ["good"],
                    }
                ],
            }
        ]

        def vector_retrieve(question, limit):
            return [review("bad", 9, 0.9), review("good", 1, 0.8)]

        def bm25_retrieve(question, limit):
            return [review("good", 1, 9.0), review("bad", 9, 8.0)]

        async def fake_decompose(question):
            self.assertEqual(question, "想带电脑工作")
            return laptop_decomposition(question)

        async def fake_judge(decomposition, batch):
            self.assertNotIn("relevanceJudgments", decomposition)
            return [
                {
                    "reviewId": item["reviewId"],
                    "relation": "support" if item["reviewId"] == "good" else "irrelevant",
                    "score": 3 if item["reviewId"] == "good" else 0,
                    "matchedConditions": [],
                    "failedConditions": [],
                    "conditionAssessments": [],
                    "reason": "test",
                }
                for item in batch
            ]

        report = await build_query_decomposed_reranker_validation_report(
            cases,
            vector_retrieve=vector_retrieve,
            bm25_retrieve=bm25_retrieve,
            decompose_query=fake_decompose,
            judge_batch=fake_judge,
            candidate_limit=2,
            top_k=2,
            batch_size=2,
        )

        metrics = report["methods"]["queryDecomposedContentReranker"]["metrics"]
        self.assertEqual(metrics["mrr"], 1.0)
        self.assertFalse(report["methodology"]["testRead"])


if __name__ == "__main__":
    unittest.main()
