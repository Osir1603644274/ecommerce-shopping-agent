"""Build or score the public used-phone v2 contract development set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.evaluation.used_phone_contract_dev_v2_builder import build, score_predictions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--expectations", type=Path)
    args = parser.parse_args()
    if args.output_dir is not None and args.predictions is None and args.expectations is None:
        result = build(args.output_dir)
    elif args.output_dir is None and args.predictions is not None and args.expectations is not None:
        result = score_predictions(args.predictions, args.expectations)
    else:
        parser.error("choose either --output-dir or --predictions with --expectations")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
