"""Audit whether pending product qrels can be judged from authoritative facts.

This command never creates relevance labels.  It joins the pending human-review
batch with the current MySQL catalog and reports whether every hard requirement
has an authoritative value.  A missing value is a data blocker, not a negative
relevance judgment.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


AGENT_ROOT = Path(__file__).resolve().parents[1]
CATEGORY_KEYWORDS = {
    "phone": ("手机", "智能手机"),
    "laptop": ("笔记本", "笔记本电脑", "游戏本", "轻薄本"),
    "headphones": ("耳机", "耳麦", "蓝牙耳机"),
}
EXCLUDED_KEYWORDS = {
    "phone": (
        "号卡", "流量卡", "手机卡", "电话卡", "上网卡", "套餐",
        "手机壳", "保护壳", "贴膜", "数据线", "充电器", "支架", "配件",
    ),
    "laptop": (
        "电脑包", "内胆包", "保护壳", "贴膜", "键盘膜", "支架", "散热器",
        "电源适配器", "配件",
    ),
    "headphones": (
        "耳机套", "保护套", "收纳盒", "充电盒", "耳罩", "耳塞套", "转接线",
        "耳机配件", "配件",
    ),
}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text("utf-8").splitlines()
        if line.strip()
    ]


def _connect(mysql_url: str):
    import pymysql

    parsed = urlparse(mysql_url)
    return pymysql.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password,
        database=parsed.path.lstrip("/"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def _category(product: dict[str, Any]) -> str | None:
    for field in ("category_l3", "category_l2", "category_l1"):
        value = str(product.get(field) or "")
        for category, keywords in CATEGORY_KEYWORDS.items():
            if any(keyword in value for keyword in keywords):
                return category
    return None


def _is_primary_product(product: dict[str, Any], category: str) -> bool:
    text = " ".join(
        str(product.get(field) or "")
        for field in ("title", "category_l1", "category_l2", "category_l3")
    )
    return not any(keyword in text for keyword in EXCLUDED_KEYWORDS[category])


def _known_fact(
    product: dict[str, Any],
    attributes: dict[int, dict[str, dict[str, Any]]],
    key: str,
) -> Any | None:
    if key == "price_minor":
        if product.get("price_status") != "verified":
            return None
        return product.get("snapshot_price_minor")
    attribute = attributes.get(int(product["id"]), {}).get(key)
    if not attribute:
        return None
    value_type = attribute.get("value_type")
    if value_type == "number":
        value = attribute.get("normalized_number")
        return float(value) if value is not None else None
    if value_type == "boolean":
        value = attribute.get("normalized_boolean")
        return bool(value) if value is not None else None
    return attribute.get("normalized_text") or None


def _matches(value: Any, requirement: dict[str, Any]) -> bool:
    expected = requirement.get("value")
    operator = requirement.get("operator")
    if operator == "eq":
        return value == expected
    if operator == "lte":
        return value <= expected
    if operator == "gte":
        return value >= expected
    if operator == "in":
        return value in expected
    if operator == "not_in":
        return value not in expected
    return False


def build_report(
    reviews: list[dict[str, Any]],
    products: list[dict[str, Any]],
    attribute_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    attributes: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in attribute_rows:
        attributes[int(row["product_id"])][str(row["attribute_key"])] = row

    products_by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for product in products:
        category = _category(product)
        if category and _is_primary_product(product, category):
            products_by_category[category].append(product)

    query_reports = []
    for review in reviews:
        category = str(review["category"])
        candidates = products_by_category.get(category, [])
        hard_requirements = [
            item
            for item in review.get("originalRequirements", [])
            if item.get("priority") == "hard"
        ]
        known_candidates = []
        matching_candidates = []
        key_coverage: dict[str, int] = {}
        for requirement in hard_requirements:
            key = str(requirement["key"])
            key_coverage[key] = sum(
                _known_fact(product, attributes, key) is not None
                for product in candidates
            )
        for product in candidates:
            facts = {
                str(requirement["key"]): _known_fact(
                    product, attributes, str(requirement["key"])
                )
                for requirement in hard_requirements
            }
            if any(value is None for value in facts.values()):
                continue
            known_candidates.append(int(product["id"]))
            if all(
                _matches(facts[str(requirement["key"])], requirement)
                for requirement in hard_requirements
            ):
                matching_candidates.append(int(product["id"]))

        blockers = [
            {
                "code": "no_authoritative_fact_coverage",
                "requirementKey": key,
                "message": f"No {category} product has an authoritative {key} value.",
            }
            for key, count in key_coverage.items()
            if count == 0
        ]
        if not blockers and not known_candidates:
            blockers.append({
                "code": "no_fully_judgeable_candidate",
                "message": "No candidate has authoritative values for every hard requirement.",
            })
        query_reports.append({
            "queryId": review["queryId"],
            "category": category,
            "candidateCatalogCount": len(candidates),
            "hardRequirementFactCoverage": key_coverage,
            "fullyJudgeableCandidateCount": len(known_candidates),
            "verifiedMatchCandidateCount": len(matching_candidates),
            "sampleVerifiedMatchProductIds": matching_candidates[:10],
            "status": "ready_for_human_review" if known_candidates else "blocked",
            "blockers": blockers,
        })

    status_counts: dict[str, int] = defaultdict(int)
    for row in query_reports:
        status_counts[row["status"]] += 1
    category_counts = {
        category: len(items) for category, items in sorted(products_by_category.items())
    }
    return {
        "schema": "product-qrel-readiness-v1",
        "truthBoundary": (
            "Missing authoritative facts are UNKNOWN. They must not be converted "
            "to relevance grades or treated as verified constraint violations."
        ),
        "reviewQueryCount": len(reviews),
        "catalogProductCount": len(products),
        "catalogCategoryCounts": category_counts,
        "statusCounts": dict(status_counts),
        "queries": query_reports,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--reviews",
        type=Path,
        default=AGENT_ROOT / "evaluation/product_qrel_review_batch_01.jsonl",
    )
    parser.add_argument(
        "--mysql-url",
        default=os.getenv(
            "PRODUCT_MYSQL_URL",
            "mysql://local_life:public-demo-secret-2-change-before-use@localhost:13306/local_life",
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    connection = _connect(args.mysql_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, category_l1, category_l2, category_l3,
                       snapshot_price_minor, price_status
                FROM product
                """
            )
            products = list(cursor.fetchall())
            cursor.execute(
                """
                SELECT product_id, attribute_key, value_type, normalized_text,
                       normalized_number, normalized_boolean
                FROM product_attribute
                """
            )
            attribute_rows = list(cursor.fetchall())
    finally:
        connection.close()

    report = build_report(_jsonl(args.reviews), products, attribute_rows)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    raise SystemExit(0 if report["statusCounts"].get("blocked", 0) == 0 else 3)


if __name__ == "__main__":
    main()
