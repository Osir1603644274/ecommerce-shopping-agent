import unittest

from app.rag_grounded_answer import (
    audit_grounded_answer,
    parse_faithfulness_response,
    parse_grounded_answer_response,
    render_grounded_answer,
)


def selection():
    return {
        "shops": [
            {
                "shopId": 1,
                "shopName": "shop-1",
                "shopRank": 1,
                "evidenceStatus": "mixed",
                "selectedSupportCount": 1,
                "selectedConflictCount": 1,
            },
            {
                "shopId": 2,
                "shopName": "shop-2",
                "shopRank": 2,
                "evidenceStatus": "supported",
                "selectedSupportCount": 1,
                "selectedConflictCount": 0,
            },
        ],
        "selectedReviews": [
            {
                "reviewId": "support-1",
                "shopId": 1,
                "selectedRole": "support",
                "contextText": "支持第一家店",
            },
            {
                "reviewId": "conflict-1",
                "shopId": 1,
                "selectedRole": "conflict",
                "contextText": "第一家店也有冲突",
            },
            {
                "reviewId": "support-2",
                "shopId": 2,
                "selectedRole": "support",
                "contextText": "支持第二家店",
            },
        ],
    }


class RagGroundedAnswerTests(unittest.TestCase):
    def test_parse_and_render_keeps_explicit_review_citations(self):
        answer = parse_grounded_answer_response(
            'prefix {"recommendations":[{"shopId":"1","shopName":"shop-1",'
            '"reason":"理由","citationReviewIds":["support-1","support-1"],'
            '"caveat":"注意冲突","caveatCitationReviewIds":["conflict-1"]}]} suffix'
        )

        rendered = render_grounded_answer(answer)

        self.assertEqual(answer["recommendations"][0]["shopId"], 1)
        self.assertEqual(
            answer["recommendations"][0]["citationReviewIds"],
            ["support-1"],
        )
        self.assertIn("[support-1]", rendered)
        self.assertIn("[conflict-1]", rendered)

    def test_audit_accepts_same_shop_support_and_cited_conflict(self):
        answer = {
            "recommendations": [
                {
                    "shopId": 1,
                    "shopName": "shop-1",
                    "reason": "理由",
                    "citationReviewIds": ["support-1"],
                    "caveat": "有冲突",
                    "caveatCitationReviewIds": ["conflict-1"],
                }
            ]
        }

        audit = audit_grounded_answer(answer, selection())

        self.assertEqual(audit["citationIdValidity"], 1.0)
        self.assertEqual(audit["citationShopConsistency"], 1.0)
        self.assertEqual(audit["citationCompleteness"], 1.0)
        self.assertEqual(audit["deterministicValidRecommendationRate"], 1.0)

    def test_audit_exposes_missing_wrong_shop_and_wrong_role_citations(self):
        answer = {
            "recommendations": [
                {
                    "shopId": 1,
                    "shopName": "shop-1",
                    "reason": "理由",
                    "citationReviewIds": ["support-2", "missing"],
                    "caveat": "有冲突",
                    "caveatCitationReviewIds": ["support-1"],
                }
            ]
        }

        audit = audit_grounded_answer(answer, selection())
        detail = audit["details"][0]

        self.assertEqual(detail["missingCitationIds"], ["missing"])
        self.assertEqual(detail["wrongShopCitationIds"], ["support-2"])
        self.assertFalse(detail["citationComplete"])
        self.assertFalse(detail["deterministicValid"])

    def test_missing_faithfulness_judgment_fails_closed(self):
        result = parse_faithfulness_response(
            '{"results":[{"index":0,"label":"entailed","reason":"ok"}]}',
            2,
        )

        self.assertEqual(result["results"][1]["label"], "unsupported")
        self.assertEqual(result["entailedRecommendationRate"], 0.5)
        self.assertFalse(result["fullyFaithful"])


if __name__ == "__main__":
    unittest.main()
