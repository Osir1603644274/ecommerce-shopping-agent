import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag_bm25 import search_yelp_reviews_bm25  # noqa: E402
from app.rag_fusion import fuse_by_normalized_score  # noqa: E402
from app.rag_quality import search_yelp_reviews  # noqa: E402


AGENT_ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = (
    AGENT_ROOT
    / "knowledge_data"
    / "eval"
    / "fuzzy_shop_discovery_test_questions.json"
)
POOL_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "fuzzy_shop_discovery_test_label_pool.json"
)


def _candidate_summary(rank: int, item: dict) -> dict:
    return {
        "rank": rank,
        "reviewId": str(item["reviewId"]),
        "shopId": int(item["shopId"]),
        "shopName": item.get("shopName"),
        "vectorRank": item.get("vectorRank"),
        "bm25Rank": item.get("bm25Rank"),
        "fusionScore": item.get("fusionScore"),
        "text": item.get("text"),
    }


def main() -> None:
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    cases = []
    for case in questions["cases"]:
        vector = search_yelp_reviews(case["question"], 30)
        bm25 = search_yelp_reviews_bm25(case["question"], 30)
        hybrid = fuse_by_normalized_score(
            vector,
            bm25,
            normalization="min_max",
            vector_weight=0.20,
            bm25_weight=0.80,
        )
        cases.append(
            {
                **case,
                "candidateCount": len(hybrid),
                "candidates": [
                    _candidate_summary(rank, item)
                    for rank, item in enumerate(hybrid, start=1)
                ],
            }
        )
    report = {
        "methodology": {
            "purpose": "manual qrel pooling only; not a test result",
            "candidatePool": "independent Vector/BM25 Top-30 union",
            "displayOrder": "frozen Min-Max Vector:BM25=0.20:0.80",
            "qrelsVisibleToRetrievers": False,
        },
        "cases": cases,
    }
    POOL_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {POOL_PATH}")


if __name__ == "__main__":
    main()
