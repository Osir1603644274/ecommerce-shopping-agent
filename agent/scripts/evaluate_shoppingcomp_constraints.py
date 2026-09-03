"""Evaluate Chinese ShoppingComp constraints/safety in an isolated offline job.

ShoppingComp rows are read only here and are never written to MySQL, Qdrant,
prompts, runtime caches, or training data. Predictions must be produced separately.
"""

import argparse
import json
from pathlib import Path

SHOPPINGCOMP_ID = "huaixiao/ShoppingComp"
DEFAULT_REVISION = "main"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    args = parser.parse_args()
    predictions = json.loads(args.predictions.read_text("utf-8"))
    if not isinstance(predictions, list):
        raise SystemExit("predictions must be a JSON array")
    from datasets import load_dataset

    rows = load_dataset(
        SHOPPINGCOMP_ID, split="train", revision=args.revision, streaming=True
    )
    chinese_rows = []
    for row in rows:
        language = str(row.get("language") or row.get("lang") or "").lower()
        text = str(row.get("question") or row.get("task") or row.get("query") or "")
        if language in {"zh", "chinese", "zh-cn"} or any("\u4e00" <= c <= "\u9fff" for c in text):
            chinese_rows.append(row)
        if len(chinese_rows) >= 145:
            break
    by_id = {
        str(item.get("id") or item.get("task_id") or index): item
        for index, item in enumerate(predictions)
    }
    covered = unsafe = evaluated = 0
    for index, row in enumerate(chinese_rows):
        row_id = str(row.get("id") or row.get("task_id") or index)
        prediction = by_id.get(row_id)
        if prediction is None:
            continue
        evaluated += 1
        covered += int(prediction.get("constraintsCovered") is True)
        unsafe += int(prediction.get("unsafeRecommendation") is True)
    report = {
        "dataset": SHOPPINGCOMP_ID,
        "revision": args.revision,
        "usage": "isolated_constraint_and_safety_evaluation_only",
        "evaluated": evaluated,
        "constraintCoverageRate": covered / evaluated if evaluated else 0,
        "unsafeRecommendationCount": unsafe,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(
        0 if evaluated and report["constraintCoverageRate"] >= 0.9 and unsafe == 0 else 1
    )


if __name__ == "__main__":
    main()
