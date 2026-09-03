"""Run a live, cross-service acceptance test for the Beijing demo Agent."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import redis


AGENT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PATH = (
    AGENT_ROOT
    / "recommendation"
    / "data"
    / "processed"
    / "yelp_local_life_sample.json"
)
PLACE_CATALOG_PATH = (
    AGENT_ROOT / "app" / "place_data" / "data" / "beijing_places_v2.json"
)
QUALITY_REPORT_PATH = (
    AGENT_ROOT
    / "recommendation"
    / "reports"
    / "beijing_localization_quality_report.json"
)
DEFAULT_OUTPUT_PATH = (
    AGENT_ROOT
    / "recommendation"
    / "reports"
    / "beijing_agent_acceptance_report.json"
)
EXPECTED_EVIDENCE_SCOPE = "yelp_source_experience_not_beijing_real_world_fact"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _post_scroll(
    client: httpx.Client,
    qdrant_url: str,
    collection: str,
    review_id: str,
) -> dict[str, Any] | None:
    response = client.post(
        f"{qdrant_url}/collections/{collection}/points/scroll",
        json={
            "filter": {
                "must": [
                    {
                        "key": "reviewId",
                        "match": {"value": review_id},
                    }
                ]
            },
            "limit": 1,
            "with_payload": True,
            "with_vector": False,
        },
    )
    response.raise_for_status()
    points = response.json()["result"]["points"]
    return points[0]["payload"] if points else None


def _post_knowledge_review_scroll(
    client: httpx.Client,
    qdrant_url: str,
    review_id: str,
) -> dict[str, Any] | None:
    response = client.post(
        f"{qdrant_url}/collections/knowledge_chunks/points/scroll",
        json={
            "filter": {
                "must": [
                    {"key": "sourceType", "match": {"value": "review"}},
                    {"key": "sourceId", "match": {"value": review_id}},
                ]
            },
            "limit": 1,
            "with_payload": True,
            "with_vector": False,
        },
    )
    response.raise_for_status()
    points = response.json()["result"]["points"]
    return points[0]["payload"] if points else None


def verify(
    *,
    backend_url: str,
    agent_url: str,
    qdrant_url: str,
    redis_url: str,
) -> dict[str, Any]:
    sample = _load(SAMPLE_PATH)
    catalog = _load(PLACE_CATALOG_PATH)
    data_quality_report = _load(QUALITY_REPORT_PATH)
    first_shop = sample["shops"][0]
    first_review = next(
        review
        for review in sample["reviews"]
        if int(review["shopId"]) == int(first_shop["shopId"])
    )
    first_behavior = sample["userBehaviors"][0]
    anchor = next(
        place
        for place in catalog["places"]
        if place["id"] == first_shop["anchorPlaceId"]
    )
    checks: list[dict[str, Any]] = []

    def add_check(
        check_id: str,
        passed: bool,
        summary: str,
        evidence: Any,
    ) -> None:
        checks.append(
            {
                "id": check_id,
                "passed": bool(passed),
                "summary": summary,
                "evidence": evidence,
            }
        )

    with httpx.Client(timeout=60.0) as client:
        backend_health = client.get(f"{backend_url}/api/health")
        backend_health.raise_for_status()
        backend_health_payload = backend_health.json()
        add_check(
            "backend_health",
            backend_health_payload.get("success") is True,
            "Spring Boot 后端可用。",
            backend_health_payload,
        )

        nearby_response = client.get(
            f"{backend_url}/api/shops/nearby",
            params={
                "typeId": first_shop["typeId"],
                "longitude": anchor["location"]["longitude"],
                "latitude": anchor["location"]["latitude"],
                "radiusMeters": 2000,
                "limit": 10,
            },
        )
        nearby_response.raise_for_status()
        nearby_shops = nearby_response.json()["data"]
        add_check(
            "backend_bd09_nearby_search",
            (
                any(int(shop["id"]) == int(first_shop["shopId"]) for shop in nearby_shops)
                and all(shop.get("coordinateSystem") == "BD-09" for shop in nearby_shops)
                and all(shop.get("dataNature") == "localized_demo" for shop in nearby_shops)
            ),
            "真实北京锚点与演示商户在统一BD-09坐标系中完成附近查询。",
            {
                "anchorPlaceId": anchor["id"],
                "anchorPlaceName": anchor["name"],
                "returnedShopCount": len(nearby_shops),
                "returnedShopIds": [shop["id"] for shop in nearby_shops],
                "distancesMeters": [
                    round(float(shop["distanceMeters"]), 2)
                    for shop in nearby_shops
                ],
            },
        )

        review_response = client.get(
            f"{backend_url}/api/reviews",
            params={"shopId": first_shop["shopId"]},
        )
        review_response.raise_for_status()
        reviews = review_response.json()["data"]
        backend_review = next(
            (
                review
                for review in reviews
                if review["reviewId"] == first_review["id"]
            ),
            None,
        )
        add_check(
            "mysql_review_lineage_visible_via_api",
            (
                backend_review is not None
                and backend_review.get("sourceReviewId")
                == first_review.get("sourceReviewId")
                and backend_review.get("sourceUserId")
                == first_review.get("sourceUserId")
                and backend_review.get("stars") == first_review.get("stars")
                and backend_review.get("sourceShopName")
                == first_review.get("sourceShopName")
                and backend_review.get("evidenceScope") == EXPECTED_EVIDENCE_SCOPE
            ),
            "评论来源ID、来源用户、评分、来源商户和证据边界已持久化并由API返回。",
            {
                "reviewId": backend_review.get("reviewId") if backend_review else None,
                "sourceReviewId": (
                    backend_review.get("sourceReviewId") if backend_review else None
                ),
                "sourceUserId": (
                    backend_review.get("sourceUserId") if backend_review else None
                ),
                "stars": backend_review.get("stars") if backend_review else None,
                "sourceShopName": (
                    backend_review.get("sourceShopName") if backend_review else None
                ),
                "evidenceScope": (
                    backend_review.get("evidenceScope") if backend_review else None
                ),
            },
        )

        behavior_response = client.get(
            f"{backend_url}/api/user-behaviors",
            params={"userId": first_behavior["userId"], "limit": 100},
        )
        behavior_response.raise_for_status()
        backend_behavior = next(
            (
                behavior
                for behavior in behavior_response.json()["data"]
                if behavior["id"] == first_behavior["id"]
            ),
            None,
        )
        add_check(
            "mysql_behavior_lineage_visible_via_api",
            (
                backend_behavior is not None
                and backend_behavior.get("sourceUserId")
                == first_behavior.get("sourceUserId")
                and backend_behavior.get("sourceReviewId")
                == first_behavior.get("sourceReviewId")
                and int(backend_behavior["shopId"]) == int(first_behavior["shopId"])
            ),
            "用户行为保留来源用户、来源评论和商户边，并可由API复核。",
            backend_behavior,
        )

        merchant_collection = client.get(
            f"{qdrant_url}/collections/merchant_reviews"
        )
        merchant_collection.raise_for_status()
        knowledge_collection = client.get(
            f"{qdrant_url}/collections/knowledge_chunks"
        )
        knowledge_collection.raise_for_status()
        merchant_collection_result = merchant_collection.json()["result"]
        knowledge_collection_result = knowledge_collection.json()["result"]
        add_check(
            "qdrant_collections_green",
            (
                merchant_collection_result.get("status") == "green"
                and knowledge_collection_result.get("status") == "green"
                and int(merchant_collection_result.get("points_count") or 0)
                >= len(sample["reviews"])
                and int(knowledge_collection_result.get("points_count") or 0)
                >= len(sample["reviews"]) + len(sample["shops"])
            ),
            "旧评论集合与统一知识集合均为green，点数覆盖评论和商户资料。",
            {
                "merchantReviews": {
                    "status": merchant_collection_result.get("status"),
                    "points": merchant_collection_result.get("points_count"),
                },
                "knowledgeChunks": {
                    "status": knowledge_collection_result.get("status"),
                    "points": knowledge_collection_result.get("points_count"),
                },
            },
        )

        qdrant_review = _post_scroll(
            client,
            qdrant_url,
            "merchant_reviews",
            first_review["id"],
        )
        add_check(
            "qdrant_review_lineage_preserved",
            (
                qdrant_review is not None
                and qdrant_review.get("sourceReviewId")
                == first_review.get("sourceReviewId")
                and qdrant_review.get("sourceUserId")
                == first_review.get("sourceUserId")
                and qdrant_review.get("stars") == first_review.get("stars")
                and qdrant_review.get("sourceShopName")
                == first_review.get("sourceShopName")
                and qdrant_review.get("evidenceScope") == EXPECTED_EVIDENCE_SCOPE
            ),
            "Qdrant评论payload保留与MySQL一致的Yelp来源谱系。",
            {
                key: qdrant_review.get(key) if qdrant_review else None
                for key in (
                    "reviewId",
                    "shopId",
                    "sourceReviewId",
                    "sourceUserId",
                    "stars",
                    "sourceShopName",
                    "evidenceScope",
                )
            },
        )

        knowledge_review = _post_knowledge_review_scroll(
            client,
            qdrant_url,
            first_review["id"],
        )
        knowledge_metadata = (
            knowledge_review.get("metadata", {}) if knowledge_review else {}
        )
        add_check(
            "qdrant_knowledge_review_lineage_preserved",
            (
                knowledge_review is not None
                and knowledge_review.get("sourceType") == "review"
                and knowledge_metadata.get("sourceReviewId")
                == first_review.get("sourceReviewId")
                and knowledge_metadata.get("sourceUserId")
                == first_review.get("sourceUserId")
                and knowledge_metadata.get("stars") == first_review.get("stars")
                and knowledge_metadata.get("sourceShopName")
                == first_review.get("sourceShopName")
                and knowledge_metadata.get("evidenceScope")
                == EXPECTED_EVIDENCE_SCOPE
            ),
            "统一knowledge_chunks集合中的评论chunk同样保留Yelp来源谱系。",
            {
                "chunkId": knowledge_review.get("chunkId")
                if knowledge_review
                else None,
                **{
                    key: knowledge_metadata.get(key)
                    for key in (
                        "reviewId",
                        "shopId",
                        "sourceReviewId",
                        "sourceUserId",
                        "stars",
                        "sourceShopName",
                        "evidenceScope",
                    )
                },
            },
        )

        agent_health = client.get(f"{agent_url}/health")
        agent_health.raise_for_status()
        agent_question = f"{anchor['name']}附近有哪些{first_shop['typeName']}店？"
        agent_response = client.post(
            f"{agent_url}/agent/chat-llm",
            json={
                "message": agent_question,
                "sessionId": "beijing-agent-live-acceptance",
            },
        )
        agent_response.raise_for_status()
        agent_payload = agent_response.json()
        tool_names = agent_payload.get("trace", {}).get("toolNames", [])
        answer = str(agent_payload.get("answer") or "")
        add_check(
            "agent_landmark_to_demo_shop_flow",
            (
                agent_payload.get("trace", {}).get("status") == "ok"
                and tool_names == ["search_shops"]
                and "演示商户" in answer
                and "不代表真实登记商家" in answer
                and anchor["name"] in answer
            ),
            "Agent完成真实地标解析、附近商户工具调用和演示边界披露。",
            {
                "question": agent_question,
                "answer": answer,
                "toolNames": tool_names,
                "durationMs": agent_payload.get("trace", {}).get("totalDurationMs"),
            },
        )

    redis_client = redis.Redis.from_url(redis_url, decode_responses=True)
    redis_geo_key = f"local-life:shop:geo:type:{first_shop['typeId']}"
    redis_geo_members = redis_client.zcard(redis_geo_key)
    add_check(
        "redis_geo_index_rebuilt",
        redis_geo_members > 0,
        "Redis GEO分类索引已由真实附近查询重建。",
        {
            "key": redis_geo_key,
            "memberCount": redis_geo_members,
        },
    )

    add_check(
        "offline_data_quality_gate",
        (
            data_quality_report.get("status") == "passed"
            and not data_quality_report["summary"]["failedRequiredChecks"]
        ),
        "离线数据完整性、地理自洽和空间分布门禁全部通过。",
        data_quality_report["summary"],
    )

    failed_checks = [check["id"] for check in checks if not check["passed"]]
    return {
        "status": "passed" if not failed_checks else "failed",
        "generatedAt": datetime.now(UTC).isoformat(),
        "localizationVersion": sample["metadata"]["localizationVersion"],
        "summary": {
            "checkCount": len(checks),
            "passedCheckCount": sum(check["passed"] for check in checks),
            "failedChecks": failed_checks,
            "candidateShopCount": data_quality_report["summary"][
                "candidateShopCount"
            ],
            "onlineShopCount": len(sample["shops"]),
            "reviewCount": len(sample["reviews"]),
            "userBehaviorCount": len(sample["userBehaviors"]),
        },
        "checks": checks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify the live Beijing Agent across Backend, Redis and Qdrant."
    )
    parser.add_argument("--backend-url", default="http://localhost:8080")
    parser.add_argument("--agent-url", default="http://localhost:8000")
    parser.add_argument("--qdrant-url", default="http://localhost:6333")
    parser.add_argument("--redis-url", default="redis://localhost:6379/0")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = verify(
        backend_url=args.backend_url.rstrip("/"),
        agent_url=args.agent_url.rstrip("/"),
        qdrant_url=args.qdrant_url.rstrip("/"),
        redis_url=args.redis_url,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
