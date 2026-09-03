import asyncio
import json
from pathlib import Path

from evaluation.rag_evaluation import evaluate_generation


REPORT_PATH = Path(__file__).resolve().parents[1] / "rag" / "eval" / "generation_baseline.json"


async def main() -> None:
    report = await evaluate_generation()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    retrieval = report["retrieval"]
    generation = report["generation"]
    print(
        "检索 Hit@{topK}: {hits}/{total} = {rate:.1%}".format(
            topK=retrieval["topK"],
            hits=retrieval["hits"],
            total=report["totalCases"],
            rate=retrieval["hitRate"],
        )
    )
    print(
        "期望事实覆盖率: {covered}/{total} = {rate:.1%}".format(
            covered=generation["coveredExpectedPoints"],
            total=generation["totalExpectedPoints"],
            rate=generation["coverageRate"],
        )
    )
    print(
        "回答忠实率: {faithful}/{total} = {rate:.1%}".format(
            faithful=generation["faithfulAnswers"],
            total=report["totalCases"],
            rate=generation["faithfulnessRate"],
        )
    )
    print(f"报告已保存到：{REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
