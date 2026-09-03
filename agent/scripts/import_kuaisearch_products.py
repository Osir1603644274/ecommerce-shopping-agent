"""Stream a pinned KuaiSearch snapshot into the authoritative MySQL catalog.

The script never downloads the full repository. Hugging Face Datasets iterable mode
opens remote shards lazily. Generated/raw data stays outside Git.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from urllib.parse import urlparse

DATASET_ID = "benchen4395/KuaiSearch"
SOURCE_URL = "https://huggingface.co/datasets/benchen4395/KuaiSearch"
LICENSE = "MIT"
DEFAULT_REVISION = "09807c7"
CATEGORY_TARGETS = {
    "phone": ("手机", "智能手机"),
    "laptop": ("笔记本", "笔记本电脑", "游戏本", "轻薄本"),
    "headphones": ("耳机", "耳麦", "蓝牙耳机"),
}
REQUIRED_FIELDS = (
    "item_id", "item_title", "brand_name", "seller_name",
    "category_level1_name", "category_level2_name", "category_level3_name",
)
UNKNOWN_VALUES = {"", "unknown", "未知", "其他/other", "null", "none"}
PRICE_PATTERN = re.compile(
    r"(?:[¥￥]\s*(?P<prefix>\d+(?:\.\d{1,2})?)|"
    r"(?P<suffix>\d+(?:\.\d{1,2})?)\s*元)"
)


def classify(row: dict[str, Any]) -> str | None:
    for field in (
        "category_level3_name", "category_level2_name", "category_level1_name"
    ):
        category_text = str(row.get(field) or "")
        for category, keywords in CATEGORY_TARGETS.items():
            if any(keyword in category_text for keyword in keywords):
                return category
    return None


def is_qualified(row: dict[str, Any]) -> bool:
    for field in REQUIRED_FIELDS:
        value = str(row.get(field) or "").strip()
        if value.lower() in UNKNOWN_VALUES:
            return False
    return classify(row) is not None


def verified_title_price(title: str) -> tuple[int | None, str]:
    match = PRICE_PATTERN.search(title)
    if not match:
        return None, "unverified"
    amount = Decimal(match.group("prefix") or match.group("suffix"))
    if amount <= 0:
        return None, "unverified"
    return int(amount * 100), "verified"


def extract_attributes(title: str, category: str) -> list[dict[str, Any]]:
    rules: list[tuple[str, str, str, float]] = []
    if category in {"phone", "laptop"}:
        rules.extend([
            ("memory_gb", r"(?<!\d)(\d{1,3})\s*GB\s*(?:运行|运存|内存|RAM)", "GB", 1.0),
            ("storage_gb", r"(?<!\d)(\d{2,4})\s*GB\s*(?:存储|硬盘|SSD|容量)", "GB", 1.0),
        ])
    if category == "phone":
        rules.append(("battery_mah", r"(?<!\d)(\d{4,5})\s*mAh", "mAh", 1.0))
    if category == "laptop":
        rules.extend([
            ("weight_kg", r"(?<!\d)(\d(?:\.\d{1,2})?)\s*kg", "kg", 1.0),
            ("screen_inch", r"(?<!\d)(\d{2}(?:\.\d)?)\s*(?:英寸|寸)", "inch", 1.0),
        ])
    if category == "headphones":
        rules.extend([
            ("battery_hours", r"(?<!\d)(\d{1,3})\s*(?:小时|h续航)", "hour", 1.0),
            ("weight_g", r"(?<!\d)(\d{1,3})\s*g(?:\s|$)", "g", 1.0),
        ])
    attributes = []
    for key, pattern, unit, confidence in rules:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            attributes.append({
                "key": key, "value_type": "number", "raw_value": match.group(0),
                "normalized_number": float(match.group(1)), "normalized_boolean": None,
                "normalized_text": None, "unit": unit, "confidence": confidence,
            })
    boolean_rules = []
    if category == "phone":
        boolean_rules.append(("supports_5g", r"(?<![A-Za-z0-9])5G(?![A-Za-z0-9])"))
    if category == "headphones":
        boolean_rules.extend([
            ("noise_cancelling", r"主动降噪|ANC"),
            ("wireless", r"无线|蓝牙"),
        ])
    for key, pattern in boolean_rules:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            attributes.append({
                "key": key, "value_type": "boolean", "raw_value": match.group(0),
                "normalized_number": None, "normalized_boolean": True,
                "normalized_text": None, "unit": "bool", "confidence": 1.0,
            })
    return attributes


def choose_rows(
    rows: Iterable[dict[str, Any]], target_per_category: int, max_scanned: int
) -> dict[str, list[dict[str, Any]]]:
    selected: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for scanned, row in enumerate(rows, 1):
        if scanned > max_scanned:
            break
        if not is_qualified(row):
            continue
        category = classify(row)
        digest = hashlib.sha256(str(row["item_id"]).encode()).hexdigest()
        bucket = selected[category]
        bucket.append((digest, row))
        bucket.sort(key=lambda item: item[0])
        del bucket[target_per_category:]
        if all(len(selected[key]) >= target_per_category for key in CATEGORY_TARGETS):
            break
    return {key: [item[1] for item in selected[key]] for key in CATEGORY_TARGETS}


def connect_mysql(mysql_url: str):
    import pymysql

    parsed = urlparse(mysql_url)
    return pymysql.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 3306,
        user=parsed.username,
        password=parsed.password,
        database=parsed.path.lstrip("/"),
        charset="utf8mb4",
        autocommit=False,
    )


def import_rows(connection, rows_by_category, revision: str) -> None:
    imported_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with connection.cursor() as cursor:
        for category, rows in rows_by_category.items():
            for row in rows:
                product_id = int(row["item_id"])
                title = str(row["item_title"]).strip()
                price_minor, price_status = verified_title_price(title)
                cursor.execute(
                    """
                    INSERT INTO product (
                      id, source, source_item_id, title, brand, seller,
                      category_l1, category_l2, category_l3,
                      snapshot_price_minor, currency, price_status, attribute_text,
                      data_nature, dataset_revision, source_license, provenance_url, imported_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      title=VALUES(title), brand=VALUES(brand), seller=VALUES(seller),
                      category_l1=VALUES(category_l1), category_l2=VALUES(category_l2),
                      category_l3=VALUES(category_l3),
                      snapshot_price_minor=VALUES(snapshot_price_minor),
                      currency=VALUES(currency), price_status=VALUES(price_status),
                      attribute_text=VALUES(attribute_text),
                      dataset_revision=VALUES(dataset_revision), imported_at=VALUES(imported_at),
                      updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        product_id, "kuaisearch", str(row["item_id"]), title,
                        str(row["brand_name"]).strip(), str(row["seller_name"]).strip(),
                        str(row["category_level1_name"]).strip(),
                        str(row["category_level2_name"]).strip(),
                        str(row["category_level3_name"]).strip(),
                        price_minor, "CNY" if price_minor is not None else None, price_status,
                        title, "historical_dataset_snapshot", revision, LICENSE,
                        SOURCE_URL, imported_at,
                    ),
                )
                cursor.execute(
                    "DELETE FROM product_attribute WHERE product_id=%s", (product_id,)
                )
                for attribute in extract_attributes(title, category):
                    cursor.execute(
                        """
                        INSERT INTO product_attribute (
                          product_id, attribute_key, value_type, raw_value,
                          normalized_text, normalized_number, normalized_boolean,
                          unit, evidence_field, extraction_method, confidence
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'title','deterministic_regex',%s)
                        """,
                        (
                            product_id, attribute["key"], attribute["value_type"],
                            attribute["raw_value"], attribute["normalized_text"],
                            attribute["normalized_number"], attribute["normalized_boolean"],
                            attribute["unit"], attribute["confidence"],
                        ),
                    )
    connection.commit()


def invalidate_product_cache(redis_url: str) -> int:
    import redis

    client = redis.Redis.from_url(redis_url)
    keys = list(client.scan_iter(match="local-life:product:detail:*", count=500))
    return int(client.delete(*keys)) if keys else 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revision", default=DEFAULT_REVISION)
    parser.add_argument("--mysql-url", default=os.getenv(
        "PRODUCT_MYSQL_URL", "mysql://local_life:local_life_password@localhost:13306/local_life"
    ))
    parser.add_argument("--target-per-category", type=int, default=500)
    parser.add_argument("--minimum-per-category", type=int, default=200)
    parser.add_argument("--max-scanned", type=int, default=18_600_000)
    parser.add_argument(
        "--redis-url", default=os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )
    args = parser.parse_args()
    if args.target_per_category > 10_000:
        parser.error("target-per-category cannot exceed the local experiment cap of 10,000")
    from datasets import load_dataset

    rows = load_dataset(
        DATASET_ID, split="train", revision=args.revision, streaming=True
    )
    selected = choose_rows(rows, args.target_per_category, args.max_scanned)
    counts = {category: len(items) for category, items in selected.items()}
    short = {category: count for category, count in counts.items()
             if count < args.minimum_per_category}
    if short:
        raise SystemExit(f"import aborted; insufficient qualified records: {short}")
    connection = connect_mysql(args.mysql_url)
    try:
        import_rows(connection, selected, args.revision)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    invalidated = invalidate_product_cache(args.redis_url)
    print({"dataset": DATASET_ID, "revision": args.revision, "license": LICENSE,
           "sourceUrl": SOURCE_URL, "counts": counts,
           "invalidatedProductCacheKeys": invalidated})


if __name__ == "__main__":
    main()
