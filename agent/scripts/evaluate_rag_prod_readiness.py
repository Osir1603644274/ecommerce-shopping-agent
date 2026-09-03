import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(AGENT_ROOT))

from app.rag_prod_readiness import evaluate_rag_prod_readiness  # noqa: E402


REPORT_PATH = (
    AGENT_ROOT / "knowledge_data" / "eval" / "rag_prod_readiness_report.json"
)


if __name__ == "__main__":
    report = evaluate_rag_prod_readiness()
    temporary_path = REPORT_PATH.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(REPORT_PATH)
    print(
        "RAG-PROD-01 readiness: "
        f"stage3={report['stage3ReviewHybrid']['decision']} "
        f"stage4={report['stage4LlmContentReranker']['decision']} "
        f"stage5={report['stage5ContextAndGroundedAnswer']['decision']} "
        f"highestEligibleStage={report['overall']['highestEligibleStage']}"
    )
    print(f"Wrote {REPORT_PATH}")
