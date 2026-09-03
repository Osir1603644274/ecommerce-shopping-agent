"""Governed ContextCompiler V1 and its redacted receipt boundary.

The compiler is deterministic and model-free.  It treats context as scoped
information, never as authorization.  Server-owned capability grants remain a
separate runtime check and are represented here only by immutable identities.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .settings import REPOSITORY_ROOT, settings


logger = logging.getLogger(__name__)

AgentRole = Literal["SHOPPING_AGENT", "EVIDENCE_RESEARCH_AGENT"]
Phase = Literal[
    "SHOPPING_PLANNER",
    "SHOPPING_EXECUTOR",
    "SHOPPING_VALIDATOR",
    "SHOPPING_REPLANNER",
    "SHOPPING_FINAL_ANSWER",
    "EVIDENCE_RESEARCH",
]
ItemType = Literal[
    "TASK_FACT",
    "HARD_CONSTRAINT",
    "SOFT_PREFERENCE",
    "FRESH_EVIDENCE",
    "WORKING_SET",
    "REFERENCE_CONTEXT",
    "MEMORY_PREFERENCE",
    "RAW_HISTORY",
    "RESEARCH_REPORT",
    "BACKGROUND",
]
RejectReason = Literal[
    "wrong_tenant",
    "wrong_owner",
    "wrong_session",
    "wrong_task",
    "wrong_revision",
    "wrong_scope",
    "wrong_recipient",
    "wrong_phase",
    "expired",
    "revoked",
    "superseded",
    "authority_lost",
    "duplicate",
    "irrelevant",
    "budget_evicted",
    "server_only",
]

_ALL_SHOPPING_PHASES: tuple[Phase, ...] = (
    "SHOPPING_PLANNER",
    "SHOPPING_EXECUTOR",
    "SHOPPING_VALIDATOR",
    "SHOPPING_REPLANNER",
    "SHOPPING_FINAL_ANSWER",
)
_PROTECTED_TYPES: frozenset[ItemType] = frozenset(
    {
        "TASK_FACT",
        "HARD_CONSTRAINT",
        "FRESH_EVIDENCE",
        "WORKING_SET",
        "REFERENCE_CONTEXT",
        "RESEARCH_REPORT",
    }
)
_EVICTION_PRIORITY: dict[ItemType, int] = {
    "BACKGROUND": 0,
    "RAW_HISTORY": 1,
    "SOFT_PREFERENCE": 2,
    "MEMORY_PREFERENCE": 3,
}
_HISTORY_REFERENCE_MARKERS = (
    "刚才",
    "之前",
    "前面",
    "最开始",
    "上一个",
    "上一轮",
    "那个",
    "那些",
    "这两个",
    "那两个",
    "上述",
    "继续",
    "还是",
)
_CONTEXT_RECEIPT_KEY_PREFIX = "agent-context-receipt-v1"
_SPOOL_LOCK = threading.Lock()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _estimate_tokens(value: Any) -> int:
    text = canonical_json(value)
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return max(1, (cjk // 2) + ((len(text) - cjk) // 3))


class RunContextV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["context-multiagent-run-context-v1"] = Field(
        default="context-multiagent-run-context-v1", alias="schemaVersion"
    )
    run_id: str = Field(alias="runId", min_length=1, max_length=160)
    parent_run_id: str | None = Field(default=None, alias="parentRunId")
    handoff_id: str | None = Field(default=None, alias="handoffId")
    tenant_id: str = Field(alias="tenantId", min_length=1, max_length=160)
    owner_id: str = Field(alias="ownerId", min_length=1, max_length=160)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=160)
    recipient_type: Literal["SELF", "OTHER", "UNKNOWN"] = Field(
        alias="recipientType"
    )
    recipient_id: str | None = Field(default=None, alias="recipientId")
    task_id: str = Field(alias="taskId", min_length=1, max_length=160)
    task_revision: int = Field(alias="taskRevision", ge=1)
    agent_role: AgentRole = Field(alias="agentRole")
    phase: Phase
    model_call_ordinal: int = Field(alias="modelCallOrdinal", ge=0)
    candidate_scope_id: str | None = Field(default=None, alias="candidateScopeId")
    candidate_scope_source_revision: int | None = Field(
        default=None, alias="candidateScopeSourceRevision", ge=1
    )
    candidate_scope_hash: str | None = Field(
        default=None, alias="candidateScopeHash", pattern=r"^[0-9a-f]{64}$"
    )
    deadline_at: datetime = Field(alias="deadlineAt")
    compiler_version: str = Field(alias="compilerVersion", min_length=1)
    policy_version: str = Field(alias="policyVersion", min_length=1)
    capability_grant_id: str = Field(alias="capabilityGrantId", min_length=1)
    capability_grant_hash: str = Field(
        alias="capabilityGrantHash", pattern=r"^[0-9a-f]{64}$"
    )
    sensitivity: Literal["SERVER_ONLY"] = "SERVER_ONLY"

    @model_validator(mode="after")
    def validate_role_phase_binding(self) -> "RunContextV1":
        if self.agent_role == "EVIDENCE_RESEARCH_AGENT":
            if self.phase != "EVIDENCE_RESEARCH":
                raise ValueError("research child run must use EVIDENCE_RESEARCH phase")
            required = {
                "parentRunId": self.parent_run_id,
                "handoffId": self.handoff_id,
                "candidateScopeId": self.candidate_scope_id,
                "candidateScopeSourceRevision": self.candidate_scope_source_revision,
                "candidateScopeHash": self.candidate_scope_hash,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(
                    "research child run is missing required bindings: "
                    + ", ".join(missing)
                )
        elif self.phase == "EVIDENCE_RESEARCH":
            raise ValueError("shopping run cannot use EVIDENCE_RESEARCH phase")
        return self


class ContextItemScopeV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    tenant_id: str = Field(alias="tenantId", min_length=1)
    owner_id: str = Field(alias="ownerId", min_length=1)
    session_id: str = Field(alias="sessionId", min_length=1)
    task_id: str = Field(alias="taskId", min_length=1)
    task_revision: int = Field(alias="taskRevision", ge=1)
    candidate_scope_id: str | None = Field(default=None, alias="candidateScopeId")
    candidate_scope_hash: str | None = Field(
        default=None, alias="candidateScopeHash", pattern=r"^[0-9a-f]{64}$"
    )


class ContextItemV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["context-item-v1"] = Field(
        default="context-item-v1", alias="schemaVersion"
    )
    item_id: str = Field(alias="itemId", min_length=1, max_length=200)
    item_type: ItemType = Field(alias="itemType")
    semantic_key: str = Field(alias="semanticKey", min_length=1, max_length=240)
    authority_domain: Literal[
        "CURRENT_REQUEST",
        "TASK_STATE",
        "TOOL_FACT",
        "REFERENCE_CONTEXT",
        "CANDIDATE_SCOPE",
        "GOVERNED_MEMORY",
        "SESSION_HISTORY",
        "BACKGROUND",
    ] = Field(alias="authorityDomain")
    source_kind: Literal[
        "USER_MESSAGE",
        "SHOPPING_TASK_STATE_V2",
        "TOOL_RECEIPT",
        "VALIDATOR",
        "REFERENCE_RESOLVER",
        "MEMORY_SNAPSHOT",
        "SESSION_STORE",
        "RESEARCH_MERGE",
    ] = Field(alias="sourceKind")
    source_refs: tuple[str, ...] = Field(alias="sourceRefs")
    source_revision: str | int | None = Field(default=None, alias="sourceRevision")
    scope: ContextItemScopeV1
    recipient_allowlist: tuple[AgentRole, ...] = Field(alias="recipientAllowlist")
    phase_allowlist: tuple[Phase, ...] = Field(alias="phaseAllowlist")
    sensitivity: Literal["MODEL_VISIBLE", "SERVER_ONLY"]
    untrusted_data: bool = Field(default=False, alias="untrustedData")
    valid_from: datetime = Field(alias="validFrom")
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    supersedes_item_ids: tuple[str, ...] = Field(
        default=(), alias="supersedesItemIds"
    )
    payload: dict[str, Any]
    content_hash: str = Field(alias="contentHash", pattern=r"^[0-9a-f]{64}$")
    estimated_tokens: int = Field(alias="estimatedTokens", ge=0)


class SelectedContextItemV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)
    item_id: str = Field(alias="itemId")
    semantic_key: str = Field(alias="semanticKey")
    source_revision: str | int | None = Field(default=None, alias="sourceRevision")
    content_hash: str = Field(alias="contentHash", pattern=r"^[0-9a-f]{64}$")
    estimated_tokens: int = Field(alias="estimatedTokens", ge=0)


class RejectedContextItemV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)
    item_id: str = Field(alias="itemId")
    reason: RejectReason


class ContextReceiptV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["context-receipt-v1"] = Field(
        default="context-receipt-v1", alias="schemaVersion"
    )
    receipt_id: str = Field(alias="receiptId")
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    task_revision: int = Field(alias="taskRevision", ge=1)
    agent_role: AgentRole = Field(alias="agentRole")
    phase: Phase
    model_call_ordinal: int = Field(alias="modelCallOrdinal", ge=0)
    selected_items: tuple[SelectedContextItemV1, ...] = Field(alias="selectedItems")
    rejected_items: tuple[RejectedContextItemV1, ...] = Field(alias="rejectedItems")
    semantic_hash: str = Field(alias="semanticHash", pattern=r"^[0-9a-f]{64}$")
    binding_hash: str = Field(alias="bindingHash", pattern=r"^[0-9a-f]{64}$")
    compiler_version: str = Field(alias="compilerVersion")
    policy_version: str = Field(alias="policyVersion")
    tokenizer_version: str = Field(alias="tokenizerVersion")
    tool_schema_hash: str = Field(alias="toolSchemaHash", pattern=r"^[0-9a-f]{64}$")
    model_config_hash: str = Field(alias="modelConfigHash", pattern=r"^[0-9a-f]{64}$")
    model_view_bytes: int = Field(alias="modelViewBytes", ge=0)
    estimated_tokens: int = Field(alias="estimatedTokens", ge=0)
    actual_tokens: int | None = Field(default=None, alias="actualTokens", ge=0)
    token_status: Literal["OBSERVED", "ESTIMATED", "NOT_INSTRUMENTED"] = Field(
        alias="tokenStatus"
    )
    compile_duration_ms: float = Field(alias="compileDurationMs", ge=0)
    persistence_status: Literal["PRIMARY", "SPOOL", "FAILED"] = Field(
        alias="persistenceStatus"
    )


class CompiledContextV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model_view: dict[str, Any]
    receipt: ContextReceiptV1


class ContextIntegrityError(ValueError):
    pass


class ContextBudgetExceeded(ValueError):
    pass


class ContextReceiptPersistenceError(RuntimeError):
    pass


def _scope_rejection(item: ContextItemV1, run: RunContextV1) -> RejectReason | None:
    scope = item.scope
    if scope.tenant_id != run.tenant_id:
        return "wrong_tenant"
    if scope.owner_id != run.owner_id:
        return "wrong_owner"
    if scope.session_id != run.session_id:
        return "wrong_session"
    if scope.task_id != run.task_id:
        return "wrong_task"
    if scope.task_revision != run.task_revision:
        return "wrong_revision"
    if scope.candidate_scope_id is not None and (
        scope.candidate_scope_id != run.candidate_scope_id
        or scope.candidate_scope_hash != run.candidate_scope_hash
    ):
        return "wrong_scope"
    if run.agent_role not in item.recipient_allowlist:
        return "wrong_recipient"
    if run.phase not in item.phase_allowlist:
        return "wrong_phase"
    return None


def _query_tokens(text: str) -> set[str]:
    lowered = text.casefold()
    tokens = set(re.findall(r"[a-z0-9]{2,}", lowered))
    cjk = "".join(char for char in lowered if "\u4e00" <= char <= "\u9fff")
    tokens.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {token for token in tokens if token}


def _history_relevant(item: ContextItemV1, query: str) -> bool:
    if item.item_type != "RAW_HISTORY":
        return True
    value = item.payload.get("value")
    kind = value.get("kind") if isinstance(value, dict) else item.payload.get("kind")
    if kind == "recent_verbatim":
        return True
    lowered_query = query.casefold()
    if any(marker in lowered_query for marker in _HISTORY_REFERENCE_MARKERS):
        return True
    query_tokens = _query_tokens(query)
    if not query_tokens:
        return False
    history_tokens = _query_tokens(canonical_json(item.payload))
    return bool(query_tokens & history_tokens)


def _model_view(
    items: list[ContextItemV1],
    *,
    source_items: list[ContextItemV1],
) -> dict[str, Any]:
    collection_fields: list[str] = []
    for item in source_items:
        field = item.payload.get("field")
        if (
            isinstance(item.payload.get("collectionIndex"), int)
            and isinstance(field, str)
            and field not in collection_fields
        ):
            collection_fields.append(field)
    result: dict[str, Any] = {field: [] for field in collection_fields}
    for item in items:
        field = item.payload.get("field")
        if not isinstance(field, str) or not field:
            result.setdefault("contextItems", []).append(
                {"semanticKey": item.semantic_key, "payload": item.payload}
            )
            continue
        if isinstance(item.payload.get("collectionIndex"), int):
            result.setdefault(field, []).append(item.payload.get("value"))
            continue
        if field in result:
            raise ContextIntegrityError("duplicate model-view field")
        result[field] = item.payload.get("value")
    return result


def compile_context_v1(
    run: RunContextV1,
    items: list[ContextItemV1],
    *,
    budget_tokens: int,
    tool_schema_hash: str,
    model_config_hash: str,
    query: str = "",
    history_policy: Literal["preserve", "query_focused"] = "preserve",
) -> CompiledContextV1:
    """Compile one bounded model view and an unpersisted double-hash receipt."""

    import time

    started = time.perf_counter()
    if budget_tokens < 1:
        raise ContextBudgetExceeded("context budget must be positive")
    if run.deadline_at <= datetime.now(timezone.utc):
        raise ContextIntegrityError("run context is expired")

    rejected: list[RejectedContextItemV1] = []
    eligible: list[ContextItemV1] = []
    now = datetime.now(timezone.utc)
    for item in items:
        if sha256_json(item.payload) != item.content_hash:
            raise ContextIntegrityError(f"context item content hash mismatch: {item.item_id}")
        reason = _scope_rejection(item, run)
        if reason is None and item.valid_from > now:
            reason = "authority_lost"
        if reason is None and item.expires_at is not None and item.expires_at <= now:
            reason = "expired"
        if reason is None and item.sensitivity == "SERVER_ONLY":
            reason = "server_only"
        if (
            reason is None
            and history_policy == "query_focused"
            and not _history_relevant(item, query)
        ):
            reason = "irrelevant"
        if reason is not None:
            rejected.append(RejectedContextItemV1(itemId=item.item_id, reason=reason))
        else:
            eligible.append(item)

    superseded_ids = {
        item_id for item in eligible for item_id in item.supersedes_item_ids
    }
    retained: list[ContextItemV1] = []
    semantic_keys: set[str] = set()
    for item in eligible:
        if item.item_id in superseded_ids:
            rejected.append(
                RejectedContextItemV1(itemId=item.item_id, reason="superseded")
            )
        elif item.semantic_key in semantic_keys:
            rejected.append(
                RejectedContextItemV1(itemId=item.item_id, reason="duplicate")
            )
        else:
            semantic_keys.add(item.semantic_key)
            retained.append(item)

    protected_tokens = sum(
        item.estimated_tokens for item in retained if item.item_type in _PROTECTED_TYPES
    )
    if protected_tokens > budget_tokens:
        raise ContextBudgetExceeded(
            f"protected context exceeds budget: {protected_tokens} > {budget_tokens}"
        )
    total = sum(item.estimated_tokens for item in retained)
    if total > budget_tokens:
        evictable = sorted(
            (
                (index, item)
                for index, item in enumerate(retained)
                if item.item_type not in _PROTECTED_TYPES
            ),
            key=lambda pair: (
                _EVICTION_PRIORITY.get(pair[1].item_type, 99),
                pair[0],
            ),
        )
        evicted_ids: set[str] = set()
        for _index, item in evictable:
            if total <= budget_tokens:
                break
            evicted_ids.add(item.item_id)
            total -= item.estimated_tokens
            rejected.append(
                RejectedContextItemV1(itemId=item.item_id, reason="budget_evicted")
            )
        retained = [item for item in retained if item.item_id not in evicted_ids]
    if sum(item.estimated_tokens for item in retained) > budget_tokens:
        raise ContextBudgetExceeded("context budget cannot be satisfied")

    view = _model_view(retained, source_items=items)
    semantic_hash = sha256_json(view)
    selected = tuple(
        SelectedContextItemV1(
            itemId=item.item_id,
            semanticKey=item.semantic_key,
            sourceRevision=item.source_revision,
            contentHash=item.content_hash,
            estimatedTokens=item.estimated_tokens,
        )
        for item in retained
    )
    binding_payload = {
        "semanticHash": semantic_hash,
        "runId": run.run_id,
        "parentRunId": run.parent_run_id,
        "handoffId": run.handoff_id,
        "tenantId": run.tenant_id,
        "ownerId": run.owner_id,
        "sessionId": run.session_id,
        "recipientType": run.recipient_type,
        "recipientId": run.recipient_id,
        "taskId": run.task_id,
        "taskRevision": run.task_revision,
        "agentRole": run.agent_role,
        "phase": run.phase,
        "modelCallOrdinal": run.model_call_ordinal,
        "candidateScopeId": run.candidate_scope_id,
        "candidateScopeSourceRevision": run.candidate_scope_source_revision,
        "candidateScopeHash": run.candidate_scope_hash,
        "compilerVersion": run.compiler_version,
        "policyVersion": run.policy_version,
        "capabilityGrantId": run.capability_grant_id,
        "capabilityGrantHash": run.capability_grant_hash,
        "toolSchemaHash": tool_schema_hash,
        "modelConfigHash": model_config_hash,
        "selected": [
            {"itemId": item.item_id, "contentHash": item.content_hash}
            for item in retained
        ],
    }
    binding_hash = sha256_json(binding_payload)
    view_json = canonical_json(view)
    receipt = ContextReceiptV1(
        receiptId="ctxr-" + binding_hash[:24],
        runId=run.run_id,
        taskId=run.task_id,
        taskRevision=run.task_revision,
        agentRole=run.agent_role,
        phase=run.phase,
        modelCallOrdinal=run.model_call_ordinal,
        selectedItems=selected,
        rejectedItems=tuple(rejected),
        semanticHash=semantic_hash,
        bindingHash=binding_hash,
        compilerVersion=run.compiler_version,
        policyVersion=run.policy_version,
        tokenizerVersion="deterministic-estimator-v1",
        toolSchemaHash=tool_schema_hash,
        modelConfigHash=model_config_hash,
        modelViewBytes=len(view_json.encode("utf-8")),
        estimatedTokens=sum(item.estimated_tokens for item in retained),
        actualTokens=None,
        tokenStatus="ESTIMATED",
        compileDurationMs=(time.perf_counter() - started) * 1000.0,
        persistenceStatus="FAILED",
    )
    return CompiledContextV1(model_view=view, receipt=receipt)


def context_items_from_pack(
    pack: Any,
    run: RunContextV1,
    *,
    valid_from: datetime | None = None,
) -> list[ContextItemV1]:
    """Losslessly adapt the current ContextPack model view into typed items."""

    payload = pack.model_dump(by_alias=True, mode="json", exclude={"run_id", "runId"})
    payload.pop("runId", None)
    payload.pop("run_id", None)
    created = valid_from or datetime.now(timezone.utc)
    scope = ContextItemScopeV1(
        tenantId=run.tenant_id,
        ownerId=run.owner_id,
        sessionId=run.session_id,
        taskId=run.task_id,
        taskRevision=run.task_revision,
        candidateScopeId=run.candidate_scope_id,
        candidateScopeHash=run.candidate_scope_hash,
    )
    result: list[ContextItemV1] = []
    for field, value in payload.items():
        item_type: ItemType = "BACKGROUND"
        authority = "TASK_STATE"
        source_kind = "SHOPPING_TASK_STATE_V2"
        if field == "confirmedFacts":
            item_type = "TASK_FACT"
        elif field == "hardConstraints":
            item_type = "HARD_CONSTRAINT"
        elif field == "softPreferences":
            item_type = "SOFT_PREFERENCE"
        elif field == "evidenceRefs":
            item_type, authority, source_kind = "FRESH_EVIDENCE", "TOOL_FACT", "VALIDATOR"
        elif field in {"candidateScope", "candidateScopeState"}:
            item_type, authority = "WORKING_SET", "CANDIDATE_SCOPE"
        elif field == "historySummaries":
            item_type, authority, source_kind = "RAW_HISTORY", "SESSION_HISTORY", "SESSION_STORE"
        if field == "historySummaries" and isinstance(value, list) and value:
            for index, history_value in enumerate(value):
                item_payload = {
                    "field": field,
                    "collectionIndex": index,
                    "value": history_value,
                }
                content_hash = sha256_json(item_payload)
                result.append(
                    ContextItemV1(
                        itemId=f"ctxi-{field}-{index}-{content_hash[:16]}",
                        itemType="RAW_HISTORY",
                        semanticKey=f"context-pack:{field}:{index}",
                        authorityDomain="SESSION_HISTORY",
                        sourceKind="SESSION_STORE",
                        sourceRefs=(f"task:{run.task_id}:revision:{run.task_revision}",),
                        sourceRevision=run.task_revision,
                        scope=scope,
                        recipientAllowlist=(run.agent_role,),
                        phaseAllowlist=(run.phase,),
                        sensitivity="MODEL_VISIBLE",
                        validFrom=created,
                        expiresAt=None,
                        supersedesItemIds=(),
                        payload=item_payload,
                        contentHash=content_hash,
                        estimatedTokens=_estimate_tokens(item_payload),
                    )
                )
            continue
        item_payload = {"field": field, "value": value}
        content_hash = sha256_json(item_payload)
        result.append(
            ContextItemV1(
                itemId=f"ctxi-{field}-{content_hash[:16]}",
                itemType=item_type,
                semanticKey=f"context-pack:{field}",
                authorityDomain=authority,
                sourceKind=source_kind,
                sourceRefs=(f"task:{run.task_id}:revision:{run.task_revision}",),
                sourceRevision=run.task_revision,
                scope=scope,
                recipientAllowlist=(run.agent_role,),
                phaseAllowlist=(run.phase,),
                sensitivity="MODEL_VISIBLE",
                validFrom=created,
                expiresAt=None,
                supersedesItemIds=(),
                payload=item_payload,
                contentHash=content_hash,
                estimatedTokens=_estimate_tokens(item_payload),
            )
        )
    return result


class ContextReceiptStore:
    def __init__(
        self,
        *,
        client: redis.Redis | None = None,
        spool_path: str | Path | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        configured = spool_path or settings.context_receipt_spool_path
        path = Path(configured)
        self._spool_path = path if path.is_absolute() else REPOSITORY_ROOT / path
        self._ttl_seconds = max(int(ttl_seconds or settings.context_receipt_ttl_seconds), 60)

    def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(settings.redis_url, decode_responses=True)
        return self._client

    async def _save_primary(self, receipt: ContextReceiptV1) -> None:
        stored = receipt.model_copy(update={"persistence_status": "PRIMARY"})
        await self._get_client().set(
            f"{_CONTEXT_RECEIPT_KEY_PREFIX}:{receipt.receipt_id}",
            stored.model_dump_json(by_alias=True, exclude_none=False),
            ex=self._ttl_seconds,
        )

    def _append_spool_sync(self, receipt: ContextReceiptV1) -> None:
        stored = receipt.model_copy(update={"persistence_status": "SPOOL"})
        self._spool_path.parent.mkdir(parents=True, exist_ok=True)
        with _SPOOL_LOCK:
            with self._spool_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(stored.model_dump_json(by_alias=True, exclude_none=False) + "\n")
                handle.flush()

    async def _append_spool(self, receipt: ContextReceiptV1) -> None:
        await asyncio.to_thread(self._append_spool_sync, receipt)

    async def save(
        self,
        receipt: ContextReceiptV1,
        *,
        evaluation_mode: bool,
    ) -> ContextReceiptV1:
        try:
            await self._save_primary(receipt)
            return receipt.model_copy(update={"persistence_status": "PRIMARY"})
        except Exception:
            logger.exception("Primary ContextReceipt persistence failed")
        try:
            await self._append_spool(receipt)
            return receipt.model_copy(update={"persistence_status": "SPOOL"})
        except Exception as exc:
            logger.exception("ContextReceipt spool persistence failed")
            if evaluation_mode:
                raise ContextReceiptPersistenceError(
                    "evaluation ContextReceipt is not durable"
                ) from exc
            return receipt.model_copy(update={"persistence_status": "FAILED"})


_receipt_store: ContextReceiptStore | None = None


def get_context_receipt_store() -> ContextReceiptStore:
    global _receipt_store
    if _receipt_store is None:
        _receipt_store = ContextReceiptStore()
    return _receipt_store


async def compile_context_pack_shadow_v1(
    pack: Any,
    *,
    tenant_id: str,
    owner_id: str,
    session_id: str,
    task_id: str,
    task_revision: int,
    phase: Phase,
    model_call_ordinal: int,
    tool_schemas: list[dict[str, Any]],
    model_config: dict[str, Any],
    deadline_at: datetime,
    budget_tokens: int,
    history_policy: Literal["preserve", "query_focused"] = "preserve",
    query: str = "",
    persist: bool = False,
    evaluation_mode: bool = False,
    store: ContextReceiptStore | None = None,
) -> CompiledContextV1:
    """Compile a lossless shadow beside ContextPack without changing its output."""

    candidate_scope = getattr(pack, "candidate_scope_state", None)
    scope_id = None
    scope_revision = None
    scope_hash = None
    if isinstance(candidate_scope, dict):
        scope_id = candidate_scope.get("scopeId")
        scope_revision = candidate_scope.get("sourceRevision")
        scope_hash = sha256_json(candidate_scope)
    grant_payload = {
        "agentRole": "SHOPPING_AGENT",
        "phase": phase,
        "allowedToolNames": sorted(
            schema.get("function", {}).get("name", "")
            for schema in tool_schemas
            if isinstance(schema, dict)
        ),
    }
    run = RunContextV1(
        runId=getattr(pack, "run_id"),
        parentRunId=None,
        handoffId=None,
        tenantId=tenant_id,
        ownerId=owner_id,
        sessionId=session_id,
        recipientType="SELF",
        recipientId=owner_id,
        taskId=task_id,
        taskRevision=task_revision,
        agentRole="SHOPPING_AGENT",
        phase=phase,
        modelCallOrdinal=model_call_ordinal,
        candidateScopeId=scope_id,
        candidateScopeSourceRevision=scope_revision,
        candidateScopeHash=scope_hash,
        deadlineAt=deadline_at,
        compilerVersion="context-compiler-v1",
        policyVersion=(
            "context-policy-query-focused-v1"
            if history_policy == "query_focused"
            else "context-policy-semantic-preserve-v1"
        ),
        capabilityGrantId="shopping-agent-read-v1",
        capabilityGrantHash=sha256_json(grant_payload),
        sensitivity="SERVER_ONLY",
    )
    items = context_items_from_pack(pack, run)
    compiled = compile_context_v1(
        run,
        items,
        budget_tokens=budget_tokens,
        tool_schema_hash=sha256_json(tool_schemas),
        model_config_hash=sha256_json(model_config),
        query=query,
        history_policy=history_policy,
    )
    expected = pack.model_dump(
        by_alias=True,
        mode="json",
        exclude={"run_id", "runId"},
    )
    expected.pop("runId", None)
    expected.pop("run_id", None)
    if history_policy == "preserve" and compiled.model_view != expected:
        raise ContextIntegrityError("CTX1a shadow is not semantically lossless")
    if persist:
        persisted = await (store or get_context_receipt_store()).save(
            compiled.receipt,
            evaluation_mode=evaluation_mode,
        )
        compiled = compiled.model_copy(update={"receipt": persisted})
    return compiled


__all__ = [
    "CompiledContextV1",
    "ContextBudgetExceeded",
    "ContextIntegrityError",
    "ContextItemScopeV1",
    "ContextItemV1",
    "ContextReceiptPersistenceError",
    "ContextReceiptStore",
    "ContextReceiptV1",
    "RunContextV1",
    "canonical_json",
    "compile_context_v1",
    "compile_context_pack_shadow_v1",
    "context_items_from_pack",
    "get_context_receipt_store",
    "sha256_json",
]
