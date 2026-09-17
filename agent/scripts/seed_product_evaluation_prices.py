"""Seed deterministic development prices for the product evaluation catalog.

The KuaiSearch snapshot is used as local development data.  Many rows do not
carry a structured price, so qrel and hard-constraint tests need a stable,
explicitly synthetic value.  This command keeps any extracted price and only
fills missing prices for primary phones, laptops, and headphones.

The command is a dry-run unless ``--apply`` is provided.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter
from urllib.parse import urlparse


PRICE_POLICY_VERSION = "product-eval-price-v1"
CATEGORY_PRICE_RANGES = {
    "phone": (100_000, 600_000),
    "laptop": (300_000, 1_000_000),
    "headphones": (10_000, 200_000),
}
PRIMARY_CATEGORY_L3 = {
    "phone": {"手机设备", "二手手机"},
    "laptop": {"笔记本电脑", "二手笔记本电脑"},
    "headphones": {"耳机/麦克风"},
}


def classify_primary(category_l3: str, title: str) -> str | None:
    for category, allowed in PRIMARY_CATEGORY_L3.items():
        if category_l3 not in allowed:
            continue
        if category == "headphones" and not any(
            keyword in title for keyword in ("耳机", "耳麦")
        ):
            return None
        return category
    return None


def deterministic_price_minor(product_id: int, category: str) -> int:
    minimum, maximum = CATEGORY_PRICE_RANGES[category]
    digest = hashlib.sha256(
        f"{PRICE_POLICY_VERSION}:{category}:{product_id}".encode("utf-8")
    ).digest()
    offset = int.from_bytes(digest[:8], "big") % (maximum - minimum + 1)
    return minimum + offset


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
        autocommit=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mysql-url",
        default=os.getenv(
            "PRODUCT_MYSQL_URL",
            "mysql://local_life:public-demo-secret-2-change-before-use@localhost:13306/local_life",
        ),
    )
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    connection = _connect(args.mysql_url)
    updates: list[tuple[int, str, int]] = []
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, title, category_l3, snapshot_price_minor
                FROM product
                WHERE source = 'kuaisearch'
                ORDER BY id
                """
            )
            for product in cursor.fetchall():
                category = classify_primary(
                    str(product["category_l3"]), str(product["title"])
                )
                if category is None or product["snapshot_price_minor"] is not None:
                    continue
                product_id = int(product["id"])
                updates.append(
                    (product_id, category, deterministic_price_minor(product_id, category))
                )

            if args.apply:
                cursor.executemany(
                    """
                    UPDATE product
                    SET snapshot_price_minor=%s,
                        currency='CNY',
                        price_status='verified',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s AND snapshot_price_minor IS NULL
                    """,
                    [(price, product_id) for product_id, _category, price in updates],
                )
                connection.commit()
            else:
                connection.rollback()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    counts = Counter(category for _id, category, _price in updates)
    print(json.dumps({
        "schema": "product-evaluation-price-seed-v1",
        "policyVersion": PRICE_POLICY_VERSION,
        "mode": "applied" if args.apply else "dry_run",
        "updatedCount": len(updates) if args.apply else 0,
        "candidateCount": len(updates),
        "byCategory": dict(sorted(counts.items())),
        "priceRangesMinor": CATEGORY_PRICE_RANGES,
        "note": "Synthetic local-development prices; no external price claim.",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
