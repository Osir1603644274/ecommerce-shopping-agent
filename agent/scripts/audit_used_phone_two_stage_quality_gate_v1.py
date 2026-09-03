"""Audit one authenticated v3 Dev10 attempt against the frozen quality gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--score", type=Path, required=True)
    parser.add_argument("--public-dev-judgments", type=Path, required=True)
    parser.add_argument("--public-dev-preregistration", type=Path, required=True)
    parser.add_argument("--quality-gate-preregistration", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from agent.evaluation.used_phone_two_stage_quality_gate_v1 import audit_quality_gate
    from agent.evaluation.used_phone_two_stage_ranking_scorer_v3 import canonical_bytes
    result = audit_quality_gate(**{
        key.replace("-", "_"): value for key, value in {
            "predictions_path": args.predictions,
            "manifest_path": args.manifest,
            "score_path": args.score,
            "public_dev_judgments_path": args.public_dev_judgments,
            "public_dev_preregistration_path": args.public_dev_preregistration,
            "quality_gate_preregistration_path": args.quality_gate_preregistration,
        }.items()
    })
    if args.output:
        if args.output.exists():
            raise FileExistsError(f"refusing to overwrite {args.output}")
        args.output.write_bytes(canonical_bytes(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["gatePassed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
