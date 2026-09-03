"""Isolated, offline 439-product lanes for the real-user Context A/B.

This module is evaluation-only.  It gives each arm a private TaskState/session
identity while both arms share the byte-identical frozen catalog and model
configuration.  No Java, MySQL, Elasticsearch or business HTTP endpoint is
reachable from the tool transport.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from types import SimpleNamespace
from typing import Any, Mapping

from openai import AsyncOpenAI

from agent.app.domains.ecommerce.models import (
    ShoppingRequirement,
    bm25_rank,
    compare_product_details,
    rule_rerank_candidates,
)
from agent.app.domains.ecommerce.ranking_contract import (
    TWO_STAGE_RANKING_CONTRACT_VERSION,
    normalize_search_products_detail,
)
from agent.app.domains.ecommerce.synthetic_prices import apply_synthetic_prices
from agent.app.schemas import ToolTrace
from agent.app.settings import settings
from agent.app.task_state import (
    TaskFact,
    TaskState,
    TaskStateCreateRequest,
    create_task_state,
    get_task_state,
)


ROOT = Path(__file__).resolve().parents[3]
CATALOG_DIR = (
    ROOT
    / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3"
)
CATALOG_PATH = CATALOG_DIR / "catalog.jsonl"
EXPECTED_CATALOG_SHA256 = "725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75"
EXPECTED_PRODUCTS = 439
PROVENANCE_URL = "https://huggingface.co/datasets/benchen4395/KuaiSearch"
ATTRIBUTE_EVIDENCE_FIELD = "relevance.attr_value"
ATTRIBUTE_RULESET_VERSION = "used-phone-exact-token-seven-field-v2"
ALLOWED_TOOLS = frozenset({"search_products", "get_product_details", "compare_products"})


class LaneRuntimeError(RuntimeError):
    pass


class _FrozenCompletions:
    def __init__(self, create: Any) -> None:
        self._create = create

    async def create(self, **kwargs: Any) -> Any:
        kwargs["temperature"] = 0
        kwargs["max_tokens"] = 1024
        if kwargs.get("tools"):
            extra_body = dict(kwargs.get("extra_body") or {})
            extra_body["thinking"] = {"type": "disabled"}
            kwargs["extra_body"] = extra_body
        return await self._create(**kwargs)


class FrozenModelClient:
    """Provider adapter that enforces the preregistered model controls."""

    def __init__(self, client: AsyncOpenAI) -> None:
        self.chat = SimpleNamespace(
            completions=_FrozenCompletions(client.chat.completions.create)
        )


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LaneRuntimeError(f"invalid catalog JSONL at {line_number}") from exc
            if type(row) is not dict:
                raise LaneRuntimeError(f"catalog row {line_number} is not an object")
            rows.append(row)
    return rows


def _product_from_catalog(row: Mapping[str, Any]) -> dict[str, Any]:
    item_text = row.get("itemId")
    categories = row.get("categoryPath")
    raw_attributes = row.get("attributes")
    if (
        type(item_text) is not str
        or not item_text.isascii()
        or not item_text.isdecimal()
        or type(categories) is not list
        or len(categories) != 3
        or type(raw_attributes) is not dict
    ):
        raise LaneRuntimeError("invalid frozen catalog row")
    attributes: list[dict[str, Any]] = []
    attribute_terms: list[str] = []
    for key in sorted(raw_attributes):
        raw = raw_attributes[key]
        if type(raw) is not dict or raw.get("status") != "known":
            continue
        value = raw.get("value")
        if type(value) is not str:
            continue
        tokens = raw.get("matchedRawTokens")
        raw_tokens = [item for item in tokens if type(item) is str] if type(tokens) is list else []
        raw_value = ",".join(raw_tokens) or value
        attributes.append({
            "key": str(key),
            "valueType": "text",
            "rawValue": raw_value[:512],
            "normalizedText": value,
            "normalizedNumber": None,
            "normalizedBoolean": None,
            "unit": None,
            "evidenceField": ATTRIBUTE_EVIDENCE_FIELD,
            "extractionMethod": ATTRIBUTE_RULESET_VERSION,
            "confidence": 1.0,
        })
        attribute_terms.extend([str(key), value, *raw_tokens])
    title = str(row.get("title") or "")
    brand = str(row.get("brand") or "")
    seller = str(row.get("seller") or "")
    return {
        "id": int(item_text),
        "source": "kuaisearch",
        "sourceItemId": item_text,
        "title": title,
        "brand": brand,
        "seller": seller,
        "categoryL1": str(categories[0]),
        "categoryL2": str(categories[1]),
        "categoryL3": str(categories[2]),
        "snapshotPriceMinor": None,
        "currency": "CNY",
        "priceStatus": "unverified",
        "lifecycleStatus": "ACTIVE",
        "entityVersion": 1,
        "availableQuantity": 1,
        "inventoryVersion": 0,
        "attributeText": " ".join([title, brand, seller, *map(str, categories), *attribute_terms]),
        "dataNature": "historical_dataset_snapshot",
        "datasetRevision": str(row.get("datasetRevision") or ""),
        "sourceLicense": "MIT",
        "provenanceUrl": PROVENANCE_URL,
        "attributes": attributes,
    }


def _parse_requirements(raw: Any) -> list[ShoppingRequirement]:
    if not isinstance(raw, list):
        return []
    return [ShoppingRequirement.model_validate(item) for item in raw]


class FrozenCatalogTransport:
    """Read-only production-shaped tool transport over the frozen 439 rows."""

    def __init__(self, products: tuple[dict[str, Any], ...]) -> None:
        self._products = products
        self._by_id = {int(item["id"]): item for item in products}
        self.transport_identity = MappingProxyType({
            "mode": "replay",
            "source": "server_resolver",
        })

    async def __call__(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        execution_context: Any = None,
    ) -> ToolTrace:
        del execution_context
        if tool_name not in ALLOWED_TOOLS:
            return ToolTrace(
                tool=tool_name,
                ok=False,
                detail={"code": "offline_tool_denied"},
            )
        if tool_name == "search_products":
            return self._search(arguments)
        if tool_name == "get_product_details":
            return self._details(arguments)
        return self._compare(arguments)

    def _search(self, arguments: Mapping[str, Any]) -> ToolTrace:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolTrace(tool="search_products", ok=False, detail={"code": "missing_query"})
        requirements = _parse_requirements(arguments.get("requirements"))
        existing_keys = {item.key for item in requirements}
        brand = arguments.get("brand")
        if isinstance(brand, str) and brand and "brand" not in existing_keys:
            requirements.append(ShoppingRequirement(
                key="brand", operator="eq", value=brand, unit="text",
                priority="hard", source="search_filter:brand",
            ))
        for source_name, key, operator in (
            ("minPriceMinor", "price_minor", "gte"),
            ("maxPriceMinor", "price_minor", "lte"),
        ):
            value = arguments.get(source_name)
            if type(value) is int and key not in existing_keys:
                requirements.append(ShoppingRequirement(
                    key=key, operator=operator, value=value, unit="CNY_MINOR",
                    priority="hard", source=f"search_filter:{source_name}",
                ))
        ranked_ids = bm25_rank(query, list(self._products))[:50]
        ranked_products = [deepcopy(self._by_id[item]) for item in ranked_ids]
        ranked_products = apply_synthetic_prices(
            ranked_products,
            directory=str(CATALOG_DIR),
            policy="budget_and_ranking",
        )
        retrieval_scores = {
            product_id: 1.0 / (60 + rank)
            for rank, product_id in enumerate(ranked_ids, 1)
        }
        limit = arguments.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 20:
            limit = 20
        reranked = rule_rerank_candidates(
            "phone", ranked_products, requirements, retrieval_scores, limit=limit,
        )
        candidates = [{
            **row["product"],
            "facts": row["facts"],
            "checks": row["checks"],
            "selectionType": row["selectionType"],
            "scoreBreakdown": row["scoreBreakdown"],
            "evidenceRefs": row["evidenceRefs"],
        } for row in reranked["products"]]
        ranked_item_ids = [int(item["id"]) for item in candidates]
        evidence = reranked["evidence"]
        detail = {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "candidatePoolIds": ranked_ids,
            "rankedItemIds": ranked_item_ids,
            "candidateIds": ranked_item_ids,
            "candidates": candidates,
            "retrievalTrace": {
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "channels": {"offlineFrozenBm25": {"status": "active", "count": len(ranked_ids)}},
                "degraded": [],
                "fusion": "single_channel",
                "rrfK": 60,
                "fusedTop50": ranked_ids,
                "candidatePoolCount": len(ranked_ids),
                "authoritativeFactCount": len(ranked_ids),
                "syntheticPricePolicy": "budget_and_ranking",
                "queryExpansion": {"policy": "none", "applied": False, "addedTerms": [], "factOrConstraint": False},
                "titleReranker": {"status": "disabled", "reason": "frozen_single_variable_context_ab", "candidateCount": 0, "modelCalled": False},
            },
            "rankingTrace": {
                **reranked["rankingTrace"],
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "rankedItemCount": len(ranked_item_ids),
            },
            "citationTrace": {
                "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
                "sourceTool": "search_products",
                "rankedItemIds": ranked_item_ids,
                "evidenceRefCount": len(evidence),
                "binding": "current_successful_tool_call_ranked_items_only",
            },
            "evidenceRefs": [item["ref"] for item in evidence],
            "evidence": evidence,
            "eliminated": reranked["eliminated"],
        }
        normalize_search_products_detail(
            detail,
            requirements=[item.model_dump() for item in requirements],
            category="phone",
        )
        return ToolTrace(tool="search_products", ok=bool(candidates), detail=detail)

    def _details(self, arguments: Mapping[str, Any]) -> ToolTrace:
        ids = arguments.get("productIds")
        if not isinstance(ids, list) or not ids:
            return ToolTrace(tool="get_product_details", ok=False, detail={"code": "missing_product_ids"})
        products = [deepcopy(self._by_id[item]) for item in ids[:10] if type(item) is int and item in self._by_id]
        products = apply_synthetic_prices(
            products,
            directory=str(CATALOG_DIR),
            policy="budget_and_ranking",
        )
        return ToolTrace(tool="get_product_details", ok=len(products) == len(ids[:10]), detail={
            "products": products,
            "productIds": [item["id"] for item in products],
            "requestedProductIds": ids,
        })

    def _compare(self, arguments: Mapping[str, Any]) -> ToolTrace:
        ids = arguments.get("productIds")
        if not isinstance(ids, list) or not ids:
            return ToolTrace(tool="compare_products", ok=False, detail={"code": "missing_product_ids"})
        products = [deepcopy(self._by_id[item]) for item in ids[:5] if type(item) is int and item in self._by_id]
        products = apply_synthetic_prices(
            products,
            directory=str(CATALOG_DIR),
            policy="budget_and_ranking",
        )
        result = compare_product_details("phone", products, _parse_requirements(arguments.get("requirements")))
        return ToolTrace(tool="compare_products", ok=True, detail=result)


@dataclass(frozen=True)
class _LaneIds:
    run_id: str
    task_id: str
    session_id: str
    branch_hash: str


class IsolatedLaneRuntime:
    def __init__(self, *, namespace: str = "formal-v2-attempt001") -> None:
        if not namespace or len(namespace) > 48 or not all(
            character.isalnum() or character in "-_" for character in namespace
        ):
            raise LaneRuntimeError("invalid lane namespace")
        self._namespace = namespace
        # Context is the independent variable in this experiment.  The
        # separately evaluated Multi-Agent hook is disabled equally in both
        # arms so child-research variability cannot confound this pair.
        settings.multi_agent_v2_enabled = False
        if _sha_file(CATALOG_PATH) != EXPECTED_CATALOG_SHA256:
            raise LaneRuntimeError("frozen 439 catalog hash mismatch")
        rows = _read_jsonl(CATALOG_PATH)
        if len(rows) != EXPECTED_PRODUCTS:
            raise LaneRuntimeError(f"expected {EXPECTED_PRODUCTS} catalog rows")
        products = tuple(_product_from_catalog(row) for row in rows)
        if len({int(item["id"]) for item in products}) != EXPECTED_PRODUCTS:
            raise LaneRuntimeError("duplicate catalog product id")
        self._transport = FrozenCatalogTransport(products)
        if not settings.deepseek_api_key:
            raise LaneRuntimeError("DEEPSEEK_API_KEY is not configured")
        raw_client = AsyncOpenAI(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            timeout=30.0,
            max_retries=0,
        )
        self._client = FrozenModelClient(raw_client)
        self._ids: dict[tuple[str, str], _LaneIds] = {}

    async def begin_conversation_async(self, conversation: dict[str, Any], arms: tuple[str, str]):
        from agent.evaluation.real_user_multiturn_ab_executor_20260902_v1.runner import LaneBinding

        conversation_id = str(conversation["conversationId"])
        branch_hash = _sha({
            "conversationId": conversation_id,
            "catalogSha256": EXPECTED_CATALOG_SHA256,
            "initialState": {"taskType": "ecommerce_guide", "status": "collecting_information", "revision": 1},
        })

        result: dict[str, LaneBinding] = {}
        for arm in arms:
            suffix = hashlib.sha256(
                f"{self._namespace}:{conversation_id}:{arm}".encode("utf-8")
            ).hexdigest()[:12]
            session_id = f"eval-context-{suffix}"
            state = await create_task_state(TaskStateCreateRequest(
                taskType="ecommerce_guide",
                goal="二手手机多轮导购评测",
                sessionId=session_id,
                facts=[TaskFact(
                    key="category", value="phone", certainty="confirmed",
                    source="system",
                )],
                domainState={
                    "origin": "real_user_context_ab_v2",
                    "conversationId": conversation_id,
                    "shoppingGuide": {
                        "mode": "recommend",
                        "category": "phone",
                        "useCases": [],
                        "requirements": [],
                        "candidateIds": [],
                        "comparedIds": [],
                        "evidenceStatus": "missing",
                    },
                },
            ))
            run_id = f"run-context-{suffix}"
            self._ids[(conversation_id, arm)] = _LaneIds(run_id, state.task_id, session_id, branch_hash)
            result[arm] = LaneBinding(run_id, state.task_id, session_id, branch_hash, state.revision)
        return result

    def begin_conversation(self, conversation: dict[str, Any], arms: tuple[str, str]):
        return asyncio.run(self.begin_conversation_async(conversation, arms))

    async def load_task_state(self, row: dict[str, Any], lane: Any) -> TaskState:
        state = await get_task_state(lane.task_id)
        if state is None:
            raise LaneRuntimeError("lane TaskState missing")
        return state

    async def persist_task_state(self, row: dict[str, Any], lane: Any, current: TaskState) -> None:
        persisted = await get_task_state(lane.task_id)
        if persisted is None or persisted.revision != current.revision:
            raise LaneRuntimeError("lane TaskState persistence mismatch")

    def model_client(self, row: dict[str, Any], lane: Any) -> FrozenModelClient:
        return self._client

    def tool_transport(self, row: dict[str, Any], lane: Any) -> FrozenCatalogTransport:
        return self._transport

    def turn_run_id(self, row: dict[str, Any], lane: Any) -> str:
        suffix = hashlib.sha256(
            (
                f"{self._namespace}:{row['conversationId']}:{row['arm']}:"
                f"{row['turnId']}:{row['executionOrdinal']}"
            ).encode("utf-8")
        ).hexdigest()[:12]
        return f"run-context-turn-{suffix}"

    @staticmethod
    def state_binding_hashes(state: TaskState) -> tuple[str, str]:
        domain = state.domain_state or {}
        reference = domain.get("referenceContextReceipt") or domain.get("referenceContext")
        guide = domain.get("shoppingGuide") if isinstance(domain.get("shoppingGuide"), dict) else {}
        scope = guide.get("candidateScope") if isinstance(guide, dict) else None
        return (_sha(reference) if reference is not None else _sha(None), _sha(scope) if scope is not None else _sha(None))
