import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(AGENT_ROOT))

from app.review_index_freshness_smoke import (  # noqa: E402
    run_review_index_freshness_smoke,
)


REPORT_PATH = (
    AGENT_ROOT
    / "knowledge_data"
    / "eval"
    / "review_index_freshness_smoke_report.json"
)


if __name__ == "__main__":
    report = asyncio.run(run_review_index_freshness_smoke())
    temporary_path = REPORT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(REPORT_PATH)
    print(
        "review index freshness smoke: "
        f"passed={report['passed']} "
        f"checks={sum(report['checks'].values())}/{len(report['checks'])}"
    )
    print(f"Wrote {REPORT_PATH}")
    raise SystemExit(0 if report["passed"] else 1)
