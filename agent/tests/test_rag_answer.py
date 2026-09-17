import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.rag_answer import (
    EMPTY_MODEL_ANSWER,
    NO_EVIDENCE_ANSWER,
    RagGenerationError,
    RagRetrievalError,
    answer_with_rag,
)


def _fake_client(create_mock):
    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_mock))
    )


def _make_response(content: str):
    message = SimpleNamespace(content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class RagAnswerTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieves_reviews_and_sends_traceable_evidence_to_llm(self):
        reviews = [
            {
                "reviewId": "review-001",
                "shopId": 1,
                "shopName": "巷子口火锅",
                "text": "微辣对不太能吃辣的人也友好，两个人吃人均约 90 元。",
                "score": 0.91,
            }
        ]
        create_mock = AsyncMock(
            return_value=_make_response(
                "巷子口火锅的微辣比较友好，人均约 90 元。[review-001]"
            )
        )

        with patch("app.rag_answer.search_hybrid_reviews", return_value=reviews) as retrieve_mock, patch(
            "app.rag_answer.get_client", return_value=_fake_client(create_mock)
        ):
            answer, sources = await answer_with_rag(
                " 两个人吃火锅，不能吃太辣，有什么推荐？ "
            )

        self.assertIn("[review-001]", answer)
        self.assertEqual(sources, reviews)
        retrieve_mock.assert_called_once_with(
            "两个人吃火锅，不能吃太辣，有什么推荐？",
            3,
        )

        request = create_mock.await_args.kwargs
        self.assertEqual(request["temperature"], 0)
        self.assertIn("只能依据", request["messages"][0]["content"])
        user_prompt = request["messages"][1]["content"]
        self.assertIn("review-001", user_prompt)
        self.assertIn("巷子口火锅", user_prompt)
        self.assertIn("微辣对不太能吃辣的人也友好", user_prompt)

    async def test_returns_fixed_message_without_calling_llm_when_no_evidence(self):
        with patch("app.rag_answer.search_hybrid_reviews", return_value=[]) as retrieve_mock, patch(
            "app.rag_answer.get_client"
        ) as get_client_mock:
            answer, sources = await answer_with_rag("有没有适合通宵学习的咖啡店？")

        self.assertEqual(answer, NO_EVIDENCE_ANSWER)
        self.assertEqual(sources, [])
        retrieve_mock.assert_called_once_with("有没有适合通宵学习的咖啡店？", 3)
        get_client_mock.assert_not_called()

    async def test_wraps_retrieval_failure_without_calling_llm(self):
        with patch(
            "app.rag_answer.search_hybrid_reviews",
            side_effect=ConnectionError("qdrant at secret-host:6333 is unavailable"),
        ), patch("app.rag_answer.get_client") as get_client_mock:
            with self.assertRaises(RagRetrievalError) as raised:
                await answer_with_rag("哪家咖啡店适合办公？")

        self.assertNotIn("secret-host", str(raised.exception))
        get_client_mock.assert_not_called()

    async def test_keeps_sources_when_generation_fails(self):
        reviews = [
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座。",
                "score": 0.89,
            }
        ]
        create_mock = AsyncMock(side_effect=TimeoutError("upstream secret detail"))

        with patch("app.rag_answer.search_hybrid_reviews", return_value=reviews), patch(
            "app.rag_answer.get_client", return_value=_fake_client(create_mock)
        ):
            with self.assertRaises(RagGenerationError) as raised:
                await answer_with_rag("哪家咖啡店适合办公？")

        self.assertEqual(raised.exception.sources, reviews)
        self.assertNotIn("secret detail", str(raised.exception))

    async def test_returns_fallback_when_model_content_is_empty(self):
        reviews = [
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座。",
                "score": 0.89,
            }
        ]
        create_mock = AsyncMock(return_value=_make_response("   "))

        with patch("app.rag_answer.search_hybrid_reviews", return_value=reviews), patch(
            "app.rag_answer.get_client", return_value=_fake_client(create_mock)
        ):
            answer, sources = await answer_with_rag("哪家咖啡店适合办公？")

        self.assertEqual(answer, EMPTY_MODEL_ANSWER)
        self.assertEqual(sources, reviews)

    async def test_replaces_answer_with_unknown_citation(self):
        reviews = [
            {
                "reviewId": "review-005",
                "shopId": 3,
                "shopName": "清晨手冲咖啡",
                "text": "店里有靠窗的单人位和插座。",
                "score": 0.89,
            }
        ]
        create_mock = AsyncMock(return_value=_make_response("这里适合办公。[missing]"))

        with patch("app.rag_answer.search_hybrid_reviews", return_value=reviews), patch(
            "app.rag_answer.get_client", return_value=_fake_client(create_mock)
        ):
            answer, sources = await answer_with_rag("地址是什么？")

        self.assertIn("回答引用未通过校验", answer)
        self.assertIn("[review-005]", answer)
        self.assertEqual(sources, reviews)


if __name__ == "__main__":
    unittest.main()
