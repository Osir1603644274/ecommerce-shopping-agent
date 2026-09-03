"""Generate MySQL import SQL from a processed Yelp local-life sample."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_SAMPLE_PATH = Path(__file__).parent / "data" / "processed" / "yelp_local_life_sample.json"
DEFAULT_SQL_OUTPUT_PATH = Path(__file__).parents[2] / "db" / "yelp_sample.sql"


def load_yelp_sample(sample_path: Path = DEFAULT_SAMPLE_PATH) -> dict[str, Any]:
    if not sample_path.exists():
        raise FileNotFoundError(
            f"Processed Yelp sample not found: {sample_path}. "
            "Run python -m scripts.prepare_yelp_sample first."
        )
    return json.loads(sample_path.read_text(encoding="utf-8"))


def sql_string(value: Any) -> str:
    """Return a MySQL string literal, or NULL for missing values."""
    if value is None:
        return "NULL"
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


def sql_number(value: Any) -> str:
    if value is None or value == "":
        return "NULL"
    return str(value)


def sql_timestamp(value: Any) -> str:
    if value is None or str(value).strip() == "":
        return "CURRENT_TIMESTAMP"
    text = str(value).strip()
    if len(text) == 10:
        text = f"{text} 00:00:00"
    return sql_string(text)


def tags_to_sql(tags: Any) -> str:
    if tags is None:
        tags = []
    if not isinstance(tags, list):
        tags = [str(tags)]
    return sql_string(json.dumps(tags, ensure_ascii=False, separators=(",", ":")))


def render_values(rows: list[str]) -> str:
    if not rows:
        return ""
    return ",\n".join(f"    {row}" for row in rows)


def generate_shop_sql(shops: list[dict[str, Any]]) -> str:
    if not shops:
        return ""
    rows = []
    for shop in shops:
        rows.append(
            "("
            f"{sql_number(shop['shopId'])}, "
            f"{sql_string(shop['name'])}, "
            f"{sql_number(shop['typeId'])}, "
            f"{sql_string(shop.get('address', ''))}, "
            f"{sql_number(shop.get('avgPrice', 0))}, "
            f"{sql_string(shop.get('phone', ''))}, "
            f"{sql_number(shop.get('longitude', 0.0))}, "
            f"{sql_number(shop.get('latitude', 0.0))}, "
            f"{sql_string(shop.get('coordinateSystem', 'WGS-84'))}, "
            f"{sql_string(shop.get('district'))}, "
            f"{sql_string(shop.get('anchorPlaceId'))}, "
            f"{sql_string(shop.get('anchorPlaceName'))}, "
            f"{sql_string(shop.get('dataNature', 'source_snapshot'))}, "
            f"{sql_string('yelp')}, "
            f"{sql_string(shop.get('sourceBusinessId'))}, "
            f"{sql_string(shop.get('originalName', shop.get('name')))}, "
            f"{sql_string(shop.get('originalAddress', shop.get('address')))}, "
            f"{sql_number(shop.get('originalLongitude', shop.get('longitude')))}, "
            f"{sql_number(shop.get('originalLatitude', shop.get('latitude')))}, "
            f"{sql_string(shop.get('localizationVersion', 'beijing-demo-v1'))}"
            ")"
        )
    return (
        "INSERT INTO shop (\n"
        "    id, name, type_id, address, avg_price, phone, longitude, latitude,\n"
        "    coordinate_system, district, anchor_place_id, anchor_place_name,\n"
        "    data_nature, source, source_entity_id, original_name, original_address,\n"
        "    original_longitude, original_latitude, localization_version\n"
        ")\n"
        "VALUES\n"
        f"{render_values(rows)}\n"
        "ON DUPLICATE KEY UPDATE\n"
        "    name = VALUES(name),\n"
        "    type_id = VALUES(type_id),\n"
        "    address = VALUES(address),\n"
        "    avg_price = VALUES(avg_price),\n"
        "    phone = VALUES(phone),\n"
        "    longitude = VALUES(longitude),\n"
        "    latitude = VALUES(latitude),\n"
        "    coordinate_system = VALUES(coordinate_system),\n"
        "    district = VALUES(district),\n"
        "    anchor_place_id = VALUES(anchor_place_id),\n"
        "    anchor_place_name = VALUES(anchor_place_name),\n"
        "    data_nature = VALUES(data_nature),\n"
        "    source = VALUES(source),\n"
        "    source_entity_id = VALUES(source_entity_id),\n"
        "    original_name = VALUES(original_name),\n"
        "    original_address = VALUES(original_address),\n"
        "    original_longitude = VALUES(original_longitude),\n"
        "    original_latitude = VALUES(original_latitude),\n"
        "    localization_version = VALUES(localization_version);"
    )


def generate_review_sql(reviews: list[dict[str, Any]]) -> str:
    if not reviews:
        return ""
    rows = []
    for review in reviews:
        rows.append(
            "("
            f"{sql_string(review['id'])}, "
            f"{sql_number(review['shopId'])}, "
            f"{sql_string(review['text'])}, "
            f"{tags_to_sql(review.get('tags', []))}, "
            f"{sql_string(review.get('source', 'yelp'))}, "
            f"{sql_string(review.get('language', 'en'))}, "
            f"{sql_string(review.get('contentZh'))}, "
            f"{sql_string(review.get('translationStatus', 'pending'))}, "
            f"{sql_string(review.get('sourceReviewId'))}, "
            f"{sql_string(review.get('sourceUserId'))}, "
            f"{sql_number(review.get('stars'))}, "
            f"{sql_string(review.get('sourceShopName'))}, "
            f"{sql_string(review.get('reviewEvidenceScope'))}, "
            f"{sql_timestamp(review.get('createdAt'))}"
            ")"
        )
    return (
        "INSERT INTO review (\n"
        "    id, shop_id, content, tags, source, language, content_zh, translation_status,\n"
        "    source_review_id, source_user_id, stars, source_shop_name, evidence_scope, created_at\n"
        ")\n"
        "VALUES\n"
        f"{render_values(rows)}\n"
        "ON DUPLICATE KEY UPDATE\n"
        "    shop_id = VALUES(shop_id),\n"
        "    content = VALUES(content),\n"
        "    tags = VALUES(tags),\n"
        "    source = VALUES(source),\n"
        "    language = VALUES(language),\n"
        "    content_zh = COALESCE(VALUES(content_zh), review.content_zh),\n"
        "    source_review_id = VALUES(source_review_id),\n"
        "    source_user_id = VALUES(source_user_id),\n"
        "    stars = VALUES(stars),\n"
        "    source_shop_name = VALUES(source_shop_name),\n"
        "    evidence_scope = VALUES(evidence_scope),\n"
        "    translation_status = CASE\n"
        "        WHEN VALUES(content_zh) IS NOT NULL AND VALUES(content_zh) <> '' THEN VALUES(translation_status)\n"
        "        ELSE review.translation_status\n"
        "    END,\n"
        "    updated_at = CURRENT_TIMESTAMP;"
    )


def generate_user_behavior_sql(user_behaviors: list[dict[str, Any]]) -> str:
    if not user_behaviors:
        return ""
    rows = []
    for behavior in user_behaviors:
        rows.append(
            "("
            f"{sql_string(behavior['id'])}, "
            f"{sql_string(behavior['userId'])}, "
            f"{sql_number(behavior['shopId'])}, "
            f"{sql_string(behavior['behaviorType'])}, "
            f"{sql_number(behavior.get('score'))}, "
            f"{sql_string(behavior['source'])}, "
            f"{sql_string(behavior.get('sourceUserId'))}, "
            f"{sql_string(behavior.get('sourceReviewId'))}, "
            f"{sql_timestamp(behavior.get('occurredAt'))}"
            ")"
        )
    return (
        "INSERT INTO user_behavior (\n"
        "    id, user_id, shop_id, behavior_type, score, source,\n"
        "    source_user_id, source_review_id, occurred_at\n"
        ")\n"
        "VALUES\n"
        f"{render_values(rows)}\n"
        "ON DUPLICATE KEY UPDATE\n"
        "    user_id = VALUES(user_id),\n"
        "    shop_id = VALUES(shop_id),\n"
        "    behavior_type = VALUES(behavior_type),\n"
        "    score = VALUES(score),\n"
        "    source = VALUES(source),\n"
        "    source_user_id = VALUES(source_user_id),\n"
        "    source_review_id = VALUES(source_review_id),\n"
        "    occurred_at = VALUES(occurred_at);"
    )


def generate_yelp_import_sql(sample: dict[str, Any]) -> str:
    sections = [
        "-- Generated from a processed Yelp local-life sample JSON.",
        "-- Import order matters: shop -> review -> user_behavior.",
        "SET NAMES utf8mb4;",
        generate_shop_sql(sample.get("shops", [])),
        generate_review_sql(sample.get("reviews", [])),
        generate_user_behavior_sql(sample.get("userBehaviors", [])),
    ]
    return "\n\n".join(section for section in sections if section).rstrip() + "\n"


def save_yelp_import_sql(
    sample_path: Path = DEFAULT_SAMPLE_PATH,
    output_path: Path = DEFAULT_SQL_OUTPUT_PATH,
) -> str:
    sample = load_yelp_sample(sample_path)
    sql = generate_yelp_import_sql(sample)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(sql, encoding="utf-8")
    return sql
