"""Apply the frozen Validation10 thresholds to an authenticated frozen score."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--score", type=Path, required=True)
    parser.add_argument("--validation-judgments", type=Path, required=True)
    parser.add_argument("--validation-preregistration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("refusing to overwrite an existing Validation10 Gate report")
    from agent.evaluation.used_phone_two_stage_validation_gate_v1 import (
        audit_validation_gate,
    )
    from agent.evaluation.used_phone_two_stage_validation_scorer_v1 import (
        canonical_bytes,
    )
    result = audit_validation_gate(
        predictions_path=args.predictions,
        manifest_path=args.manifest,
        score_path=args.score,
        validation_judgments_path=args.validation_judgments,
        validation_preregistration_path=args.validation_preregistration,
    )
    args.output.write_bytes(canonical_bytes(result))
    print(json.dumps({
        "checks": result["checks"], "gatePassed": result["gatePassed"],
        "metrics": result["metrics"],
    }, ensure_ascii=False, sort_keys=True))
    return 0 if result["gatePassed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
