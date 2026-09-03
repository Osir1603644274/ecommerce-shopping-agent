"""Machine-verifiable acceptance checks for the Beijing demo projection."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from recommendation.beijing_localization import (
    COORDINATE_SYSTEM,
    DATA_NATURE,
    MAX_ANCHOR_DISTANCE_METERS,
    MIN_ANCHOR_DISTANCE_METERS,
    PROJECTION_METHOD,
    REVIEW_EVIDENCE_SCOPE,
)


DEFAULT_QUALITY_REPORT_PATH = (
    Path(__file__).parent / "reports" / "beijing_localization_quality_report.json"
)

SHOP_INVARIANT_FIELDS = (
    "shopId",
    "sourceBusinessId",
    "typeId",
    "avgPrice",
    "phone",
)
REVIEW_INVARIANT_FIELDS = (
    "id",
    "shopId",
    "text",
    "sourceReviewId",
    "sourceUserId",
    "stars",
    "createdAt",
)
BEHAVIOR_INVARIANT_FIELDS = (
    "id",
    "sourceUserId",
    "userId",
    "shopId",
    "behaviorType",
    "score",
    "source",
    "occurredAt",
    "sourceReviewId",
)
ITEM_INVARIANT_FIELDS = (
    "itemId",
    "sourceBusinessId",
    "typeId",
    "avgPrice",
    "reviewCount",
)


def _canonical_rows(
    rows: Iterable[dict[str, Any]],
    fields: tuple[str, ...],
    id_field: str,
) -> list[dict[str, Any]]:
    return sorted(
        ({field: row.get(field) for field in fields} for row in rows),
        key=lambda row: str(row.get(id_field)),
    )


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _haversine_meters(
    longitude_a: float,
    latitude_a: float,
    longitude_b: float,
    latitude_b: float,
) -> float:
    earth_radius_meters = 6_371_008.8
    latitude_a_radians = math.radians(latitude_a)
    latitude_b_radians = math.radians(latitude_b)
    latitude_delta = math.radians(latitude_b - latitude_a)
    longitude_delta = math.radians(longitude_b - longitude_a)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(latitude_a_radians)
        * math.cos(latitude_b_radians)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * earth_radius_meters * math.asin(math.sqrt(haversine))


def audit_beijing_localization(
    *,
    source_sample: dict[str, Any],
    localized_sample: dict[str, Any],
    source_items: dict[str, Any],
    localized_items: dict[str, Any],
    projection: dict[int, dict[str, Any]],
    anchors: list[dict[str, Any]],
    expected_translations: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a report whose required checks must all pass before publishing."""

    checks: list[dict[str, Any]] = []

    def add_check(
        check_id: str,
        passed: bool,
        *,
        summary: str,
        actual: Any = None,
        expected: Any = None,
        required: bool = True,
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "required": required,
                "passed": bool(passed),
                "summary": summary,
                **({"actual": actual} if actual is not None else {}),
                **({"expected": expected} if expected is not None else {}),
            }
        )

    source_shops = _canonical_rows(
        source_sample.get("shops", []),
        SHOP_INVARIANT_FIELDS,
        "shopId",
    )
    localized_shops = _canonical_rows(
        localized_sample.get("shops", []),
        SHOP_INVARIANT_FIELDS,
        "shopId",
    )
    add_check(
        "online_shop_lineage_preserved",
        source_shops == localized_shops,
        summary="在线商户ID、Yelp business_id、分类、价格和电话保持不变。",
        actual={"count": len(localized_shops), "sha256": _digest(localized_shops)},
        expected={"count": len(source_shops), "sha256": _digest(source_shops)},
    )

    source_reviews = _canonical_rows(
        source_sample.get("reviews", []),
        REVIEW_INVARIANT_FIELDS,
        "id",
    )
    localized_reviews = _canonical_rows(
        localized_sample.get("reviews", []),
        REVIEW_INVARIANT_FIELDS,
        "id",
    )
    add_check(
        "review_source_evidence_preserved",
        source_reviews == localized_reviews,
        summary="评论ID、商户边、来源用户、评分、时间和英文原文逐条保持不变。",
        actual={"count": len(localized_reviews), "sha256": _digest(localized_reviews)},
        expected={"count": len(source_reviews), "sha256": _digest(source_reviews)},
    )

    source_behaviors = _canonical_rows(
        source_sample.get("userBehaviors", []),
        BEHAVIOR_INVARIANT_FIELDS,
        "id",
    )
    localized_behaviors = _canonical_rows(
        localized_sample.get("userBehaviors", []),
        BEHAVIOR_INVARIANT_FIELDS,
        "id",
    )
    add_check(
        "user_behavior_graph_preserved",
        source_behaviors == localized_behaviors,
        summary="用户、商户、行为类型、评分、来源和发生时间组成的行为图保持不变。",
        actual={"count": len(localized_behaviors), "sha256": _digest(localized_behaviors)},
        expected={"count": len(source_behaviors), "sha256": _digest(source_behaviors)},
    )

    source_recommendation_items = _canonical_rows(
        source_items.get("items", []),
        ITEM_INVARIANT_FIELDS,
        "itemId",
    )
    localized_recommendation_items = _canonical_rows(
        localized_items.get("items", []),
        ITEM_INVARIANT_FIELDS,
        "itemId",
    )
    add_check(
        "recommendation_candidates_preserved",
        source_recommendation_items == localized_recommendation_items,
        summary="推荐候选ID、Yelp business_id、分类、价格和评论数保持不变。",
        actual={
            "count": len(localized_recommendation_items),
            "sha256": _digest(localized_recommendation_items),
        },
        expected={
            "count": len(source_recommendation_items),
            "sha256": _digest(source_recommendation_items),
        },
    )

    translated_reviews = {
        str(review["id"]): str(review.get("contentZh") or "")
        for review in localized_sample.get("reviews", [])
    }
    expected_translations = expected_translations or {}
    translation_mismatches = [
        review_id
        for review_id, expected_text in expected_translations.items()
        if translated_reviews.get(review_id) != expected_text
    ]
    add_check(
        "canonical_chinese_translation_preserved",
        not translation_mismatches,
        summary="已有规范中文译文不替换商户名或地名，按翻译检查点原样发布。",
        actual={
            "expectedTranslationCount": len(expected_translations),
            "mismatchCount": len(translation_mismatches),
            "sampleMismatchIds": translation_mismatches[:10],
        },
        expected={"mismatchCount": 0},
    )

    review_scope_mismatches = [
        str(review.get("id"))
        for review in localized_sample.get("reviews", [])
        if review.get("reviewEvidenceScope") != REVIEW_EVIDENCE_SCOPE
        or review.get("sourceShopName") in (None, "")
    ]
    add_check(
        "review_evidence_scope_explicit",
        not review_scope_mismatches,
        summary="每条评论均保留来源商户名，并声明不能作为北京现实经营事实。",
        actual={
            "mismatchCount": len(review_scope_mismatches),
            "sampleMismatchIds": review_scope_mismatches[:10],
        },
        expected={"mismatchCount": 0},
    )

    anchor_by_id = {str(anchor["id"]): anchor for anchor in anchors}
    source_item_ids = {int(item["itemId"]) for item in source_items.get("items", [])}
    projection_ids = set(projection)
    add_check(
        "projection_entity_coverage",
        source_item_ids == projection_ids,
        summary="每个推荐候选恰好有一个北京展示投影，没有漏项或额外实体。",
        actual={
            "projectionCount": len(projection_ids),
            "missingCount": len(source_item_ids - projection_ids),
            "extraCount": len(projection_ids - source_item_ids),
        },
        expected={"projectionCount": len(source_item_ids), "missingCount": 0, "extraCount": 0},
    )

    names = [str(item.get("displayName") or "") for item in projection.values()]
    add_check(
        "display_names_unique",
        bool(names) and len(names) == len(set(names)) and all(names),
        summary="北京化展示名称非空且全局唯一，便于实体解析和答辩演示。",
        actual={"count": len(names), "uniqueCount": len(set(names))},
        expected={"uniqueCount": len(names)},
    )

    coordinate_pairs = [
        (item.get("longitude"), item.get("latitude"))
        for item in projection.values()
    ]
    coordinate_errors: list[dict[str, Any]] = []
    measured_distances: list[float] = []
    for entity_id, item in projection.items():
        anchor = anchor_by_id.get(str(item.get("anchorPlaceId")))
        if anchor is None:
            coordinate_errors.append({"entityId": entity_id, "reason": "missing_anchor"})
            continue
        location = anchor.get("location") or {}
        try:
            longitude = float(item["longitude"])
            latitude = float(item["latitude"])
            anchor_longitude = float(location["longitude"])
            anchor_latitude = float(location["latitude"])
        except (KeyError, TypeError, ValueError):
            coordinate_errors.append({"entityId": entity_id, "reason": "invalid_coordinate"})
            continue
        distance = _haversine_meters(
            longitude,
            latitude,
            anchor_longitude,
            anchor_latitude,
        )
        measured_distances.append(distance)
        declared_distance = item.get("projectionDistanceMeters")
        if (
            item.get("coordinateSystem") != COORDINATE_SYSTEM
            or item.get("district") != anchor.get("district")
            or item.get("projectionMethod") != PROJECTION_METHOD
            or not (115.3 <= longitude <= 117.7 and 39.3 <= latitude <= 41.2)
            or not (
                MIN_ANCHOR_DISTANCE_METERS - 2
                <= distance
                <= MAX_ANCHOR_DISTANCE_METERS + 2
            )
            or not isinstance(declared_distance, int)
            or abs(distance - declared_distance) > 2
        ):
            coordinate_errors.append(
                {
                    "entityId": entity_id,
                    "reason": "geographic_invariant_failed",
                    "measuredDistanceMeters": round(distance, 2),
                    "declaredDistanceMeters": declared_distance,
                    "anchorPlaceId": item.get("anchorPlaceId"),
                }
            )
    add_check(
        "geographic_invariants_hold",
        not coordinate_errors,
        summary="坐标均为BD-09、位于北京范围内、行政区继承自真实锚点，且距锚点120至900米。",
        actual={
            "checkedCount": len(projection),
            "errorCount": len(coordinate_errors),
            "sampleErrors": coordinate_errors[:10],
            "measuredDistanceMeters": {
                "minimum": round(min(measured_distances), 2) if measured_distances else None,
                "maximum": round(max(measured_distances), 2) if measured_distances else None,
                "average": (
                    round(sum(measured_distances) / len(measured_distances), 2)
                    if measured_distances
                    else None
                ),
            },
        },
        expected={"errorCount": 0},
    )

    add_check(
        "coordinates_unique",
        len(coordinate_pairs) == len(set(coordinate_pairs)),
        summary="投影坐标无完全重复点，避免附近检索出现不可解释的重叠实体。",
        actual={"count": len(coordinate_pairs), "uniqueCount": len(set(coordinate_pairs))},
        expected={"uniqueCount": len(coordinate_pairs)},
    )

    anchor_counts = Counter(str(item["anchorPlaceId"]) for item in projection.values())
    district_counts = Counter(str(item["district"]) for item in projection.values())
    used_anchor_count = len(anchor_counts)
    minimum_expected_anchor_count = min(80, len(anchors))
    average_anchor_load = len(projection) / used_anchor_count if used_anchor_count else 0
    maximum_anchor_load = max(anchor_counts.values()) if anchor_counts else 0
    add_check(
        "spatial_distribution_explainable",
        (
            used_anchor_count >= minimum_expected_anchor_count
            and len(district_counts) >= min(12, len({str(anchor["district"]) for anchor in anchors}))
            and maximum_anchor_load <= max(1, math.ceil(average_anchor_load * 2))
        ),
        summary="候选分散到足够多的真实北京锚点和行政区，单锚点负载不超过平均值两倍。",
        actual={
            "usedAnchorCount": used_anchor_count,
            "availableAnchorCount": len(anchors),
            "districtCount": len(district_counts),
            "averageEntitiesPerAnchor": round(average_anchor_load, 2),
            "maximumEntitiesPerAnchor": maximum_anchor_load,
            "anchorCounts": dict(sorted(anchor_counts.items())),
            "districtCounts": dict(sorted(district_counts.items())),
        },
        expected={
            "minimumUsedAnchorCount": minimum_expected_anchor_count,
            "minimumDistrictCount": min(
                12,
                len({str(anchor["district"]) for anchor in anchors}),
            ),
            "maximumToAverageAnchorLoadRatio": 2,
        },
    )

    projection_lineage_errors = [
        int(entity_id)
        for entity_id, item in projection.items()
        if not item.get("originalName")
        or item.get("originalLongitude") is None
        or item.get("originalLatitude") is None
        or item.get("dataNature") != DATA_NATURE
        or item.get("realWorldNavigationSupported") is not False
        or not item.get("mappingExplanation")
    ]
    add_check(
        "projection_lineage_and_boundary_explicit",
        not projection_lineage_errors,
        summary="投影保留Yelp原始身份和坐标，并明确其演示性质、映射解释和不可导航边界。",
        actual={
            "errorCount": len(projection_lineage_errors),
            "sampleEntityIds": projection_lineage_errors[:10],
        },
        expected={"errorCount": 0},
    )

    required_failures = [
        check["id"]
        for check in checks
        if check["required"] and not check["passed"]
    ]
    return {
        "status": "passed" if not required_failures else "failed",
        "localizationVersion": localized_sample.get("metadata", {}).get(
            "localizationVersion"
        ),
        "dataNature": DATA_NATURE,
        "scope": {
            "realBeijingFacts": "锚点地点目录及其BD-09坐标快照",
            "syntheticFields": "商户展示名称、地址和锚点周边投影坐标",
            "preservedYelpEvidence": "实体ID、评论原文/规范译文、评分、时间和用户行为边",
            "unsupportedClaims": "真实商户存在性、实时营业状态、现实导航和路线",
        },
        "summary": {
            "requiredCheckCount": sum(check["required"] for check in checks),
            "passedRequiredCheckCount": sum(
                check["required"] and check["passed"] for check in checks
            ),
            "failedRequiredChecks": required_failures,
            "onlineShopCount": len(localized_sample.get("shops", [])),
            "candidateShopCount": len(projection),
            "reviewCount": len(localized_sample.get("reviews", [])),
            "userBehaviorCount": len(localized_sample.get("userBehaviors", [])),
        },
        "checks": checks,
    }


def write_quality_report(
    report: dict[str, Any],
    path: Path = DEFAULT_QUALITY_REPORT_PATH,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
