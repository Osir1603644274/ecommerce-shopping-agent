from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.place_data import PlaceCatalog
from app.place_data.evaluation import DEFAULT_EVAL_CASES_PATH, evaluate_place_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic evaluation for the Beijing place catalog.")
    parser.add_argument("--catalog", type=Path, default=None)
    parser.add_argument("--cases", type=Path, default=DEFAULT_EVAL_CASES_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=AGENT_ROOT / "place_data" / "eval" / "beijing_places_v2_report.json",
    )
    args = parser.parse_args()

    catalog = PlaceCatalog.load(args.catalog.resolve() if args.catalog else None)
    report = evaluate_place_catalog(catalog, args.cases.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output.resolve()),
        "totalCases": report["totalCases"],
        "passedCases": report["passedCases"],
        "passRate": report["passRate"],
    }, ensure_ascii=False, indent=2))
    return 0 if report["passedCases"] == report["totalCases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
