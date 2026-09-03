from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from recommendation.beijing_localization import (
    DEFAULT_CASES_PATH,
    DEFAULT_ITEMS_PATH,
    DEFAULT_PROJECTION_PATH,
    DEFAULT_SAMPLE_PATH,
    DEFAULT_SPLITS_PATH,
    build_projection,
    load_anchor_places,
    load_translation_checkpoint,
    localize_cases,
    localize_recommendation_items,
    localize_sample,
    projection_payload,
)
from recommendation.beijing_localization_quality import (
    DEFAULT_QUALITY_REPORT_PATH,
    audit_beijing_localization,
    write_quality_report,
)
from recommendation.yelp_content_zh_translations import (
    YELP_CONTENT_ZH_TRANSLATIONS,
)
from recommendation.yelp_sql import DEFAULT_SQL_OUTPUT_PATH, generate_yelp_import_sql


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def migrate(
    *,
    sample_path: Path = DEFAULT_SAMPLE_PATH,
    items_path: Path = DEFAULT_ITEMS_PATH,
    cases_path: Path = DEFAULT_CASES_PATH,
    splits_path: Path = DEFAULT_SPLITS_PATH,
    projection_path: Path = DEFAULT_PROJECTION_PATH,
    sql_path: Path = DEFAULT_SQL_OUTPUT_PATH,
    quality_report_path: Path = DEFAULT_QUALITY_REPORT_PATH,
) -> dict[str, int]:
    sample = _load(sample_path)
    items_payload = _load(items_path)
    all_items = [
        {
            **item,
            "shopId": item["itemId"],
        }
        for item in items_payload["items"]
    ]
    anchors = load_anchor_places()
    projection = build_projection(all_items, anchors=anchors)
    translations = dict(YELP_CONTENT_ZH_TRANSLATIONS)
    translations.update(load_translation_checkpoint())

    localized_sample = localize_sample(
        sample,
        projection,
        translations=translations,
    )
    localized_items = localize_recommendation_items(items_payload, projection)
    localized_cases = localize_cases(_load(cases_path), projection)
    localized_splits = localize_cases(_load(splits_path), projection)
    quality_report = audit_beijing_localization(
        source_sample=sample,
        localized_sample=localized_sample,
        source_items=items_payload,
        localized_items=localized_items,
        projection=projection,
        anchors=anchors,
        expected_translations=translations,
    )
    if quality_report["status"] != "passed":
        failed_checks = quality_report["summary"]["failedRequiredChecks"]
        raise ValueError(
            "Beijing localization quality gate failed: "
            + ", ".join(failed_checks)
        )

    _write(sample_path, localized_sample)
    _write(items_path, localized_items)
    _write(cases_path, localized_cases)
    _write(splits_path, localized_splits)
    _write(projection_path, projection_payload(projection))
    write_quality_report(quality_report, quality_report_path)
    sql_path.write_text(
        generate_yelp_import_sql(localized_sample),
        encoding="utf-8",
    )
    return {
        "projectedItems": len(projection),
        "onlineShops": len(localized_sample["shops"]),
        "reviews": len(localized_sample["reviews"]),
        "translatedReviews": sum(
            1 for review in localized_sample["reviews"] if review.get("contentZh")
        ),
        "userBehaviors": len(localized_sample["userBehaviors"]),
        "qualityChecks": quality_report["summary"]["requiredCheckCount"],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Project Yelp shops, reviews and recommendation artifacts into Beijing."
    )
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE_PATH)
    parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS_PATH)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--splits", type=Path, default=DEFAULT_SPLITS_PATH)
    parser.add_argument("--projection", type=Path, default=DEFAULT_PROJECTION_PATH)
    parser.add_argument("--sql", type=Path, default=DEFAULT_SQL_OUTPUT_PATH)
    parser.add_argument(
        "--quality-report",
        type=Path,
        default=DEFAULT_QUALITY_REPORT_PATH,
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    summary = migrate(
        sample_path=args.sample,
        items_path=args.items,
        cases_path=args.cases,
        splits_path=args.splits,
        projection_path=args.projection,
        sql_path=args.sql,
        quality_report_path=args.quality_report,
    )
    print(json.dumps(summary, ensure_ascii=False))
