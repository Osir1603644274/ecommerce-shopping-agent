import json
import subprocess
import sys
from pathlib import Path

from evaluation.product_retrieval_evaluation import (
    evaluate_gate,
    evaluate_partition,
    ndcg_at_10,
    summarize_qrels,
)
from scripts.benchmark_product_search import percentile


ROOT = Path(__file__).parents[1]


def _qrel(query_id="phone-001", split="validation"):
    return {
        "queryId": query_id,
        "split": split,
        "category": "phone",
        "reviewStatus": "human_confirmed",
        "judgments": [
            {"productId": 1, "grade": 3, "labelSource": "human"},
            {"productId": 2, "grade": 1, "labelSource": "human"},
            {"productId": 3, "grade": 0, "labelSource": "human"},
        ],
    }


def test_draft_has_exact_90_query_distribution_and_no_fake_labels():
    rows = [
        json.loads(line)
        for line in (ROOT / "evaluation/product_qrel_draft.jsonl").read_text("utf-8").splitlines()
    ]

    assert len(rows) == 90
    assert summarize_qrels(rows) == {
        "phone": {"validation": 20, "sealed_test": 10},
        "laptop": {"validation": 20, "sealed_test": 10},
        "headphones": {"validation": 20, "sealed_test": 10},
    }
    assert all(row["reviewStatus"] == "draft" and row["judgments"] == [] for row in rows)


def test_qrel_validation_command_accepts_review_draft():
    result = subprocess.run(
        [sys.executable, "scripts/evaluate_evidence_product_retrieval.py", "--validate-only"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["qrelValidation"]["valid"] is True
    assert report["qrelValidation"]["humanConfirmedCount"] == 0


def test_metrics_include_constraints_and_evidence_coverage():
    qrels = [_qrel()]
    runs = {
        "phone-001": {
            "bm25": [1, 3],
            "rrf_rule": [
                {
                    "productId": 1,
                    "hardConstraintViolations": 0,
                    "factClaims": [
                        {"field": "title", "evidenceRef": "product:1:title"},
                        {"field": "brand", "evidenceRef": "product:1:brand"},
                    ],
                },
                {
                    "productId": 2,
                    "hardConstraintViolations": 0,
                    "factClaims": [
                        {"field": "price", "evidenceRef": "product:2:snapshotPriceMinor"},
                    ],
                },
            ],
        }
    }
    report = evaluate_partition(qrels, runs, "validation")
    proposed = report["metrics"]["rrf_rule"]

    assert proposed["NDCG@10"] == 1.0
    assert proposed["Recall@20"] == 1.0
    assert proposed["Hit@20"] == 1.0
    assert proposed["hardConstraintViolationRate"] == 0.0
    assert proposed["factEvidenceCoverage"] == 1.0
    assert ndcg_at_10([1, 2], {1: 3, 2: 1, 3: 0}) == 1.0


def test_gate_is_unverified_without_confirmed_runs():
    result = evaluate_gate(
        {"metrics": {}, "finalMetricsAllowed": False},
        proposed_system="rrf_rule",
        baseline_systems=["bm25"],
    )
    assert result["status"] == "unverified"


def test_latency_percentile_uses_nearest_rank():
    assert percentile([0.5, 0.1, 0.3, 0.2, 0.4], 0.95) == 0.5
