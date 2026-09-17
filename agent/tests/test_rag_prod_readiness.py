import unittest

from app.rag_prod_readiness import build_rag_prod_readiness_report


def _inputs(*, reranker_ms: float = 9000.0, faithful: float = 0.5):
    validation = {"summary": {"passed": True}}
    agent = {
        "summary": {"passed": True, "complete": True},
        "failures": [],
    }
    sealed = {
        "retrieval": {
            "hybridV02B08": {
                "meanNdcgAt5": 0.3,
                "meanEvidenceShopRecall": 1.0,
            },
            "llmContentReranker": {
                "meanNdcgAt5": 0.7,
                "meanEvidenceShopRecall": 1.0,
            },
        },
        "contextSelection": {
            "reviewCountReduction": 0.8,
            "averageSelectedReviewCount": 7.0,
        },
        "generation": {
            "meanCitationIdValidity": 1.0,
            "meanCitationShopConsistency": 1.0,
            "fullyFaithfulAnswerRate": faithful,
        },
        "timing": {
            "averageContentRerankerMs": reranker_ms,
            "details": [{"contentRerankerMs": reranker_ms}],
        },
    }
    return validation, agent, sealed


class RagProdReadinessTests(unittest.TestCase):
    def test_keeps_stages_four_and_five_shadow_when_gates_fail(self):
        report = build_rag_prod_readiness_report(
            *_inputs(), sealed_report_sha256="abc"
        )

        self.assertEqual(report["stage3ReviewHybrid"]["decision"], "canary_ready")
        self.assertEqual(
            report["stage4LlmContentReranker"]["decision"],
            "no_go_keep_shadow",
        )
        self.assertEqual(
            report["stage5ContextAndGroundedAnswer"]["decision"],
            "blocked_keep_shadow",
        )
        self.assertFalse(report["overall"]["defaultRouteChangeAuthorized"])

    def test_allows_later_stages_only_when_every_gate_passes(self):
        report = build_rag_prod_readiness_report(
            *_inputs(reranker_ms=2000.0, faithful=0.95),
            sealed_report_sha256="abc",
        )

        self.assertEqual(
            report["stage4LlmContentReranker"]["decision"], "canary_ready"
        )
        self.assertEqual(
            report["stage5ContextAndGroundedAnswer"]["decision"], "canary_ready"
        )


if __name__ == "__main__":
    unittest.main()
