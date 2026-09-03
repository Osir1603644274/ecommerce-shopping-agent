import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(AGENT_ROOT))

from evaluation.bm25_inverted_evaluation import (  # noqa: E402
    build_bm25_inverted_evaluation_report,
)


REPORT_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "bm25_inverted_report.json"
)


if __name__ == "__main__":
    report = build_bm25_inverted_evaluation_report()
    temporary_path = REPORT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(REPORT_PATH)
    summary = report["summary"]
    print(
        "BM25 inverted evaluation: "
        f"passed={summary['passed']} "
        f"scanAvgMs={summary['averageReferenceScanMs']:.2f} "
        f"invertedAvgMs={summary['averageInvertedSearchMs']:.2f} "
        f"speedup={summary['averageSpeedup']:.2f}x"
    )
    print(f"Wrote {REPORT_PATH}")
    raise SystemExit(0 if summary["passed"] else 1)
