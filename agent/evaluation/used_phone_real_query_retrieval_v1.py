"""Offline retrieval ablation for the derived KuaiSearch used-phone qrel.

This evaluator never imports qrel identities into production retrieval.  It
uses the sparse judgments only after ranking to measure known-positive recall.
Unjudged products remain unknown rather than being counted as negatives.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

from agent.app.domains.ecommerce.models import (
    bm25_rank,
    bm25_rank_legacy_concat,
    expand_product_query,
    product_embedding_text,
    reciprocal_rank_fusion,
    tokenize_product_text,
)


Ranker = Callable[[str, list[dict[str, Any]]], list[int]]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL {path}:{line_number}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL row is not an object {path}:{line_number}")
            rows.append(row)
    return rows


def load_bundle(directory: Path) -> tuple[
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, list[dict[str, Any]]],
]:
    queries = _read_jsonl(directory / "queries.jsonl")
    raw_documents = _read_jsonl(directory / "documents.jsonl")
    qrels = _read_jsonl(directory / "qrels.jsonl")
    documents = [
        {
            "id": int(item["product_id"]),
            "title": item.get("item_title") or "",
            "brand": item.get("brand") or "",
            "attributeText": item.get("attr_value") or "",
            "seller": item.get("seller_name") or "",
        }
        for item in raw_documents
    ]
    queries_by_id = {str(item["query_id"]): item for item in queries}
    qrels_by_query: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in qrels:
        qrels_by_query[str(item["query_id"])].append(item)
    if set(queries_by_id) != set(qrels_by_query):
        raise ValueError("query/qrel identity mismatch")
    return documents, queries_by_id, dict(qrels_by_query)


def _has_lexical_match(
    query: str,
    documents: list[dict[str, Any]],
    fields: Iterable[str],
) -> bool:
    query_tokens = set(tokenize_product_text(query))
    return any(
        query_tokens.intersection(tokenize_product_text(str(product.get(field) or "")))
        for product in documents
        for field in fields
    )


def _evaluate_ranker(
    ranker: Ranker,
    *,
    documents: list[dict[str, Any]],
    queries: dict[str, dict[str, Any]],
    qrels: dict[str, list[dict[str, Any]]],
    split: str,
    lexical_fields: tuple[str, ...] | None,
) -> dict[str, Any]:
    cases = [item for item in queries.values() if item.get("split") == split]
    positive_cases = [
        item for item in cases
        if any(int(row["relevance"]) > 0 for row in qrels[str(item["query_id"])])
    ]
    negative_only_cases = [item for item in cases if item not in positive_cases]
    totals = {
        "hit1": 0.0,
        "hit3": 0.0,
        "recall20": 0.0,
        "recall50": 0.0,
        "ndcg3": 0.0,
        "noResult": 0.0,
    }
    started = time.perf_counter()
    for query_row in positive_cases:
        query_id = str(query_row["query_id"])
        query = str(query_row["query"])
        ranking = ranker(query, documents)
        positive = {
            int(item["product_id"]): int(item["relevance"])
            for item in qrels[query_id]
            if int(item["relevance"]) > 0
        }
        totals["hit1"] += float(bool(ranking[:1] and ranking[0] in positive))
        totals["hit3"] += float(any(item in positive for item in ranking[:3]))
        totals["recall20"] += len(set(ranking[:20]) & set(positive)) / len(positive)
        totals["recall50"] += len(set(ranking[:50]) & set(positive)) / len(positive)
        dcg = sum(
            (2 ** positive[product_id] - 1) / math.log2(index + 2)
            for index, product_id in enumerate(ranking[:3])
            if product_id in positive
        )
        ideal = sum(
            (2 ** grade - 1) / math.log2(index + 2)
            for index, grade in enumerate(sorted(positive.values(), reverse=True)[:3])
        )
        totals["ndcg3"] += dcg / ideal if ideal else 0.0
        if lexical_fields is not None:
            totals["noResult"] += float(
                not _has_lexical_match(query, documents, lexical_fields)
            )
    elapsed_ms = (time.perf_counter() - started) * 1000
    denominator = max(len(positive_cases), 1)
    explicit_negative_top1 = 0
    for query_row in negative_only_cases:
        query_id = str(query_row["query_id"])
        ranking = ranker(str(query_row["query"]), documents)
        negatives = {
            int(item["product_id"])
            for item in qrels[query_id]
            if int(item["relevance"]) == 0
        }
        explicit_negative_top1 += int(bool(ranking[:1] and ranking[0] in negatives))
    return {
        "split": split,
        "queryCount": len(cases),
        "positiveQueryCount": len(positive_cases),
        "negativeOnlyQueryCount": len(negative_only_cases),
        "hitAt1": round(totals["hit1"] / denominator, 6),
        "hitAt3": round(totals["hit3"] / denominator, 6),
        "recallAt20": round(totals["recall20"] / denominator, 6),
        "recallAt50": round(totals["recall50"] / denominator, 6),
        "ndcgAt3": round(totals["ndcg3"] / denominator, 6),
        "lexicalNoResultRate": (
            round(totals["noResult"] / denominator, 6)
            if lexical_fields is not None else None
        ),
        "explicitGrade0Top1Count": explicit_negative_top1,
        "elapsedMs": round(elapsed_ms, 2),
        "meanLatencyMs": round(elapsed_ms / max(len(cases), 1), 4),
        "hardConstraintViolationRate": None,
        "hardConstraintMetricReason": (
            "real sparse qrel has no structured hard-constraint gold; use the "
            "separate controlled ranking contract suite"
        ),
    }


def build_vector_ranker(
    documents: list[dict[str, Any]],
) -> Ranker:
    from agent.app.rag import get_embedding_model

    model = get_embedding_model()
    document_vectors = [
        list(vector.tolist())
        for vector in model.embed([product_embedding_text(item) for item in documents])
    ]
    document_ids = [int(item["id"]) for item in documents]
    document_norms = [
        math.sqrt(sum(value * value for value in vector))
        for vector in document_vectors
    ]

    def rank(query: str, _documents: list[dict[str, Any]]) -> list[int]:
        query_vector = list(next(iter(model.embed([query]))).tolist())
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        scored = []
        for product_id, vector, norm in zip(
            document_ids, document_vectors, document_norms, strict=True
        ):
            denominator = query_norm * norm
            score = (
                sum(left * right for left, right in zip(query_vector, vector, strict=True))
                / denominator
                if denominator else 0.0
            )
            scored.append((product_id, score))
        return [
            item[0]
            for item in sorted(scored, key=lambda item: (-item[1], item[0]))
        ]

    return rank


def evaluate_bundle(
    directory: Path,
    *,
    split: str = "train",
    include_vector: bool = False,
) -> dict[str, Any]:
    documents, queries, qrels = load_bundle(directory)
    variants: dict[str, tuple[Ranker, tuple[str, ...] | None]] = {
        "legacy_concat": (bm25_rank_legacy_concat, (
            "title", "brand", "seller", "attributeText",
        )),
        "title_only": (
            lambda query, rows: bm25_rank(
                query, rows, field_weights={"title": 1.0}
            ),
            ("title",),
        ),
        "title_brand": (
            lambda query, rows: bm25_rank(
                query, rows, field_weights={"title": 1.0, "brand": 0.25}
            ),
            ("title", "brand"),
        ),
        "title_brand_attribute": (bm25_rank, (
            "title", "brand", "attributeText",
        )),
        "title_brand_attribute_expanded": (
            lambda query, rows: bm25_rank(expand_product_query(query)[0], rows),
            ("title", "brand", "attributeText"),
        ),
    }
    if include_vector:
        vector_ranker = build_vector_ranker(documents)
        variants["vector"] = (vector_ranker, None)
        variants["hybrid_bm25_vector"] = (
            lambda query, rows: [
                product_id
                for product_id, _score in reciprocal_rank_fusion([
                    bm25_rank(expand_product_query(query)[0], rows),
                    vector_ranker(expand_product_query(query)[0], rows),
                ], k=60)
            ],
            None,
        )
    return {
        "schemaVersion": "used-phone-real-query-retrieval-eval-v1",
        "artifact": directory.name,
        "split": split,
        "sparseQrel": True,
        "unjudgedIsNegative": False,
        "variants": {
            name: _evaluate_ranker(
                ranker,
                documents=documents,
                queries=queries,
                qrels=qrels,
                split=split,
                lexical_fields=fields,
            )
            for name, (ranker, fields) in variants.items()
        },
    }


__all__ = ["build_vector_ranker", "evaluate_bundle", "load_bundle"]
