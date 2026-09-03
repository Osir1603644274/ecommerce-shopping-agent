from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .domains.ecommerce import CandidateScope
from .schemas import ReferenceContextHint
from .settings import settings
from .task_state import TaskState


REFERENCE_CONTEXT_SCHEMA_VERSION = "shopping-reference-context-v1"
REFERENCE_CONTEXT_PUBLIC_SCHEMA_VERSION = "shopping-reference-context-public-v1"
_client: redis.Redis | None = None


class ReferenceContextError(RuntimeError):
    """Stable fail-closed rejection for an untrusted or stale UI reference."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def safe_answer(self) -> str:
        return (
            "你引用的商品展示已过期、已被更新或不属于当前任务。"
            "请基于当前候选重新选择商品卡片，或明确提供商品 ID。"
        )


class _ReferenceContextReceipt(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_version: Literal["shopping-reference-context-v1"] = Field(
        alias="schemaVersion"
    )
    session_binding_hash: str = Field(alias="sessionBindingHash")
    task_id: str = Field(alias="taskId")
    task_revision: int = Field(alias="taskRevision", ge=1)
    scope_id: str = Field(alias="scopeId")
    scope_source_revision: int = Field(alias="scopeSourceRevision", ge=1)
    source_turn: int = Field(alias="sourceTurn", ge=0)
    compact_product_ids: list[int] = Field(alias="compactProductIds")
    expanded_product_ids: list[int] = Field(alias="expandedProductIds")
    compared_product_ids: list[int] = Field(alias="comparedProductIds")
    previous_batch_product_ids: list[int] = Field(alias="previousBatchProductIds")
    created_at_epoch_ms: int = Field(alias="createdAtEpochMs", ge=1)
    expires_at_epoch_ms: int = Field(alias="expiresAtEpochMs", ge=1)
    binding_hash: str = Field(alias="bindingHash")

    @field_validator(
        "compact_product_ids",
        "expanded_product_ids",
        "compared_product_ids",
        "previous_batch_product_ids",
    )
    @classmethod
    def validate_product_ids(cls, value: list[int]) -> list[int]:
        if any(type(item) is not int or item <= 0 for item in value):
            raise ValueError("reference product IDs must be positive integers")
        if len(set(value)) != len(value):
            raise ValueError("reference product IDs must be unique")
        return value


@dataclass(frozen=True, slots=True)
class ResolvedReferenceContext:
    task_id: str
    task_revision: int
    scope_id: str
    scope_source_revision: int
    presentation_mode: Literal["compact", "expanded"]
    presentation_ids: tuple[int, ...]
    compact_product_ids: tuple[int, ...]
    expanded_product_ids: tuple[int, ...]
    compared_product_ids: tuple[int, ...]
    previous_batch_product_ids: tuple[int, ...]
    focused_product_id: int | None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, decode_responses=True)
    return _client


def _session_binding_hash(session_id: str) -> str:
    return hashlib.sha256(
        ("shopping-reference-session-v1\0" + session_id).encode("utf-8")
    ).hexdigest()


def _receipt_key(handle: str) -> str:
    digest = hashlib.sha256(handle.encode("ascii")).hexdigest()
    return f"shopping:reference-context:v1:{digest}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _binding_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_product_id(value: Any) -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9]\d*", value) is not None:
        return int(value)
    raise ReferenceContextError("invalid_published_product_id")


def _guide_product_ids(rows: Any) -> list[int]:
    if not isinstance(rows, list):
        raise ReferenceContextError("invalid_published_product_rows")
    result: list[int] = []
    for row in rows:
        product = row.get("product") if isinstance(row, dict) else None
        if not isinstance(product, dict):
            raise ReferenceContextError("invalid_published_product_row")
        product_id = _canonical_product_id(product.get("id"))
        if product_id in result:
            raise ReferenceContextError("duplicate_published_product_id")
        result.append(product_id)
    return result


def _comparison_product_ids(rows: Any) -> list[int]:
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ReferenceContextError("invalid_comparison_rows")
    result: list[int] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ReferenceContextError("invalid_comparison_row")
        product_id = _canonical_product_id(row.get("productId"))
        if product_id in result:
            raise ReferenceContextError("duplicate_comparison_product_id")
        result.append(product_id)
    return result


def _active_scope(state: TaskState) -> CandidateScope:
    raw_scope = state.domain_state.get("candidateScope")
    if not isinstance(raw_scope, dict):
        raise ReferenceContextError("candidate_scope_missing")
    try:
        scope = CandidateScope.model_validate(raw_scope)
    except ValueError as exc:
        raise ReferenceContextError("candidate_scope_invalid") from exc
    if scope.task_id != state.task_id or scope.status != "active":
        raise ReferenceContextError("candidate_scope_stale")
    return scope


async def _persist_reference_context_receipt(
    *,
    session_id: str,
    state: TaskState,
    scope: CandidateScope,
    compact_ids: list[int],
    expanded_ids: list[int],
    compared_ids: list[int],
    previous_batch_ids: list[int],
) -> dict[str, Any]:
    ttl_seconds = max(int(settings.reference_context_ttl_seconds), 1)
    now_ms = int(time.time() * 1000)
    payload: dict[str, Any] = {
        "schemaVersion": REFERENCE_CONTEXT_SCHEMA_VERSION,
        "sessionBindingHash": _session_binding_hash(session_id),
        "taskId": state.task_id,
        "taskRevision": state.revision,
        "scopeId": scope.scope_id,
        "scopeSourceRevision": scope.source_revision,
        "sourceTurn": max(int(state.domain_state.get("turnCount", 0)), 0),
        "compactProductIds": compact_ids,
        "expandedProductIds": expanded_ids,
        "comparedProductIds": compared_ids,
        "previousBatchProductIds": previous_batch_ids,
        "createdAtEpochMs": now_ms,
        "expiresAtEpochMs": now_ms + ttl_seconds * 1000,
    }
    payload["bindingHash"] = _binding_hash(payload)
    receipt = _ReferenceContextReceipt.model_validate(payload)
    handle = secrets.token_urlsafe(32)
    await _get_client().set(
        _receipt_key(handle),
        receipt.model_dump_json(by_alias=True),
        ex=ttl_seconds,
    )
    return {
        "schemaVersion": REFERENCE_CONTEXT_PUBLIC_SCHEMA_VERSION,
        "handle": handle,
        "presentationMode": "compact",
        "scopeId": scope.scope_id,
        "taskRevision": state.revision,
        "compactCount": len(compact_ids),
        "expandedCount": len(expanded_ids),
    }


async def publish_reference_context(
    *,
    session_id: str,
    state: TaskState,
    guide_result: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Persist the exact card order actually published to this browser."""

    if not isinstance(guide_result, dict):
        return None
    if state.session_id != session_id:
        raise ReferenceContextError("session_task_binding_mismatch")
    scope = _active_scope(state)
    compact_ids = _guide_product_ids(guide_result.get("products"))
    if not compact_ids:
        return None
    expanded_rows = guide_result.get("expandedProducts")
    expanded_ids = (
        _guide_product_ids(expanded_rows)
        if isinstance(expanded_rows, list) and expanded_rows
        else list(compact_ids)
    )
    compared_ids = _comparison_product_ids(guide_result.get("comparisonMatrix"))
    previous_rows = guide_result.get("previousBatchProducts")
    previous_batch_ids = (
        _guide_product_ids(previous_rows)
        if isinstance(previous_rows, list) and previous_rows
        else []
    )
    if compact_ids != expanded_ids[: len(compact_ids)]:
        raise ReferenceContextError("compact_expanded_order_mismatch")
    allowed_ids = set(scope.ranked_item_ids)
    if not set(expanded_ids).issubset(allowed_ids):
        raise ReferenceContextError("published_product_outside_scope")
    if not set(compared_ids).issubset(allowed_ids):
        raise ReferenceContextError("comparison_product_outside_scope")
    if not set(previous_batch_ids).issubset(allowed_ids):
        raise ReferenceContextError("previous_batch_product_outside_scope")

    return await _persist_reference_context_receipt(
        session_id=session_id,
        state=state,
        scope=scope,
        compact_ids=compact_ids,
        expanded_ids=expanded_ids,
        compared_ids=compared_ids,
        previous_batch_ids=previous_batch_ids,
    )


async def refresh_reference_context(
    *,
    session_id: str,
    state: TaskState,
    resolved: ResolvedReferenceContext,
) -> dict[str, Any]:
    """Advance a validated browser presentation across same-scope revisions.

    Comparison/planning revisions must not manufacture a new CandidateScope or
    silently discard the browser's server-owned presentation order.  This
    reissues only an already-resolved presentation against the current revision;
    any task/scope/source drift fails closed.
    """

    if state.session_id != session_id or resolved.task_id != state.task_id:
        raise ReferenceContextError("session_task_binding_mismatch")
    scope = _active_scope(state)
    if (
        scope.scope_id != resolved.scope_id
        or scope.source_revision != resolved.scope_source_revision
        or state.revision < resolved.task_revision
    ):
        raise ReferenceContextError("reference_context_stale_scope")
    compact_ids = list(resolved.compact_product_ids)
    expanded_ids = list(resolved.expanded_product_ids)
    previous_batch_ids = list(resolved.previous_batch_product_ids)
    allowed_ids = set(scope.ranked_item_ids)
    if (
        not compact_ids
        or compact_ids != expanded_ids[: len(compact_ids)]
        or not set(expanded_ids).issubset(allowed_ids)
        or not set(previous_batch_ids).issubset(allowed_ids)
    ):
        raise ReferenceContextError("reference_context_scope_mismatch")
    raw_guide = state.domain_state.get("shoppingGuide")
    compared_ids = list(resolved.compared_product_ids)
    if isinstance(raw_guide, dict):
        current_compared = raw_guide.get("comparedIds")
        if isinstance(current_compared, list):
            if (
                any(type(item) is not int for item in current_compared)
                or len(set(current_compared)) != len(current_compared)
                or not set(current_compared).issubset(allowed_ids)
            ):
                raise ReferenceContextError("comparison_product_outside_scope")
            compared_ids = list(current_compared)
    return await _persist_reference_context_receipt(
        session_id=session_id,
        state=state,
        scope=scope,
        compact_ids=compact_ids,
        expanded_ids=expanded_ids,
        compared_ids=compared_ids,
        previous_batch_ids=previous_batch_ids,
    )


async def resolve_reference_context(
    *,
    session_id: str | None,
    state: TaskState,
    hint: ReferenceContextHint,
) -> ResolvedReferenceContext:
    """Resolve browser hints only after all server-owned bindings still agree."""

    if session_id is None or state.session_id != session_id:
        raise ReferenceContextError("session_binding_missing")
    raw = await _get_client().get(_receipt_key(hint.handle))
    if not isinstance(raw, str):
        raise ReferenceContextError("reference_context_missing")
    try:
        receipt = _ReferenceContextReceipt.model_validate_json(raw)
    except ValueError as exc:
        raise ReferenceContextError("reference_context_invalid") from exc
    payload = receipt.model_dump(by_alias=True, exclude={"binding_hash"})
    if not hmac.compare_digest(receipt.binding_hash, _binding_hash(payload)):
        raise ReferenceContextError("reference_context_tampered")
    now_ms = int(time.time() * 1000)
    if receipt.expires_at_epoch_ms <= now_ms:
        raise ReferenceContextError("reference_context_expired")
    if not hmac.compare_digest(
        receipt.session_binding_hash,
        _session_binding_hash(session_id),
    ):
        raise ReferenceContextError("reference_context_cross_session")
    if receipt.task_id != state.task_id or receipt.task_revision != state.revision:
        raise ReferenceContextError("reference_context_stale_task")
    scope = _active_scope(state)
    if (
        scope.scope_id != receipt.scope_id
        or scope.source_revision != receipt.scope_source_revision
    ):
        raise ReferenceContextError("reference_context_stale_scope")
    allowed_ids = set(scope.ranked_item_ids)
    if (
        not receipt.compact_product_ids
        or receipt.compact_product_ids
        != receipt.expanded_product_ids[: len(receipt.compact_product_ids)]
        or not set(receipt.expanded_product_ids).issubset(allowed_ids)
        or not set(receipt.compared_product_ids).issubset(allowed_ids)
        or not set(receipt.previous_batch_product_ids).issubset(allowed_ids)
    ):
        raise ReferenceContextError("reference_context_scope_mismatch")
    presentation_ids = (
        receipt.expanded_product_ids
        if hint.presentation_mode == "expanded"
        else receipt.compact_product_ids
    )
    focused_id = (
        int(hint.focused_product_id)
        if hint.focused_product_id is not None
        else None
    )
    if focused_id is not None and focused_id not in presentation_ids:
        raise ReferenceContextError("focused_product_outside_presentation")
    return ResolvedReferenceContext(
        task_id=receipt.task_id,
        task_revision=receipt.task_revision,
        scope_id=receipt.scope_id,
        scope_source_revision=receipt.scope_source_revision,
        presentation_mode=hint.presentation_mode,
        presentation_ids=tuple(presentation_ids),
        compact_product_ids=tuple(receipt.compact_product_ids),
        expanded_product_ids=tuple(receipt.expanded_product_ids),
        compared_product_ids=tuple(receipt.compared_product_ids),
        previous_batch_product_ids=tuple(receipt.previous_batch_product_ids),
        focused_product_id=focused_id,
    )
