"""Score one authenticated held-out natural-guide Validation20 run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--public-catalog", type=Path, required=True)
    parser.add_argument("--preregistration", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from agent.evaluation.used_phone_natural_guide_validation20_evaluator_v1 import canonical_bytes, score_validation20
    result = score_validation20(predictions_path=args.predictions,manifest_path=args.manifest,public_catalog_path=args.public_catalog,preregistration_path=args.preregistration)
    if args.output is not None:
        if args.output.exists():
            raise ValueError("score output already exists")
        args.output.write_bytes(canonical_bytes(result))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["gatePassed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
