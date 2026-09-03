from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


AGENT_ROOT = Path(__file__).resolve().parents[1]
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.place_data.agent_evaluation import (  # noqa: E402
    DEFAULT_PLACE_AGENT_CASES_PATH,
    evaluate_place_agent_cases,
    load_place_agent_cases,
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate structured place tools in the full Agent loop.")
    parser.add_argument("--split", choices=["validation", "test"], default="validation")
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Run one case id. Repeat the option to run multiple targeted cases.",
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_PLACE_AGENT_CASES_PATH)
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Defaults to agent/place_data/eval/place_agent_<split>_report.json",
    )
    args = parser.parse_args()
    output = args.output or AGENT_ROOT / "place_data" / "eval" / f"place_agent_{args.split}_report.json"
    report = await evaluate_place_agent_cases(
        load_place_agent_cases(args.cases.resolve()),
        split=args.split,
        case_ids=set(args.case_ids) if args.case_ids else None,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"Place Agent eval: split={args.split}, complete={report['completePasses']}/"
        f"{report['caseCount']} ({report['completeAccuracy']:.2%}), fallbacks={report['fallbackCount']}"
    )
    for name, metric in report["metrics"].items():
        accuracy = metric["accuracy"]
        rendered = "n/a" if accuracy is None else f"{accuracy:.2%}"
        print(f"  {name}: {metric['passed']}/{metric['applicable']} ({rendered})")
    for failure in report["failures"]:
        print(
            f"  FAIL {failure['caseId']}: model={failure['modelToolNames']} "
            f"executed={failure['executedToolNames']}"
        )
    print(f"Wrote {output.resolve()}")
    return 0 if report["completePasses"] == report["caseCount"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
