import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge_runtime_smoke import (  # noqa: E402
    build_source_aware_runtime_smoke_report,
)


DEFAULT_REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "source_aware_runtime_smoke_report.json"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    parser.add_argument("--latency-ceiling-ms", type=float, default=5000.0)
    parser.add_argument("--fallback-latency-ceiling-ms", type=float, default=8000.0)
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    report = build_source_aware_runtime_smoke_report(
        latency_ceiling_ms=args.latency_ceiling_ms,
        fallback_latency_ceiling_ms=args.fallback_latency_ceiling_ms,
    )
    args.report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = args.report_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(args.report_path)

    summary = report["summary"]
    print(
        f"source-aware runtime smoke: passed={summary['passed']} "
        f"cases={summary['sourceAwarePassed']}/{summary['sourceAwareCases']} "
        f"fallback={summary['fallbackPassed']} "
        f"eligibleForCanary={summary['eligibleForCanary']}"
    )
    for failure in report["failures"]:
        failed_checks = [
            name
            for name, passed in failure["checks"].items()
            if not passed
        ]
        print(f"FAIL {failure['caseId']}: {', '.join(failed_checks)}")
    print(f"Wrote {args.report_path}")
