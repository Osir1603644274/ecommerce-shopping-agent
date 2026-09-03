from app.rag import evaluate_retrieval, search_reviews


if __name__ == "__main__":
    report = evaluate_retrieval(search_reviews)
    print(
        "Hit@1: {hitsAt1}/{total} = {hitAt1Rate:.1%}".format(**report)
    )
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
    print("\n按难度分组：")
    for difficulty, metrics in report["byDifficulty"].items():
        print(
            "{difficulty}: Hit@1 {hitsAt1}/{total} = {hitAt1Rate:.1%}, "
            "Hit@{topK} {hits}/{total} = {hitRate:.1%}, MRR = {mrr:.4f}".format(
                difficulty=difficulty,
                topK=report["topK"],
                **metrics,
            )
        )
    print("\n按挑战类型分组：")
    for challenge_type, metrics in report["byChallengeType"].items():
        print(
            "{challenge_type}: Hit@1 {hitsAt1}/{total} = {hitAt1Rate:.1%}, "
            "Hit@{topK} {hits}/{total} = {hitRate:.1%}, MRR = {mrr:.4f}".format(
                challenge_type=challenge_type,
                topK=report["topK"],
                **metrics,
            )
        )
    for detail in report["details"]:
        if not detail["hit"]:
            print(
                "未命中 {caseId}: 期望 {expectedReviewIds}，实际 {retrievedReviewIds}".format(
                    **detail
                )
            )
