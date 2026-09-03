"""Build merchant_docs for the localized Beijing demo projection.

The output is a compact, project-specific knowledge source. It only includes
businesses already selected into yelp_local_life_sample.json. Display identity
and geography come from the deterministic Beijing projection; Yelp attributes,
hours, ratings and interactions remain attached as source-dataset snapshots.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any


AGENT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = AGENT_ROOT / "recommendation" / "data" / "processed" / "yelp_local_life_sample.json"
RAW_BUSINESS_PATH = (
    AGENT_ROOT
    / "recommendation"
    / "data"
    / "raw"
    / "yelp"
    / "yelp_academic_dataset_business.json"
)
OUTPUT_PATH = AGENT_ROOT / "knowledge_data" / "raw" / "merchant_docs" / "yelp_merchant_docs.json"


SELECTED_ATTRIBUTE_KEYS = [
    "WiFi",
    "OutdoorSeating",
    "RestaurantsTakeOut",
    "RestaurantsDelivery",
    "RestaurantsReservations",
    "RestaurantsGoodForGroups",
    "GoodForKids",
    "BikeParking",
    "BusinessAcceptsCreditCards",
    "RestaurantsPriceRange2",
    "NoiseLevel",
    "Alcohol",
    "HasTV",
    "DogsAllowed",
    "WheelchairAccessible",
    "HappyHour",
    "Caters",
    "ByAppointmentOnly",
    "BusinessParking",
    "Ambience",
    "GoodForMeal",
]


ATTRIBUTE_LABELS = {
    "WiFi": "WiFi",
    "OutdoorSeating": "户外座位",
    "RestaurantsTakeOut": "外带",
    "RestaurantsDelivery": "配送",
    "RestaurantsReservations": "预约",
    "RestaurantsGoodForGroups": "适合多人",
    "GoodForKids": "适合儿童",
    "BikeParking": "自行车停车",
    "BusinessAcceptsCreditCards": "信用卡",
    "RestaurantsPriceRange2": "价格档位",
    "NoiseLevel": "噪音水平",
    "Alcohol": "酒水",
    "HasTV": "电视",
    "DogsAllowed": "允许带狗",
    "WheelchairAccessible": "轮椅友好",
    "HappyHour": "欢乐时光",
    "Caters": "承接餐饮",
    "ByAppointmentOnly": "仅预约",
    "BusinessParking": "停车",
    "Ambience": "氛围",
    "GoodForMeal": "适合餐段",
}


DAY_LABELS = {
    "Monday": "周一",
    "Tuesday": "周二",
    "Wednesday": "周三",
    "Thursday": "周四",
    "Friday": "周五",
    "Saturday": "周六",
    "Sunday": "周日",
}


def _parse_yelp_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, int, float, dict, list)):
        return value
    text = str(value).strip()
    if text in {"True", "False", "None"}:
        return {"True": True, "False": False, "None": None}[text]
    try:
        return ast.literal_eval(text)
    except (SyntaxError, ValueError):
        return text.strip("\"'")


def _clean_attribute_value(value: Any) -> Any:
    parsed = _parse_yelp_value(value)
    if isinstance(parsed, str):
        return parsed.strip().strip("\"'")
    if isinstance(parsed, dict):
        return {
            str(key): _clean_attribute_value(inner_value)
            for key, inner_value in parsed.items()
            if inner_value is not None
        }
    return parsed


def _selected_attributes(raw_attributes: dict[str, Any] | None) -> dict[str, Any]:
    if not raw_attributes:
        return {}
    cleaned = {
        key: _clean_attribute_value(raw_attributes[key])
        for key in SELECTED_ATTRIBUTE_KEYS
        if key in raw_attributes and raw_attributes[key] is not None
    }
    return {key: value for key, value in cleaned.items() if value not in (None, {}, [])}


def _format_scalar(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    if value is None:
        return "未知"
    text = str(value).strip()
    if text == "free":
        return "免费"
    if text in {"no", "none"}:
        return "无"
    if text == "paid":
        return "付费"
    return text


def _format_mapping(value: dict[str, Any]) -> str:
    enabled = [key for key, item in value.items() if item is True]
    if enabled:
        return "、".join(enabled)
    pairs = [
        f"{key}={_format_scalar(item)}"
        for key, item in value.items()
        if item not in (None, False)
    ]
    return "；".join(pairs) if pairs else "无明确可用项"


def _format_attribute(key: str, value: Any) -> str:
    label = ATTRIBUTE_LABELS.get(key, key)
    if isinstance(value, dict):
        return f"{label}：{_format_mapping(value)}"
    return f"{label}：{_format_scalar(value)}"


def _format_hours(hours: dict[str, str] | None) -> str:
    if not hours:
        return "暂无营业时间字段"
    def normalize_time_range(value: str) -> str:
        parts = []
        for time_text in value.split("-"):
            hour, separator, minute = time_text.partition(":")
            if not separator:
                parts.append(time_text)
                continue
            parts.append(f"{int(hour)}:{int(minute):02d}")
        return "-".join(parts)

    ordered = [
        f"{DAY_LABELS.get(day, day)} {normalize_time_range(hours[day])}"
        for day in DAY_LABELS
        if day in hours
    ]
    return "；".join(ordered)


def _categories(raw_categories: str | None) -> list[str]:
    if not raw_categories:
        return []
    return [category.strip() for category in raw_categories.split(",") if category.strip()]


def _build_content(shop: dict[str, Any], business: dict[str, Any], attributes: dict[str, Any]) -> str:
    categories = _categories(business.get("categories"))
    lines = [
        f"商户名称：{shop['name']}。",
        f"本地商户编号：{shop['shopId']}；Yelp business_id：{shop['sourceBusinessId']}。",
        f"本地分类：{shop['typeName']}（typeId={shop['typeId']}）。",
        f"北京化演示地址：{shop['address']}。",
        f"行政区：{shop.get('district') or '未知'}；锚点地点：{shop.get('anchorPlaceName') or '未知'}。",
        (
            f"位置映射：距锚点约{shop.get('projectionDistanceMeters')}米；"
            f"方法：{shop.get('projectionMethod') or '未知'}。"
        ),
        f"坐标系：{shop.get('coordinateSystem') or 'BD-09'}；数据性质：北京化演示商户。",
        (
            "重要说明：名称、地址和位置是确定性演示投影，不代表北京真实登记商家；"
            "评论原文/译文、用户行为、评分、属性与营业时间来自 Yelp 教育数据源快照；"
            "演示坐标不可用于现实导航。"
        ),
        f"原始 Yelp 名称（历史检索别名）：{shop.get('originalName') or business.get('name', '')}。",
        f"Yelp 来源评分：{business.get('stars')}；来源评论数：{business.get('review_count')}。",
        f"Yelp 来源营业状态：{'当时标记营业' if business.get('is_open') == 1 else '当时标记关闭或未知'}。",
    ]
    if categories:
        lines.append(f"Yelp 来源分类：{'、'.join(categories)}。")
    lines.append(f"Yelp 来源营业时间快照：{_format_hours(business.get('hours'))}。")
    if attributes:
        lines.append("Yelp 来源属性快照：")
        lines.extend(f"- {_format_attribute(key, value)}。" for key, value in attributes.items())
    else:
        lines.append("Yelp 来源属性快照：暂无可用属性字段。")
    return "\n".join(lines)


def build_merchant_docs(
    sample_path: Path = SAMPLE_PATH,
    raw_business_path: Path = RAW_BUSINESS_PATH,
) -> dict[str, Any]:
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    shops = sample["shops"]
    shop_by_business_id = {shop["sourceBusinessId"]: shop for shop in shops}
    remaining_ids = set(shop_by_business_id)
    business_by_id: dict[str, dict[str, Any]] = {}

    with raw_business_path.open(encoding="utf-8") as file:
        for line in file:
            business = json.loads(line)
            business_id = business["business_id"]
            if business_id not in remaining_ids:
                continue
            business_by_id[business_id] = business
            remaining_ids.remove(business_id)
            if not remaining_ids:
                break

    if remaining_ids:
        raise ValueError(f"missing Yelp businesses: {sorted(remaining_ids)[:5]}")

    docs = []
    for shop in shops:
        business = business_by_id[shop["sourceBusinessId"]]
        attributes = _selected_attributes(business.get("attributes"))
        categories = _categories(business.get("categories"))
        docs.append(
            {
                "sourceType": "merchant_doc",
                "sourceId": shop["sourceBusinessId"],
                "shopId": shop["shopId"],
                "sourceBusinessId": shop["sourceBusinessId"],
                "name": shop["name"],
                "typeId": shop["typeId"],
                "typeName": shop["typeName"],
                "address": shop["address"],
                "city": "北京",
                "state": "北京市",
                "postalCode": None,
                "longitude": shop.get("longitude"),
                "latitude": shop.get("latitude"),
                "coordinateSystem": shop.get("coordinateSystem"),
                "district": shop.get("district"),
                "anchorPlaceId": shop.get("anchorPlaceId"),
                "anchorPlaceName": shop.get("anchorPlaceName"),
                "projectionDistanceMeters": shop.get("projectionDistanceMeters"),
                "projectionBearingDegrees": shop.get("projectionBearingDegrees"),
                "projectionMethod": shop.get("projectionMethod"),
                "dataNature": shop.get("dataNature"),
                "realWorldNavigationSupported": shop.get(
                    "realWorldNavigationSupported"
                ),
                "mappingExplanation": shop.get("mappingExplanation"),
                "reviewEvidenceScope": sample.get("metadata", {}).get(
                    "reviewEvidenceScope"
                ),
                "localizationVersion": sample.get("metadata", {}).get(
                    "localizationVersion"
                ),
                "originalName": shop.get("originalName") or business.get("name"),
                "originalAddress": shop.get("originalAddress"),
                "sourceCity": business.get("city"),
                "sourceState": business.get("state"),
                "sourcePostalCode": business.get("postal_code"),
                "stars": business.get("stars"),
                "reviewCount": business.get("review_count"),
                "isOpen": business.get("is_open") == 1,
                "categories": categories,
                "attributes": attributes,
                "hours": business.get("hours") or {},
                "visibility": "public",
                "language": "zh",
                "content": _build_content(shop, business, attributes),
            }
        )

    return {
        "metadata": {
            "sourceDataset": "Yelp Open Dataset",
            "sourceFile": str(raw_business_path.relative_to(AGENT_ROOT)),
            "baseSample": str(sample_path.relative_to(AGENT_ROOT)),
            "businessCount": len(docs),
            "city": sample["metadata"].get("city"),
            "displayCity": sample["metadata"].get("displayCity"),
            "coordinateSystem": sample["metadata"].get("coordinateSystem"),
            "dataNature": sample["metadata"].get("dataNature"),
            "localizationVersion": sample["metadata"].get("localizationVersion"),
            "notice": (
                "商户显示身份与位置是北京化演示投影；Yelp来源属性、评论和行为关系保持不变。"
            ),
        },
        "docs": docs,
    }


def main() -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = build_merchant_docs()
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(payload['docs'])} merchant docs to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
