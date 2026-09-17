import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.domains.ecommerce.title_relevance import rerank_titles


def _response(rows):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"judgments": rows}, ensure_ascii=False)
        ))],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40),
    )


def _client(rows):
    client = SimpleNamespace()
    client.chat = SimpleNamespace()
    client.chat.completions = SimpleNamespace(
        create=AsyncMock(return_value=_response(rows))
    )
    return client


PRODUCTS = [
    {
        "id": 101,
        "title": "红米k70pro大内存拍照音乐游戏竞技二手99新手机",
    },
    {
        "id": 102,
        "title": "vivoY100i和平精英吃鸡手机",
    },
    {
        "id": 103,
        "title": "苹果7学生备用二手手机",
    },
]


def test_title_reranker_accepts_only_literal_title_evidence_and_orders_scores():
    rows = [
        {"productId": 101, "relevance": 3, "matchedEvidence": ["游戏竞技"]},
        {"productId": 102, "relevance": 3, "matchedEvidence": ["和平精英", "吃鸡"]},
        {"productId": 103, "relevance": 0, "matchedEvidence": []},
    ]
    result = asyncio.run(rerank_titles(
        "有没有适合打游戏的手机",
        PRODUCTS,
        client=_client(rows),
    ))

    assert result.ordered_product_ids == (101, 102, 103)
    assert result.prompt_tokens == 120
    assert result.completion_tokens == 40


@pytest.mark.parametrize(
    "rows",
    [
        [
            {"productId": 999, "relevance": 3, "matchedEvidence": []},
            {"productId": 102, "relevance": 3, "matchedEvidence": ["吃鸡"]},
            {"productId": 103, "relevance": 0, "matchedEvidence": []},
        ],
        [
            {"productId": 101, "relevance": 3, "matchedEvidence": ["高帧率"]},
            {"productId": 102, "relevance": 3, "matchedEvidence": ["吃鸡"]},
            {"productId": 103, "relevance": 0, "matchedEvidence": []},
        ],
        [
            {"productId": 101, "relevance": 3, "matchedEvidence": ["游戏竞技"]},
            {"productId": 102, "relevance": 3, "matchedEvidence": ["吃鸡"]},
        ],
    ],
)
def test_title_reranker_rejects_invented_ids_evidence_and_missing_rows(rows):
    with pytest.raises(ValueError):
        asyncio.run(rerank_titles(
            "有没有适合打游戏的手机",
            PRODUCTS,
            client=_client(rows),
        ))


def test_title_reranker_timeout_is_bounded_for_caller_fallback():
    async def never_returns(**_kwargs):
        await asyncio.Future()

    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=never_returns)
        )
    )
    with patch(
        "app.domains.ecommerce.title_relevance.settings.product_title_reranker_timeout_seconds",
        0.01,
    ), pytest.raises(asyncio.TimeoutError):
        asyncio.run(rerank_titles(
            "有没有适合打游戏的手机",
            PRODUCTS,
            client=client,
        ))
