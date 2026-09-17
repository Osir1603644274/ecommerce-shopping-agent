"""Redacted, per-provider-call receipts for Context + Multi-Agent V1.

The receipt never stores prompts, user text, tool arguments, credentials, model
output, candidate data, or evidence bodies.  It records only call identity,
binding identity, provider usage, latency, retry ordinal, and terminal status.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

import redis.asyncio as redis
from pydantic import BaseModel, ConfigDict, Field

from .settings import REPOSITORY_ROOT, settings


logger = logging.getLogger(__name__)

CallPurpose = Literal[
    "task_manager_relation",
    "task_state_extraction",
    "shopping_policy_decision",
    "research_policy_decision",
    "research_synthesis",
    "final_answer",
    "memory_candidate_extraction",
    "contract_authoring",
]
AgentRole = Literal["SYSTEM", "SHOPPING_AGENT", "EVIDENCE_RESEARCH_AGENT"]
ReceiptStatus = Literal["STARTED", "SUCCEEDED", "FAILED", "CANCELLED"]
TokenStatus = Literal["OBSERVED", "NOT_INSTRUMENTED"]
PersistenceStatus = Literal["PRIMARY", "SPOOL", "FAILED", "DISABLED", "EMPTY"]

_RECEIPT_KEY_PREFIX = "agent-model-call-receipt-v1"
_SPOOL_LOCK = threading.Lock()


class ModelCallReceipt(BaseModel):
    """One redacted record for exactly one real provider request."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["model-call-receipt-v1"] = Field(
        default="model-call-receipt-v1", alias="schemaVersion"
    )
    model_call_id: str = Field(alias="modelCallId", min_length=1, max_length=200)
    run_id: str = Field(alias="runId", min_length=1, max_length=200)
    parent_run_id: str | None = Field(
        default=None, alias="parentRunId", min_length=1, max_length=200
    )
    handoff_id: str | None = Field(
        default=None, alias="handoffId", min_length=1, max_length=200
    )
    context_receipt_id: str | None = Field(
        default=None, alias="contextReceiptId", min_length=1, max_length=200
    )
    context_binding_hash: str | None = Field(
        default=None, alias="contextBindingHash", pattern=r"^[0-9a-f]{64}$"
    )
    call_purpose: CallPurpose = Field(alias="callPurpose")
    agent_role: AgentRole | None = Field(default=None, alias="agentRole")
    provider: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=200)
    input_tokens: int | None = Field(default=None, alias="inputTokens", ge=0)
    output_tokens: int | None = Field(default=None, alias="outputTokens", ge=0)
    cached_input_tokens: int | None = Field(
        default=None, alias="cachedInputTokens", ge=0
    )
    token_status: TokenStatus = Field(alias="tokenStatus")
    duration_ms: float = Field(alias="durationMs", ge=0)
    retry_ordinal: int = Field(alias="retryOrdinal", ge=0)
    status: ReceiptStatus
    error_code: str | None = Field(
        default=None, alias="errorCode", max_length=200
    )


class ModelCallReceiptPersistenceError(RuntimeError):
    """Raised when evaluation mode cannot durably persist its receipts."""


def new_model_call_id() -> str:
    return "mcall-" + uuid.uuid4().hex


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def provider_usage(response: Any | None) -> tuple[int | None, int | None, int | None, TokenStatus]:
    """Return provider-reported usage without turning absence into zero."""

    usage = _field(response, "usage") if response is not None else None
    prompt = _field(usage, "prompt_tokens") if usage is not None else None
    completion = _field(usage, "completion_tokens") if usage is not None else None
    details = _field(usage, "prompt_tokens_details") if usage is not None else None
    cached = _field(details, "cached_tokens") if details is not None else None
    if type(prompt) is int and prompt >= 0 and type(completion) is int and completion >= 0:
        return (
            prompt,
            completion,
            cached if type(cached) is int and cached >= 0 else None,
            "OBSERVED",
        )
    return None, None, None, "NOT_INSTRUMENTED"


def build_model_call_receipt(
    *,
    run_id: str,
    call_purpose: CallPurpose,
    agent_role: AgentRole | None,
    provider: str,
    model: str,
    duration_ms: float,
    retry_ordinal: int,
    failed: bool,
    response: Any | None = None,
    model_call_id: str | None = None,
    parent_run_id: str | None = None,
    handoff_id: str | None = None,
    context_receipt_id: str | None = None,
    context_binding_hash: str | None = None,
    error_code: str | None = None,
) -> ModelCallReceipt:
    input_tokens, output_tokens, cached_tokens, token_status = provider_usage(response)
    return ModelCallReceipt(
        modelCallId=model_call_id or new_model_call_id(),
        runId=run_id,
        parentRunId=parent_run_id,
        handoffId=handoff_id,
        contextReceiptId=context_receipt_id,
        contextBindingHash=context_binding_hash,
        callPurpose=call_purpose,
        agentRole=agent_role,
        provider=provider,
        model=model,
        inputTokens=input_tokens,
        outputTokens=output_tokens,
        cachedInputTokens=cached_tokens,
        tokenStatus=token_status,
        durationMs=max(float(duration_ms), 0.0),
        retryOrdinal=max(int(retry_ordinal), 0),
        status="FAILED" if failed else "SUCCEEDED",
        errorCode=(error_code or "provider_call_failed") if failed else None,
    )


class ModelCallReceiptStore:
    """Redis primary store with a redacted append-only local spool fallback."""

    def __init__(
        self,
        *,
        client: redis.Redis | None = None,
        spool_path: str | Path | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._client = client
        configured = spool_path or settings.model_call_receipt_spool_path
        path = Path(configured)
        self._spool_path = path if path.is_absolute() else REPOSITORY_ROOT / path
        self._ttl_seconds = max(
            int(ttl_seconds or settings.model_call_receipt_ttl_seconds), 60
        )

    def _get_client(self) -> redis.Redis:
        if self._client is None:
            self._client = redis.from_url(settings.redis_url, decode_responses=True)
        return self._client

    async def _save_primary(self, receipts: list[ModelCallReceipt]) -> None:
        client = self._get_client()
        pipe = client.pipeline(transaction=True)
        run_ids: set[str] = set()
        for receipt in receipts:
            payload = receipt.model_dump_json(by_alias=True, exclude_none=False)
            key = f"{_RECEIPT_KEY_PREFIX}:{receipt.model_call_id}"
            pipe.set(key, payload, ex=self._ttl_seconds)
            run_ids.add(receipt.run_id)
            index_key = f"{_RECEIPT_KEY_PREFIX}:run:{receipt.run_id}"
            pipe.rpush(index_key, receipt.model_call_id)
            pipe.expire(index_key, self._ttl_seconds)
        await pipe.execute()

    def _append_spool_sync(self, receipts: list[ModelCallReceipt]) -> None:
        self._spool_path.parent.mkdir(parents=True, exist_ok=True)
        lines = "".join(
            receipt.model_dump_json(by_alias=True, exclude_none=False) + "\n"
            for receipt in receipts
        )
        with _SPOOL_LOCK:
            with self._spool_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(lines)
                handle.flush()

    async def _append_spool(self, receipts: list[ModelCallReceipt]) -> None:
        await asyncio.to_thread(self._append_spool_sync, receipts)

    async def save_many(
        self,
        receipts: list[ModelCallReceipt],
        *,
        evaluation_mode: bool,
    ) -> PersistenceStatus:
        if not receipts:
            return "EMPTY"
        try:
            await self._save_primary(receipts)
            return "PRIMARY"
        except Exception:
            logger.exception("Primary model-call receipt persistence failed")
        try:
            await self._append_spool(receipts)
            return "SPOOL"
        except Exception as exc:
            logger.exception("Model-call receipt spool persistence failed")
            if evaluation_mode:
                raise ModelCallReceiptPersistenceError(
                    "evaluation model-call receipts are not durable"
                ) from exc
            return "FAILED"


_store: ModelCallReceiptStore | None = None


def get_model_call_receipt_store() -> ModelCallReceiptStore:
    global _store
    if _store is None:
        _store = ModelCallReceiptStore()
    return _store


async def persist_model_call_receipts(
    raw_receipts: list[dict[str, Any]],
    *,
    evaluation_mode: bool = False,
    store: ModelCallReceiptStore | None = None,
) -> PersistenceStatus:
    receipts = [ModelCallReceipt.model_validate(item) for item in raw_receipts]
    return await (store or get_model_call_receipt_store()).save_many(
        receipts,
        evaluation_mode=evaluation_mode,
    )


def receipt_json(receipt: ModelCallReceipt) -> dict[str, Any]:
    """Stable JSON projection used by spans, tests, and experiment receipts."""

    return json.loads(receipt.model_dump_json(by_alias=True, exclude_none=False))


__all__ = [
    "AgentRole",
    "CallPurpose",
    "ModelCallReceipt",
    "ModelCallReceiptPersistenceError",
    "ModelCallReceiptStore",
    "PersistenceStatus",
    "build_model_call_receipt",
    "get_model_call_receipt_store",
    "persist_model_call_receipts",
    "provider_usage",
    "receipt_json",
]
