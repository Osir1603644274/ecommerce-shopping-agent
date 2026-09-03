import json
import time
from pathlib import Path
from typing import Any

from app.bm25 import BM25Index
from app.rag_bm25 import get_yelp_review_bm25_index, prepare_review_bm25_indexes


CASES_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "fuzzy_shop_discovery_validation_cases.json"
)


def _reference_scan(
    index: BM25Index,
    query: str,
    limit: int,
) -> list[tuple[str, float]]:
    scored = [
        (document.doc_id, index.score(query, position))
        for position, document in enumerate(index.documents)
    ]
    scored = [item for item in scored if item[1] > 0]
    scored.sort(key=lambda item: (-item[1], item[0]))
    return scored[:limit]


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def build_bm25_inverted_evaluation_report(
    questions: list[str] | None = None,
    *,
    index: BM25Index | None = None,
    limit: int = 30,
) -> dict[str, Any]:
    selected_questions = questions
    if selected_questions is None:
        payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        selected_questions = [
            str(case["question"])
            for case in payload["cases"]
        ]
    if not selected_questions:
        raise ValueError("questions must not be empty")
    if limit <= 0:
        raise ValueError("limit must be positive")
    if index is None:
        prepare_review_bm25_indexes()
        index = get_yelp_review_bm25_index()

    scan_durations: list[float] = []
    inverted_durations: list[float] = []
    details: list[dict[str, Any]] = []
    for case_number, question in enumerate(selected_questions, start=1):
        start = time.perf_counter()
        scan = _reference_scan(index, question, limit)
        scan_ms = (time.perf_counter() - start) * 1000

        start = time.perf_counter()
        inverted = index.search(question, limit=limit)
        inverted_ms = (time.perf_counter() - start) * 1000

        scan_ids = [review_id for review_id, _score in scan]
        inverted_ids = [str(item["reviewId"]) for item in inverted]
        equivalent = scan_ids == inverted_ids
        scan_durations.append(scan_ms)
        inverted_durations.append(inverted_ms)
        details.append(
            {
                "case": case_number,
                "rankingEquivalent": equivalent,
                "referenceScanMs": scan_ms,
                "invertedSearchMs": inverted_ms,
                "returned": len(inverted_ids),
            }
        )

    average_scan = _average(scan_durations)
    average_inverted = _average(inverted_durations)
    checks = {
        "allRankingsEquivalent": all(
            item["rankingEquivalent"] for item in details
        ),
        "invertedAverageFaster": average_inverted < average_scan,
    }
    return {
        "experiment": "BM25 forward scan versus inverted posting search",
        "methodology": {
            "split": "validation questions only",
            "sealedDataRead": False,
            "documentCount": index.document_count,
            "topK": limit,
            "sameTokenizerAndBm25Formula": True,
        },
        "summary": {
            "passed": all(checks.values()),
            "checks": checks,
            "averageReferenceScanMs": average_scan,
            "averageInvertedSearchMs": average_inverted,
            "averageSpeedup": (
                average_scan / average_inverted
                if average_inverted > 0
                else None
            ),
        },
        "details": details,
    }
