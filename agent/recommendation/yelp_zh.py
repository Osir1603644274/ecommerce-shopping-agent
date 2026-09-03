"""Prepare a small Chinese translation sample for Yelp reviews.

The Yelp Open Dataset reviews are English.  In this project we keep the
original English text in ``review.content`` and write faithful Chinese
translations to ``review.content_zh`` for a small, reviewable sample.  Qdrant
can then embed Chinese text while MySQL still preserves the original review.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .yelp import DEFAULT_OUTPUT_PATH as DEFAULT_SAMPLE_PATH
from .yelp_content_zh_translations import YELP_CONTENT_ZH_TRANSLATIONS
from .yelp_sql import sql_string


DEFAULT_ZH_SQL_OUTPUT_PATH = Path(__file__).parents[2] / "db" / "yelp_content_zh_sample.sql"

WORK_KEYWORDS = (
    "laptop",
    "wifi",
    "wi-fi",
    "outlet",
    "plug",
    "power",
)
QUIET_KEYWORDS = ("quiet", "calm", "relax", "relaxing", "peaceful", "cozy")
NOISY_KEYWORDS = ("noisy", "loud", "crowded", "busy", "packed", "wait", "line")
COFFEE_KEYWORDS = ("coffee", "cafe", "espresso", "latte", "tea", "barista")
FOOD_KEYWORDS = ("food", "brunch", "breakfast", "pastry", "bakery", "dessert", "sandwich")
SERVICE_KEYWORDS = ("service", "staff", "server", "friendly", "rude")
PRICE_KEYWORDS = ("price", "cheap", "expensive", "worth", "value")
LOCATION_KEYWORDS = ("parking", "location", "street", "walk", "near")


def load_yelp_sample(sample_path: Path = DEFAULT_SAMPLE_PATH) -> dict[str, Any]:
    return json.loads(sample_path.read_text(encoding="utf-8"))


def build_shop_lookup(sample: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(shop["shopId"]): shop for shop in sample.get("shops", [])}


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _aspect_phrases(lower_text: str) -> list[str]:
    aspects: list[str] = []
    if _contains_any(lower_text, COFFEE_KEYWORDS):
        aspects.append("饮品")
    if _contains_any(lower_text, WORK_KEYWORDS):
        aspects.append("WiFi/插座线索")
    if _contains_any(lower_text, QUIET_KEYWORDS):
        aspects.append("舒适度线索")
    if _contains_any(lower_text, NOISY_KEYWORDS):
        aspects.append("人流/排队线索")
    if _contains_any(lower_text, FOOD_KEYWORDS):
        aspects.append("餐食/甜点")
    if _contains_any(lower_text, SERVICE_KEYWORDS):
        aspects.append("服务体验")
    if _contains_any(lower_text, PRICE_KEYWORDS):
        aspects.append("价格感受")
    if _contains_any(lower_text, LOCATION_KEYWORDS):
        aspects.append("位置/停车便利性")
    return aspects or ["服务和消费体验"]


def build_yelp_review_retrieval_text(
    review: dict[str, Any],
    shop: dict[str, Any] | None = None,
) -> str:
    """Create conservative Chinese fallback text from one Yelp review row."""
    lower_text = str(review.get("text", "")).casefold()
    shop_name = (shop or {}).get("name") or review.get("shopName") or "这家店"
    type_name = (shop or {}).get("typeName") or "本地生活商户"
    aspects = "、".join(_aspect_phrases(lower_text))

    sentences = [
        f"Yelp 评论备用线索：「{shop_name}」，分类：{type_name}，评论线索：{aspects}。",
    ]
    if _contains_any(lower_text, WORK_KEYWORDS):
        sentences.append("原文涉及 WiFi、插座或用电便利性。")
    if _contains_any(lower_text, QUIET_KEYWORDS):
        sentences.append("原文涉及店内舒适度。")
    if _contains_any(lower_text, NOISY_KEYWORDS):
        sentences.append("原文涉及人流、排队或等待。")
    if _contains_any(lower_text, COFFEE_KEYWORDS):
        sentences.append("可作为饮品相关检索证据。")
    if _contains_any(lower_text, FOOD_KEYWORDS):
        sentences.append("如果用户关心早餐、甜点、简餐或餐食品质，也可以参考这条评论。")
    if _contains_any(lower_text, SERVICE_KEYWORDS):
        sentences.append("评论涉及店员态度或服务体验。")

    stars = review.get("stars")
    if stars is not None:
        sentences.append(f"原始 Yelp 评分约为 {float(stars):g} 星。")
    return "".join(sentences)


summarize_yelp_review_to_zh = build_yelp_review_retrieval_text


def _selection_score(review: dict[str, Any], shop: dict[str, Any] | None) -> int:
    lower_text = str(review.get("text", "")).casefold()
    type_name = str((shop or {}).get("typeName", ""))
    score = 0
    if type_name == "咖啡":
        score += 10
    if _contains_any(lower_text, WORK_KEYWORDS):
        score += 8
    if _contains_any(lower_text, COFFEE_KEYWORDS):
        score += 6
    if _contains_any(lower_text, QUIET_KEYWORDS):
        score += 4
    if _contains_any(lower_text, NOISY_KEYWORDS):
        score += 3
    if _contains_any(lower_text, FOOD_KEYWORDS):
        score += 2
    return score


def select_yelp_reviews_for_zh(
    sample: dict[str, Any],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    if limit <= 0:
        raise ValueError("limit must be positive")

    shop_lookup = build_shop_lookup(sample)
    reviews = sorted(
        sample.get("reviews", []),
        key=lambda review: (
            -_selection_score(review, shop_lookup.get(int(review["shopId"]))),
            str(review["id"]),
        ),
    )
    return reviews[:limit]


def select_yelp_reviews_with_translations(
    sample: dict[str, Any],
    translations: dict[str, str],
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Select review rows whose ids have prepared Chinese translations."""
    if limit <= 0:
        raise ValueError("limit must be positive")

    review_lookup = {str(review["id"]): review for review in sample.get("reviews", [])}
    selected_ids = list(translations.keys())[:limit]
    missing_ids = [review_id for review_id in selected_ids if review_id not in review_lookup]
    if missing_ids:
        raise ValueError(f"missing translated Yelp reviews in sample: {missing_ids}")
    return [review_lookup[review_id] for review_id in selected_ids]


def generate_yelp_content_zh_sql(
    sample: dict[str, Any],
    *,
    limit: int = 50,
    translations: dict[str, str] | None = None,
) -> str:
    translation_lookup = translations or YELP_CONTENT_ZH_TRANSLATIONS
    selected_reviews = select_yelp_reviews_with_translations(
        sample,
        translation_lookup,
        limit=limit,
    )
    statements = [
        "-- Generated faithful Chinese translations for a small Yelp review sample.",
        "-- MySQL keeps original English in review.content; Qdrant embeds content_zh when present.",
        "SET NAMES utf8mb4;",
        """
UPDATE review
SET source = 'yelp',
    language = 'en',
    translation_status = CASE
        WHEN content_zh IS NULL OR content_zh = '' THEN 'pending'
        ELSE translation_status
    END
WHERE id LIKE 'yelp-%';
""".strip(),
    ]
    for review in selected_reviews:
        content_zh = translation_lookup[str(review["id"])]
        statements.append(
            "UPDATE review\n"
            f"SET content_zh = {sql_string(content_zh)},\n"
            "    source = 'yelp',\n"
            "    language = 'en',\n"
            "    translation_status = 'translated'\n"
            f"WHERE id = {sql_string(review['id'])};"
        )
    return "\n\n".join(statements).rstrip() + "\n"


def save_yelp_content_zh_sql(
    sample_path: Path = DEFAULT_SAMPLE_PATH,
    output_path: Path = DEFAULT_ZH_SQL_OUTPUT_PATH,
    *,
    limit: int = 50,
    translations: dict[str, str] | None = None,
) -> str:
    sample = load_yelp_sample(sample_path)
    sql = generate_yelp_content_zh_sql(sample, limit=limit, translations=translations)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(sql, encoding="utf-8")
    return sql
