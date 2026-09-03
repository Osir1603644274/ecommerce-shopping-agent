import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.capability_router_evaluation import evaluate_capability_router  # noqa: E402


REPORT_DIR = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the LLM capability router.")
    parser.add_argument(
        "--split",
        choices=["validation", "test"],
        default="validation",
    )
    args = parser.parse_args()
    report = await evaluate_capability_router(split=args.split)
    report_path = REPORT_DIR / f"capability_router_{args.split}_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "Capability Router eval: "
        f"split={args.split}, "
        f"complete={report['completePasses']}/{report['caseCount']} "
        f"({report['completeAccuracy']:.2%}), "
        f"fallbacks={report['fallbackCount']}"
    )
    for name, metric in report["metrics"].items():
        accuracy = metric["accuracy"]
        rendered = "n/a" if accuracy is None else f"{accuracy:.2%}"
        print(
            f"  {name}: {metric['passed']}/{metric['applicable']} ({rendered})"
        )
    if report["failures"]:
        print("Failures:")
        for failure in report["failures"]:
            print(
                f"  {failure['caseId']}: expected={failure['expectedTools']} "
                f"model={failure['modelToolNames']} executed={failure['executedToolNames']}"
            )
    print(f"Wrote {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
