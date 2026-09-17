import asyncio
import re
import time
from typing import Any

from .knowledge.models import SearchKnowledgeResult
from .knowledge.review_hybrid_search import search_review_hybrid
from .llm import get_client
from .rag import DEFAULT_TOP_K
from .settings import settings


NO_EVIDENCE_ANSWER = "现有评论中没有找到足够相关的证据，暂时无法据此回答。"
EMPTY_MODEL_ANSWER = "已找到相关评论，但大模型暂时没有生成有效回答，请稍后重试。"

RAG_SYSTEM_PROMPT = (
    "你是“本地生活智能体”的评论问答助手。"
    "你只能依据用户消息中提供的评论证据回答，不能使用常识补充未出现的商户事实。"
    "每个事实结论后都要用方括号标注对应的评论编号，例如 [review-001]。"
    "如果证据不足以回答，就明确说现有评论信息不足，不要猜测。"
    "回答使用简洁、自然的中文。"
)

CITATION_PATTERN = re.compile(r"\[([^\[\]]+)\]")


class RagRetrievalError(RuntimeError):
    """Qdrant 评论检索失败；原始异常只保留在服务端异常链中。"""


class RagGenerationError(RuntimeError):
    """已经取得评论证据，但调用大模型或解析模型响应失败。"""

    def __init__(
        self,
        sources: list[dict[str, Any]],
        metrics: dict[str, float] | None = None,
    ):
        super().__init__("RAG answer generation failed")
        self.sources = sources
        self.metrics = metrics or {}


def format_review_evidence(reviews: list[dict[str, Any]]) -> str:
    """把检索结果整理成边界清晰、带来源编号的模型上下文。"""
    blocks = []
    for review in reviews:
        blocks.append(
            "\n".join(
                [
                    f"评论编号：{review['reviewId']}",
                    f"商户：{review['shopName']}",
                    f"评论原文：{review['text']}",
                ]
            )
        )
    return "\n\n".join(blocks)


def knowledge_result_to_reviews(
    result: SearchKnowledgeResult,
) -> list[dict[str, Any]]:
    scores = {citation.chunk_id: citation.score for citation in result.citations}
    reviews: list[dict[str, Any]] = []
    for chunk in result.chunks:
        metadata = chunk.metadata
        reviews.append(
            {
                "reviewId": str(metadata.get("reviewId") or chunk.source_id),
                "shopId": int(metadata["shopId"]),
                "shopName": str(metadata.get("shopName") or ""),
                "text": chunk.content,
                "score": float(scores.get(chunk.chunk_id) or 0.0),
                "vectorRank": metadata.get("vectorRank"),
                "bm25Rank": metadata.get("bm25Rank"),
                "vectorScore": metadata.get("vectorScore"),
                "bm25Score": metadata.get("bm25Score"),
                "fusionScore": metadata.get("fusionScore"),
            }
        )
    return reviews


def search_hybrid_reviews(
    question: str,
    limit: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    return knowledge_result_to_reviews(search_review_hybrid(question, limit))


def render_review_evidence_fallback(
    reviews: list[dict[str, Any]],
    *,
    prefix: str = "以下是检索到的相关评论原文：",
) -> str:
    lines = [prefix]
    for review in reviews:
        text = " ".join(str(review.get("text") or "").split())
        lines.append(
            f"- {review.get('shopName') or '未知商户'}：{text} "
            f"[{review['reviewId']}]"
        )
    return "\n".join(lines)


def answer_citations_are_valid(
    answer: str,
    reviews: list[dict[str, Any]],
) -> bool:
    cited = CITATION_PATTERN.findall(answer)
    allowed = {str(review["reviewId"]) for review in reviews}
    return bool(cited) and all(review_id in allowed for review_id in cited)


async def answer_with_rag(
    question: str,
    limit: int = DEFAULT_TOP_K,
) -> tuple[str, list[dict[str, Any]]]:
    """返回受评论证据约束的回答，以及本次实际提供给模型的检索结果。"""
    answer, reviews, _metrics = await answer_with_rag_observed(question, limit)
    return answer, reviews


async def answer_with_rag_observed(
    question: str,
    limit: int = DEFAULT_TOP_K,
) -> tuple[str, list[dict[str, Any]], dict[str, float]]:
    """返回 RAG 回答、评论来源，以及本次检索/生成的耗时指标。"""
    normalized_question = question.strip()
    metrics: dict[str, float] = {}
    if not normalized_question:
        return NO_EVIDENCE_ANSWER, [], metrics

    # FastEmbed 和 Qdrant 客户端目前是同步调用，放到工作线程中，避免阻塞 FastAPI 事件循环。
    try:
        retrieval_start = time.perf_counter()
        reviews = await asyncio.to_thread(
            search_hybrid_reviews,
            normalized_question,
            limit,
        )
        metrics["retrievalDurationMs"] = (time.perf_counter() - retrieval_start) * 1000
    except Exception as exc:
        raise RagRetrievalError("RAG review retrieval failed") from exc

    if not reviews:
        return NO_EVIDENCE_ANSWER, [], metrics

    try:
        evidence = format_review_evidence(reviews)
        client = get_client()
        llm_start = time.perf_counter()
        response = await client.chat.completions.create(
            model=settings.deepseek_model,
            messages=[
                {"role": "system", "content": RAG_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"用户问题：\n{normalized_question}\n\n"
                        f"可用评论证据：\n{evidence}\n\n"
                        "请直接回答用户问题，并为事实标注对应的评论编号。"
                    ),
                },
            ],
            temperature=0,
        )
        metrics["llmDurationMs"] = (time.perf_counter() - llm_start) * 1000
        answer = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        raise RagGenerationError(reviews, metrics) from exc

    if not answer:
        return EMPTY_MODEL_ANSWER, reviews, metrics
    if not answer_citations_are_valid(answer, reviews):
        return (
            render_review_evidence_fallback(
                reviews,
                prefix="回答引用未通过校验，以下仅提供检索到的评论原文：",
            ),
            reviews,
            metrics,
        )

    return answer, reviews, metrics
