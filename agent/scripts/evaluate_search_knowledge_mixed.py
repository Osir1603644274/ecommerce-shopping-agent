import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.knowledge_mixed_evaluation import evaluate_mixed_search  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "search_knowledge_mixed_report.json"
)


if __name__ == "__main__":
    report = evaluate_mixed_search()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "Mixed search eval: "
        f"cases={report['caseCount']}, "
        f"selected={report['selectedExact']}/{report['caseCount']} "
        f"({report['selectedAccuracy']:.2%}), "
        f"citations={report['citationCovered']}/{report['caseCount']} "
        f"({report['citationCoverageRate']:.2%})"
    )
    for category, metrics in report["byCategory"].items():
        print(
            f"  {category}: "
            f"selected={metrics['selectedExact']}/{metrics['total']} "
            f"({metrics['selectedAccuracy']:.2%}), "
            f"citations={metrics['citationCovered']}/{metrics['total']} "
            f"({metrics['citationCoverageRate']:.2%})"
        )
    if report["failures"]:
        print("Failures:")
        for failure in report["failures"]:
            print(
                f"  {failure['caseId']}: "
                f"expected={failure['expectedSources']} "
                f"actual={failure['actualSources']} "
                f"citationSources={failure['citationSources']} "
                f"missing={failure['missingCitationSources']}"
            )
    print(f"Wrote {REPORT_PATH}")
