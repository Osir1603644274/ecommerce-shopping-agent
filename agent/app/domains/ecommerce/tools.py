import asyncio
import hashlib
import logging
import math
import threading
import time
import weakref
from typing import Any

import httpx

from ...schemas import ToolTrace
from ...settings import settings
from .models import (
    ShoppingRequirement,
    bm25_rank,
    compare_product_details,
    expand_product_query,
    reciprocal_rank_fusion,
    rule_rerank_candidates,
    structured_requirement_rank,
    product_embedding_text,
)
from .ranking_contract import (
    SCOPE_RERANK_CONTRACT_VERSION,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
    TwoStageRankingContractError,
    normalize_search_products_detail,
    normalize_scope_rerank_detail,
)
from .synthetic_prices import apply_synthetic_prices
from .title_relevance import rerank_titles

logger = logging.getLogger(__name__)
_product_embedding_lock = threading.Lock()
_product_embedding_cache: dict[tuple[str, str], list[float]] = {}
_product_catalog_embedding_cache: dict[
    tuple[str, str], tuple[list[int], list[list[float]]]
] = {}
_product_http_clients: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, httpx.AsyncClient
] = weakref.WeakKeyDictionary()


def _product_http_client() -> httpx.AsyncClient:
    """Keep one backend connection pool per event loop."""
    loop = asyncio.get_running_loop()
    client = _product_http_clients.get(loop)
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(timeout=10.0)
        _product_http_clients[loop] = client
    return client


async def close_product_search_clients() -> None:
    """Close connection pools owned by this module during app shutdown."""
    clients = list(_product_http_clients.values())
    _product_http_clients.clear()
    for client in clients:
        if not getattr(client, "is_closed", False) and hasattr(client, "aclose"):
            await client.aclose()


async def warm_local_product_vector_cache() -> int:
    """Precompute the small catalog embedding matrix before serving queries."""

    response = await _product_http_client().get(
        f"{settings.backend_base_url}/api/products",
        params={"category": "手机", "limit": 1500,
                **({"catalogVersion": settings.product_legacy_catalog_version} if settings.product_legacy_catalog_version else {})},
    )
    response.raise_for_status()
    products = response.json().get("data", [])
    if not isinstance(products, list) or not products:
        raise RuntimeError("phone catalog unavailable for local vector warmup")
    await asyncio.to_thread(_local_product_vector_rank, "手机", products)
    return len(products)


def _product_query_embedding(query: str) -> list[float]:
    """Coalesce concurrent identical embeddings and keep a bounded warm cache."""
    from ...rag import get_embedding_model

    key = (settings.rag_embedding_model, query)
    with _product_embedding_lock:
        cached = _product_embedding_cache.get(key)
        if cached is not None:
            return list(cached)
        vector = list(get_embedding_model().embed([query]))[0].tolist()
        if len(_product_embedding_cache) >= 256:
            _product_embedding_cache.pop(next(iter(_product_embedding_cache)))
        _product_embedding_cache[key] = list(vector)
        return list(vector)


def _local_product_vector_rank(
    query: str,
    products: list[dict[str, Any]],
) -> list[int]:
    """Cosine-rank the current authoritative catalog without Qdrant."""

    from ...rag import get_embedding_model

    fingerprint = hashlib.sha256()
    for product in products:
        fingerprint.update(str(int(product["id"])).encode("ascii"))
        fingerprint.update(b"\0")
        fingerprint.update(product_embedding_text(product).encode("utf-8"))
        fingerprint.update(b"\n")
    key = (settings.rag_embedding_model, fingerprint.hexdigest())
    with _product_embedding_lock:
        cached = _product_catalog_embedding_cache.get(key)
        if cached is None:
            model = get_embedding_model()
            ids = [int(product["id"]) for product in products]
            vectors = [
                list(vector.tolist())
                for vector in model.embed([
                    product_embedding_text(product) for product in products
                ])
            ]
            if len(_product_catalog_embedding_cache) >= 4:
                _product_catalog_embedding_cache.pop(
                    next(iter(_product_catalog_embedding_cache))
                )
            _product_catalog_embedding_cache[key] = (ids, vectors)
        else:
            ids, vectors = cached
    query_vector = _product_query_embedding(query)
    query_norm = math.sqrt(sum(value * value for value in query_vector))
    scored: list[tuple[int, float]] = []
    for product_id, vector in zip(ids, vectors, strict=True):
        denominator = query_norm * math.sqrt(sum(value * value for value in vector))
        score = (
            sum(left * right for left, right in zip(query_vector, vector, strict=True))
            / denominator
            if denominator else 0.0
        )
        scored.append((product_id, score))
    return [
        product_id
        for product_id, _score in sorted(
            scored,
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _finish_tool_trace(start: float, trace: ToolTrace) -> ToolTrace:
    return trace.model_copy(
        update={"duration_ms": round((time.perf_counter() - start) * 1000, 2)}
    )


async def search_products_tool(
    query: str,
    category: str,
    brand: str | None = None,
    min_price_minor: int | None = None,
    max_price_minor: int | None = None,
    limit: int = 20,
    requirements: list[dict[str, Any]] | None = None,
) -> ToolTrace:
    start = time.perf_counter()
    if not settings.ecommerce_guide_enabled:
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_products",
                ok=False,
                detail={
                    "code": "ecommerce_disabled",
                    "message": "商品导购当前未启用。",
                },
            ),
        )
    try:
        category_codes = {"手机": "phone", "笔记本": "laptop", "耳机": "headphones"}
        category_code = category_codes.get(category, category)
        parsed_requirements = [
            ShoppingRequirement.model_validate(item) for item in (requirements or [])
        ]
        existing_keys = {item.key for item in parsed_requirements}
        if brand and "brand" not in existing_keys:
            parsed_requirements.append(ShoppingRequirement(
                key="brand", operator="eq", value=brand, unit="text",
                priority="hard", source="search_filter:brand",
            ))
        if min_price_minor is not None and "price_minor" not in existing_keys:
            parsed_requirements.append(ShoppingRequirement(
                key="price_minor", operator="gte", value=min_price_minor,
                unit="CNY_MINOR", priority="hard", source="search_filter:minPriceMinor",
            ))
        if max_price_minor is not None and "price_minor" not in existing_keys:
            parsed_requirements.append(ShoppingRequirement(
                key="price_minor", operator="lte", value=max_price_minor,
                unit="CNY_MINOR", priority="hard", source="search_filter:maxPriceMinor",
            ))

        synthetic_policy = settings.used_phone_synthetic_price_policy
        retrieval_query, expanded_terms = expand_product_query(query)
        synthetic_budget = (
            category_code == "phone" and synthetic_policy == "budget_and_ranking"
        )
        filter_params: dict[str, object] = {"category": category, "limit": 50}
        catalog_scope = ({"catalogVersion": settings.product_legacy_catalog_version}
                         if category_code == "phone" and settings.product_legacy_catalog_version else {})
        filter_params.update(catalog_scope)
        if brand:
            filter_params["brand"] = brand
        if min_price_minor is not None and not synthetic_budget:
            filter_params["minPriceMinor"] = min_price_minor
        if max_price_minor is not None and not synthetic_budget:
            filter_params["maxPriceMinor"] = max_price_minor

        async def backend_search() -> dict[str, Any]:
            response = await _product_http_client().get(
                f"{settings.backend_base_url}/api/products/retrieval",
                params={"query": retrieval_query, **filter_params},
            )
            response.raise_for_status()
            return response.json().get("data", {})

        async def fetch_catalog() -> list[dict[str, Any]]:
            response = await _product_http_client().get(
                f"{settings.backend_base_url}/api/products",
                params={"category": category, "limit": 1500, **catalog_scope},
            )
            response.raise_for_status()
            return response.json().get("data", [])

        def vector_search() -> list[int]:
            from ...rag import get_qdrant_client

            embedding = _product_query_embedding(retrieval_query)
            result = get_qdrant_client().query_points(
                collection_name=settings.product_collection_name,
                query=embedding,
                limit=50,
                with_payload=False,
            )
            return [int(point.id) for point in result.points]

        retrieval_mode = settings.product_retrieval_mode
        vector_backend = settings.product_vector_backend
        if retrieval_mode == "elasticsearch":
            backend_result = (await asyncio.gather(
                backend_search(), return_exceptions=True
            ))[0]
            catalog_result = None
            vector_result = None
        elif retrieval_mode in {"rrf", "hybrid"} and vector_backend == "qdrant":
            async def bounded_vector_search() -> list[int]:
                timeout = max(
                    float(settings.product_vector_timeout_seconds),
                    0.1,
                )
                return await asyncio.wait_for(
                    asyncio.to_thread(vector_search),
                    timeout=timeout,
                )

            backend_result, catalog_result, vector_result = await asyncio.gather(
                backend_search(),
                fetch_catalog(),
                bounded_vector_search(),
                return_exceptions=True,
            )
        else:
            backend_result, catalog_result = await asyncio.gather(
                backend_search(),
                fetch_catalog(),
                return_exceptions=True,
            )
            vector_result = None
            if retrieval_mode in {"rrf", "hybrid"} and vector_backend == "local":
                if isinstance(catalog_result, Exception):
                    vector_result = RuntimeError("catalog unavailable for local vector")
                else:
                    try:
                        vector_result = await asyncio.wait_for(
                            asyncio.to_thread(
                                _local_product_vector_rank,
                                retrieval_query,
                                catalog_result,
                            ),
                            timeout=max(
                                float(settings.product_vector_timeout_seconds),
                                0.1,
                            ),
                        )
                    except Exception as exc:
                        vector_result = exc
        channel_trace: dict[str, dict[str, Any]] = {}
        degraded: list[dict[str, str]] = []
        ranked_lists: list[list[int]] = []
        bm25_ids: list[int] = []
        structured_ids: list[int] = []

        if isinstance(backend_result, Exception):
            channel_trace["elasticsearch"] = {"status": "failed", "count": 0}
            degraded.append({"channel": "elasticsearch", "reason": type(backend_result).__name__})
        else:
            backend_products = backend_result.get("products", [])
            backend_ids = [int(item["id"]) for item in backend_products]
            backend_channel = backend_result.get("channel", "unknown")
            if backend_channel == "elasticsearch":
                channel_trace["elasticsearch"] = {
                    "status": "active",
                    "count": int(backend_result.get("recallCount", len(backend_ids))),
                    "analyzer": "standard",
                    "depth": 50,
                }
            else:
                channel_trace["elasticsearch"] = {"status": "failed", "count": 0}
                degraded.append({"channel": "elasticsearch", "reason": "java_mysql_fallback"})
                channel_trace["mysqlFallback"] = {"status": "active", "count": len(backend_ids)}
            if retrieval_mode == "elasticsearch":
                channel_trace["mysqlAuthority"] = {
                    "status": "active",
                    "count": int(backend_result.get("authoritativeEligibleCount", len(backend_ids))),
                    "factAuthority": backend_result.get("factAuthority", "mysql"),
                }
            if backend_ids:
                ranked_lists.append(backend_ids[:50])

        if retrieval_mode == "elasticsearch":
            channel_trace["bm25"] = {
                "status": "disabled", "count": 0,
                "reason": "production_elasticsearch_only",
            }
            channel_trace["structuredRequirements"] = {
                "status": "disabled", "count": 0,
                "reason": "hard_conditions_applied_after_mysql_fact_resolution",
            }
        elif isinstance(catalog_result, Exception):
            channel_trace["bm25"] = {"status": "failed", "count": 0}
            channel_trace["structuredRequirements"] = {
                "status": "failed", "count": 0,
            }
            degraded.append({"channel": "bm25", "reason": type(catalog_result).__name__})
        else:
            if category_code == "phone" and synthetic_policy != "disabled":
                catalog_result = apply_synthetic_prices(
                    catalog_result,
                    directory=settings.used_phone_synthetic_price_dir,
                    policy=synthetic_policy,
                )
            bm25_ids = bm25_rank(retrieval_query, catalog_result)[:50]
            channel_trace["bm25"] = {"status": "active", "count": len(bm25_ids)}
            if bm25_ids:
                ranked_lists.append(bm25_ids)
            structured_ids = structured_requirement_rank(
                category_code,
                catalog_result,
                parsed_requirements,
                bm25_ids,
            )
            channel_trace["structuredRequirements"] = {
                "status": "active" if structured_ids else "inactive",
                "count": len(structured_ids),
                "source": "public_catalog_summary_exact_facts",
                "unknownConflictPolicy": "retained_after_confirmed",
                "syntheticPricePolicy": synthetic_policy,
            }
            if structured_ids:
                ranked_lists.append(structured_ids)

        if retrieval_mode in {"bm25", "elasticsearch"}:
            channel_trace["qdrant"] = {
                "status": "disabled",
                "count": 0,
                "reason": f"retrieval_mode_{retrieval_mode}",
            }
        elif isinstance(vector_result, Exception):
            vector_channel = "localVector" if vector_backend == "local" else "qdrant"
            channel_trace[vector_channel] = {"status": "failed", "count": 0}
            if vector_backend == "local":
                channel_trace["qdrant"] = {
                    "status": "disabled", "count": 0,
                    "reason": "vector_backend_local",
                }
            degraded.append({
                "channel": vector_channel,
                "reason": type(vector_result).__name__,
            })
            logger.info("Product %s recall unavailable: %s", vector_channel, vector_result)
        else:
            vector_ids = vector_result[:50]
            vector_channel = "localVector" if vector_backend == "local" else "qdrant"
            channel_trace[vector_channel] = {
                "status": "active", "count": len(vector_ids),
                "embeddingFields": ["title"],
            }
            if vector_backend == "local":
                channel_trace["qdrant"] = {
                    "status": "disabled", "count": 0,
                    "reason": "vector_backend_local",
                }
            if vector_ids:
                ranked_lists.append(vector_ids)

        if not ranked_lists:
            return _finish_tool_trace(start, ToolTrace(
                tool="search_products", ok=False,
                detail={
                    "code": "product_recall_unavailable",
                    "message": "All product recall channels are unavailable or empty.",
                    "requestedCategory": category_code,
                    "degraded": degraded,
                    "retrievalTrace": {"channels": channel_trace},
                },
            ))

        active_lists = ranked_lists
        if (
            retrieval_mode == "bm25"
            and not isinstance(catalog_result, Exception)
            and bm25_ids
        ):
            active_lists = [bm25_ids, *([structured_ids] if structured_ids else [])]
        fused = reciprocal_rank_fusion(active_lists, k=60)
        hard_max_budget = next((
            requirement for requirement in parsed_requirements
            if requirement.key == "price_minor"
            and requirement.operator == "lte"
            and requirement.priority == "hard"
        ), None)
        if hard_max_budget is not None and structured_ids:
            # A user-stated maximum budget is both an eligibility gate and the
            # primary ordering contract: closest-to-ceiling first.  RRF remains
            # useful for queries without that explicit contract, but must not
            # dilute the budget order here.
            top50 = structured_ids[:50]
            score_map = {
                product_id: 1.0 / (60 + index)
                for index, product_id in enumerate(top50, start=1)
            }
            pool_fusion = "structured_budget_primary"
        else:
            top50 = [product_id for product_id, _ in fused[:50]]
            score_map = dict(fused)
            pool_fusion = "rrf" if len(active_lists) > 1 else "single_channel"

        detail_traces = await asyncio.gather(*(
            get_product_details_tool(top50[index:index + 10])
            for index in range(0, len(top50), 10)
        ))
        authoritative_by_id: dict[int, dict[str, Any]] = {}
        failed_fact_batches = 0
        fact_failures = []
        for trace in detail_traces:
            if trace.ok and isinstance(trace.detail, dict):
                products = trace.detail.get("products", [])
                if not isinstance(products, list):
                    failed_fact_batches += 1
                    continue
                for product in products:
                    product_id = product.get("id") if isinstance(product, dict) else None
                    if (
                        type(product_id) is int
                        and product_id > 0
                        and product_id in score_map
                        and product_id not in authoritative_by_id
                    ):
                        authoritative_by_id[product_id] = product
            else:
                failed_fact_batches += 1
                failure = trace.detail if isinstance(trace.detail, dict) else {}
                fact_failures.append({k: failure[k] for k in ('code', 'httpStatus', 'retryable', 'attemptCount') if k in failure})
        if failed_fact_batches:
            degraded.append({
                "channel": "javaFacts",
                "reason": f"{failed_fact_batches}_resolve_batches_failed",
            })
        candidate_pool_ids = [
            product_id for product_id in top50
            if product_id in authoritative_by_id
        ]
        authoritative_products = [
            authoritative_by_id[product_id]
            for product_id in candidate_pool_ids
        ]
        if retrieval_mode == "elasticsearch":
            authoritative_products = [
                product for product in authoritative_products
                if product.get("lifecycleStatus") == "ACTIVE"
                and product.get("priceStatus") == "verified"
                and type(product.get("snapshotPriceMinor")) is int
                and type(product.get("entityVersion")) is int
                and product["entityVersion"] > 0
                and type(product.get("availableQuantity")) is int
                and product["availableQuantity"] > 0
                and type(product.get("inventoryVersion")) is int
                and product["inventoryVersion"] >= 0
            ]
            eligible_ids = {int(product["id"]) for product in authoritative_products}
            candidate_pool_ids = [
                product_id for product_id in candidate_pool_ids
                if product_id in eligible_ids
            ]
        if category_code == "phone" and synthetic_policy != "disabled":
            authoritative_products = apply_synthetic_prices(
                authoritative_products,
                directory=settings.used_phone_synthetic_price_dir,
                policy=synthetic_policy,
            )
        if not authoritative_products:
            return _finish_tool_trace(start, ToolTrace(
                tool="search_products", ok=False,
                detail={
                    "code": "authoritative_product_facts_unavailable",
                    "message": "Recall succeeded but Java/MySQL facts could not be resolved.",
                    "degraded": degraded,
                    "failedBatchCount": failed_fact_batches,
                    "failureReasons": fact_failures,
                    "retrievalTrace": {"channels": channel_trace},
                },
            ))

        title_reranker_trace: dict[str, Any] = {
            "status": "disabled",
            "reason": "feature_flag_off",
            "candidateCount": 0,
            "modelCalled": False,
        }
        if settings.product_title_reranker_enabled and retrieval_mode != "elasticsearch":
            if not settings.deepseek_api_key:
                title_reranker_trace = {
                    "status": "skipped",
                    "reason": "model_not_configured",
                    "candidateCount": 0,
                    "modelCalled": False,
                }
            else:
                preliminary = rule_rerank_candidates(
                    category_code,
                    authoritative_products,
                    parsed_requirements,
                    score_map,
                    limit=50,
                )
                candidate_limit = max(
                    1,
                    min(int(settings.product_title_reranker_candidate_limit), 50),
                )
                eligible_products = [
                    row["product"]
                    for row in preliminary["products"][:candidate_limit]
                ]
                try:
                    title_result = await rerank_titles(query, eligible_products)
                    eligible_ids = [int(item["id"]) for item in eligible_products]
                    reordered = [
                        *title_result.ordered_product_ids,
                        *(item for item in eligible_ids if item not in title_result.ordered_product_ids),
                    ]
                    semantic_fused = reciprocal_rank_fusion(
                        [top50, list(reordered)],
                        k=60,
                    )
                    score_map = dict(semantic_fused)
                    title_reranker_trace = {
                        "status": "active",
                        "reason": "validated_complete_candidate_permutation",
                        "candidateCount": len(eligible_products),
                        "modelCalled": True,
                        "durationMs": title_result.duration_ms,
                        "promptTokens": title_result.prompt_tokens,
                        "completionTokens": title_result.completion_tokens,
                        "orderedProductIds": list(title_result.ordered_product_ids),
                        "judgments": [
                            {
                                "productId": item.product_id,
                                "relevance": item.relevance,
                                "matchedEvidence": list(item.matched_evidence),
                                "evidenceClass": "seller_title_text_claim",
                            }
                            for item in title_result.judgments
                        ],
                    }
                except Exception as exc:
                    logger.info("Product title relevance reranker degraded: %s", type(exc).__name__)
                    degraded.append({
                        "channel": "titleReranker",
                        "reason": type(exc).__name__,
                    })
                    title_reranker_trace = {
                        "status": "failed",
                        "reason": type(exc).__name__,
                        "candidateCount": len(eligible_products),
                        "modelCalled": True,
                        "fallback": "bm25_vector_rule_ranking",
                    }

        cross_encoder_trace: dict[str, Any] = {
            "status": "disabled", "modelCalled": False, "candidateCount": 0,
        }
        if settings.product_cross_encoder_enabled:
            from .cross_encoder import cross_encoder_rank

            try:
                semantic_scores, cross_encoder_trace = await cross_encoder_rank(
                    query, authoritative_products,
                )
                score_map = semantic_scores
            except Exception as exc:
                # Keep the prior score map byte-for-byte in this failure path.
                # Provider invocation does not prove an underlying model ran.
                cross_encoder_trace = {
                    "status": "failed", "modelCalled": None,
                    "candidateCount": len(authoritative_products),
                    "errorType": type(exc).__name__, "fallback": "previous_score_map",
                }
                degraded.append({"channel": "crossEncoder", "reason": type(exc).__name__})

        reranked = rule_rerank_candidates(
            category_code,
            authoritative_products,
            parsed_requirements,
            score_map,
            limit=min(limit, 20),
        )
        candidates = [
            {
                **row["product"],
                "facts": row["facts"],
                "checks": row["checks"],
                "selectionType": row["selectionType"],
                "scoreBreakdown": row["scoreBreakdown"],
                "evidenceRefs": row["evidenceRefs"],
            }
            for row in reranked["products"]
        ]
        ranked_item_ids = [item["id"] for item in candidates]
        ranking_trace = {
            **reranked["rankingTrace"],
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "rankedItemCount": len(ranked_item_ids),
        }
        evidence_refs = [item["ref"] for item in reranked["evidence"]]
        detail = {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "candidatePoolIds": candidate_pool_ids,
            "rankedItemIds": ranked_item_ids,
            "candidateIds": list(ranked_item_ids),
            "candidates": candidates,
            "retrievalTrace": {
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "channels": channel_trace,
                "degraded": degraded,
                "fusion": pool_fusion,
                "rrfK": 60,
                "fusedTop50": top50,
                "candidatePoolCount": len(candidate_pool_ids),
                "authoritativeFactCount": len(candidate_pool_ids),
                "syntheticPricePolicy": synthetic_policy,
                "queryExpansion": {
                    "policy": "controlled_title_synonyms_v1",
                    "applied": bool(expanded_terms),
                    "addedTerms": expanded_terms,
                    "factOrConstraint": False,
                },
                "titleReranker": title_reranker_trace,
                "crossEncoder": cross_encoder_trace,
            },
            "rankingTrace": ranking_trace,
            "citationTrace": {
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "sourceTool": "search_products",
                "rankedItemIds": list(ranked_item_ids),
                "evidenceRefCount": len(evidence_refs),
                "binding": "current_successful_tool_call_ranked_items_only",
            },
            "evidenceRefs": evidence_refs,
            "evidence": reranked["evidence"],
            "eliminated": reranked["eliminated"],
        }
        try:
            normalize_search_products_detail(
                detail,
                requirements=[item.model_dump() for item in parsed_requirements],
                category=category_code,
            )
        except TwoStageRankingContractError as exc:
            return _finish_tool_trace(start, ToolTrace(
                tool="search_products",
                ok=False,
                detail={
                    "code": exc.code,
                    "message": str(exc),
                },
            ))
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_products",
                ok=True,
                detail=detail,
            ),
        )
    except Exception as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="search_products",
                ok=False,
                detail={"code": "product_search_unavailable", "message": str(exc)},
            ),
        )


async def get_product_details_tool(product_ids: list[int]) -> ToolTrace:
    start = time.perf_counter()
    attempts = 0
    try:
        # /resolve is read-only despite POST. Retry transient transport/5xx
        # once; never apply this policy to order, payment or inventory writes.
        for attempt in range(2):
            attempts += 1
            try:
                response = await _product_http_client().post(
                    f"{settings.backend_base_url}/api/products/resolve",
                    json={"productIds": product_ids[:10]},
                )
                response.raise_for_status()
                break
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                transient = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code >= 500
                if attempt or not transient:
                    raise
                await asyncio.sleep(0.15)
        payload = response.json()
        products = payload.get("data")
        if payload.get("success") is False or not isinstance(products, list):
            raise ValueError('invalid_product_facts_response')
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="get_product_details",
                ok=True,
                detail={
                    "products": products,
                    "productIds": [item["id"] for item in products],
                    "requestedProductIds": product_ids,
                    "attemptCount": attempts,
                },
            ),
        )
    except Exception as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="get_product_details",
                ok=False,
                detail={
                    "code": "product_facts_timeout" if isinstance(exc, httpx.TimeoutException)
                            else "product_facts_http_error" if isinstance(exc, httpx.HTTPStatusError)
                            else "product_facts_invalid_response" if isinstance(exc, (ValueError, KeyError, TypeError))
                            else "product_facts_unavailable",
                    "httpStatus": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                    "retryable": isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)) or
                        (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500),
                    "attemptCount": attempts,
                },
            ),
        )


async def compare_products_tool(
    product_ids: list[int],
    category: str,
    requirements: list[dict],
) -> ToolTrace:
    start = time.perf_counter()
    try:
        details_trace = await get_product_details_tool(product_ids[:5])
        if not details_trace.ok or not isinstance(details_trace.detail, dict):
            return _finish_tool_trace(
                start,
                ToolTrace(
                    tool="compare_products",
                    ok=False,
                    detail=details_trace.detail,
                ),
            )
        parsed = [
            ShoppingRequirement.model_validate(item)
            for item in requirements
        ]
        products = details_trace.detail.get("products", [])
        if (
            category == "phone"
            and settings.used_phone_synthetic_price_policy != "disabled"
        ):
            products = apply_synthetic_prices(
                products,
                directory=settings.used_phone_synthetic_price_dir,
                policy=settings.used_phone_synthetic_price_policy,
            )
        result = compare_product_details(
            category,
            products,
            parsed,
        )
        return _finish_tool_trace(
            start,
            ToolTrace(tool="compare_products", ok=True, detail=result),
        )
    except Exception as exc:
        return _finish_tool_trace(
            start,
            ToolTrace(
                tool="compare_products",
                ok=False,
                detail={"code": "invalid_comparison", "message": str(exc)},
            ),
        )


_SCOPE_RERANK_TITLE_CLAIM_TERMS = {
    "camera_title_claim": ("拍照", "相机", "摄影", "摄像头", "像素"),
    "gaming_title_claim": ("打游戏", "游戏", "电竞", "高刷"),
}


def _title_claim_hits(product: dict[str, Any], intent: str) -> int:
    """Count controlled seller-title/public-text claim terms in a snapshot."""

    terms = _SCOPE_RERANK_TITLE_CLAIM_TERMS.get(intent, ())
    haystack = " ".join(
        str(product.get(field, ""))
        for field in ("title", "description")
    ).casefold()
    return sum(haystack.count(term) for term in terms)


async def rerank_products_in_scope_tool(
    scope_id: str,
    product_ids: list[int],
    ranking_intent: str,
    category: str,
    requirements: list[dict[str, Any]],
) -> ToolTrace:
    """Deterministically re-rank inside a server-owned CandidateScope only.

    Input IDs must already have been copied (never invented) from the active
    CandidateScope's ``rankedItemIds`` by the Planner and verified by the
    Executor.  This tool re-fetches authoritative facts for exactly those IDs,
    re-runs the existing hard requirement gate, then applies a stable title/text
    claim reorder.  It never scans the full catalog and never calls the Java
    full-catalog recall path.
    """

    start = time.perf_counter()
    if ranking_intent not in {"camera_title_claim", "gaming_title_claim"}:
        return _finish_tool_trace(start, ToolTrace(
            tool="rerank_products_in_scope",
            ok=False,
            detail={
                "code": "unsupported_ranking_intent",
                "message": "仅支持标题/公开文本排序意图",
            },
        ))
    try:
        # The Java /api/products/resolve gateway only resolves up to 10 IDs per
        # call (see get_product_details_tool).  A scope can hold 20 ranked IDs,
        # so fetch in 10-ID batches and merge — the identity gate below must
        # cover every requested scope ID, not just the first batch.
        detail_traces = await asyncio.gather(*(
            get_product_details_tool(product_ids[index:index + 10])
            for index in range(0, len(product_ids), 10)
        ))
        fetched: list[dict[str, Any]] = []
        for trace in detail_traces:
            if not trace.ok or not isinstance(trace.detail, dict):
                return _finish_tool_trace(start, ToolTrace(
                    tool="rerank_products_in_scope",
                    ok=False,
                    detail=trace.detail,
                ))
            batch_products = trace.detail.get("products", [])
            if not isinstance(batch_products, list):
                return _finish_tool_trace(start, ToolTrace(
                    tool="rerank_products_in_scope",
                    ok=False,
                    detail={
                        "code": "scope_product_facts_unavailable",
                        "message": "范围候选商品事实不可用",
                    },
                ))
            fetched.extend(batch_products)
        by_id: dict[int, dict[str, Any]] = {
            int(item["id"]): item
            for item in fetched
            if isinstance(item, dict) and type(item.get("id")) is int
        }
        requested_set = set(product_ids)
        if (
            len(by_id) != len(requested_set)
            or set(by_id) != requested_set
        ):
            return _finish_tool_trace(start, ToolTrace(
                tool="rerank_products_in_scope",
                ok=False,
                detail={
                    "code": "scope_product_identity_mismatch",
                    "message": "范围商品与请求的scope rankedItemIds不一致",
                },
            ))
        products = [by_id[product_id] for product_id in product_ids]
        parsed = [ShoppingRequirement.model_validate(item) for item in requirements]
        retrieval_scores = {
            product_id: len(product_ids) - index
            for index, product_id in enumerate(product_ids)
        }
        reranked = rule_rerank_candidates(
            category,
            products,
            parsed,
            retrieval_scores,
            limit=20,
        )
        rows = reranked["products"]
        if rows:
            # Stable title/public-text claim reorder inside the already-hard-gated
            # rows.  Unknowns still sink below known rows; ties break on the
            # deterministic round-1 score then ascending product id.
            rows.sort(key=lambda row: (
                row["hardUnknowns"] > 0,
                -_title_claim_hits(row["product"], ranking_intent),
                -row["scoreBreakdown"]["final"],
                int(row["product"]["id"]),
            ))
        candidates = [
            {
                **row["product"],
                "facts": row["facts"],
                "checks": row["checks"],
                "selectionType": row["selectionType"],
                "scoreBreakdown": row["scoreBreakdown"],
                "evidenceRefs": row["evidenceRefs"],
            }
            for row in rows
        ]
        ranked_item_ids = [item["id"] for item in candidates]
        evidence_by_ref = {
            item["ref"]: item for item in reranked["evidence"]
        }
        detail_evidence = [
            evidence_by_ref[ref]
            for row in rows
            for ref in row["evidenceRefs"]
        ]
        evidence_refs = [item["ref"] for item in detail_evidence]
        detail = {
            "contractVersion": SCOPE_RERANK_CONTRACT_VERSION,
            "scopeId": scope_id,
            "inputProductIds": list(product_ids),
            "rankedItemIds": ranked_item_ids,
            "productIds": list(ranked_item_ids),
            "rankingSignal": f"scope_{ranking_intent}",
            "degraded": [],
            "noFullSearch": True,
            "candidates": candidates,
            "citationTrace": {
                "contractVersion": SCOPE_RERANK_CONTRACT_VERSION,
                "sourceTool": "rerank_products_in_scope",
                "rankedItemIds": list(ranked_item_ids),
                "evidenceRefCount": len(evidence_refs),
                "binding": "current_successful_scope_rerank_only",
            },
            "evidenceRefs": evidence_refs,
            "evidence": detail_evidence,
            "eliminated": reranked["eliminated"],
            "rankingTrace": {
                "contractVersion": SCOPE_RERANK_CONTRACT_VERSION,
                "inputCandidateCount": len(product_ids),
                "eligibleCandidateCount": len(rows),
                "eliminatedHardViolationCount": len(reranked["eliminated"]),
                "rankingIntent": ranking_intent,
                "signal": f"scope_{ranking_intent}",
                "fullSearch": False,
                "tieBreak": "productId_ascending",
            },
        }
        try:
            normalize_scope_rerank_detail(
                detail,
                requirements=[item.model_dump() for item in parsed],
                category=category,
            )
        except TwoStageRankingContractError as exc:
            return _finish_tool_trace(start, ToolTrace(
                tool="rerank_products_in_scope",
                ok=False,
                detail={"code": exc.code, "message": str(exc)},
            ))
        return _finish_tool_trace(start, ToolTrace(
            tool="rerank_products_in_scope",
            ok=True,
            detail=detail,
        ))
    except Exception as exc:
        return _finish_tool_trace(start, ToolTrace(
            tool="rerank_products_in_scope",
            ok=False,
            detail={"code": "scope_rerank_unavailable", "message": str(exc)},
        ))


__all__ = [
    "close_product_search_clients",
    "compare_products_tool",
    "get_product_details_tool",
    "rerank_products_in_scope_tool",
    "search_products_tool",
    "warm_local_product_vector_cache",
]
