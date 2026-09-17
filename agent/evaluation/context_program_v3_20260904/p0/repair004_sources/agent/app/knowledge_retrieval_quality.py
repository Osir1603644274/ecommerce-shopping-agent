import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .knowledge import SearchKnowledgeResult, search_knowledge


RETRIEVAL_QUALITY_CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "retrieval_quality_cases.json"
)
SUPPORTED_SPLITS = {"validation", "test"}
SOURCE_PREFIXES = {
    "review:": "reviews",
    "merchant_doc:": "merchant_docs",
    "policy_doc:": "policy_docs",
}

RetrieveQualityCase = Callable[
    [str, list[str], int],
    SearchKnowledgeResult,
]


def source_for_chunk_id(chunk_id: str) -> str:
    for prefix, source in SOURCE_PREFIXES.items():
        if chunk_id.startswith(prefix):
            return source
    raise ValueError(f"unsupported relevant chunk id: {chunk_id}")


def validate_retrieval_quality_cases(
    cases: list[dict[str, Any]],
) -> None:
    seen_case_ids: set[str] = set()
    chunks_by_split: dict[str, set[str]] = {
        split: set() for split in SUPPORTED_SPLITS
    }
    for case in cases:
        case_id = str(case.get("id", "")).strip()
        if not case_id or case_id in seen_case_ids:
            raise ValueError(f"invalid or duplicate case id: {case_id}")
        seen_case_ids.add(case_id)

        split = str(case.get("split", "")).strip()
        if split not in SUPPORTED_SPLITS:
            raise ValueError(f"unsupported split for {case_id}: {split}")

        expected_sources = case.get("expectedSources")
        relevant_chunk_ids = case.get("relevantChunkIds")
        if not isinstance(expected_sources, list) or not expected_sources:
            raise ValueError(f"expectedSources must not be empty for {case_id}")
        if not isinstance(relevant_chunk_ids, list) or not relevant_chunk_ids:
            raise ValueError(f"relevantChunkIds must not be empty for {case_id}")

        relevant_sources = {
            source_for_chunk_id(str(chunk_id))
            for chunk_id in relevant_chunk_ids
        }
        if relevant_sources != set(expected_sources):
            raise ValueError(
                f"relevant chunk sources do not match expectedSources for {case_id}"
            )
        review_filter = case.get("reviewFilter")
        if review_filter is not None:
            if "reviews" not in expected_sources:
                raise ValueError(f"reviewFilter requires reviews for {case_id}")
            if not isinstance(review_filter, dict):
                raise ValueError(f"reviewFilter must be an object for {case_id}")
            if int(review_filter.get("shopId", 0)) <= 0:
                raise ValueError(f"reviewFilter.shopId must be positive for {case_id}")
            if not str(review_filter.get("source", "")).strip():
                raise ValueError(f"reviewFilter.source must not be blank for {case_id}")
        shop_entity = case.get("shopEntity")
        if shop_entity is not None:
            if review_filter is None:
                raise ValueError(f"shopEntity requires reviewFilter for {case_id}")
            if not isinstance(shop_entity, dict):
                raise ValueError(f"shopEntity must be an object for {case_id}")
            if not str(shop_entity.get("name", "")).strip():
                raise ValueError(f"shopEntity.name must not be blank for {case_id}")
        chunks_by_split[split].update(str(item) for item in relevant_chunk_ids)

    leaked_chunks = chunks_by_split["validation"] & chunks_by_split["test"]
    if leaked_chunks:
        raise ValueError(
            "relevant chunks must not cross validation/test: "
            + ", ".join(sorted(leaked_chunks))
        )


def load_retrieval_quality_cases(
    path: Path = RETRIEVAL_QUALITY_CASES_PATH,
) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("retrieval quality cases must be a list")
    validate_retrieval_quality_cases(cases)
    return cases


def _default_retrieve(
    question: str,
    sources: list[str],
    limit: int,
) -> SearchKnowledgeResult:
    return search_knowledge(question, sources=sources, limit=limit)


def _summarize(details: list[dict[str, Any]]) -> dict[str, Any]:
    case_count = len(details)
    relevant_count = sum(item["relevantCount"] for item in details)
    retrieved_relevant_count = sum(
        item["retrievedRelevantCount"] for item in details
    )
    complete_hits = sum(item["completeHit"] for item in details)
    reciprocal_rank_sum = sum(
        rank["reciprocalRank"]
        for item in details
        for rank in item["relevantRanks"]
    )
    return {
        "caseCount": case_count,
        "completeHits": complete_hits,
        "completeHitRate": complete_hits / case_count if case_count else 0.0,
        "relevantChunkCount": relevant_count,
        "retrievedRelevantChunkCount": retrieved_relevant_count,
        "relevantChunkRecall": (
            retrieved_relevant_count / relevant_count
            if relevant_count
            else 0.0
        ),
        "meanRelevantReciprocalRank": (
            reciprocal_rank_sum / relevant_count
            if relevant_count
            else 0.0
        ),
    }


def evaluate_retrieval_quality_cases(
    cases: list[dict[str, Any]],
    *,
    retrieve: RetrieveQualityCase = _default_retrieve,
    top_k_per_source: int = 3,
) -> dict[str, Any]:
    if top_k_per_source <= 0:
        raise ValueError("top_k_per_source must be positive")
    validate_retrieval_quality_cases(cases)

    details: list[dict[str, Any]] = []
    for case in cases:
        expected_sources = list(case["expectedSources"])
        result = retrieve(case["question"], expected_sources, top_k_per_source)
        retrieved_by_source: dict[str, list[str]] = {
            source: [] for source in expected_sources
        }
        for chunk in result.chunks:
            source = source_for_chunk_id(chunk.chunk_id)
            if source in retrieved_by_source:
                retrieved_by_source[source].append(chunk.chunk_id)

        relevant_ranks: list[dict[str, Any]] = []
        for chunk_id in case["relevantChunkIds"]:
            source = source_for_chunk_id(chunk_id)
            source_results = retrieved_by_source.get(source, [])
            rank = (
                source_results.index(chunk_id) + 1
                if chunk_id in source_results
                else None
            )
            relevant_ranks.append(
                {
                    "chunkId": chunk_id,
                    "source": source,
                    "rankWithinSource": rank,
                    "reciprocalRank": 1 / rank if rank else 0.0,
                }
            )

        retrieved_relevant_count = sum(
            item["rankWithinSource"] is not None
            for item in relevant_ranks
        )
        relevant_count = len(relevant_ranks)
        details.append(
            {
                "caseId": case["id"],
                "split": case["split"],
                "category": case["category"],
                "question": case["question"],
                "expectedSources": expected_sources,
                "relevantChunkIds": case["relevantChunkIds"],
                "retrievedChunkIdsBySource": retrieved_by_source,
                "relevantRanks": relevant_ranks,
                "relevantCount": relevant_count,
                "retrievedRelevantCount": retrieved_relevant_count,
                "completeHit": retrieved_relevant_count == relevant_count,
                "durationMs": result.trace.duration_ms,
            }
        )

    split_names = sorted({item["split"] for item in details})
    category_names = sorted({item["category"] for item in details})
    return {
        "methodology": {
            "sourceSelection": "explicit; router is evaluated separately",
            "topKPerSource": top_k_per_source,
            "globalCrossSourceRanking": False,
            "completeHit": "all labeled relevant chunks are returned by their source retrievers",
        },
        "overall": _summarize(details),
        "bySplit": {
            split: _summarize(
                [item for item in details if item["split"] == split]
            )
            for split in split_names
        },
        "byCategory": {
            category: _summarize(
                [item for item in details if item["category"] == category]
            )
            for category in category_names
        },
        "failures": [item for item in details if not item["completeHit"]],
        "details": details,
    }


def evaluate_retrieval_quality(
    path: Path = RETRIEVAL_QUALITY_CASES_PATH,
    *,
    retrieve: RetrieveQualityCase = _default_retrieve,
    top_k_per_source: int = 3,
) -> dict[str, Any]:
    return evaluate_retrieval_quality_cases(
        load_retrieval_quality_cases(path),
        retrieve=retrieve,
        top_k_per_source=top_k_per_source,
    )
