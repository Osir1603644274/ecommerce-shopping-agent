import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.rag_answer import NO_EVIDENCE_ANSWER, RagGenerationError, RagRetrievalError


class RagEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_returns_answer_and_serialized_sources(self):
        sources = [
            {
                "reviewId": "review-001",
                "shopId": 1,
                "shopName": "巷子口火锅",
                "text": "微辣对不太能吃辣的人也友好，两个人吃人均约 90 元。",
                "score": 0.91,
            }
        ]
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(
                return_value=(
                    "巷子口火锅的微辣比较友好，人均约 90 元。[review-001]",
                    sources,
                    {"retrievalDurationMs": 12.34, "llmDurationMs": 56.78},
                )
            ),
        ) as answer_mock:
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "两个人吃火锅，不能吃太辣，有什么推荐？", "mode": "standard"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("[review-001]", body["answer"])
        self.assertEqual(body["sources"], sources)
        self.assertEqual(body["trace"]["route"], "/agent/rag-chat")
        self.assertEqual(body["trace"]["status"], "ok")
        self.assertEqual(body["trace"]["reviewIds"], ["review-001"])
        self.assertEqual(body["trace"]["retrievalDurationMs"], 12.34)
        self.assertEqual(body["trace"]["llmDurationMs"], 56.78)
        self.assertEqual(body["trace"]["bottleneck"], "none")
        answer_mock.assert_awaited_once_with("两个人吃火锅，不能吃太辣，有什么推荐？")

    def test_explicit_advanced_mode_uses_quality_pipeline(self):
        sources = [
            {
                "reviewId": "review-advanced",
                "shopId": 7,
                "shopName": "目标咖啡店",
                "text": "安静且有插座。",
                "score": 0.92,
            }
        ]
        pipeline = {
            "requestedMode": "advanced",
            "effectiveMode": "advanced",
            "stages": ["hybrid_retrieval", "llm_content_rerank"],
            "citationAudit": {"deterministicValidRecommendationRate": 1.0},
        }
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.answer_with_advanced_rag_observed",
            new=AsyncMock(
                return_value=(
                    "目标咖啡店适合办公。[review-advanced]",
                    sources,
                    {"retrievalDurationMs": 20.0, "llmDurationMs": 200.0},
                    pipeline,
                )
            ),
        ) as advanced_mock, patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(),
        ) as standard_mock:
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "哪家咖啡店适合办公？", "mode": "advanced"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["mode"], "advanced")
        self.assertEqual(body["pipeline"]["effectiveMode"], "advanced")
        self.assertEqual(body["pipeline"]["autoRouteReason"], "explicit_mode")
        self.assertEqual(body["trace"]["reviewIds"], ["review-advanced"])
        advanced_mock.assert_awaited_once_with("哪家咖啡店适合办公？")
        standard_mock.assert_not_awaited()

    def test_auto_routes_preference_question_to_advanced(self):
        pipeline = {"effectiveMode": "advanced", "stages": []}
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.answer_with_advanced_rag_observed",
            new=AsyncMock(return_value=("回答", [], {}, pipeline)),
        ) as advanced_mock, patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(),
        ) as standard_mock:
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "想找一家安静、适合办公的咖啡店"},
            )

        self.assertEqual(response.json()["mode"], "advanced")
        self.assertEqual(
            response.json()["pipeline"]["autoRouteReason"],
            "preference_or_recommendation_query",
        )
        advanced_mock.assert_awaited_once()
        standard_mock.assert_not_awaited()

    def test_auto_routes_plain_fact_question_to_standard(self):
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.answer_with_advanced_rag_observed",
            new=AsyncMock(),
        ) as advanced_mock, patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(return_value=("回答", [], {})),
        ) as standard_mock:
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "评论里提到这家店几点关门？"},
            )

        self.assertEqual(response.json()["mode"], "standard")
        standard_mock.assert_awaited_once()
        advanced_mock.assert_not_awaited()

    def test_marks_slow_rag_request_as_llm_bottleneck(self):
        sources = [
            {
                "reviewId": "review-001",
                "shopId": 1,
                "shopName": "巷子口火锅",
                "text": "微辣比较友好。",
                "score": 0.91,
            }
        ]

        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.SLOW_REQUEST_THRESHOLD_MS",
            0.0,
        ), patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(
                return_value=(
                    "巷子口火锅的微辣比较友好。[review-001]",
                    sources,
                    {"retrievalDurationMs": 20.0, "llmDurationMs": 100.0},
                )
            ),
        ):
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "哪家火锅不太辣？", "mode": "standard"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["trace"]["slow"])
        self.assertEqual(body["trace"]["bottleneck"], "llm")

    def test_marks_slow_rag_request_as_retrieval_bottleneck(self):
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.SLOW_REQUEST_THRESHOLD_MS",
            0.0,
        ), patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(
                return_value=(
                    NO_EVIDENCE_ANSWER,
                    [],
                    {"retrievalDurationMs": 100.0, "llmDurationMs": 20.0},
                )
            ),
        ):
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "有没有适合通宵学习的咖啡店？", "mode": "standard"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["trace"]["slow"])
        self.assertEqual(body["trace"]["bottleneck"], "retrieval")

    def test_returns_empty_sources_when_no_evidence(self):
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(return_value=(NO_EVIDENCE_ANSWER, [], {"retrievalDurationMs": 9.87})),
        ):
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "有没有适合通宵学习的咖啡店？", "mode": "standard"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], NO_EVIDENCE_ANSWER)
        self.assertEqual(response.json()["sources"], [])
        self.assertEqual(response.json()["trace"]["reviewIds"], [])

    def test_returns_safe_message_when_retrieval_fails(self):
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(side_effect=RagRetrievalError("internal secret")),
        ):
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "哪家火锅不太辣？", "mode": "standard"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["answer"], "评论检索服务暂时不可用，请稍后重试。")
        self.assertNotIn("secret", body["answer"])
        self.assertEqual(body["sources"], [])
        self.assertEqual(body["trace"]["status"], "retrieval_error")

    def test_returns_retrieved_sources_when_generation_fails(self):
        sources = [
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座。",
                "score": 0.89,
            }
        ]
        with patch("app.main.settings.deepseek_api_key", "test-key"), patch(
            "app.main.answer_with_rag_observed",
            new=AsyncMock(
                side_effect=RagGenerationError(
                    sources,
                    {"retrievalDurationMs": 15.0},
                )
            ),
        ):
            response = self.client.post(
                "/agent/rag-chat",
                json={"message": "哪家咖啡店适合办公？", "mode": "standard"},
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            body["answer"],
            "已找到相关评论，但回答生成服务暂时不可用，请稍后重试。",
        )
        self.assertEqual(body["sources"], sources)
        self.assertEqual(body["trace"]["status"], "generation_error")
        self.assertEqual(body["trace"]["reviewIds"], ["review-005"])
        self.assertEqual(body["trace"]["retrievalDurationMs"], 15.0)


if __name__ == "__main__":
    unittest.main()
