import unittest

from evaluation.rag_evaluation import evaluate_generation, parse_judge_response


class RagEvaluationTests(unittest.IsolatedAsyncioTestCase):
    def test_parse_judge_response_normalizes_fenced_json_and_missing_points(self):
        content = """```json
        {
          "pointResults": [
            {"index": 0, "covered": true, "reason": "回答明确提到"}
          ],
          "unsupportedClaims": [],
          "faithful": true
        }
        ```"""

        result = parse_judge_response(content, ["有插座", "有靠窗单人位"])

        self.assertTrue(result["pointResults"][0]["covered"])
        self.assertFalse(result["pointResults"][1]["covered"])
        self.assertTrue(result["faithful"])

    def test_parse_judge_response_ignores_claim_not_marked_unsupported(self):
        content = """
        {
          "pointResults": [],
          "unsupportedClaims": [
            {
              "claim": "其他评论与问题无关",
              "unsupported": false,
              "reason": "这是对证据范围的说明，不是新增商户事实"
            }
          ],
          "faithful": false
        }
        """

        result = parse_judge_response(content, [])

        self.assertEqual(result["unsupportedClaims"], [])
        self.assertTrue(result["faithful"])

    async def test_reports_retrieval_coverage_and_faithfulness_separately(self):
        cases = [
            {
                "id": "case-ok",
                "question": "哪家适合办公？",
                "relevantReviewIds": ["review-005"],
                "expectedPoints": ["有插座", "适合办公"],
            },
            {
                "id": "case-bad",
                "question": "能通宵办公吗？",
                "relevantReviewIds": ["review-099"],
                "expectedPoints": ["没有通宵营业证据"],
            },
        ]

        async def fake_answerer(question):
            if "哪家" in question:
                return "清晨手冲咖啡有插座，适合办公。[review-005]", [
                    {
                        "reviewId": "review-005",
                        "shopId": 3,
                        "shopName": "清晨手冲咖啡",
                        "text": "店里有插座，适合办公。",
                        "score": 0.9,
                    }
                ]
            return "这家店可以通宵，而且提供免费夜宵。", [
                {
                    "reviewId": "review-006",
                    "shopId": 3,
                    "shopName": "清晨手冲咖啡",
                    "text": "周末下午客人较多。",
                    "score": 0.4,
                }
            ]

        async def fake_judge(question, answer, sources, expected_points):
            if "哪家" in question:
                return {
                    "pointResults": [
                        {"expectedPoint": "有插座", "covered": True, "reason": "已提到"},
                        {"expectedPoint": "适合办公", "covered": True, "reason": "已提到"},
                    ],
                    "unsupportedClaims": [],
                    "faithful": True,
                }
            return {
                "pointResults": [
                    {
                        "expectedPoint": "没有通宵营业证据",
                        "covered": False,
                        "reason": "回答反而声称可以通宵",
                    }
                ],
                "unsupportedClaims": [
                    {"claim": "可以通宵", "reason": "评论没有营业时间"},
                    {"claim": "免费夜宵", "reason": "评论没有夜宵信息"},
                ],
                "faithful": False,
            }

        report = await evaluate_generation(cases, fake_answerer, fake_judge)

        self.assertEqual(report["totalCases"], 2)
        self.assertEqual(report["retrieval"]["hits"], 1)
        self.assertEqual(report["retrieval"]["hitRate"], 0.5)
        self.assertEqual(report["generation"]["coveredExpectedPoints"], 2)
        self.assertAlmostEqual(report["generation"]["coverageRate"], 2 / 3)
        self.assertEqual(report["generation"]["faithfulAnswers"], 1)
        self.assertEqual(report["failureCounts"]["retrieval_miss"], 1)
        self.assertEqual(report["failureCounts"]["missing_expected_points"], 1)
        self.assertEqual(report["failureCounts"]["unsupported_claims"], 1)
        self.assertEqual(
            report["details"][1]["failureTypes"],
            ["retrieval_miss", "missing_expected_points", "unsupported_claims"],
        )


if __name__ == "__main__":
    unittest.main()
