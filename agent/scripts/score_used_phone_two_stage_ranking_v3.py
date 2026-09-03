"""Score a v3 two-stage ranking run against the frozen public-dev qrel identity."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--public-dev-judgments", type=Path, required=True)
    parser.add_argument("--public-dev-preregistration", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from agent.evaluation.used_phone_two_stage_ranking_scorer_v3 import (
        canonical_bytes,
        score_public_dev,
    )
    result = score_public_dev(
        predictions_path=args.predictions,
        manifest_path=args.manifest,
        public_dev_judgments_path=args.public_dev_judgments,
        public_dev_preregistration_path=args.public_dev_preregistration,
    )
    if args.output:
        args.output.write_bytes(canonical_bytes(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
