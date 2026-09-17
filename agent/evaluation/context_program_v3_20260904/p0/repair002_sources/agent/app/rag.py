import json
import math
import re
import time
import uuid
from collections.abc import Callable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from fastembed import TextEmbedding
from qdrant_client import QdrantClient, models

from .settings import settings


# BAAI/bge-small-zh-v1.5 的向量维度。维度属于 collection 的固定结构，不能随意改。
EMBEDDING_DIMENSION = 512
DEFAULT_TOP_K = 3
INGEST_BATCH_SIZE = 256
# 语料属于运行时数据，不依赖 app 被从源码还是 site-packages 导入。
# Docker 工作目录为 /app，本地从 agent/ 运行，因此默认相对路径都指向 agent/rag。
RAG_DIRECTORY = Path(settings.rag_data_dir)
# 旧 JSON 只保留为检索评测夹具；运行时全量入库以 MySQL 评论接口为准。
CORPUS_PATH = RAG_DIRECTORY / "corpus" / "merchant_reviews.json"
EVAL_PATH = RAG_DIRECTORY / "eval" / "retrieval_cases.json"
YELP_EVAL_PATH = RAG_DIRECTORY / "eval" / "yelp_retrieval_cases.json"
REVIEW_POINT_NAMESPACE = uuid.UUID("8af727a5-23a0-4ce9-9125-fd77fd1f1a17")


def _clean_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_review_payload(review: dict[str, Any]) -> dict[str, Any]:
    """Normalize backend review JSON into the shape used by Qdrant ingestion."""
    review_id = review.get("reviewId", review.get("id"))
    content = str(review.get("content", review.get("originalText", review.get("text", "")))).strip()
    content_zh = _clean_optional_text(review.get("contentZh", review.get("content_zh")))
    embedding_text = content_zh or content
    source_language = review.get("sourceLanguage") or review.get("language") or "zh"
    normalized = {
        "id": review_id,
        "reviewId": review_id,
        "shopId": review["shopId"],
        "shopName": review["shopName"],
        "text": embedding_text,
        "embeddingText": embedding_text,
        "originalText": content,
        "contentZh": content_zh,
        "source": review.get("source", "seed"),
        # language describes the indexed text; sourceLanguage preserves the original review.
        "language": "zh" if content_zh else source_language,
        "sourceLanguage": source_language,
        "translationStatus": review.get(
            "translationStatus",
            review.get("translation_status", "not_required"),
        ),
        "tags": review.get("tags", []),
    }
    lineage = {
        "sourceReviewId": review.get(
            "sourceReviewId",
            review.get("source_review_id"),
        ),
        "sourceUserId": review.get("sourceUserId", review.get("source_user_id")),
        "stars": review.get("stars"),
        "sourceShopName": review.get(
            "sourceShopName",
            review.get("source_shop_name"),
        ),
        "evidenceScope": review.get(
            "evidenceScope",
            review.get("evidence_scope"),
        ),
    }
    normalized.update(
        {
            key: value
            for key, value in lineage.items()
            if value is not None
        }
    )
    return normalized


def load_reviews() -> list[dict[str, Any]]:
    """通过 Spring Boot 评论接口读取 MySQL 中的全部评论。"""
    response = httpx.get(
        f"{settings.backend_base_url}/api/reviews",
        timeout=10.0,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") is not True or not isinstance(payload.get("data"), list):
        raise ValueError("评论接口返回格式错误")

    return [_normalize_review_payload(review) for review in payload["data"]]


def load_retrieval_cases(eval_path: Path = EVAL_PATH) -> list[dict[str, Any]]:
    """读取带标准证据 ID 的检索评测题。"""
    return json.loads(eval_path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def get_embedding_model() -> TextEmbedding:
    """懒加载本地模型，避免 Agent 普通聊天启动时下载或占用模型内存。"""
    return TextEmbedding(
        model_name=settings.rag_embedding_model,
        cache_dir=settings.rag_model_cache_dir,
    )


@lru_cache(maxsize=4)
def _qdrant_client_for_url(url: str) -> QdrantClient:
    """Reuse Qdrant's connection pool instead of rebuilding it per query."""
    return QdrantClient(url=url)


def get_qdrant_client() -> QdrantClient:
    return _qdrant_client_for_url(settings.qdrant_url)


def ensure_collection(client: QdrantClient) -> None:
    if client.collection_exists(settings.rag_collection_name):
        return

    client.create_collection(
        collection_name=settings.rag_collection_name,
        vectors_config=models.VectorParams(
            size=EMBEDDING_DIMENSION,
            distance=models.Distance.COSINE,
        ),
    )


def review_point_id(review_id: str) -> int | str:
    """旧 seed 评论沿用数字 point ID；业务新增评论稳定映射为 Qdrant UUID。"""
    seed_match = re.fullmatch(r"review-(\d{3})", review_id)
    if seed_match:
        return int(seed_match.group(1))
    return str(uuid.uuid5(REVIEW_POINT_NAMESPACE, review_id))


def ingest_reviews() -> int:
    """从 MySQL 获取全部评论，生成向量并重建 Qdrant collection。"""
    reviews = [_normalize_review_payload(review) for review in load_reviews()]
    vectors = []
    if reviews:
        model = get_embedding_model()
        vectors = list(model.embed([review["embeddingText"] for review in reviews]))

    client = get_qdrant_client()
    if client.collection_exists(settings.rag_collection_name):
        client.delete_collection(collection_name=settings.rag_collection_name)
    ensure_collection(client)

    if not reviews:
        return 0

    count = 0
    for start in range(0, len(reviews), INGEST_BATCH_SIZE):
        batch_reviews = reviews[start : start + INGEST_BATCH_SIZE]
        batch_vectors = vectors[start : start + INGEST_BATCH_SIZE]
        points = [
            models.PointStruct(
                id=review_point_id(review["id"]),
                vector=vector.tolist(),
                payload={
                    "reviewId": review["id"],
                    "shopId": review["shopId"],
                    "shopName": review["shopName"],
                    "text": review["text"],
                    "originalText": review["originalText"],
                    "contentZh": review["contentZh"],
                    "source": review["source"],
                    "language": review["language"],
                    "sourceLanguage": review["sourceLanguage"],
                    "translationStatus": review["translationStatus"],
                    "sourceReviewId": review.get("sourceReviewId"),
                    "sourceUserId": review.get("sourceUserId"),
                    "stars": review.get("stars"),
                    "sourceShopName": review.get("sourceShopName"),
                    "evidenceScope": review.get("evidenceScope"),
                    "tags": review["tags"],
                },
            )
            for review, vector in zip(batch_reviews, batch_vectors)
        ]
        client.upsert(collection_name=settings.rag_collection_name, points=points, wait=True)
        count += len(points)
    return count


def upsert_review_vector(
    review: dict[str, Any],
    *,
    vector: Sequence[float] | None = None,
) -> None:
    """将一条新增或修改后的业务评论幂等写入 Qdrant。"""
    normalized = _normalize_review_payload(review)
    selected_vector = vector
    if selected_vector is None:
        model = get_embedding_model()
        selected_vector = next(model.embed([normalized["embeddingText"]])).tolist()
    client = get_qdrant_client()
    ensure_collection(client)
    point = models.PointStruct(
        id=review_point_id(normalized["reviewId"]),
        vector=list(selected_vector),
        payload={
            "reviewId": normalized["reviewId"],
            "shopId": normalized["shopId"],
            "shopName": normalized["shopName"],
            "text": normalized["text"],
            "originalText": normalized["originalText"],
            "contentZh": normalized["contentZh"],
            "source": normalized["source"],
            "language": normalized["language"],
            "sourceLanguage": normalized["sourceLanguage"],
            "translationStatus": normalized["translationStatus"],
            "sourceReviewId": normalized.get("sourceReviewId"),
            "sourceUserId": normalized.get("sourceUserId"),
            "stars": normalized.get("stars"),
            "sourceShopName": normalized.get("sourceShopName"),
            "evidenceScope": normalized.get("evidenceScope"),
            "tags": normalized["tags"],
        },
    )
    client.upsert(
        collection_name=settings.rag_collection_name,
        points=[point],
        wait=True,
    )


def delete_review_vector(review_id: str) -> None:
    """按稳定 point ID 幂等删除一条评论向量。"""
    client = get_qdrant_client()
    ensure_collection(client)
    client.delete(
        collection_name=settings.rag_collection_name,
        points_selector=models.PointIdsList(points=[review_point_id(review_id)]),
        wait=True,
    )


def _review_query_filter(
    source: str | None = None,
    shop_id: int | None = None,
    shop_ids: list[int] | None = None,
) -> models.Filter | None:
    if shop_id is not None and shop_ids is not None:
        raise ValueError("shop_id and shop_ids cannot be used together")
    if shop_id is not None and shop_id <= 0:
        raise ValueError("shop_id must be positive")
    normalized_shop_ids = (
        list(dict.fromkeys(shop_ids))
        if shop_ids is not None
        else None
    )
    if normalized_shop_ids is not None and any(
        candidate <= 0 for candidate in normalized_shop_ids
    ):
        raise ValueError("shop_ids must contain only positive integers")

    conditions: list[models.FieldCondition] = []
    if source:
        conditions.append(
            models.FieldCondition(
                key="source",
                match=models.MatchValue(value=source),
            )
        )
    if shop_id is not None:
        conditions.append(
            models.FieldCondition(
                key="shopId",
                match=models.MatchValue(value=shop_id),
            )
        )
    elif normalized_shop_ids is not None:
        conditions.append(
            models.FieldCondition(
                key="shopId",
                match=models.MatchAny(any=normalized_shop_ids),
            )
        )
    if not conditions:
        return None
    return models.Filter(must=conditions)


def search_reviews(
    question: str,
    limit: int = DEFAULT_TOP_K,
    *,
    source: str | None = None,
    shop_id: int | None = None,
    shop_ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """将问题向量化，并返回最相关的评论及相似度分数。"""
    query_filter = _review_query_filter(source, shop_id, shop_ids)
    if shop_ids is not None and not shop_ids:
        return []
    model = get_embedding_model()
    query_vector = next(model.embed([question])).tolist()
    client = get_qdrant_client()
    result = client.query_points(
        collection_name=settings.rag_collection_name,
        query=query_vector,
        query_filter=query_filter,
        limit=limit,
        with_payload=True,
    )
    return [
        _review_from_scored_point(point)
        for point in result.points
    ]


def _review_from_scored_point(point: Any) -> dict[str, Any]:
    payload = point.payload
    return {
        "reviewId": payload["reviewId"],
        "shopId": payload["shopId"],
        "shopName": payload["shopName"],
        "text": payload["text"],
        "originalText": payload.get("originalText"),
        "contentZh": payload.get("contentZh"),
        "source": payload.get("source"),
        "language": payload.get("language"),
        "sourceLanguage": payload.get("sourceLanguage"),
        "translationStatus": payload.get("translationStatus"),
        "sourceReviewId": payload.get("sourceReviewId"),
        "sourceUserId": payload.get("sourceUserId"),
        "stars": payload.get("stars"),
        "sourceShopName": payload.get("sourceShopName"),
        "evidenceScope": payload.get("evidenceScope"),
        "score": point.score,
    }


def aggregate_reviews_by_shop(
    reviews: list[dict[str, Any]],
    shop_ids: list[int],
    *,
    evidence_per_shop: int = DEFAULT_TOP_K,
) -> list[dict[str, Any]]:
    """将候选集合内的评论汇总为商户级证据，并显式保留无证据候选。"""
    if evidence_per_shop <= 0:
        raise ValueError("evidence_per_shop must be positive")
    normalized_shop_ids = list(dict.fromkeys(shop_ids))
    _review_query_filter(shop_ids=normalized_shop_ids)
    candidate_positions = {
        shop_id: position
        for position, shop_id in enumerate(normalized_shop_ids)
    }
    reviews_by_shop_id: dict[int, list[dict[str, Any]]] = {
        shop_id: [] for shop_id in normalized_shop_ids
    }
    for review in reviews:
        review_shop_id = int(review["shopId"])
        if review_shop_id not in candidate_positions:
            raise ValueError(
                f"review aggregation received unexpected shopId {review_shop_id}"
            )
        reviews_by_shop_id[review_shop_id].append(review)

    groups: list[dict[str, Any]] = []
    for shop_id in normalized_shop_ids:
        evidence = sorted(
            reviews_by_shop_id[shop_id],
            key=lambda review: float(review["score"]),
            reverse=True,
        )[:evidence_per_shop]
        scores = [float(review["score"]) for review in evidence]
        groups.append({
            "shopId": shop_id,
            "shopName": evidence[0]["shopName"] if evidence else None,
            "aggregateScore": sum(scores) / len(scores) if scores else None,
            "maxScore": max(scores) if scores else None,
            "evidenceCount": len(evidence),
            "reviews": evidence,
        })
    groups.sort(
        key=lambda group: (
            group["aggregateScore"] is None,
            -group["aggregateScore"] if group["aggregateScore"] is not None else 0,
            candidate_positions[group["shopId"]],
        )
    )
    return groups


def search_reviews_aggregated_by_shop(
    question: str,
    shop_ids: list[int],
    *,
    evidence_budget: int = 6,
    evidence_per_shop: int = 2,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """在候选集合内取全局Top-K，再按商户聚合；这是当前实验基线。"""
    reviews = search_reviews(
        question,
        evidence_budget,
        source=source,
        shop_ids=shop_ids,
    )
    return aggregate_reviews_by_shop(
        reviews,
        shop_ids,
        evidence_per_shop=evidence_per_shop,
    )


def search_reviews_grouped_by_shop(
    question: str,
    shop_ids: list[int],
    *,
    reviews_per_shop: int = DEFAULT_TOP_K,
    source: str | None = None,
) -> list[dict[str, Any]]:
    """按商户分组检索固定数量评论；保留为覆盖优先的实验对照。"""
    if reviews_per_shop <= 0:
        raise ValueError("reviews_per_shop must be positive")
    normalized_shop_ids = list(dict.fromkeys(shop_ids))
    query_filter = _review_query_filter(source=source, shop_ids=normalized_shop_ids)
    if not normalized_shop_ids:
        return []

    model = get_embedding_model()
    query_vector = next(model.embed([question])).tolist()
    client = get_qdrant_client()
    result = client.query_points_groups(
        collection_name=settings.rag_collection_name,
        query=query_vector,
        group_by="shopId",
        query_filter=query_filter,
        limit=len(normalized_shop_ids),
        group_size=reviews_per_shop,
        with_payload=True,
    )
    reviews = [
        _review_from_scored_point(point)
        for group in result.groups
        for point in group.hits
    ]
    return aggregate_reviews_by_shop(
        reviews,
        normalized_shop_ids,
        evidence_per_shop=reviews_per_shop,
    )


def summarize_retrieval_details(details: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总一组检索明细，同一套公式同时用于整体和分难度指标。"""
    total = len(details)
    hits = sum(item["hit"] for item in details)
    hits_at_1 = sum(item["hitAt1"] for item in details)
    duration_values = sorted(item.get("durationMs", 0.0) for item in details)
    p95_index = math.ceil(total * 0.95) - 1 if total else 0
    return {
        "total": total,
        "hits": hits,
        "hitRate": hits / total if total else 0,
        "hitsAt1": hits_at_1,
        "hitAt1Rate": hits_at_1 / total if total else 0,
        "mrr": (
            sum(item["reciprocalRank"] for item in details) / total
            if total
            else 0
        ),
        "timing": {
            "totalMs": sum(duration_values),
            "avgMs": sum(duration_values) / total if total else 0,
            "p50Ms": duration_values[total // 2] if total else 0,
            "p95Ms": duration_values[p95_index] if total else 0,
            "maxMs": max(duration_values) if total else 0,
        },
    }


def evaluate_retrieval_cases(
    cases: list[dict[str, Any]],
    retrieve: Callable[[str, int], list[dict[str, Any]]],
    limit: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    """计算 Hit@K：每道题的任一标准证据进入 Top-K 即视为命中。"""
    details = []
    for case in cases:
        start = time.perf_counter()
        retrieved = retrieve(case["question"], limit)
        duration_ms = (time.perf_counter() - start) * 1000
        retrieved_ids = [item["reviewId"] for item in retrieved]
        relevant_ids = set(case["relevantReviewIds"])
        first_relevant_rank = next(
            (
                rank
                for rank, review_id in enumerate(retrieved_ids, start=1)
                if review_id in relevant_ids
            ),
            None,
        )
        hit = first_relevant_rank is not None
        hit_at_1 = first_relevant_rank == 1
        reciprocal_rank = 1 / first_relevant_rank if first_relevant_rank else 0.0
        details.append(
            {
                "caseId": case["id"],
                "difficulty": case["difficulty"],
                "challengeTypes": case["challengeTypes"],
                "hit": hit,
                "hitAt1": hit_at_1,
                "firstRelevantRank": first_relevant_rank,
                "reciprocalRank": reciprocal_rank,
                "durationMs": duration_ms,
                "expectedReviewIds": case["relevantReviewIds"],
                "retrievedReviewIds": retrieved_ids,
            }
        )

    overall = summarize_retrieval_details(details)
    details_by_difficulty: dict[str, list[dict[str, Any]]] = {}
    for item in details:
        details_by_difficulty.setdefault(item["difficulty"], []).append(item)
    by_difficulty = {
        difficulty: summarize_retrieval_details(group_details)
        for difficulty, group_details in details_by_difficulty.items()
    }
    details_by_challenge_type: dict[str, list[dict[str, Any]]] = {}
    for item in details:
        for challenge_type in item["challengeTypes"]:
            details_by_challenge_type.setdefault(challenge_type, []).append(item)
    by_challenge_type = {
        challenge_type: summarize_retrieval_details(group_details)
        for challenge_type, group_details in sorted(details_by_challenge_type.items())
    }

    return {
        "topK": limit,
        **overall,
        "byDifficulty": by_difficulty,
        "byChallengeType": by_challenge_type,
        "details": details,
    }


def evaluate_retrieval(
    retrieve: Callable[[str, int], list[dict[str, Any]]],
    limit: int = DEFAULT_TOP_K,
) -> dict[str, Any]:
    return evaluate_retrieval_cases(load_retrieval_cases(), retrieve, limit)
