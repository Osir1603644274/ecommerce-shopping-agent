"""Default-off internal capability for the formal Context A/B evaluator.

Nothing in HTTP schemas or settings can construct this object.  The normal web
path never imports the issuer and therefore remains byte-for-byte unchanged.
Receipts retain hashes and counters only; prompts, arguments and results are
hashed in memory and are never stored by this module.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Literal


EvaluationArm = Literal["RAW_FULL_CONTROL", "CONTEXT_TREATMENT"]
_ISSUER = object()


class EvaluationContextBoundaryError(RuntimeError):
    pass


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return _jsonable(dump(mode="json"))
    if is_dataclass(value):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {
            str(key): _jsonable(child)
            for key, child in raw.items()
            if not str(key).startswith("_")
        }
    raise EvaluationContextBoundaryError("evaluation_value_not_canonically_hashable")


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    def field(name: str) -> Any:
        return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    prompt = field("prompt_tokens")
    completion = field("completion_tokens")
    if type(prompt) is not int or prompt < 0 or type(completion) is not int or completion < 0:
        raise EvaluationContextBoundaryError("evaluation_provider_usage_missing")
    return {
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": prompt + completion,
    }


@dataclass(frozen=True, slots=True)
class EvaluationLaneIdentity:
    run_id: str
    task_id: str
    session_id: str


class EvaluationCallLedger:
    def __init__(self) -> None:
        self.model_calls: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.context_receipts: list[dict[str, Any]] = []

    def snapshot(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "modelCalls": [dict(item) for item in self.model_calls],
            "toolCalls": [dict(item) for item in self.tool_calls],
            "contextReceipts": [dict(item) for item in self.context_receipts],
        }


class _RecordingCompletions:
    def __init__(self, create: Any, capability: "EvaluationContextArmCapability") -> None:
        self._create = create
        self._capability = capability

    async def create(self, **kwargs: Any) -> Any:
        request_hash = _sha(kwargs)
        call_id = "mcall-eval-" + uuid.uuid4().hex
        started = time.perf_counter()
        try:
            response = await self._create(**kwargs)
            result_hash = _sha(_jsonable(response))
            usage = _usage(response)
        except Exception as exc:
            self._capability.ledger.model_calls.append({
                "callId": call_id, "callType": "model",
                "model": str(kwargs.get("model") or self._capability.model),
                "status": "FAILED", "durationMs": (time.perf_counter() - started) * 1000.0,
                "usage": {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0},
                "requestSha256": request_hash,
                "resultSha256": _sha({"errorType": type(exc).__name__}),
            })
            raise
        self._capability.ledger.model_calls.append({
            "callId": call_id, "callType": "model",
            "model": str(kwargs.get("model") or self._capability.model),
            "status": "SUCCEEDED", "durationMs": (time.perf_counter() - started) * 1000.0,
            "usage": usage, "requestSha256": request_hash, "resultSha256": result_hash,
        })
        return response


class _RecordingClient:
    def __init__(self, raw: Any, capability: "EvaluationContextArmCapability") -> None:
        create = getattr(getattr(raw, "chat", None), "completions", None)
        if create is None or not callable(getattr(create, "create", None)):
            raise EvaluationContextBoundaryError("evaluation_model_client_invalid")
        self.chat = SimpleNamespace(
            completions=_RecordingCompletions(create.create, capability)
        )


class _RecordingToolTransport:
    def __init__(self, raw: Any, capability: "EvaluationContextArmCapability") -> None:
        if not callable(raw):
            raise EvaluationContextBoundaryError("evaluation_tool_transport_invalid")
        self._raw = raw
        self._capability = capability
        identity = getattr(raw, "transport_identity", None)
        if identity is not None:
            self.transport_identity = identity

    async def __call__(
        self, tool_name: str, arguments: dict[str, Any], *, execution_context: Any = None,
    ) -> Any:
        request_hash = _sha({
            "toolName": tool_name,
            "arguments": arguments,
            "executionContext": _jsonable(execution_context) if execution_context is not None else None,
        })
        call_id = "tcall-eval-" + uuid.uuid4().hex
        started = time.perf_counter()
        try:
            if execution_context is None:
                result = await self._raw(tool_name, arguments)
            else:
                result = await self._raw(
                    tool_name, arguments, execution_context=execution_context
                )
            result_hash = _sha(_jsonable(result))
            status = "SUCCEEDED" if bool(getattr(result, "ok", True)) else "FAILED"
        except Exception as exc:
            self._capability.ledger.tool_calls.append({
                "callId": call_id, "callType": "tool", "toolName": tool_name,
                "status": "FAILED", "durationMs": (time.perf_counter() - started) * 1000.0,
                "requestSha256": request_hash,
                "resultSha256": _sha({"errorType": type(exc).__name__}),
            })
            raise
        self._capability.ledger.tool_calls.append({
            "callId": call_id, "callType": "tool", "toolName": tool_name,
            "status": status, "durationMs": (time.perf_counter() - started) * 1000.0,
            "requestSha256": request_hash, "resultSha256": result_hash,
        })
        return result


class EvaluationContextArmCapability:
    __slots__ = (
        "_issuer", "arm", "identity", "model", "ledger", "model_client",
        "tool_transport", "_context_binding_hash",
    )

    def __init__(
        self, issuer: object, *, arm: EvaluationArm, identity: EvaluationLaneIdentity,
        model: str, model_client: Any, tool_transport: Any,
    ) -> None:
        if issuer is not _ISSUER:
            raise EvaluationContextBoundaryError("evaluation_capability_not_internal")
        if arm not in {"RAW_FULL_CONTROL", "CONTEXT_TREATMENT"}:
            raise EvaluationContextBoundaryError("evaluation_arm_invalid")
        if not all((identity.run_id, identity.task_id, identity.session_id)):
            raise EvaluationContextBoundaryError("evaluation_lane_identity_invalid")
        self._issuer = issuer
        self.arm = arm
        self.identity = identity
        self.model = model
        self.ledger = EvaluationCallLedger()
        self._context_binding_hash: str | None = None
        self.model_client = _RecordingClient(model_client, self)
        self.tool_transport = _RecordingToolTransport(tool_transport, self)

    @property
    def context_binding_hash(self) -> str | None:
        return self._context_binding_hash

    def bind_context(self, *, binding_hash: str, semantic_hash: str, revision: int) -> None:
        if len(binding_hash) != 64 or len(semantic_hash) != 64:
            raise EvaluationContextBoundaryError("evaluation_context_hash_invalid")
        self._context_binding_hash = binding_hash
        self.ledger.context_receipts.append({
            "bindingHash": binding_hash,
            "semanticHash": semantic_hash,
            "taskRevision": revision,
            "arm": self.arm,
        })


def issue_evaluation_context_arm(
    *, arm: EvaluationArm, run_id: str, task_id: str, session_id: str,
    model: str, model_client: Any, tool_transport: Any, provider_max_retries: int,
) -> EvaluationContextArmCapability:
    """Mint an in-process capability. There is intentionally no HTTP/env issuer."""

    if provider_max_retries != 0:
        raise EvaluationContextBoundaryError("evaluation_provider_retries_must_be_zero")
    return EvaluationContextArmCapability(
        _ISSUER,
        arm=arm,
        identity=EvaluationLaneIdentity(run_id, task_id, session_id),
        model=model,
        model_client=model_client,
        tool_transport=tool_transport,
    )


def validate_evaluation_capability(
    value: EvaluationContextArmCapability | None, *, task_id: str, session_id: str | None,
) -> EvaluationContextArmCapability | None:
    if value is None:
        return None
    if type(value) is not EvaluationContextArmCapability or value._issuer is not _ISSUER:
        raise EvaluationContextBoundaryError("evaluation_capability_invalid")
    if value.identity.task_id != task_id or value.identity.session_id != session_id:
        raise EvaluationContextBoundaryError("evaluation_lane_identity_mismatch")
    return value


def apply_history_policy(
    history: list[dict[str, Any]] | None,
    capability: EvaluationContextArmCapability | None,
) -> list[dict[str, Any]] | None:
    if capability is None:
        return history
    if capability.arm == "CONTEXT_TREATMENT":
        if history not in (None, []):
            raise EvaluationContextBoundaryError("treatment_raw_history_forbidden")
        return None
    if history is None:
        raise EvaluationContextBoundaryError("control_full_history_required")
    copied = [dict(item) for item in history]
    if any(set(item) != {"role", "content"} for item in copied):
        raise EvaluationContextBoundaryError("control_history_shape_invalid")
    return copied


async def authoritative_context_pack(
    pack: Any, *, capability: EvaluationContextArmCapability | None,
    task_revision: int, phase: str, tool_schemas: list[dict[str, Any]], query: str,
) -> Any:
    if capability is None:
        return pack
    from .context_compiler_v1 import compile_context_pack_shadow_v1, sha256_json

    if capability.arm == "RAW_FULL_CONTROL":
        payload = pack.model_dump(by_alias=True, mode="json")
        binding = sha256_json({
            "arm": capability.arm,
            "runId": capability.identity.run_id,
            "taskId": capability.identity.task_id,
            "taskRevision": task_revision,
            "modelView": payload,
        })
        capability.bind_context(
            binding_hash=binding, semantic_hash=sha256_json(payload), revision=task_revision
        )
        return pack

    compiled = await compile_context_pack_shadow_v1(
        pack,
        tenant_id="local-evaluation",
        owner_id=capability.identity.session_id,
        session_id=capability.identity.session_id,
        task_id=capability.identity.task_id,
        task_revision=task_revision,
        phase=phase,
        model_call_ordinal=len(capability.ledger.context_receipts),
        tool_schemas=tool_schemas,
        model_config={"provider": "deepseek", "model": capability.model},
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        budget_tokens=4000,
        history_policy="query_focused",
        query=query,
        persist=False,
        evaluation_mode=True,
    )
    payload = dict(compiled.model_view)
    payload["runId"] = getattr(pack, "run_id")
    projected = type(pack).model_validate(payload)
    if projected.history_summaries:
        raise EvaluationContextBoundaryError("treatment_compiled_view_contains_raw_history")
    capability.bind_context(
        binding_hash=compiled.receipt.binding_hash,
        semantic_hash=compiled.receipt.semantic_hash,
        revision=task_revision,
    )
    return projected


__all__ = [
    "EvaluationCallLedger", "EvaluationContextArmCapability",
    "EvaluationContextBoundaryError", "EvaluationLaneIdentity",
    "apply_history_policy", "authoritative_context_pack",
    "issue_evaluation_context_arm", "validate_evaluation_capability",
]
