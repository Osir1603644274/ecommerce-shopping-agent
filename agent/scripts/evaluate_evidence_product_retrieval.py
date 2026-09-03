"""Validate product qrels and evaluate evidence-aware ranking runs."""

import argparse
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, FormatChecker

AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from evaluation.product_retrieval_evaluation import (
    evaluate_gate,
    evaluate_partition,
    summarize_qrels,
)


def _jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line) for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    root = AGENT_ROOT
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--qrels", type=Path,
        default=root / "evaluation/product_qrel_draft.jsonl",
    )
    parser.add_argument(
        "--schema", type=Path,
        default=root / "evaluation/product_qrel.schema.json",
    )
    parser.add_argument(
        "--runs", type=Path,
        help="JSONL: queryId and runs mapping system names to ranked result arrays",
    )
    parser.add_argument("--proposed-system", default="rrf_rule")
    parser.add_argument("--baseline-systems", default="bm25,elasticsearch,qdrant,rrf")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    qrels = _jsonl(args.qrels)
    schema = json.loads(args.schema.read_text("utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = [
        f"row {index + 1}: {error.message}"
        for index, row in enumerate(qrels)
        for error in validator.iter_errors(row)
    ]
    ids = [row["queryId"] for row in qrels]
    if len(ids) != len(set(ids)):
        errors.append("queryId values must be unique")
    summary = summarize_qrels(qrels)
    expected_summary = {
        category: {"validation": 20, "sealed_test": 10}
        for category in ("phone", "laptop", "headphones")
    }
    if len(qrels) != 90 or summary != expected_summary:
        errors.append(f"expected 90 rows with 20/10 per category, got {summary}")

    report = {
        "schema": "product-retrieval-evaluation-v1",
        "qrelValidation": {
            "valid": not errors,
            "count": len(qrels),
            "distribution": summary,
            "errors": errors,
            "humanConfirmedCount": sum(
                row["reviewStatus"] == "human_confirmed" for row in qrels
            ),
        },
        "truthBoundary": (
            "AI-created draft rows are not human-confirmed relevance judgments. "
            "Sealed-test metrics are non-final until every sealed row is human_confirmed."
        ),
    }
    if args.runs:
        run_rows = _jsonl(args.runs)
        runs = {row["queryId"]: row["runs"] for row in run_rows}
        validation = evaluate_partition(qrels, runs, "validation")
        sealed = evaluate_partition(qrels, runs, "sealed_test")
        report["validation"] = validation
        report["sealedTest"] = sealed
        report["qualityGate"] = evaluate_gate(
            validation,
            proposed_system=args.proposed_system,
            baseline_systems=[
                item.strip() for item in args.baseline_systems.split(",") if item.strip()
            ],
        )
        report["sealedTestClaimStatus"] = (
            "final" if sealed["finalMetricsAllowed"] else "unverified_not_final"
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        raise SystemExit(2)
    if args.validate_only:
        raise SystemExit(0)
    if not args.runs:
        raise SystemExit(2)
    raise SystemExit(0 if report["qualityGate"]["status"] == "passed" else 3)


if __name__ == "__main__":
    main()
