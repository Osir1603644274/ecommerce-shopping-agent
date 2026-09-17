from typing import Any


def _ranked_by_review_id(
    ranked: list[dict[str, Any]],
) -> dict[str, tuple[int, dict[str, Any]]]:
    return {
        str(item["reviewId"]): (rank, item)
        for rank, item in enumerate(ranked, start=1)
    }


def _min_max_scores(
    ranked_by_id: dict[str, tuple[int, dict[str, Any]]],
) -> dict[str, float]:
    if not ranked_by_id:
        return {}
    raw_scores = {
        review_id: float(item.get("score") or 0.0)
        for review_id, (_, item) in ranked_by_id.items()
    }
    minimum = min(raw_scores.values())
    maximum = max(raw_scores.values())
    if maximum == minimum:
        return {review_id: 1.0 for review_id in raw_scores}
    return {
        review_id: (score - minimum) / (maximum - minimum)
        for review_id, score in raw_scores.items()
    }


def _max_scores(
    ranked_by_id: dict[str, tuple[int, dict[str, Any]]],
) -> dict[str, float]:
    if not ranked_by_id:
        return {}
    raw_scores = {
        review_id: float(item.get("score") or 0.0)
        for review_id, (_, item) in ranked_by_id.items()
    }
    maximum = max(raw_scores.values())
    if maximum <= 0:
        return {review_id: 0.0 for review_id in raw_scores}
    return {
        review_id: score / maximum
        for review_id, score in raw_scores.items()
    }


def _merged_payload(
    review_id: str,
    vector_by_id: dict[str, tuple[int, dict[str, Any]]],
    bm25_by_id: dict[str, tuple[int, dict[str, Any]]],
) -> dict[str, Any]:
    vector_entry = vector_by_id.get(review_id)
    bm25_entry = bm25_by_id.get(review_id)
    payload: dict[str, Any] = {}
    if bm25_entry is not None:
        payload.update(bm25_entry[1])
    if vector_entry is not None:
        payload.update(vector_entry[1])
    payload.pop("score", None)
    return payload


def fuse_by_normalized_score(
    vector_ranked: list[dict[str, Any]],
    bm25_ranked: list[dict[str, Any]],
    *,
    vector_weight: float = 1.0,
    bm25_weight: float = 1.0,
    normalization: str = "min_max",
) -> list[dict[str, Any]]:
    """对独立召回结果按每题、每路分数归一化后加权融合。"""
    if vector_weight < 0 or bm25_weight < 0:
        raise ValueError("fusion weights must be non-negative")
    if vector_weight == 0 and bm25_weight == 0:
        raise ValueError("at least one fusion weight must be positive")
    normalizers = {
        "min_max": _min_max_scores,
        "max": _max_scores,
    }
    if normalization not in normalizers:
        raise ValueError("normalization must be 'min_max' or 'max'")

    vector_by_id = _ranked_by_review_id(vector_ranked)
    bm25_by_id = _ranked_by_review_id(bm25_ranked)
    normalize = normalizers[normalization]
    normalized_vector = normalize(vector_by_id)
    normalized_bm25 = normalize(bm25_by_id)
    review_ids = set(vector_by_id) | set(bm25_by_id)
    fused: list[dict[str, Any]] = []
    for review_id in review_ids:
        vector_entry = vector_by_id.get(review_id)
        bm25_entry = bm25_by_id.get(review_id)
        vector_score = (
            float(vector_entry[1].get("score") or 0.0)
            if vector_entry is not None
            else None
        )
        bm25_score = (
            float(bm25_entry[1].get("score") or 0.0)
            if bm25_entry is not None
            else None
        )
        fusion_score = (
            vector_weight * normalized_vector.get(review_id, 0.0)
            + bm25_weight * normalized_bm25.get(review_id, 0.0)
        )
        fused.append(
            {
                **_merged_payload(review_id, vector_by_id, bm25_by_id),
                "reviewId": review_id,
                "vectorRank": vector_entry[0] if vector_entry else None,
                "bm25Rank": bm25_entry[0] if bm25_entry else None,
                "vectorScore": vector_score,
                "bm25Score": bm25_score,
                "normalizedVectorScore": normalized_vector.get(review_id, 0.0),
                "normalizedBm25Score": normalized_bm25.get(review_id, 0.0),
                "fusionScore": fusion_score,
            }
        )
    fused.sort(
        key=lambda item: (
            -float(item["fusionScore"]),
            min(
                rank
                for rank in (item["vectorRank"], item["bm25Rank"])
                if rank is not None
            ),
            str(item["reviewId"]),
        )
    )
    return fused


def fuse_by_rrf(
    vector_ranked: list[dict[str, Any]],
    bm25_ranked: list[dict[str, Any]],
    *,
    rrf_k: int = 60,
) -> list[dict[str, Any]]:
    """使用Reciprocal Rank Fusion融合独立排名，不直接比较原始分数。"""
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive")

    vector_by_id = _ranked_by_review_id(vector_ranked)
    bm25_by_id = _ranked_by_review_id(bm25_ranked)
    review_ids = set(vector_by_id) | set(bm25_by_id)
    fused: list[dict[str, Any]] = []
    for review_id in review_ids:
        vector_entry = vector_by_id.get(review_id)
        bm25_entry = bm25_by_id.get(review_id)
        vector_rank = vector_entry[0] if vector_entry else None
        bm25_rank = bm25_entry[0] if bm25_entry else None
        rrf_score = sum(
            1 / (rrf_k + rank)
            for rank in (vector_rank, bm25_rank)
            if rank is not None
        )
        fused.append(
            {
                **_merged_payload(review_id, vector_by_id, bm25_by_id),
                "reviewId": review_id,
                "vectorRank": vector_rank,
                "bm25Rank": bm25_rank,
                "vectorScore": (
                    float(vector_entry[1].get("score") or 0.0)
                    if vector_entry
                    else None
                ),
                "bm25Score": (
                    float(bm25_entry[1].get("score") or 0.0)
                    if bm25_entry
                    else None
                ),
                "rrfScore": rrf_score,
            }
        )
    fused.sort(
        key=lambda item: (
            -float(item["rrfScore"]),
            min(
                rank
                for rank in (item["vectorRank"], item["bm25Rank"])
                if rank is not None
            ),
            str(item["reviewId"]),
        )
    )
    return fused
