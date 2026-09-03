import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.shop_review_entity_evaluation import (  # noqa: E402
    build_shop_review_entity_validation_report,
)


REPORT_PATH = (
    Path(__file__).resolve().parents[1]
    / "knowledge_data"
    / "eval"
    / "shop_review_entity_report.json"
)


async def main() -> None:
    report = await build_shop_review_entity_validation_report()
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    entity = report["entityResolution"]
    retrieval = report["reviewRetrieval"]
    print(
        "Shop entity -> review validation: "
        f"entity={entity['correct']}/{report['caseCount']} "
        f"Hit@3={retrieval['hits']}/{retrieval['total']} "
        f"MRR={retrieval['mrr']:.4f}"
    )
    print(f"Wrote {REPORT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
