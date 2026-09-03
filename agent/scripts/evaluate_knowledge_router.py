import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.knowledge_router_evaluation import evaluate_router  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "router_report.json"
)


if __name__ == "__main__":
    report = evaluate_router()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "Router eval: "
        f"cases={report['caseCount']}, "
        f"exact={report['exactMatches']}/{report['caseCount']}, "
        f"accuracy={report['accuracy']:.2%}"
    )
    for category, metrics in report["byCategory"].items():
        print(
            f"  {category}: "
            f"{metrics['exactMatches']}/{metrics['total']} "
            f"({metrics['accuracy']:.2%})"
        )
    if report["failures"]:
        print("Failures:")
        for failure in report["failures"]:
            print(
                f"  {failure['caseId']}: "
                f"expected={failure['expectedSources']} "
                f"actual={failure['actualSources']} "
                f"question={failure['question']}"
            )
    print(f"Wrote {REPORT_PATH}")
