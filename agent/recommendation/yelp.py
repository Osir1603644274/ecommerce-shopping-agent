"""Convert a small Yelp Open Dataset slice into local-life recommendation data.

The raw Yelp files are not committed to this repository. Put extracted JSONL
files under:

    agent/recommendation/data/raw/yelp/

This module does not write to MySQL directly. It creates a processed JSON file
that can later be imported into ``shop``, ``review`` and ``user_behavior``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any


DEFAULT_RAW_DIR = Path(__file__).parent / "data" / "raw" / "yelp"
DEFAULT_OUTPUT_PATH = Path(__file__).parent / "data" / "processed" / "yelp_local_life_sample.json"
DEFAULT_START_SHOP_ID = 100_001
DEMO_USER_ID = "demo-user-1"

SHOP_TYPE_RULES = [
    {
        "typeId": 2,
        "typeName": "咖啡",
        "keywords": ("coffee", "cafes", "cafe", "tea"),
    },
    {
        "typeId": 3,
        "typeName": "电影",
        "keywords": ("cinema", "movie theater", "movies"),
    },
    {
        "typeId": 4,
        "typeName": "酒店",
        "keywords": ("hotels", "hotel", "bed & breakfast", "travel services"),
    },
    {
        "typeId": 5,
        "typeName": "健身",
        "keywords": ("gyms", "fitness", "yoga", "pilates"),
    },
    {
        "typeId": 1,
        "typeName": "美食",
        "keywords": ("restaurants", "food", "breakfast", "brunch", "pizza", "chinese", "burgers"),
    },
]

PRICE_RANGE_TO_AVG_PRICE = {
    "1": 50,
    "2": 100,
    "3": 180,
    "4": 300,
}


class YelpSampleError(ValueError):
    """Raised when the Yelp sample cannot be built from the provided data."""


def iter_json_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from a Yelp JSON Lines file."""
    if not path.exists():
        raise FileNotFoundError(f"Yelp raw file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise YelpSampleError(f"Invalid JSON at {path}:{line_number}") from exc
            if not isinstance(item, dict):
                raise YelpSampleError(f"Expected JSON object at {path}:{line_number}")
            yield item


def normalize_categories(categories: Any) -> str:
    """Return Yelp categories as one lowercase searchable string."""
    if categories is None:
        return ""
    if isinstance(categories, list):
        return ", ".join(str(item) for item in categories).lower()
    return str(categories).lower()


def match_shop_type(categories: Any) -> dict[str, Any] | None:
    """Map Yelp categories to the project's existing shop_type ids."""
    normalized = normalize_categories(categories)
    if not normalized:
        return None

    for rule in SHOP_TYPE_RULES:
        if any(keyword in normalized for keyword in rule["keywords"]):
            return {
                "typeId": rule["typeId"],
                "typeName": rule["typeName"],
            }
    return None


def build_yelp_sample(
    businesses: Iterable[dict[str, Any]],
    reviews: Iterable[dict[str, Any]],
    *,
    city: str | None = None,
    max_businesses: int = 300,
    max_reviews_per_shop: int = 30,
    start_shop_id: int = DEFAULT_START_SHOP_ID,
    min_demo_user_behaviors: int = 10,
) -> dict[str, Any]:
    """Build a small local-life sample from Yelp business and review rows."""
    if max_businesses <= 0:
        raise ValueError("max_businesses must be positive")
    if max_reviews_per_shop <= 0:
        raise ValueError("max_reviews_per_shop must be positive")
    if min_demo_user_behaviors <= 0:
        raise ValueError("min_demo_user_behaviors must be positive")

    selected_city = city.casefold() if city else None
    shops: list[dict[str, Any]] = []
    business_id_to_shop: dict[str, dict[str, Any]] = {}

    for business in businesses:
        if selected_city and str(business.get("city", "")).casefold() != selected_city:
            continue
        if business.get("is_open") == 0:
            continue

        shop_type = match_shop_type(business.get("categories"))
        if shop_type is None:
            continue

        business_id = str(business.get("business_id", "")).strip()
        name = str(business.get("name", "")).strip()
        if not business_id or not name:
            continue

        shop_id = start_shop_id + len(shops)
        shop = {
            "shopId": shop_id,
            "sourceBusinessId": business_id,
            "name": name,
            "typeId": shop_type["typeId"],
            "typeName": shop_type["typeName"],
            "address": build_address(business),
            "avgPrice": estimate_avg_price(business.get("attributes")),
            "phone": str(business.get("phone") or ""),
            "longitude": float(business.get("longitude", 0.0)),
            "latitude": float(business.get("latitude", 0.0)),
        }
        shops.append(shop)
        business_id_to_shop[business_id] = shop
        if len(shops) >= max_businesses:
            break

    review_rows: list[dict[str, Any]] = []
    raw_behavior_rows: list[dict[str, Any]] = []
    review_count_by_shop: Counter[int] = Counter()

    for review in reviews:
        business_id = str(review.get("business_id", "")).strip()
        shop = business_id_to_shop.get(business_id)
        if shop is None:
            continue
        shop_id = shop["shopId"]
        if review_count_by_shop[shop_id] >= max_reviews_per_shop:
            continue

        text = str(review.get("text", "")).strip()
        raw_user_id = str(review.get("user_id", "")).strip()
        raw_review_id = str(review.get("review_id", "")).strip()
        if not text or not raw_user_id or not raw_review_id:
            continue

        stars = float(review.get("stars", 0.0))
        occurred_at = str(review.get("date", "")).strip()
        review_id = f"yelp-{raw_review_id}"
        review_rows.append(
            {
                "id": review_id,
                "shopId": shop_id,
                "shopName": shop["name"],
                "text": text,
                "source": "yelp",
                "language": "en",
                "contentZh": None,
                "translationStatus": "pending",
                "tags": ["yelp", shop["typeName"], f"stars:{stars:g}"],
                "sourceReviewId": raw_review_id,
                "sourceUserId": raw_user_id,
                "stars": stars,
                "createdAt": occurred_at,
            }
        )
        raw_behavior_rows.append(
            {
                "id": f"yelp-behavior-{raw_review_id}",
                "sourceUserId": raw_user_id,
                "shopId": shop_id,
                "behaviorType": "rating",
                "score": stars,
                "source": "yelp_review",
                "occurredAt": occurred_at,
                "sourceReviewId": raw_review_id,
            }
        )
        review_count_by_shop[shop_id] += 1

    demo_users = build_demo_users(raw_behavior_rows, min_demo_user_behaviors=min_demo_user_behaviors)
    demo_source_user_id = demo_users[0]["sourceUserId"] if demo_users else None
    user_behaviors = [
        {
            **item,
            "userId": (
                DEMO_USER_ID
                if item["sourceUserId"] == demo_source_user_id
                else project_user_id(item["sourceUserId"])
            ),
        }
        for item in raw_behavior_rows
    ]

    used_type_ids = sorted({shop["typeId"] for shop in shops})
    return {
        "metadata": {
            "sourceDataset": "Yelp Open Dataset",
            "city": city,
            "startShopId": start_shop_id,
            "businessCount": len(shops),
            "reviewCount": len(review_rows),
            "userBehaviorCount": len(user_behaviors),
            "demoUserId": DEMO_USER_ID if demo_users else None,
        },
        "shopTypes": [
            {"id": rule["typeId"], "name": rule["typeName"]}
            for rule in SHOP_TYPE_RULES
            if rule["typeId"] in used_type_ids
        ],
        "shops": shops,
        "reviews": review_rows,
        "userBehaviors": user_behaviors,
        "demoUsers": demo_users,
    }


def build_address(business: dict[str, Any]) -> str:
    parts = [
        str(business.get("address") or "").strip(),
        str(business.get("city") or "").strip(),
        str(business.get("state") or "").strip(),
    ]
    return ", ".join(part for part in parts if part)


def estimate_avg_price(attributes: Any) -> int:
    if not isinstance(attributes, dict):
        return 0
    price_range = attributes.get("RestaurantsPriceRange2")
    return PRICE_RANGE_TO_AVG_PRICE.get(str(price_range), 0)


def build_demo_users(
    behavior_rows: list[dict[str, Any]],
    *,
    min_demo_user_behaviors: int,
) -> list[dict[str, Any]]:
    counts = Counter(item["sourceUserId"] for item in behavior_rows)
    if not counts:
        return []

    source_user_id, behavior_count = sorted(
        counts.items(),
        key=lambda pair: (-pair[1], pair[0]),
    )[0]
    if behavior_count < min_demo_user_behaviors:
        return []
    return [
        {
            "userId": DEMO_USER_ID,
            "sourceUserId": source_user_id,
            "behaviorCount": behavior_count,
        }
    ]


def project_user_id(source_user_id: str) -> str:
    """Map non-demo Yelp user ids into project user ids."""
    return f"yelp-user-{source_user_id}"[:64]


def save_yelp_sample(sample: dict[str, Any], output_path: Path = DEFAULT_OUTPUT_PATH) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(sample, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def convert_yelp_sample(
    raw_dir: Path = DEFAULT_RAW_DIR,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    *,
    city: str | None = None,
    max_businesses: int = 300,
    max_reviews_per_shop: int = 30,
    min_demo_user_behaviors: int = 10,
) -> dict[str, Any]:
    """Read Yelp raw JSONL files and save a processed local-life sample."""
    business_path = raw_dir / "yelp_academic_dataset_business.json"
    review_path = raw_dir / "yelp_academic_dataset_review.json"
    sample = build_yelp_sample(
        iter_json_lines(business_path),
        iter_json_lines(review_path),
        city=city,
        max_businesses=max_businesses,
        max_reviews_per_shop=max_reviews_per_shop,
        min_demo_user_behaviors=min_demo_user_behaviors,
    )
    save_yelp_sample(sample, output_path)
    return sample
