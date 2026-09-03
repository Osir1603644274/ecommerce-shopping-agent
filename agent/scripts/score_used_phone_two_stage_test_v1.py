"""Score one authenticated frozen Test10 prediction."""

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--test-judgments", type=Path, required=True)
    parser.add_argument("--test-preregistration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("refusing to overwrite an existing Test10 score")
    from agent.evaluation.used_phone_two_stage_test_scorer_v1 import canonical_bytes, score_test10
    result = score_test10(
        predictions_path=args.predictions, manifest_path=args.manifest,
        test_judgments_path=args.test_judgments,
        test_preregistration_path=args.test_preregistration,
    )
    args.output.write_bytes(canonical_bytes(result))
    print(json.dumps(result["metrics"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
