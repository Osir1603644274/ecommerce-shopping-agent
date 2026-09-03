import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag_quality import write_yelp_retrieval_failure_analysis  # noqa: E402


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "yelp_retrieval_failure_analysis_yelp_only.json"
)


if __name__ == "__main__":
    report = write_yelp_retrieval_failure_analysis(REPORT_PATH)
    print(
        "Yelp retrieval failure analysis: "
        f"cases={report['caseCount']}, "
        f"Hit@{report['topK']}={report['hitAtTopK']}/{report['caseCount']} "
        f"({report['hitAtTopKRate']:.1%}), "
        f"failures={report['failureCount']}, "
        f"recoveredBeyondTopK={report['recoveredBeyondTopK']}"
    )
    print("Failure reasons:")
    for reason, count in report["failureReasons"].items():
        print(f"  {reason}: {count}")
    print("Failure challenge types:")
    for challenge_type, count in report["failureChallengeTypes"].items():
        print(f"  {challenge_type}: {count}")
    print(f"Wrote {REPORT_PATH}")
