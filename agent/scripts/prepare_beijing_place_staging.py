from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = AGENT_ROOT.parent
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.place_data import build_beijing_staging


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize Beijing public place datasets into a versioned staging directory."
    )
    parser.add_argument(
        "--raw-dir", type=Path,
        default=REPOSITORY_ROOT / "data" / "raw" / "beijing" / "official",
    )
    parser.add_argument(
        "--output-root", type=Path,
        default=REPOSITORY_ROOT / "data" / "processed" / "beijing",
    )
    parser.add_argument("--candidate-limit", type=int, default=100)
    args = parser.parse_args()
    if args.candidate_limit < 1:
        parser.error("--candidate-limit must be positive")

    result = build_beijing_staging(
        args.raw_dir.resolve(), args.output_root.resolve(), candidate_limit=args.candidate_limit
    )
    summary = result["manifest"]["quality_summary"]
    print(json.dumps({
        "output_dir": result["output_dir"],
        "counts": summary["counts"],
        "quarantine_count": summary["quarantine_count"],
        "park_matching": summary["park_matching"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
