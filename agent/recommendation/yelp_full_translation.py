from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TRANSLATION_SYSTEM_PROMPT = """你是专业的英译中译者。请把 Yelp 用户评论忠实翻译为简体中文。

必须遵守：
1. 不得总结、删减、扩写、润色成推荐文案，也不得添加原文没有的判断。
2. 保留原文的事实、否定、语气、情绪、讽刺、价格、日期、数字和段落关系。
3. 店名、品牌名、人名、地名和没有通行译名的产品名保留原文；其余自然翻译成中文。
4. WiFi、URL、型号、缩写等必要英文可以保留。
5. 如果原文用多种语言重复了同一篇评论，每个重复段落也都要翻译成中文，不得保留整段外语叙述。
6. 只输出指定 JSON，不要输出 Markdown、解释或前后缀。

输出格式：
{"translations":[{"id":"原始ID","textZh":"忠实的简体中文译文"}]}"""


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_translation_batches(
    reviews: list[dict[str, Any]],
    *,
    max_chars: int,
    max_items: int,
) -> list[list[dict[str, Any]]]:
    if max_chars <= 0 or max_items <= 0:
        raise ValueError("max_chars and max_items must be positive")

    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_chars = 0
    for review in reviews:
        text = str(review["content"])
        if current and (
            len(current) >= max_items
            or current_chars + len(text) > max_chars
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(review)
        current_chars += len(text)
    if current:
        batches.append(current)
    return batches


def translation_user_message(reviews: list[dict[str, Any]]) -> str:
    items = [
        {"id": str(review["reviewId"]), "text": str(review["content"])}
        for review in reviews
    ]
    return "请翻译以下评论：\n" + json.dumps(items, ensure_ascii=False)


def validate_translation(source: str, translated: str) -> str:
    normalized = translated.strip()
    if not normalized:
        raise ValueError("translation must not be blank")
    source_for_ratio = re.sub(r"https?://\S+", "", source).strip()
    translated_for_ratio = re.sub(r"https?://\S+", "", normalized).strip()
    chinese_count = len(re.findall(r"[\u4e00-\u9fff]", translated_for_ratio))
    latin_count = len(re.findall(r"[A-Za-z]", translated_for_ratio))
    if latin_count > 40 and latin_count / max(latin_count + chinese_count, 1) > 0.70:
        raise ValueError("translation contains too much untranslated Latin text")
    if chinese_count < max(2, int(len(translated_for_ratio) * 0.15)):
        raise ValueError("translation does not contain enough Chinese text")
    ratio = len(translated_for_ratio) / max(len(source_for_ratio), 1)
    bilingual_duplicate = re.search(
        r"(?:^|\n)\s*(?:\[English review below\]|(?:English|Spanish|Espa[nñ]ol)\s*:?)\s*(?:\n|$)",
        source,
        flags=re.IGNORECASE,
    )
    minimum_ratio = 0.10 if bilingual_duplicate else 0.15
    if ratio < minimum_ratio or ratio > 3.0:
        raise ValueError(f"suspicious translation length ratio: {ratio:.3f}")
    return normalized


def parse_translation_response(
    raw_content: str,
    reviews: list[dict[str, Any]],
) -> dict[str, str]:
    payload = json.loads(raw_content)
    items = payload.get("translations")
    if not isinstance(items, list):
        raise ValueError("response translations must be a list")

    expected = {
        str(review["reviewId"]): str(review["content"])
        for review in reviews
    }
    translations: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("translation item must be an object")
        review_id = str(item.get("id", "")).strip()
        if review_id not in expected or review_id in translations:
            raise ValueError(f"unexpected or duplicate translation id: {review_id}")
        translations[review_id] = validate_translation(
            expected[review_id],
            str(item.get("textZh", "")),
        )

    if set(translations) != set(expected):
        missing = sorted(set(expected) - set(translations))
        raise ValueError(f"response is missing translation ids: {missing}")
    return translations


def checkpoint_record(
    review: dict[str, Any],
    translated: str,
    *,
    model: str,
) -> dict[str, Any]:
    return {
        "reviewId": str(review["reviewId"]),
        "contentHash": content_hash(str(review["content"])),
        "contentZh": translated,
        "model": model,
        "translatedAt": datetime.now(UTC).isoformat(),
    }


def append_checkpoint(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    records: dict[str, dict[str, Any]] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1:
                break
            raise
        records[str(record["reviewId"])] = record
    return records


def cached_translation(
    review: dict[str, Any],
    checkpoints: dict[str, dict[str, Any]],
) -> str | None:
    record = checkpoints.get(str(review["reviewId"]))
    if record is None:
        return None
    if record.get("contentHash") != content_hash(str(review["content"])):
        return None
    return validate_translation(
        str(review["content"]),
        str(record.get("contentZh", "")),
    )


def _utf8_hex(value: str) -> str:
    return value.encode("utf-8").hex()


def build_translation_update_sql(translations: dict[str, str]) -> str:
    statements = [
        "SET NAMES utf8mb4;",
        "START TRANSACTION;",
    ]
    for review_id, translated in translations.items():
        statements.append(
            "UPDATE review SET "
            f"content_zh=CONVERT(0x{_utf8_hex(translated)} USING utf8mb4), "
            "translation_status='translated', updated_at=CURRENT_TIMESTAMP "
            f"WHERE id=(CONVERT(0x{_utf8_hex(review_id)} USING utf8mb4) "
            "COLLATE utf8mb4_unicode_ci) "
            "AND source='yelp' AND translation_status IN ('pending','failed');"
        )
    statements.extend(["COMMIT;", ""])
    return "\n".join(statements)
