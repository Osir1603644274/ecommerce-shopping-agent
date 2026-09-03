"""Deterministically project Yelp entities into a Beijing demo world.

The projection deliberately preserves every source ID and interaction edge.
Only presentation and geography fields are localized.  The generated shops
are synthetic demo entities near real Beijing landmarks; they must never be
presented as real registered Beijing businesses.
"""

from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from pathlib import Path
from typing import Any


AGENT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = AGENT_ROOT.parent
DEFAULT_SAMPLE_PATH = (
    AGENT_ROOT / "recommendation" / "data" / "processed" / "yelp_local_life_sample.json"
)
DEFAULT_ITEMS_PATH = (
    AGENT_ROOT / "recommendation" / "data" / "processed" / "yelp_recommendation_items.json"
)
DEFAULT_CASES_PATH = (
    AGENT_ROOT / "recommendation" / "data" / "processed" / "yelp_recommendation_cases.json"
)
DEFAULT_SPLITS_PATH = (
    AGENT_ROOT
    / "recommendation"
    / "data"
    / "processed"
    / "yelp_recommendation_case_splits.json"
)
DEFAULT_PLACE_CATALOG_PATH = (
    AGENT_ROOT / "app" / "place_data" / "data" / "beijing_places_v2.json"
)
DEFAULT_CHECKPOINT_PATH = (
    AGENT_ROOT / "translation_runs" / "yelp_full_translation.jsonl"
)
DEFAULT_PROJECTION_PATH = (
    AGENT_ROOT
    / "recommendation"
    / "data"
    / "processed"
    / "beijing_shop_projection.json"
)

LOCALIZATION_VERSION = "beijing-demo-v2"
COORDINATE_SYSTEM = "BD-09"
DATA_NATURE = "localized_demo"
REVIEW_EVIDENCE_SCOPE = "yelp_source_experience_not_beijing_real_world_fact"
PROJECTION_METHOD = "deterministic_real_anchor_radial_projection"
MIN_ANCHOR_DISTANCE_METERS = 120
MAX_ANCHOR_DISTANCE_METERS = 900

TYPE_NAMES = {
    1: "美食",
    2: "咖啡",
    3: "电影",
    4: "酒店",
    5: "健身",
}

NAME_PREFIXES = (
    "槐序",
    "春和",
    "松风",
    "晴川",
    "拾光",
    "青禾",
    "云栖",
    "望京",
    "京华",
    "长安",
    "燕云",
    "北辰",
    "清晏",
    "朝露",
    "知春",
    "景明",
    "和光",
    "归园",
    "星河",
    "暖树",
)

TYPE_CONCEPTS = {
    1: ("小馆", "食集", "家常菜", "面馆", "烤肉", "味坊", "餐吧", "饭堂"),
    2: ("咖啡", "手冲咖啡", "咖啡书屋", "烘焙咖啡", "茶咖", "咖啡工房"),
    3: ("影城", "电影馆", "光影空间"),
    4: ("酒店", "旅居", "精品酒店", "客舍"),
    5: ("健身馆", "运动工坊", "瑜伽馆", "训练中心"),
}

def _stable_int(value: str) -> int:
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:8], "big")


def load_anchor_places(path: Path = DEFAULT_PLACE_CATALOG_PATH) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    anchors = []
    for item in payload["places"]:
        location = item.get("location") or {}
        if (
            item.get("kind") != "park"
            or not item.get("district")
            or location.get("coordinateSystem") != COORDINATE_SYSTEM
            or location.get("longitude") is None
            or location.get("latitude") is None
        ):
            continue
        anchors.append(item)
    anchors.sort(key=lambda item: str(item["id"]))
    if not anchors:
        raise ValueError("Beijing place catalog contains no eligible BD-09 park anchors")
    return anchors


def _localized_coordinate(
    entity_id: int,
    anchor: dict[str, Any],
) -> tuple[float, float, int, int]:
    seed = _stable_int(f"{LOCALIZATION_VERSION}:{entity_id}:coordinate")
    distance_span = MAX_ANCHOR_DISTANCE_METERS - MIN_ANCHOR_DISTANCE_METERS + 1
    radius_meters = MIN_ANCHOR_DISTANCE_METERS + seed % distance_span
    bearing_degrees = (seed // distance_span) % 360
    angle = math.radians(bearing_degrees)
    latitude = float(anchor["location"]["latitude"])
    longitude = float(anchor["location"]["longitude"])
    latitude_delta = (radius_meters * math.sin(angle)) / 111_320.0
    longitude_scale = max(math.cos(math.radians(latitude)), 0.1)
    longitude_delta = (radius_meters * math.cos(angle)) / (111_320.0 * longitude_scale)
    return (
        round(longitude + longitude_delta, 6),
        round(latitude + latitude_delta, 6),
        radius_meters,
        bearing_degrees,
    )


def _name_candidate(entity_id: int, type_id: int, anchor: dict[str, Any]) -> str:
    seed = _stable_int(f"{LOCALIZATION_VERSION}:{entity_id}:name")
    prefixes = NAME_PREFIXES
    concepts = TYPE_CONCEPTS.get(type_id, ("生活馆",))
    prefix = prefixes[seed % len(prefixes)]
    concept = concepts[(seed // len(prefixes)) % len(concepts)]
    anchor_label = str(anchor["name"]).replace("公园", "").replace("（东城）", "")
    return f"{prefix}{concept}·{anchor_label}店"


def build_projection(
    items: list[dict[str, Any]],
    *,
    anchors: list[dict[str, Any]] | None = None,
) -> dict[int, dict[str, Any]]:
    anchors = anchors or load_anchor_places()
    projection: dict[int, dict[str, Any]] = {}
    used_names: dict[str, int] = {}
    for item in sorted(items, key=lambda value: int(value.get("shopId", value.get("itemId")))):
        entity_id = int(item.get("shopId", item.get("itemId")))
        type_id = int(item["typeId"])
        anchor = anchors[_stable_int(f"{LOCALIZATION_VERSION}:{entity_id}:anchor") % len(anchors)]
        longitude, latitude, distance_meters, bearing_degrees = _localized_coordinate(
            entity_id,
            anchor,
        )
        base_name = _name_candidate(entity_id, type_id, anchor)
        duplicate_index = used_names.get(base_name, 0) + 1
        used_names[base_name] = duplicate_index
        display_name = base_name if duplicate_index == 1 else f"{base_name}{duplicate_index}号"
        original_name = str(item.get("originalName") or item.get("name") or "")
        original_address = str(item.get("originalAddress") or item.get("address") or "")
        original_longitude = item.get("originalLongitude", item.get("longitude"))
        original_latitude = item.get("originalLatitude", item.get("latitude"))
        projection[entity_id] = {
            "entityId": entity_id,
            "displayName": display_name,
            "district": anchor.get("district"),
            "address": (
                f"北京市{anchor.get('district') or ''}{anchor['name']}周边"
                f"（演示点位{entity_id}）"
            ),
            "longitude": longitude,
            "latitude": latitude,
            "coordinateSystem": COORDINATE_SYSTEM,
            "anchorPlaceId": anchor["id"],
            "anchorPlaceName": anchor["name"],
            "projectionDistanceMeters": distance_meters,
            "projectionBearingDegrees": bearing_degrees,
            "projectionMethod": PROJECTION_METHOD,
            "dataNature": DATA_NATURE,
            "sourceDataset": "Yelp Open Dataset",
            "realWorldNavigationSupported": False,
            "mappingExplanation": (
                f"保留Yelp实体{entity_id}及其评论/行为关系；展示位置按稳定哈希投影到"
                f"真实北京锚点“{anchor['name']}”周边约{distance_meters}米。"
            ),
            "originalName": original_name,
            "originalAddress": original_address,
            "originalLongitude": original_longitude,
            "originalLatitude": original_latitude,
        }
    return projection


def _localized_shop(
    shop: dict[str, Any],
    projected: dict[str, Any],
) -> dict[str, Any]:
    result = deepcopy(shop)
    result.update(
        {
            "name": projected["displayName"],
            "address": projected["address"],
            "longitude": projected["longitude"],
            "latitude": projected["latitude"],
            "coordinateSystem": projected["coordinateSystem"],
            "district": projected["district"],
            "anchorPlaceId": projected["anchorPlaceId"],
            "anchorPlaceName": projected["anchorPlaceName"],
            "projectionDistanceMeters": projected["projectionDistanceMeters"],
            "projectionBearingDegrees": projected["projectionBearingDegrees"],
            "projectionMethod": projected["projectionMethod"],
            "dataNature": projected["dataNature"],
            "realWorldNavigationSupported": projected["realWorldNavigationSupported"],
            "mappingExplanation": projected["mappingExplanation"],
            "localizationVersion": LOCALIZATION_VERSION,
            "originalName": projected["originalName"],
            "originalAddress": projected["originalAddress"],
            "originalLongitude": projected["originalLongitude"],
            "originalLatitude": projected["originalLatitude"],
        }
    )
    result["typeName"] = TYPE_NAMES.get(int(result["typeId"]), result.get("typeName"))
    return result


def load_translation_checkpoint(
    checkpoint_path: Path = DEFAULT_CHECKPOINT_PATH,
) -> dict[str, str]:
    if not checkpoint_path.exists():
        return {}
    translations: dict[str, str] = {}
    for line in checkpoint_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        translations[str(record["reviewId"])] = str(record["contentZh"])
    return translations


def localize_sample(
    sample: dict[str, Any],
    projection: dict[int, dict[str, Any]],
    *,
    translations: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = deepcopy(sample)
    original_shop_by_id = {int(shop["shopId"]): shop for shop in sample["shops"]}
    result["shops"] = [
        _localized_shop(shop, projection[int(shop["shopId"])])
        for shop in sample["shops"]
    ]
    translated = translations or {}
    localized_reviews: list[dict[str, Any]] = []
    for review in sample["reviews"]:
        localized = deepcopy(review)
        shop_id = int(review["shopId"])
        projected = projection[shop_id]
        original_shop = original_shop_by_id[shop_id]
        content_zh = translated.get(str(review["id"])) or review.get("contentZh")
        localized["shopName"] = projected["displayName"]
        localized["sourceShopName"] = projected["originalName"]
        localized["reviewEvidenceScope"] = REVIEW_EVIDENCE_SCOPE
        localized["reviewDisplayNotice"] = (
            "评论原文与译文保留自Yelp来源商户，不证明北京演示商户的现实经营事实。"
        )
        localized["tags"] = list(
            dict.fromkeys(
                [
                    *[str(tag) for tag in review.get("tags", [])],
                    "yelp",
                    TYPE_NAMES.get(
                        int(original_shop["typeId"]),
                        str(original_shop.get("typeName") or ""),
                    ),
                    f"stars:{float(review.get('stars', 0)):g}",
                    DATA_NATURE,
                    "source_review",
                ]
            )
        )
        if content_zh:
            # The review is source evidence, not a fictional Beijing review.  Keep
            # the canonical translation byte-for-byte instead of replacing source
            # merchant names or US geography with Beijing-looking wording.
            localized["contentZh"] = str(content_zh)
            localized["translationStatus"] = "translated"
        localized_reviews.append(localized)
    result["reviews"] = localized_reviews
    metadata = dict(result.get("metadata") or {})
    metadata.update(
        {
            "city": "Beijing",
            "displayCity": "北京",
            "localizationVersion": LOCALIZATION_VERSION,
            "coordinateSystem": COORDINATE_SYSTEM,
            "dataNature": DATA_NATURE,
            "localizationNotice": (
                "商户名称、地址和坐标为北京化演示投影；Yelp评论原文、译文和行为关系按来源保留。"
            ),
            "reviewEvidenceScope": REVIEW_EVIDENCE_SCOPE,
            "realWorldNavigationSupported": False,
        }
    )
    result["metadata"] = metadata
    return result


def localize_recommendation_items(
    payload: dict[str, Any],
    projection: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    result = deepcopy(payload)
    result["items"] = [
        {
            **item,
            "name": projection[int(item["itemId"])]["displayName"],
            "address": projection[int(item["itemId"])]["address"],
            "longitude": projection[int(item["itemId"])]["longitude"],
            "latitude": projection[int(item["itemId"])]["latitude"],
            "coordinateSystem": COORDINATE_SYSTEM,
            "district": projection[int(item["itemId"])]["district"],
            "anchorPlaceId": projection[int(item["itemId"])]["anchorPlaceId"],
            "anchorPlaceName": projection[int(item["itemId"])]["anchorPlaceName"],
            "projectionDistanceMeters": projection[int(item["itemId"])][
                "projectionDistanceMeters"
            ],
            "projectionBearingDegrees": projection[int(item["itemId"])][
                "projectionBearingDegrees"
            ],
            "projectionMethod": projection[int(item["itemId"])]["projectionMethod"],
            "dataNature": DATA_NATURE,
            "realWorldNavigationSupported": False,
            "mappingExplanation": projection[int(item["itemId"])]["mappingExplanation"],
            "localizationVersion": LOCALIZATION_VERSION,
            "originalName": projection[int(item["itemId"])]["originalName"],
            "originalAddress": projection[int(item["itemId"])]["originalAddress"],
            "originalLongitude": projection[int(item["itemId"])]["originalLongitude"],
            "originalLatitude": projection[int(item["itemId"])]["originalLatitude"],
        }
        for item in payload["items"]
    ]
    result["metadata"] = {
        **result.get("metadata", {}),
        "city": "Beijing",
        "displayCity": "北京",
        "localizationVersion": LOCALIZATION_VERSION,
        "coordinateSystem": COORDINATE_SYSTEM,
        "dataNature": DATA_NATURE,
        "reviewEvidenceScope": REVIEW_EVIDENCE_SCOPE,
        "realWorldNavigationSupported": False,
    }
    return result


def _localize_case_item_names(value: Any, projection: dict[int, dict[str, Any]]) -> Any:
    if isinstance(value, list):
        return [_localize_case_item_names(item, projection) for item in value]
    if not isinstance(value, dict):
        return value
    result = {
        key: _localize_case_item_names(item, projection)
        for key, item in value.items()
    }
    item_id = result.get("itemId")
    if item_id is not None and int(item_id) in projection:
        result["itemName"] = projection[int(item_id)]["displayName"]
    return result


def localize_cases(value: Any, projection: dict[int, dict[str, Any]]) -> Any:
    result = _localize_case_item_names(value, projection)
    if isinstance(result, dict) and "counts" in result:
        result["localization"] = {
            "city": "Beijing",
            "displayCity": "北京",
            "localizationVersion": LOCALIZATION_VERSION,
            "dataNature": DATA_NATURE,
            "reviewEvidenceScope": REVIEW_EVIDENCE_SCOPE,
        }
    return result


def projection_payload(projection: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "metadata": {
            "localizationVersion": LOCALIZATION_VERSION,
            "coordinateSystem": COORDINATE_SYSTEM,
            "dataNature": DATA_NATURE,
            "entityCount": len(projection),
            "anchorCount": len({item["anchorPlaceId"] for item in projection.values()}),
            "projectionMethod": PROJECTION_METHOD,
            "distanceRangeMeters": {
                "minimum": MIN_ANCHOR_DISTANCE_METERS,
                "maximum": MAX_ANCHOR_DISTANCE_METERS,
            },
            "reviewEvidenceScope": REVIEW_EVIDENCE_SCOPE,
            "realWorldNavigationSupported": False,
            "notice": (
                "北京地标为真实快照；商户名称、地址和位置为确定性演示投影；"
                "Yelp来源ID、评论原文/译文和用户行为关系保持不变；不可用于现实导航。"
            ),
        },
        "shops": [projection[key] for key in sorted(projection)],
    }
