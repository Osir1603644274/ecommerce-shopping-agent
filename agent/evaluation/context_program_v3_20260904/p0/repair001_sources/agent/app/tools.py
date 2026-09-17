import asyncio
import json
import logging
import time
from functools import lru_cache
from typing import TYPE_CHECKING

import httpx

from .knowledge import (
    REVIEW_SOURCE_NAME,
    build_review_retrieval_trace,
    search_knowledge as query_knowledge,
)
from .place_data import PlaceCatalog
from .rag import DEFAULT_TOP_K, search_reviews as query_reviews
from .schemas import ToolTrace
from .domains.ecommerce.tools import (
    compare_products_tool,
    get_product_details_tool,
    rerank_products_in_scope_tool,
    search_products_tool,
)
from .domains.ecommerce.models import SPEC_REGISTRY
from .domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_REGISTRY,
)
from .shop_entity import resolve_unique_shop
from .settings import settings

if TYPE_CHECKING:
    from .tool_execution_v2 import ToolExecutionContext


logger = logging.getLogger(__name__)

SHOPPING_REQUIREMENT_KEYS = sorted({
    key for category_specs in SPEC_REGISTRY.values() for key in category_specs
})
SHOPPING_REQUIREMENT_UNITS = sorted({
    unit
    for category_specs in SPEC_REGISTRY.values()
    for _kind, unit, _operators in category_specs.values()
})
USED_PHONE_REQUIREMENT_DESCRIPTION = "; ".join(
    f"{key}={','.join(spec.allowed_values)} "
    f"(unit={spec.unit}, operators={','.join(spec.operators)})"
    for key, spec in sorted(USED_PHONE_ATTRIBUTE_REGISTRY.items())
)
_ECOMMERCE_TOOL_ARGUMENT_KEYS = {
    "search_products": {
        "query", "category", "brand", "minPriceMinor", "maxPriceMinor",
        "limit", "requirements",
    },
    "get_product_details": {"productIds"},
    "compare_products": {"productIds", "category", "requirements"},
    "rerank_products_in_scope": {
        "scopeId", "productIds", "rankingIntent", "category", "requirements",
    },
}


@lru_cache(maxsize=1)
def get_place_catalog() -> PlaceCatalog:
    """Load the packaged demo catalog once per Agent process."""

    return PlaceCatalog.load()


def _finish_tool_trace(start: float, trace: ToolTrace) -> ToolTrace:
    return trace.model_copy(
        update={"duration_ms": round((time.perf_counter() - start) * 1000, 2)}
    )


async def check_backend() -> ToolTrace:
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{settings.backend_base_url}/api/health")
            response.raise_for_status()
        return _finish_tool_trace(
            start,
            ToolTrace(tool="backend_health", ok=True, detail="后端服务可访问"),
        )
    except Exception as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="backend_health", ok=False, detail=str(exc)),
        )


async def list_shop_types(keyword: str | None = None) -> ToolTrace:
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{settings.backend_base_url}/api/shop-types",
                params={"keyword": keyword} if keyword else None,
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            payload = response.json()

        shop_types = payload.get("data", [])
        names = [shop_type["name"] for shop_type in shop_types]
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="list_shop_types",
                ok=True,
                detail={
                    "keyword": keyword,
                    "count": len(names),
                    "names": names,
                },
            ),
        )
    except Exception as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="list_shop_types", ok=False, detail=str(exc)),
        )


async def search_shops(
    type_id: int | None = None,
    name: str | None = None,
    near_place_id: str | None = None,
    radius_meters: float | None = None,
    limit: int | None = None,
) -> ToolTrace:
    start = time.perf_counter()
    normalized_name = name.strip() if name and name.strip() else None
    normalized_place_id = (
        near_place_id.strip() if near_place_id and near_place_id.strip() else None
    )
    params: dict[str, object] = {}
    if type_id is not None:
        params["typeId"] = type_id
    try:
        nearby_place = None
        endpoint = "/api/shops"
        if normalized_place_id:
            nearby_place = get_place_catalog().get(normalized_place_id)
            if nearby_place is None:
                raise ValueError(f"未找到地点：{normalized_place_id}")
            if nearby_place.longitude is None or nearby_place.latitude is None:
                raise ValueError(f"地点 {nearby_place.name} 暂无可用坐标")
            if nearby_place.coordinate_system != "BD-09":
                raise ValueError(
                    f"地点 {nearby_place.name} 的坐标系不是 BD-09，不能与商户做距离计算"
                )
            endpoint = "/api/shops/nearby"
            params.update(
                {
                    "longitude": nearby_place.longitude,
                    "latitude": nearby_place.latitude,
                }
            )
            if radius_meters is not None:
                params["radiusMeters"] = radius_meters
            if limit is not None:
                params["limit"] = limit
        elif normalized_name:
            params["name"] = normalized_name

        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(
                f"{settings.backend_base_url}{endpoint}",
                params=params or None,
            )
            response.raise_for_status()
            response.encoding = "utf-8"
            payload = response.json()

        shops = payload.get("data", [])
        items = [
            {
                "id": shop["id"],
                "name": shop["name"],
                "address": shop["address"],
                "avgPrice": shop["avgPrice"],
                "longitude": shop.get("longitude"),
                "latitude": shop.get("latitude"),
                "coordinateSystem": shop.get("coordinateSystem"),
                "district": shop.get("district"),
                "anchorPlaceId": shop.get("anchorPlaceId"),
                "anchorPlaceName": shop.get("anchorPlaceName"),
                "dataNature": shop.get("dataNature"),
                **(
                    {"distanceMeters": shop.get("distanceMeters")}
                    if normalized_place_id
                    else {}
                ),
            }
            for shop in shops
        ]
        has_localized_demo = any(
            item.get("dataNature") == "localized_demo" for item in items
        )
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_shops",
                ok=True,
                detail={
                    "typeId": type_id,
                    "name": normalized_name,
                    "nearPlaceId": normalized_place_id,
                    "nearPlaceName": nearby_place.name if nearby_place else None,
                    "radiusMeters": radius_meters or (2000 if nearby_place else None),
                    "coordinateSystem": (
                        nearby_place.coordinate_system if nearby_place else None
                    ),
                    "count": len(items),
                    "shops": items,
                    "dataNotice": (
                        "地点来自北京真实地点目录；商户为保留 Yelp 评论与行为关系的北京化演示实体，"
                        "不代表真实登记商家。"
                        if nearby_place
                        else (
                            "返回结果包含北京化演示商户，不代表真实登记商家。"
                            if has_localized_demo
                            else None
                        )
                    ),
                },
            ),
        )
    except Exception as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_shops", ok=False, detail=str(exc)),
        )


async def get_shop_detail(shop_id: int) -> ToolTrace:
    start = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            response = await client.get(f"{settings.backend_base_url}/api/shops/{shop_id}")
            response.raise_for_status()
            payload = response.json()

        shop = payload.get("data", {})
        is_localized_demo = shop.get("dataNature") == "localized_demo"
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="get_shop_detail",
                ok=True,
                detail={
                    "id": shop["id"],
                    "name": shop["name"],
                    "typeId": shop["typeId"],
                    "address": shop["address"],
                    "avgPrice": shop["avgPrice"],
                    "phone": shop.get("phone"),
                    "longitude": shop.get("longitude"),
                    "latitude": shop.get("latitude"),
                    "coordinateSystem": shop.get("coordinateSystem"),
                    "district": shop.get("district"),
                    "anchorPlaceId": shop.get("anchorPlaceId"),
                    "anchorPlaceName": shop.get("anchorPlaceName"),
                    "dataNature": shop.get("dataNature"),
                    "dataNotice": (
                        "该商户是北京化演示实体，不代表真实登记商家；地址和位置不可用于现实导航。"
                        if is_localized_demo
                        else None
                    ),
                },
            ),
        )
    except Exception as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="get_shop_detail", ok=False, detail=str(exc)),
        )


async def recommend_shops(
    user_id: str,
    limit: int | None = None,
    longitude: float | None = None,
    latitude: float | None = None,
    radius_meters: float | None = None,
) -> ToolTrace:
    """根据用户历史行为调用 Spring Boot 推荐接口，返回个性化商户候选。"""
    start = time.perf_counter()
    normalized_user_id = user_id.strip()
    if not normalized_user_id:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="recommend_shops", ok=False, detail="缺少用户编号 userId"),
        )

    params: dict[str, object] = {"userId": normalized_user_id}
    if limit is not None:
        params["limit"] = limit
    if longitude is not None:
        params["longitude"] = longitude
    if latitude is not None:
        params["latitude"] = latitude
    if radius_meters is not None:
        params["radiusMeters"] = radius_meters

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"{settings.backend_base_url}/api/recommendations/shops",
                params=params,
            )
            response.raise_for_status()
            payload = (
                json.loads(response.content.decode("utf-8"))
                if hasattr(response, "content")
                else response.json()
            )

        shops = payload.get("data", [])
        items = [
            {
                "shopId": shop["shopId"],
                "shopName": shop["shopName"],
                "typeId": shop["typeId"],
                "avgPrice": shop["avgPrice"],
                "address": shop["address"],
                "reason": shop["reason"],
                "triggerShopIds": shop.get("triggerShopIds", []),
                "triggerShopNames": shop.get("triggerShopNames", []),
                "score": shop.get("score"),
                "distanceMeters": shop.get("distanceMeters"),
                "coordinateSystem": shop.get("coordinateSystem"),
                "district": shop.get("district"),
                "anchorPlaceId": shop.get("anchorPlaceId"),
                "anchorPlaceName": shop.get("anchorPlaceName"),
                "dataNature": shop.get("dataNature"),
            }
            for shop in shops
        ]
        has_localized_demo = any(
            item.get("dataNature") == "localized_demo" for item in items
        )
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="recommend_shops",
                ok=True,
                detail={
                    "userId": normalized_user_id,
                    "count": len(items),
                    "shops": items,
                    "dataNotice": (
                        "推荐分数和触发关系来自保留的Yelp用户行为；商户名称与北京位置是演示投影，"
                        "不代表真实登记商家，也不可用于现实导航。"
                        if has_localized_demo
                        else None
                    ),
                },
            ),
        )
    except Exception:
        logger.exception("Agent 商户推荐工具调用失败")
        return _finish_tool_trace(
            start,
            ToolTrace(tool="recommend_shops", ok=False, detail="商户推荐服务暂时不可用"),
        )


async def search_reviews_tool(
    query: str,
    shop_id: int | None = None,
) -> ToolTrace:
    """检索评论证据，供 Agent 回答环境、体验、预算和适合人群等问题。"""
    start = time.perf_counter()
    normalized_query = query.strip()
    if not normalized_query:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_reviews", ok=False, detail="缺少评论检索问题 query"),
        )
    if shop_id is not None and shop_id <= 0:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_reviews", ok=False, detail="shopId 必须是正整数"),
        )

    try:
        retrieval_start = time.perf_counter()
        if shop_id is None:
            reviews = await asyncio.to_thread(
                query_reviews,
                normalized_query,
                DEFAULT_TOP_K,
            )
        else:
            reviews = await asyncio.to_thread(
                query_reviews,
                normalized_query,
                DEFAULT_TOP_K,
                shop_id=shop_id,
            )
        retrieval_duration_ms = round((time.perf_counter() - retrieval_start) * 1000, 2)
    except Exception:
        logger.exception("Agent 评论检索工具调用失败")
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_reviews", ok=False, detail="评论检索服务暂时不可用"),
        )

    retrieval_trace = build_review_retrieval_trace(
        normalized_query,
        reviews,
        limit=DEFAULT_TOP_K,
        duration_ms=retrieval_duration_ms,
        collection_name=settings.rag_collection_name,
        filters={"shopId": shop_id} if shop_id is not None else None,
    )
    return _finish_tool_trace(
        start,
        ToolTrace(
            tool="search_reviews",
            ok=True,
            detail={
                "query": normalized_query,
                "shopId": shop_id,
                "count": len(reviews),
                "reviews": reviews,
                "evidenceNotice": (
                    "Yelp评论保留来源原文/译文，只能作为演示推荐证据，"
                    "不证明北京演示商户的现实经营事实。"
                    if any(review.get("source") == "yelp" for review in reviews)
                    else None
                ),
                "retrievalTrace": retrieval_trace.model_dump(by_alias=True),
            },
        ),
    )


async def search_shop_reviews_tool(query: str, shop_name: str) -> ToolTrace:
    """Resolve a named merchant and retrieve only that merchant's reviews."""

    start = time.perf_counter()
    normalized_query = query.strip()
    normalized_shop_name = shop_name.strip()
    if not normalized_query:
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_shop_reviews",
                ok=False,
                detail="缺少评论检索问题 query",
            ),
        )
    if not normalized_shop_name:
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_shop_reviews",
                ok=False,
                detail="缺少商户名称 shopName",
            ),
        )

    shop_trace = await search_shops(name=normalized_shop_name)
    if not shop_trace.ok or not isinstance(shop_trace.detail, dict):
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_shop_reviews",
                ok=False,
                detail={
                    "query": normalized_query,
                    "requestedShopName": normalized_shop_name,
                    "resolutionStatus": "shop_search_failed",
                    "message": "商户查询服务暂时不可用",
                    "candidates": [],
                },
            ),
        )

    shops = shop_trace.detail.get("shops", [])
    if not isinstance(shops, list):
        shops = []
    candidates = [shop for shop in shops if isinstance(shop, dict)]
    resolved_shop, resolution_error = resolve_unique_shop(
        candidates,
        normalized_shop_name,
    )
    if resolved_shop is None:
        message = (
            "没有找到这个商户，请用户确认店名"
            if resolution_error == "not_found"
            else "找到多个同名或相似商户，请用户从候选中确认具体门店"
        )
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_shop_reviews",
                ok=False,
                detail={
                    "query": normalized_query,
                    "requestedShopName": normalized_shop_name,
                    "resolutionStatus": resolution_error,
                    "message": message,
                    "candidates": candidates,
                },
            ),
        )

    try:
        resolved_shop_id = int(resolved_shop["id"])
    except (KeyError, TypeError, ValueError):
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_shop_reviews",
                ok=False,
                detail={
                    "query": normalized_query,
                    "requestedShopName": normalized_shop_name,
                    "resolutionStatus": "invalid_shop_id",
                    "message": "商户数据缺少合法编号，暂时无法检索评论",
                    "candidates": candidates,
                },
            ),
        )

    try:
        result = await asyncio.to_thread(
            query_knowledge,
            normalized_query,
            [REVIEW_SOURCE_NAME],
            DEFAULT_TOP_K,
            shop_id=resolved_shop_id,
        )
    except Exception:
        logger.exception("Agent 点名商户评论统一检索失败")
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_shop_reviews",
                ok=False,
                detail={
                    "query": normalized_query,
                    "requestedShopName": normalized_shop_name,
                    "resolutionStatus": "resolved",
                    "resolvedShop": resolved_shop,
                    "message": "该商户的评论检索服务暂时不可用",
                },
            ),
        )

    knowledge_result = result.model_dump(by_alias=True)
    return _finish_tool_trace(
        start,
        ToolTrace(
            tool="search_shop_reviews",
            ok=True,
            detail={
                "query": normalized_query,
                "requestedShopName": normalized_shop_name,
                "resolutionStatus": "resolved",
                "resolvedShop": resolved_shop,
                "count": len(result.legacy_reviews),
                "reviews": result.legacy_reviews,
                "evidenceNotice": (
                    "Yelp评论保留来源原文/译文，只能作为演示推荐证据，"
                    "不证明北京演示商户的现实经营事实。"
                    if any(
                        review.get("source") == "yelp"
                        for review in result.legacy_reviews
                    )
                    else None
                ),
                "citations": [
                    citation.model_dump(by_alias=True)
                    for citation in result.citations
                ],
                "evidenceNotice": (
                    "检索结果中的Yelp评论保留来源原文/译文，只能作为演示推荐证据，"
                    "不证明北京演示商户的现实经营事实。"
                    if "reviews" in result.trace.selected_sources
                    else None
                ),
                "retrievalTrace": result.trace.model_dump(by_alias=True),
            },
            knowledgeResult=knowledge_result,
        ),
    )


async def search_knowledge_tool(
    query: str,
    sources: list[str] | None = None,
    shop_id: int | None = None,
) -> ToolTrace:
    """统一知识检索工具，封装 reviews / merchant_docs / policy_docs 多知识源检索。"""

    start = time.perf_counter()
    normalized_query = query.strip()
    if not normalized_query:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_knowledge", ok=False, detail="缺少知识检索问题 query"),
        )

    try:
        if shop_id is None:
            result = await asyncio.to_thread(
                query_knowledge,
                normalized_query,
                sources,
                DEFAULT_TOP_K,
            )
        else:
            result = await asyncio.to_thread(
                query_knowledge,
                normalized_query,
                sources,
                DEFAULT_TOP_K,
                shop_id=shop_id,
            )
    except ValueError as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_knowledge", ok=False, detail=str(exc)),
        )
    except Exception:
        logger.exception("Agent 统一知识检索工具调用失败")
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_knowledge", ok=False, detail="知识检索服务暂时不可用"),
        )

    knowledge_result = result.model_dump(by_alias=True)
    return _finish_tool_trace(
        start,
        ToolTrace(
            tool="search_knowledge",
            ok=True,
            detail={
                "query": normalized_query,
                "sources": result.trace.selected_sources,
                **({"shopId": shop_id} if shop_id is not None else {}),
                "count": len(result.chunks),
                "chunks": [
                    chunk.model_dump(by_alias=True)
                    for chunk in result.chunks
                ],
                "citations": [
                    citation.model_dump(by_alias=True)
                    for citation in result.citations
                ],
                "retrievalTrace": result.trace.model_dump(by_alias=True),
            },
            knowledgeResult=knowledge_result,
        ),
    )


async def search_places_tool(
    query: str | None = None,
    district: str | None = None,
    kind: str | None = None,
    park_type: str | None = None,
    park_level: str | None = None,
    facility_name: str | None = None,
    facility_status: str | None = None,
    signage_status: str | None = None,
    limit: int = 10,
) -> ToolTrace:
    """Search the packaged, reviewed place snapshot with structured filters."""

    start = time.perf_counter()
    normalized = {
        "query": query.strip() if query and query.strip() else None,
        "district": district.strip() if district and district.strip() else None,
        "kind": kind.strip() if kind and kind.strip() else None,
        "park_type": park_type.strip() if park_type and park_type.strip() else None,
        "park_level": park_level.strip() if park_level and park_level.strip() else None,
        "facility_name": facility_name.strip() if facility_name and facility_name.strip() else None,
        "facility_status": facility_status.strip() if facility_status and facility_status.strip() else None,
        "signage_status": signage_status.strip() if signage_status and signage_status.strip() else None,
        "limit": limit,
    }
    try:
        catalog = get_place_catalog()
        result = await asyncio.to_thread(catalog.search, **normalized)
    except ValueError as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_places", ok=False, detail=str(exc)),
        )
    except Exception:
        logger.exception("Agent 地点目录搜索工具调用失败")
        return _finish_tool_trace(
            start,
            ToolTrace(tool="search_places", ok=False, detail="地点目录暂时不可用"),
        )

    detail = result.to_dict()
    detail.update(
        catalogVersion=catalog.metadata["catalogVersion"],
        scope=catalog.metadata["scope"],
        scopeNotice="当前演示目录包含100个北京公园和190个北京地区备案开放博物馆快照，不代表实时全量名录。",
        answerConstraint="回答数量时必须说“当前演示目录中找到N个”，不得表述为某区或北京市共有N个。",
    )
    return _finish_tool_trace(
        start,
        ToolTrace(tool="search_places", ok=True, detail=detail),
    )


async def get_place_detail_tool(place_id: str) -> ToolTrace:
    """Read one place from the packaged snapshot by its stable catalog ID."""

    start = time.perf_counter()
    normalized_place_id = place_id.strip()
    try:
        catalog = get_place_catalog()
        record = await asyncio.to_thread(catalog.get, normalized_place_id)
    except Exception:
        logger.exception("Agent 地点详情工具调用失败")
        return _finish_tool_trace(
            start,
            ToolTrace(tool="get_place_detail", ok=False, detail="地点目录暂时不可用"),
        )

    if record is None:
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="get_place_detail",
                ok=False,
                detail={
                    "placeId": normalized_place_id,
                    "message": "没有找到该地点编号，请先调用 search_places 获取候选。",
                },
            ),
        )
    return _finish_tool_trace(
        start,
        ToolTrace(
            tool="get_place_detail",
            ok=True,
            detail={
                "catalogVersion": catalog.metadata["catalogVersion"],
                "scope": catalog.metadata["scope"],
                "scopeNotice": "该详情来自北京地点演示快照，不是实时开放状态。",
                "answerConstraint": "只陈述返回的结构化事实，不推断实时开放、拥挤程度或适合人群。",
                "place": record.to_detail(),
            },
        ),
    )


# ① 工具“说明书”：交给大模型读，让它知道有哪些工具、什么时候用、参数怎么填。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "检索手机、笔记本或耳机。Elasticsearch、BM25 与 Qdrant 可用通道以固定 k=60 做 RRF，随后基于 Java/MySQL 权威事实对前 50 名执行硬约束淘汰、未知后置和证据型规则重排，最多返回 20 个候选。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string", "enum": ["手机", "笔记本", "耳机"]},
                    "brand": {"type": "string"},
                    "minPriceMinor": {"type": "integer"},
                    "maxPriceMinor": {"type": "integer"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "requirements": {
                        "type": "array",
                        "description": "Optional hard/soft requirements for Top-50 fact reranking.",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "enum": SHOPPING_REQUIREMENT_KEYS,
                                    "description": (
                                        "Supported fact keys. Controlled used-phone values: "
                                        + USED_PHONE_REQUIREMENT_DESCRIPTION
                                    ),
                                },
                                "operator": {"type": "string", "enum": ["eq", "lte", "gte", "in", "not_in"]},
                                "value": {},
                                "unit": {"type": "string", "enum": SHOPPING_REQUIREMENT_UNITS},
                                "priority": {"type": "string", "enum": ["hard", "soft"]},
                                "source": {"type": "string"},
                            },
                            "required": ["key", "operator", "value", "unit", "priority", "source"],
                        },
                    },
                },
                "required": ["query", "category"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "按 search_products 返回的商品 ID 批量读取权威商品快照和字段证据，最多 10 条。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "productIds": {"type": "array", "items": {"type": "integer"}, "maxItems": 10}
                },
                "required": ["productIds"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_products",
            "description": "对最多 5 个已召回商品执行确定性硬约束判定与软偏好评分，生成最多 3 个决选及字段级证据。缺失规格视为 unknown。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "productIds": {"type": "array", "items": {"type": "integer"}, "maxItems": 5},
                    "category": {"type": "string", "enum": ["phone", "laptop", "headphones"]},
                    "requirements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "enum": SHOPPING_REQUIREMENT_KEYS,
                                    "description": (
                                        "Supported fact keys. Controlled used-phone values: "
                                        + USED_PHONE_REQUIREMENT_DESCRIPTION
                                    ),
                                },
                                "operator": {"type": "string", "enum": ["eq", "lte", "gte", "in", "not_in"]},
                                "value": {},
                                "unit": {"type": "string", "enum": SHOPPING_REQUIREMENT_UNITS},
                                "priority": {"type": "string", "enum": ["hard", "soft"]},
                                "source": {"type": "string"},
                            },
                            "required": ["key", "operator", "value", "unit", "priority", "source"],
                        },
                    },
                },
                "required": ["productIds", "category", "requirements"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rerank_products_in_scope",
            "description": (
                "仅在服务端已验证的上一轮 CandidateScope 的 rankedItemIds 范围内，"
                "按商品标题/公开文本与排序意图的相关性确定性重排。"
                "绝不重新全库搜索，绝不把标题相关性当成真实能力结论。"
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "scopeId": {"type": "string"},
                    "productIds": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "maxItems": 20,
                    },
                    "rankingIntent": {
                        "type": "string",
                        "enum": ["camera_title_claim", "gaming_title_claim"],
                    },
                    "category": {"type": "string", "enum": ["phone", "laptop", "headphones"]},
                    "requirements": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "key": {
                                    "type": "string",
                                    "enum": SHOPPING_REQUIREMENT_KEYS,
                                    "description": (
                                        "Supported fact keys. Controlled used-phone values: "
                                        + USED_PHONE_REQUIREMENT_DESCRIPTION
                                    ),
                                },
                                "operator": {"type": "string", "enum": ["eq", "lte", "gte", "in", "not_in"]},
                                "value": {},
                                "unit": {"type": "string", "enum": SHOPPING_REQUIREMENT_UNITS},
                                "priority": {"type": "string", "enum": ["hard", "soft"]},
                                "source": {"type": "string"},
                            },
                            "required": ["key", "operator", "value", "unit", "priority", "source"],
                        },
                    },
                },
                "required": ["scopeId", "productIds", "rankingIntent", "category", "requirements"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_places",
            "description": (
                "搜索北京地点演示目录，当前包含100个公园候选和190个备案开放博物馆快照。"
                "用户询问公园或博物馆名称、行政区、地址、电话，或者公园类型、等级、无障碍设施记录时调用。"
                "这些是结构化官方事实；安静、拥挤、是否适合老人等主观体验不在本目录证据范围内。"
                "返回稳定placeId；需要电话、主管单位和完整设施明细时，再调用get_place_detail。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "可选，地点名称或名称片段，例如北京世界公园。",
                    },
                    "district": {
                        "type": "string",
                        "description": "可选，北京行政区，例如海淀区。",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["park", "museum"],
                        "description": "地点类型；park表示公园，museum表示博物馆。",
                    },
                    "parkType": {
                        "type": "string",
                        "enum": ["历史名园", "综合公园", "自然（类）公园", "专类公园", "生态公园", "社区公园", "游园"],
                        "description": "可选，官方公园类型。",
                    },
                    "parkLevel": {
                        "type": "string",
                        "enum": ["一级", "二级", "三级", "四级"],
                        "description": "可选，官方公园等级。",
                    },
                    "facilityName": {
                        "type": "string",
                        "description": "可选，无障碍设施元素名称，例如无障碍厕所/厕位。",
                    },
                    "facilityStatus": {
                        "type": "string",
                        "description": "可选，设施元素状态，例如标准或不涉及；必须与facilityName一起使用。",
                    },
                    "signageStatus": {
                        "type": "string",
                        "description": "可选，无障碍标识状态；与设施元素状态分开，必须与facilityName一起使用。",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "最多返回多少个候选，默认10。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_place_detail",
            "description": (
                "根据search_places返回的稳定placeId读取地点详情。公园可返回官方类型、等级、"
                "主管单位、BD-09坐标和无障碍设施；博物馆可返回地址、电话和邮编。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "placeId": {
                        "type": "string",
                        "description": "地点编号，例如beijing-park-433；必须来自search_places结果。",
                    }
                },
                "required": ["placeId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_shops",
            "description": (
                "查询北京化演示商户，返回 ID、名称、地址、人均价格、坐标和数据性质。"
                "用户明确询问有哪些商户、某分类列表或指定店名时调用；"
                "指定name可先解析商户ID，供详情或评论过滤使用。"
                "如果用户询问某个真实北京地点附近的商户，传入search_places目录中的nearPlaceId，"
                "工具会在统一BD-09坐标系下按距离筛选。"
                "这些商户保留Yelp评论与行为关系，但名称和位置是北京化演示投影，不是真实登记商家。"
                "如果用户问哪家更适合某种体验，应改用评论证据能力。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "typeId": {
                        "type": "integer",
                        "description": "分类编号：1=美食 2=咖啡 3=电影 4=酒店 5=健身。不确定分类就不要传这个参数，表示查全部商户。",
                    },
                    "name": {
                        "type": "string",
                        "description": "可选，按北京化商户名称或原Yelp名称别名包含匹配，用于先获得商户ID。",
                    },
                    "nearPlaceId": {
                        "type": "string",
                        "description": "可选，真实北京地点的稳定ID，例如beijing-park-178；传入后按该地点坐标查询附近商户。",
                    },
                    "radiusMeters": {
                        "type": "number",
                        "description": "可选，附近搜索半径（米），默认2000。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "可选，最多返回数量，默认10，最大50。",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_shop_detail",
            "description": "根据商户 ID 查询单个商户详情，包括地址、人均价格和联系电话。当用户追问某家店的详情、地址或电话时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "shopId": {
                        "type": "integer",
                        "description": "商户编号。通常先从 search_shops 返回结果的 id 字段获得。",
                    }
                },
                "required": ["shopId"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_shop_reviews",
            "description": (
                "检索用户明确点名的某家商户的真实评论。"
                "当问题同时包含具体商户名称和口味、环境、排队、设施、适合办公等体验诉求时使用。"
                "本工具会自行把店名解析为shopId并限定该店评论；不要猜测shopId。"
                "如果返回多个候选，必须请用户确认具体门店，不能擅自选择。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "保留用户体验条件的完整问题。",
                    },
                    "shopName": {
                        "type": "string",
                        "description": "用户明确提到的商户名称，例如 Red Hook Coffee & Tea。",
                    },
                },
                "required": ["query", "shopName"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge",
            "description": (
                "统一知识检索工具，可检索真实评论 reviews、商户资料 merchant_docs、"
                "平台规则 policy_docs。默认由后端 Router 自动判断 sources。"
                "当问题同时涉及商户事实和真实体验，例如“有没有 WiFi 且实际适合办公”时适用。"
                "当用户询问口味、环境、排队、预算、营业时间、设施、适合人群或消费体验时，"
                "优先使用 reviews 作为证据来源。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "保留用户条件的完整检索问题。",
                    },
                    "sources": {
                        "type": "array",
                        "description": "可选，显式指定知识源；不传则由后端 Router 自动判断。",
                        "items": {
                            "type": "string",
                            "enum": ["reviews", "merchant_docs", "policy_docs"],
                        },
                    },
                    "shopId": {
                        "type": "integer",
                        "description": "可选，仅用于reviews来源的商户硬过滤；必须来自已解析的真实商户ID。",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_shop_types",
            "description": "查询当前有哪些商户分类。当用户问“有哪些分类/类型/品类”时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "可选，按关键词过滤分类名，例如“咖啡”。",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recommend_shops",
            "description": (
                "根据用户历史行为推荐商户。当用户明确要求“根据我的历史/浏览记录/行为/喜好”"
                "做个性化推荐，且提供了用户编号 userId（例如 demo-user-1）时调用。"
                "如果用户没有提供 userId，不要调用本工具，应先请用户提供用户编号。"
                "本工具返回推荐商户、推荐原因、触发推荐的历史商户、推荐分和可选距离。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "userId": {
                        "type": "string",
                        "description": "用户编号，例如 demo-user-1。必须来自用户问题或会话上下文。",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少家商户，默认由后端决定。",
                    },
                    "longitude": {
                        "type": "number",
                        "description": "可选，用户当前位置经度；和 latitude 一起提供时可用于距离排序。",
                    },
                    "latitude": {
                        "type": "number",
                        "description": "可选，用户当前位置纬度；和 longitude 一起提供时可用于距离排序。",
                    },
                    "radiusMeters": {
                        "type": "number",
                        "description": "可选，推荐半径，单位米。",
                    },
                },
                "required": ["userId"],
            },
        },
    },
]

# ② 派单台：模型“开单”说要调某个工具后，由这里真正去执行对应的 Python 函数。
async def call_tool(
    name: str,
    arguments: dict,
    *,
    execution_context: "ToolExecutionContext | None" = None,
) -> ToolTrace:
    start = time.perf_counter()
    # Keep V2 identity outside model arguments and ToolTrace.  This is the
    # last internal dispatcher before the concrete business implementations.
    from .tool_transport import (
        execution_audit_identity,
        validate_execution_context,
    )

    checked_context = validate_execution_context(
        name,
        arguments,
        execution_context,
    )
    audit_identity = execution_audit_identity(checked_context)
    if audit_identity is not None:
        logger.info(
            "durable_tool_dispatch tool=%s execution_id=%s logical_slot=%s fence=%s input_hash=%s",
            name,
            audit_identity["executionId"],
            audit_identity["logicalSlotKey"],
            audit_identity["fence"],
            audit_identity["inputHash"],
        )
    if not isinstance(arguments, dict):
        return _finish_tool_trace(
            start,
            ToolTrace(tool=name, ok=False, detail="tool arguments must be an object"),
        )
    # Transaction tools are intentionally absent from the model-visible menu.
    # If a model or caller guesses a reserved name, fail closed here without
    # constructing Redis/backend transport or attempting to mint authority.
    from .transaction_agent.capabilities import (
        TRANSACTION_PREVIEW_TOOL_NAMES,
        TRANSACTION_WRITE_TOOL_NAMES,
    )
    if name in (TRANSACTION_PREVIEW_TOOL_NAMES | TRANSACTION_WRITE_TOOL_NAMES):
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool=name,
                ok=False,
                detail={"code": "transaction_auth_provider_unavailable"},
            ),
        )
    allowed_keys = _ECOMMERCE_TOOL_ARGUMENT_KEYS.get(name)
    if allowed_keys is not None:
        extra_keys = sorted(set(arguments) - allowed_keys)
        if extra_keys:
            return _finish_tool_trace(
                start,
                ToolTrace(
                    tool=name,
                    ok=False,
                    detail={
                        "code": "unexpected_tool_arguments",
                        "keys": extra_keys,
                    },
                ),
            )
    if name == "search_products":
        query, category = arguments.get("query"), arguments.get("category")
        limit = arguments.get("limit", 20)
        if not isinstance(query, str) or not query.strip():
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="query 必须是非空字符串"))
        if category not in {"手机", "笔记本", "耳机"}:
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="category 不受支持"))
        if type(limit) is not int or not 1 <= limit <= 20:
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="limit 必须在 1 到 20 之间"))
        return await search_products_tool(
            query, category, arguments.get("brand"),
            arguments.get("minPriceMinor"), arguments.get("maxPriceMinor"), limit,
            arguments.get("requirements"),
        )
    if name == "get_product_details":
        product_ids = arguments.get("productIds")
        if not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 10 or not all(
            type(item) is int and item > 0 for item in product_ids
        ):
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="productIds 必须包含 1 到 10 个正整数"))
        return await get_product_details_tool(product_ids)
    if name == "compare_products":
        product_ids = arguments.get("productIds")
        requirements = arguments.get("requirements")
        if not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 5 or not all(
            type(item) is int and item > 0 for item in product_ids
        ):
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="productIds 必须包含 1 到 5 个正整数"))
        if arguments.get("category") not in {"phone", "laptop", "headphones"}:
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="category 不受支持"))
        if not isinstance(requirements, list):
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="requirements 必须是数组"))
        return await compare_products_tool(product_ids, arguments["category"], requirements)
    if name == "rerank_products_in_scope":
        scope_id = arguments.get("scopeId")
        product_ids = arguments.get("productIds")
        ranking_intent = arguments.get("rankingIntent")
        if not isinstance(scope_id, str) or not scope_id.strip():
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="scopeId 必须是非空字符串"))
        if not isinstance(product_ids, list) or not 1 <= len(product_ids) <= 20 or not all(
            type(item) is int and item > 0 for item in product_ids
        ):
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="productIds 必须包含 1 到 20 个正整数"))
        if ranking_intent not in {"camera_title_claim", "gaming_title_claim"}:
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="rankingIntent 不受支持"))
        if arguments.get("category") not in {"phone", "laptop", "headphones"}:
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="category 不受支持"))
        requirements = arguments.get("requirements")
        if not isinstance(requirements, list):
            return _finish_tool_trace(start, ToolTrace(tool=name, ok=False, detail="requirements 必须是数组"))
        return await rerank_products_in_scope_tool(
            scope_id,
            product_ids,
            ranking_intent,
            arguments["category"],
            requirements,
        )
    if name == "search_places":
        string_arguments = {
            "query": "query",
            "district": "district",
            "kind": "kind",
            "parkType": "parkType",
            "parkLevel": "parkLevel",
            "facilityName": "facilityName",
            "facilityStatus": "facilityStatus",
            "signageStatus": "signageStatus",
        }
        for argument_name in string_arguments:
            value = arguments.get(argument_name)
            if value is not None and not isinstance(value, str):
                return _finish_tool_trace(
                    start,
                    ToolTrace(tool=name, ok=False, detail=f"{argument_name} 必须是字符串"),
                )
        limit = arguments.get("limit", 10)
        if type(limit) is not int or not 1 <= limit <= 50:
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="limit 必须是1到50之间的整数"),
            )
        return await search_places_tool(
            query=arguments.get("query"),
            district=arguments.get("district"),
            kind=arguments.get("kind"),
            park_type=arguments.get("parkType"),
            park_level=arguments.get("parkLevel"),
            facility_name=arguments.get("facilityName"),
            facility_status=arguments.get("facilityStatus"),
            signage_status=arguments.get("signageStatus"),
            limit=limit,
        )
    if name == "get_place_detail":
        place_id = arguments.get("placeId")
        if not isinstance(place_id, str) or not place_id.strip():
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="缺少合法的字符串参数 placeId"),
            )
        return await get_place_detail_tool(place_id)
    if name == "search_shops":
        type_id = arguments.get("typeId")
        shop_name = arguments.get("name")
        near_place_id = arguments.get("nearPlaceId")
        radius_meters = arguments.get("radiusMeters")
        limit = arguments.get("limit")
        if type_id is not None and not isinstance(type_id, int):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="typeId 必须是整数"),
            )
        if shop_name is not None and not isinstance(shop_name, str):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="name 必须是字符串"),
            )
        if near_place_id is not None and not isinstance(near_place_id, str):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="nearPlaceId 必须是字符串"),
            )
        if radius_meters is not None and (
            not isinstance(radius_meters, (int, float)) or radius_meters <= 0
        ):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="radiusMeters 必须是正数"),
            )
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="limit 必须是正整数"),
            )
        return await search_shops(
            type_id,
            shop_name,
            near_place_id,
            float(radius_meters) if radius_meters is not None else None,
            limit,
        )
    if name == "get_shop_detail":
        shop_id = arguments.get("shopId")
        if not isinstance(shop_id, int):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="缺少合法的整数参数 shopId"),
            )
        return await get_shop_detail(shop_id)
    if name == "list_shop_types":
        return await list_shop_types(arguments.get("keyword"))
    if name == "search_reviews":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="缺少合法的字符串参数 query"),
            )
        shop_id = arguments.get("shopId")
        if shop_id is not None and (
            not isinstance(shop_id, int) or shop_id <= 0
        ):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="shopId 必须是正整数"),
            )
        return await search_reviews_tool(query, shop_id)
    if name == "search_shop_reviews":
        query = arguments.get("query")
        shop_name = arguments.get("shopName")
        if not isinstance(query, str) or not query.strip():
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="缺少合法的字符串参数 query"),
            )
        if not isinstance(shop_name, str) or not shop_name.strip():
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="缺少合法的字符串参数 shopName"),
            )
        return await search_shop_reviews_tool(query, shop_name)
    if name == "search_knowledge":
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="缺少合法的字符串参数 query"),
            )
        sources = arguments.get("sources")
        if sources is not None and not (
            isinstance(sources, list)
            and all(isinstance(source, str) for source in sources)
        ):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="sources 必须是字符串数组"),
            )
        shop_id = arguments.get("shopId")
        if shop_id is not None and (
            not isinstance(shop_id, int) or isinstance(shop_id, bool) or shop_id <= 0
        ):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="shopId 必须是正整数"),
            )
        if shop_id is None:
            return await search_knowledge_tool(query, sources)
        return await search_knowledge_tool(query, sources, shop_id)
    if name == "recommend_shops":
        user_id = arguments.get("userId")
        if not isinstance(user_id, str) or not user_id.strip():
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="缺少合法的字符串参数 userId"),
            )
        limit = arguments.get("limit")
        longitude = arguments.get("longitude")
        latitude = arguments.get("latitude")
        radius_meters = arguments.get("radiusMeters")
        if limit is not None and not isinstance(limit, int):
            return _finish_tool_trace(
                start,
                ToolTrace(tool=name, ok=False, detail="limit 必须是整数"),
            )
        for numeric_name, numeric_value in (
            ("longitude", longitude),
            ("latitude", latitude),
            ("radiusMeters", radius_meters),
        ):
            if numeric_value is not None and not isinstance(numeric_value, (int, float)):
                return _finish_tool_trace(
                    start,
                    ToolTrace(tool=name, ok=False, detail=f"{numeric_name} 必须是数字"),
                )
        return await recommend_shops(
            user_id,
            limit,
            longitude,
            latitude,
            radius_meters,
        )
    return _finish_tool_trace(
        start,
        ToolTrace(tool=name, ok=False, detail=f"未知工具：{name}"),
    )
