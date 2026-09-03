"""Audit frozen attempt-004 public-dev ranking metrics without new SUT execution."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--legacy-score", type=Path, required=True)
    parser.add_argument("--public-dev-judgments", type=Path, required=True)
    parser.add_argument("--audit-preregistration", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from agent.evaluation.used_phone_two_stage_ranking_metric_audit_v1 import (
        audit_public_dev_metrics,
    )
    from agent.evaluation.used_phone_two_stage_ranking_scorer_v3 import canonical_bytes

    result = audit_public_dev_metrics(
        predictions_path=args.predictions,
        manifest_path=args.manifest,
        legacy_score_path=args.legacy_score,
        public_dev_judgments_path=args.public_dev_judgments,
        audit_preregistration_path=args.audit_preregistration,
    )
    if args.output:
        args.output.write_bytes(canonical_bytes(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
