"""Legacy binary-relevance retrieval experiment.

Use evaluate_evidence_product_retrieval.py for the strict graded Chinese gate.
This legacy script must not be used to claim final Chinese online quality.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path

from app.shopping_guide import reciprocal_rank_fusion


def split(query: str) -> str:
    bucket = int(hashlib.sha256(query.encode()).hexdigest()[:8], 16) % 10
    return "validation" if bucket < 5 else "test"


def hit_at(ranked: list[int], relevant: set[int], k: int) -> float:
    return float(bool(set(ranked[:k]) & relevant))


def ndcg_at(ranked: list[int], relevant: set[int], k: int) -> float:
    dcg = sum(
        1.0 / math.log2(rank + 2)
        for rank, item in enumerate(ranked[:k])
        if item in relevant
    )
    ideal = sum(
        1.0 / math.log2(rank + 2)
        for rank in range(min(len(relevant), k))
    )
    return dcg / ideal if ideal else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "runs", type=Path,
        help="JSONL rows: query, relevantProductIds, bm25Ids, vectorIds",
    )
    args = parser.parse_args()
    rows = [
        json.loads(line) for line in args.runs.read_text("utf-8").splitlines()
        if line.strip()
    ]
    report = {}
    for partition in ("validation", "test"):
        selected = [row for row in rows if split(row["query"]) == partition]
        metrics = {}
        for name in ("bm25", "vector", "rrf"):
            hit, ndcg = [], []
            for row in selected:
                if name == "rrf":
                    ranked = [
                        item[0] for item in reciprocal_rank_fusion(
                            [row["bm25Ids"][:50], row["vectorIds"][:50]], k=60
                        )
                    ]
                else:
                    ranked = row[f"{name}Ids"]
                relevant = set(row["relevantProductIds"])
                hit.append(hit_at(ranked, relevant, 20))
                ndcg.append(ndcg_at(ranked, relevant, 10))
            metrics[name] = {
                "Hit@20": sum(hit) / len(hit) if hit else 0,
                "NDCG@10": sum(ndcg) / len(ndcg) if ndcg else 0,
            }
        report[partition] = {"count": len(selected), "metrics": metrics}
    enable_rrf = all(
        report[partition]["count"] > 0
        and report[partition]["metrics"]["rrf"]["Hit@20"]
        >= report[partition]["metrics"]["bm25"]["Hit@20"]
        and report[partition]["metrics"]["rrf"]["NDCG@10"]
        >= report[partition]["metrics"]["bm25"]["NDCG@10"]
        for partition in ("validation", "test")
    )
    report["recommendedProductRetrievalMode"] = "rrf" if enable_rrf else "bm25"
    report["policy"] = (
        "Offline legacy signal only; it is not the strict Chinese quality gate. "
        "Use evaluate_evidence_product_retrieval.py with human-confirmed qrels."
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if enable_rrf else 2)


if __name__ == "__main__":
    main()
