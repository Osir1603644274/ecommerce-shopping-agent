"""Record an explicit human Track B query approval without mutating source drafts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from evaluation.kuaisearch_synthetic_evidence_benchmark import (
    BENCHMARK_ID,
    JsonlWriter,
    artifact_fingerprint,
    canonical_json,
    write_json,
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"expected object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    writer = JsonlWriter(path)
    for row in rows:
        writer.write(row)
    return writer.close()


def finalize_approve_all(
    benchmark_dir: Path,
    *,
    reviewer_id: str,
    reviewed_at: str,
    approval_evidence: str,
) -> dict[str, Any]:
    pending_query_path = benchmark_dir / "blind_stage_1_query_review_pending.jsonl"
    pilot_query_path = benchmark_dir / "pilot_queries.jsonl"
    pending_product_path = benchmark_dir / "blind_stage_2_product_review_pending.jsonl"
    pending_queries = load_jsonl(pending_query_path)
    pilot_queries = load_jsonl(pilot_query_path)
    pending_products = load_jsonl(pending_product_path)

    pending_ids = [row["queryId"] for row in pending_queries]
    pilot_ids = [row["queryId"] for row in pilot_queries]
    product_ids = {row["queryId"] for row in pending_products}
    if len(pending_ids) != 9 or len(set(pending_ids)) != 9:
        raise ValueError("expected exactly 9 unique stage-1 query rows")
    if pending_ids != pilot_ids:
        raise ValueError("stage-1 and pilot query ordering/identity mismatch")
    if product_ids != set(pending_ids):
        raise ValueError("stage-2 product rows do not cover exactly the approved query set")
    if len(pending_products) != 216:
        raise ValueError("expected exactly 216 stage-2 product rows")
    if any(row["humanApproval"]["decision"] != "pending" for row in pending_queries):
        raise ValueError("stage-1 source is not a pristine pending batch")

    evidence_hash = hashlib.sha256(approval_evidence.encode("utf-8")).hexdigest()
    decisions = []
    for row in pending_queries:
        decisions.append(
            {
                "benchmarkId": BENCHMARK_ID,
                "queryId": row["queryId"],
                "decision": "accept",
                "queryNaturalness": "approved",
                "semanticFidelity": "approved",
                "approvedQuery": row["proposedQuery"],
                "reviewerId": reviewer_id,
                "reviewedAt": reviewed_at,
                "decisionProvenance": {
                    "type": "project_owner_human_conversation_approval",
                    "transcribedBy": "codex",
                    "approvalEvidenceSha256": evidence_hash,
                    "approvalEvidenceVerbatim": approval_evidence,
                    "automaticDecision": False,
                },
                "notes": "Reviewer approved the complete nine-query batch without rewrite.",
            }
        )

    approved_queries = []
    for row in pilot_queries:
        approved = dict(row)
        approved["humanApproval"] = {
            "queryNaturalness": "approved",
            "semanticFidelity": "approved",
            "reviewerId": reviewer_id,
            "reviewedAt": reviewed_at,
            "notes": "Approved as submitted in the nine-query batch.",
        }
        approved["goldStatus"] = "query_human_approved_candidate_labels_pending"
        approved_queries.append(approved)

    ready_products = []
    for row in pending_products:
        ready = dict(row)
        ready["gateStatus"] = "ready_after_stage_1_query_approval"
        ready["queryApprovalRef"] = {
            "artifact": "query_review_decisions_v1.jsonl",
            "queryId": row["queryId"],
        }
        ready_products.append(ready)

    artifacts = {
        "queryReviewDecisions": write_jsonl(
            benchmark_dir / "query_review_decisions_v1.jsonl", decisions
        ),
        "approvedQueries": write_jsonl(
            benchmark_dir / "approved_queries_v1.jsonl", approved_queries
        ),
        "blindProductReviewReady": write_jsonl(
            benchmark_dir / "blind_stage_2_product_review_ready.jsonl", ready_products
        ),
    }
    audit = {
        "benchmarkId": BENCHMARK_ID,
        "reviewStage": 1,
        "status": "query_review_complete_product_review_ready",
        "reviewerId": reviewer_id,
        "reviewedAt": reviewed_at,
        "decisionCounts": {"accept": 9, "rewrite": 0, "reject": 0},
        "humanApprovalCounts": {"query": 9, "candidate": 0},
        "inputs": {
            "pendingQueries": artifact_fingerprint(pending_query_path, rows=9),
            "pilotQueries": artifact_fingerprint(pilot_query_path, rows=9),
            "pendingProducts": artifact_fingerprint(pending_product_path, rows=216),
        },
        "outputs": artifacts,
        "nextGate": "Begin blinded stage-2 product judgment; automatic prelabels remain hidden.",
    }
    artifacts["queryReviewAudit"] = write_json(
        benchmark_dir / "query_review_audit_v1.json", audit
    )
    review_manifest = {
        "benchmarkId": BENCHMARK_ID,
        "layer": "human_review_v1",
        "baseManifest": artifact_fingerprint(benchmark_dir / "manifest.json"),
        "status": audit["status"],
        "artifacts": artifacts,
    }
    write_json(benchmark_dir / "human_review_manifest_v1.json", review_manifest)
    return audit


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", type=Path, required=True)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--reviewed-at", required=True)
    parser.add_argument("--approval-evidence", required=True)
    parser.add_argument("--approve-all", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    finalize_approve_all(
        args.benchmark_dir,
        reviewer_id=args.reviewer_id,
        reviewed_at=args.reviewed_at,
        approval_evidence=args.approval_evidence,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
