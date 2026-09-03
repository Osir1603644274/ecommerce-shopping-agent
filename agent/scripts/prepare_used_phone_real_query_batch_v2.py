"""Audit retained web queries and prepare non-gold candidates for Qrel v2."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.evaluation.used_phone_real_query_batch_v2 import (
    prepare_real_query_candidates,
    read_intake_records_read_only,
)


def main() -> int:
    repository = Path(__file__).resolve().parents[2]
    output_dir = repository / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v2" / "intake"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake-db", type=Path, default=repository / ".runtime" / "used-phone-demo" / "web-query-intake.sqlite3")
    parser.add_argument("--audit-output", type=Path, default=output_dir / "intake_quality_audit_v2.json")
    parser.add_argument("--candidate-output", type=Path, default=output_dir / "candidate_queries_v2.jsonl")
    args = parser.parse_args()
    if args.audit_output.exists() or args.candidate_output.exists():
        parser.error("refusing to overwrite a prepared real-query intake artifact")
    audit = prepare_real_query_candidates(read_intake_records_read_only(args.intake_db))
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    args.audit_output.write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.candidate_output.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
            for row in audit["acceptedCandidates"]
        ),
        encoding="utf-8",
    )
    print(json.dumps({
        "auditOutput": str(args.audit_output),
        "candidateOutput": str(args.candidate_output),
        "recordCount": audit["recordCount"],
        "acceptedCandidateCount": audit["acceptedCandidateCount"],
        "rejectedRecordCount": audit["rejectedRecordCount"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
