import unittest

from evaluation.rag_q15_evaluation import build_q15_test_report, load_q15_test_cases


def candidate(review_id, shop_id, score):
    return {
        "reviewId": review_id,
        "shopId": shop_id,
        "shopName": f"shop-{shop_id}",
        "text": f"evidence-{review_id}",
        "score": score,
    }


class RagQ15EvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_sealed_test_cases_and_pooled_evidence_are_consistent(self):
        cases = load_q15_test_cases()

        self.assertEqual(len(cases), 6)
        self.assertEqual(
            sum(len(case["relevanceJudgments"]) for case in cases),
            29,
        )

    async def test_qrels_are_not_passed_to_generation_or_faithfulness_judge(self):
        cases = [
            {
                "id": "case-1",
                "question": "安静且有早餐",
                "relevanceJudgments": [
                    {
                        "shopId": 1,
                        "relevance": 3,
                        "supportingReviewIds": ["r1"],
                    },
                    {
                        "shopId": 2,
                        "relevance": 2,
                        "supportingReviewIds": ["r2"],
                    },
                ],
            }
        ]

        def vector_retrieve(question, limit):
            self.assertEqual(question, "安静且有早餐")
            return [candidate("r1", 1, 0.9), candidate("r2", 2, 0.8)]

        def bm25_retrieve(question, limit):
            return [candidate("r2", 2, 5.0), candidate("r1", 1, 4.0)]

        async def content_rerank(question, reviews):
            return [
                {
                    **review,
                    "originalRank": index,
                    "contentRelation": "support",
                    "contentScore": 3,
                    "contentReason": "direct support",
                }
                for index, review in enumerate(reviews, start=1)
            ]

        async def answer_generator(question, context):
            self.assertNotIn("relevanceJudgments", context)
            first = context["selectedReviews"][0]
            return {
                "answer": "grounded",
                "recommendations": [
                    {
                        "shopId": first["shopId"],
                        "shopName": first["shopName"],
                        "reason": "direct support",
                        "citationReviewIds": [first["reviewId"]],
                        "caveat": "",
                        "caveatCitationReviewIds": [],
                    }
                ],
            }

        async def faithfulness_judge(question, answer, context):
            self.assertNotIn("relevanceJudgments", context)
            return {
                "results": [
                    {"index": 0, "label": "entailed", "reason": "supported"}
                ],
                "entailedRecommendationCount": 1,
                "entailedRecommendationRate": 1.0,
                "fullyFaithful": True,
            }

        report = await build_q15_test_report(
            cases,
            vector_retrieve=vector_retrieve,
            bm25_retrieve=bm25_retrieve,
            content_rerank=content_rerank,
            answer_generator=answer_generator,
            faithfulness_judge=faithfulness_judge,
        )

        self.assertEqual(report["caseCount"], 1)
        self.assertTrue(
            report["methodology"][
                "qrelsVisibleToRetrievalRankingGenerationOrJudge"
            ]
            is False
        )
        self.assertEqual(report["generation"]["meanCitationIdValidity"], 1.0)
        self.assertEqual(report["generation"]["fullyFaithfulAnswerRate"], 1.0)


if __name__ == "__main__":
    unittest.main()
