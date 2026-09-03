import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(AGENT_ROOT))

from evaluation.review_hybrid_evaluation import build_review_hybrid_validation_report  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "review_hybrid_validation_report.json"
)


if __name__ == "__main__":
    report = build_review_hybrid_validation_report()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = REPORT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(REPORT_PATH)
    vector = report["methods"]["sourceAwareVector"]
    hybrid = report["methods"]["reviewHybrid"]
    print(
        "review Hybrid validation: "
        f"passed={report['summary']['passed']} "
        f"vectorNdcg={vector['metrics']['meanNdcgAt5']:.4f} "
        f"hybridNdcg={hybrid['metrics']['meanNdcgAt5']:.4f} "
        f"hybridAvgMs={hybrid['timing']['averageMs']:.2f} "
        f"hybridMaxMs={hybrid['timing']['maxMs']:.2f}"
    )
    print(f"Wrote {REPORT_PATH}")
    raise SystemExit(0 if report["summary"]["passed"] else 1)
