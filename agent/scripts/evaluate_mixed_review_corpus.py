import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(AGENT_ROOT))

from evaluation.mixed_review_corpus_evaluation import (  # noqa: E402
    build_mixed_review_corpus_artifacts,
)


EVAL_DIR = AGENT_ROOT / "knowledge_data" / "eval"
REPORT_PATH = EVAL_DIR / "mixed_review_corpus_validation_report.json"
POOL_PATH = EVAL_DIR / "mixed_review_corpus_candidate_pool.json"
JUDGMENT_PATH = EVAL_DIR / "mixed_review_corpus_judgments.json"


def _write_json(path: Path, payload: dict) -> None:
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


if __name__ == "__main__":
    judgment_artifact = json.loads(JUDGMENT_PATH.read_text(encoding="utf-8"))
    report, pool = build_mixed_review_corpus_artifacts(
        judgment_artifact=judgment_artifact,
    )
    _write_json(REPORT_PATH, report)
    _write_json(POOL_PATH, pool)
    comparison = report["comparison"]
    seed = comparison["seedHybridKnownEvidenceTop3"]
    yelp = comparison["yelpHybridKnownQrels"]
    print(
        "Mixed review validation: "
        f"seedHits={seed['yelpOnlyBm25']['hits']}->{seed['allReviewBm25']['hits']} "
        f"yelpNdcg={yelp['yelpOnlyBm25']['meanNdcgAt5']:.4f}"
        f"->{yelp['allReviewBm25']['meanNdcgAt5']:.4f} "
        f"judgedCandidates={pool['judgmentSummary']['judgedCount']} "
        f"unjudgedCandidates={pool['judgmentSummary']['unjudgedCount']}"
    )
    print(f"Wrote {REPORT_PATH}")
    print(f"Wrote {POOL_PATH}")
