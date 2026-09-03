import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.rag import (
    YELP_EVAL_PATH,
    evaluate_retrieval_cases,
    load_retrieval_cases,
    search_reviews,
)


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "rag"
    / "eval"
    / "yelp_retrieval_report_yelp_only.json"
)


def search_yelp_reviews(question: str, limit: int):
    return search_reviews(question, limit, source="yelp")


if __name__ == "__main__":
    cases = load_retrieval_cases(YELP_EVAL_PATH)
    report = evaluate_retrieval_cases(cases, search_yelp_reviews)
    report["sourceFilter"] = "yelp"
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print("Yelp real retrieval eval (source=yelp only)")
    print("Case count: {total}".format(**report))
    print("Hit@1: {hitsAt1}/{total} = {hitAt1Rate:.1%}".format(**report))
    print(
        "Hit@{topK}: {hits}/{total} = {hitRate:.1%}".format(
            topK=report["topK"],
            hits=report["hits"],
            total=report["total"],
            hitRate=report["hitRate"],
        )
    )
    print("MRR: {mrr:.4f}".format(**report))
    print(
        "耗时: total={totalMs:.2f}ms, avg={avgMs:.2f}ms, "
        "p50={p50Ms:.2f}ms, p95={p95Ms:.2f}ms, max={maxMs:.2f}ms".format(
            **report["timing"]
        )
    )
    for detail in report["details"]:
        if not detail["hit"]:
            print(
                "未命中 {caseId}: 期望 {expectedReviewIds}，实际 {retrievedReviewIds}".format(
                    **detail
                )
            )
    print(f"Wrote {REPORT_PATH}")
