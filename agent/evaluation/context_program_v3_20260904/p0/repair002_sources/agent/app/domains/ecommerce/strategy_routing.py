"""Pure runner-owned Strategy Routing and BOUNDED_REACT contracts.

The issuer registries below are intentionally process-local.  They provide a
domain seam against ordinary Python copying and mutation; they are not a
cross-process signature or durable replay store.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol


class Strategy(StrEnum): FAST = "FAST"; PAE = "PAE"; BOUNDED_REACT = "BOUNDED_REACT"
class ReasonCode(StrEnum): SIMPLE_TASK = "SIMPLE_TASK"; STRUCTURED_MULTI_STEP = "STRUCTURED_MULTI_STEP"; DYNAMIC_OBSERVATION = "DYNAMIC_OBSERVATION"; TOOL_UNCERTAINTY = "TOOL_UNCERTAINTY"; POLICY_DENIED = "POLICY_DENIED"
class TerminalReason(StrEnum): COMPLETED = "completed"; BUDGET_EXHAUSTED = "budget_exhausted"; NEEDS_CLARIFICATION = "needs_clarification"; POLICY_DENIED = "policy_denied"; TOOL_FAILED = "tool_failed"; REVISION_CONFLICT = "revision_conflict"
class ErrorCode(StrEnum): TOOL_FAILED = "TOOL_FAILED"; POLICY_DENIED = "POLICY_DENIED"; REVISION_CONFLICT = "REVISION_CONFLICT"

READ_ONLY_TOOL_ALLOWLIST = frozenset({"search_products", "get_product_details", "compare_products"})
_ROUTE_POLICY_VERSION = "strategy-routing-v1"
_MAX_TRANSITIONS, _MAX_TOOL_CALLS, _MAX_REPLANS = 128, 256, 32
_MAX_TOKENS, _MAX_ELAPSED_MS = 2_000_000, 3_600_000
_MAX_FUTURE_SKEW = timedelta(seconds=5)
_SENSITIVE_TEXT = re.compile(r"(?:bearer|authorization|password|diagnosis|anaphylaxis|raw[_ -]?prompt|access[_ -]?token)", re.I)
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PATCH_FIELDS = frozenset({"unknowns", "candidateScope", "currentAction"})
_CAPS = tuple(object() for _ in range(10))
_OBS_CAP, _ROUTE_CAP, _BUDGET_CAP, _REQUEST_CAP, _DELTA_CAP, _RECEIPT_CAP, _COMMIT_CAP, _LEDGER_CAP, _TERMINAL_CAP, _ISSUER_CAP = _CAPS


def _strict_text(value: object, field: str, *, maximum: int = 128) -> str:
    if type(value) is not str or not value or value.strip() != value or len(value.encode("utf-8")) > maximum or _SENSITIVE_TEXT.search(value):
        raise ValueError(f"{field} must be a bounded non-sensitive string")
    return value


def _opaque(value: object, field: str) -> str:
    if type(value) is str and _SENSITIVE_TEXT.search(value): raise ValueError(f"{field} contains forbidden semantic text")
    value = _strict_text(value, field, maximum=64)
    if not value.isascii() or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in value): raise ValueError(f"{field} must be an opaque controlled identifier")
    if any(word in value.lower() for word in ("diagnosis", "anaphylaxis", "bearer", "token", "authorization", "password")): raise ValueError(f"{field} contains forbidden semantic text")
    return value


def _strict_int(value: object, field: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum: raise ValueError(f"{field} must be a bounded strict integer")
    return value


def _canonical_value(value: object) -> object:
    if value is None or type(value) is bool or type(value) is int: return value
    if type(value) is float:
        if value != value or value in {float("inf"), float("-inf")}: raise ValueError("JSON number must be finite")
        return value
    if type(value) is str:
        if len(value.encode("utf-8")) > 512 or _SENSITIVE_TEXT.search(value): raise ValueError("JSON string is sensitive or too long")
        return value
    if type(value) in {list, tuple}: return [_canonical_value(item) for item in value]
    if type(value) is dict:
        result: dict[str, object] = {}
        for key, nested in value.items():
            if type(key) is not str or not key.isascii() or unicodedata.normalize("NFKC", key) != key: raise ValueError("JSON keys must be canonical ASCII strings")
            if key in result: raise ValueError("duplicate JSON key")
            result[key] = _canonical_value(nested)
        return result
    raise ValueError("only exact plain JSON values are accepted")


def _json_bytes(value: object) -> bytes:
    return json.dumps(_canonical_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _json_object(raw: bytes, field: str) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result: raise ValueError(f"{field} contains duplicate key")
            result[key] = value
        return result
    try: parsed = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("non-finite JSON")))
    except (json.JSONDecodeError, ValueError) as exc: raise ValueError(f"{field} must be canonical plain JSON") from exc
    if type(parsed) is not dict or _json_bytes(parsed) != raw: raise ValueError(f"{field} must be canonical JSON object")
    return parsed


def _digest(raw: bytes) -> str: return hashlib.sha256(raw).hexdigest()
def _strict_digest(value: object, field: str) -> str:
    if type(value) is not str or not _DIGEST_RE.fullmatch(value): raise ValueError(f"{field} must be lowercase SHA-256 hex")
    return value
def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None: raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


class MonotonicClock(Protocol):
    def now_ms(self) -> int: ...
class UtcClock(Protocol):
    def now_utc(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class _Record:
    obj: object
    nonce: object
    raw: bytes


_REGISTRIES: dict[str, dict[int, _Record]] = {name: {} for name in ("obs", "route", "budget", "request", "delta", "receipt", "commit", "ledger", "terminal")}


def _register(kind: str, obj: object, raw: bytes, nonce: object) -> object:
    _REGISTRIES[kind][id(obj)] = _Record(obj, nonce, bytes(raw)); return obj


def _issued(kind: str, value: object, expected_type: type, nonce: object, canonical: bytes) -> bytes:
    message = f"{kind} was not runner-issued" if kind in {"obs", "route"} else f"{kind} was not issued by runner"
    if type(value) is not expected_type: raise PermissionError(message)
    record = _REGISTRIES[kind].get(id(value))
    if record is None or record.obj is not value or record.nonce is not nonce: raise PermissionError(message)
    if canonical != record.raw: raise ValueError(f"{kind} signed snapshot was mutated")
    return record.raw


@dataclass(frozen=True, slots=True)
class _IssuedObservation:
    raw: bytes
    nonce: object


@dataclass(frozen=True, slots=True)
class RouteDecision:
    strategy: Strategy
    reason_code: ReasonCode
    policy_version: str
    signals_digest: str
    _nonce: object


def _observation_plain(raw: object) -> dict[str, object]:
    if type(raw) is not dict or set(raw) != {"taskKind", "planSteps", "needsObservation", "dynamicRevision", "toolUncertainty", "policyDenied"}: raise ValueError("server observation has unknown or missing fields")
    if raw["taskKind"] not in {"simple", "structured"} or type(raw["taskKind"]) is not str: raise ValueError("server observation taskKind is invalid")
    steps = _strict_int(raw["planSteps"], "planSteps", minimum=1, maximum=32)
    if any(type(raw[name]) is not bool for name in ("needsObservation", "dynamicRevision", "toolUncertainty", "policyDenied")): raise ValueError("server observation flags must be strict booleans")
    if raw["taskKind"] == "simple" and steps != 1: raise ValueError("conflicting simple and multi-step signals")
    return {"taskKind": raw["taskKind"], "planSteps": steps, **{name: raw[name] for name in ("needsObservation", "dynamicRevision", "toolUncertainty", "policyDenied")}}


class RunnerRouteAuthority:
    def observe_server(self, raw: object) -> _IssuedObservation:
        canonical = _json_bytes(_observation_plain(raw)); issued = _IssuedObservation(canonical, _OBS_CAP); return _register("obs", issued, canonical, _OBS_CAP)  # type: ignore[return-value]
    def route(self, observation: object) -> RouteDecision:
        if type(observation) is not _IssuedObservation: raise PermissionError("observation was not runner-issued")
        raw = _issued("obs", observation, _IssuedObservation, _OBS_CAP, _json_bytes(_observation_plain(_json_object(observation.raw, "server observation"))))
        values = _observation_plain(_json_object(raw, "server observation"))
        if values["policyDenied"]: strategy, reason = Strategy.BOUNDED_REACT, ReasonCode.POLICY_DENIED
        elif values["needsObservation"] or values["dynamicRevision"]: strategy, reason = Strategy.BOUNDED_REACT, ReasonCode.DYNAMIC_OBSERVATION
        elif values["toolUncertainty"]: strategy, reason = Strategy.BOUNDED_REACT, ReasonCode.TOOL_UNCERTAINTY
        elif values["taskKind"] == "simple": strategy, reason = Strategy.FAST, ReasonCode.SIMPLE_TASK
        else: strategy, reason = Strategy.PAE, ReasonCode.STRUCTURED_MULTI_STEP
        raw_decision = _json_bytes({"strategy": strategy.value, "reasonCode": reason.value, "policyVersion": _ROUTE_POLICY_VERSION, "signalsDigest": _digest(raw)})
        decision = RouteDecision(strategy, reason, _ROUTE_POLICY_VERSION, _digest(raw), _ROUTE_CAP); return _register("route", decision, raw_decision, _ROUTE_CAP)  # type: ignore[return-value]


def _route_plain(value: RouteDecision) -> dict[str, object]: return {"strategy": value.strategy.value, "reasonCode": value.reason_code.value, "policyVersion": value.policy_version, "signalsDigest": value.signals_digest}
def serialize_route_decision(value: object) -> bytes:
    raw = _issued("route", value, RouteDecision, _ROUTE_CAP, _json_bytes(_route_plain(value)))
    _strict_digest(value.signals_digest, "signalsDigest")
    if value.policy_version != _ROUTE_POLICY_VERSION: raise ValueError("route decision policy binding is invalid")
    valid = {(Strategy.FAST, ReasonCode.SIMPLE_TASK), (Strategy.PAE, ReasonCode.STRUCTURED_MULTI_STEP), (Strategy.BOUNDED_REACT, ReasonCode.DYNAMIC_OBSERVATION), (Strategy.BOUNDED_REACT, ReasonCode.TOOL_UNCERTAINTY), (Strategy.BOUNDED_REACT, ReasonCode.POLICY_DENIED)}
    if (value.strategy, value.reason_code) not in valid: raise ValueError("route decision strategy/reason mismatch")
    return raw


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    max_transitions: int; max_tool_calls: int; max_replans: int; max_tokens: int; max_elapsed_ms: int
    @classmethod
    def from_plain(cls, raw: object) -> BudgetLimits:
        if type(raw) is not dict or set(raw) != {"maxTransitions", "maxToolCalls", "maxReplans", "maxTokens", "maxElapsedMs"}: raise ValueError("budget limits have unknown or missing fields")
        return cls(_strict_int(raw["maxTransitions"], "maxTransitions", minimum=1, maximum=_MAX_TRANSITIONS), _strict_int(raw["maxToolCalls"], "maxToolCalls", minimum=1, maximum=_MAX_TOOL_CALLS), _strict_int(raw["maxReplans"], "maxReplans", minimum=1, maximum=_MAX_REPLANS), _strict_int(raw["maxTokens"], "maxTokens", minimum=1, maximum=_MAX_TOKENS), _strict_int(raw["maxElapsedMs"], "maxElapsedMs", minimum=1, maximum=_MAX_ELAPSED_MS))
    def plain(self) -> dict[str, int]: return {"maxTransitions": self.max_transitions, "maxToolCalls": self.max_tool_calls, "maxReplans": self.max_replans, "maxTokens": self.max_tokens, "maxElapsedMs": self.max_elapsed_ms}


@dataclass(frozen=True, slots=True)
class BudgetState:
    limits: BudgetLimits; transitions_used: int; tool_calls_used: int; replans_used: int; tokens_used: int; elapsed_ms_used: int; _last_tick: int; _authority_clock: MonotonicClock; _nonce: object
    @classmethod
    def start(cls, limits: object, *, clock: MonotonicClock) -> BudgetState:
        if not callable(getattr(clock, "now_ms", None)):
            raise TypeError("authority clock must expose now_ms")
        validated = BudgetLimits.from_plain(limits.plain() if type(limits) is BudgetLimits else limits); tick = _strict_int(clock.now_ms(), "monotonic clock", minimum=0, maximum=10**15)
        state = cls(validated, 0, 0, 0, 0, 0, tick, clock, _BUDGET_CAP); return _register("budget", state, _budget_bytes(state), _BUDGET_CAP)  # type: ignore[return-value]
    def consume(self, *, transitions: int = 0, tool_calls: int = 0, replans: int = 0, tokens: int = 0) -> BudgetState:
        _verify_budget(self)
        values = (transitions, tool_calls, replans, tokens)
        if any(type(value) is not int or value < 0 for value in values) or not any(values): raise ValueError("budget consumption must contain positive strict integer work")
        tick = _strict_int(self._authority_clock.now_ms(), "monotonic clock", minimum=0, maximum=10**15)
        if tick < self._last_tick: raise ValueError("authoritative monotonic clock moved backwards")
        used = (self.transitions_used + transitions, self.tool_calls_used + tool_calls, self.replans_used + replans, self.tokens_used + tokens, self.elapsed_ms_used + tick - self._last_tick)
        caps = (self.limits.max_transitions, self.limits.max_tool_calls, self.limits.max_replans, self.limits.max_tokens, self.limits.max_elapsed_ms)
        if any(value > cap for value, cap in zip(used, caps)): raise ValueError("bounded budget exhausted")
        state = BudgetState(BudgetLimits.from_plain(self.limits.plain()), *used, tick, self._authority_clock, _BUDGET_CAP); return _register("budget", state, _budget_bytes(state), _BUDGET_CAP)  # type: ignore[return-value]


def _budget_plain(value: BudgetState) -> dict[str, object]: return {"limits": value.limits.plain(), "transitionsUsed": value.transitions_used, "toolCallsUsed": value.tool_calls_used, "replansUsed": value.replans_used, "tokensUsed": value.tokens_used, "elapsedMsUsed": value.elapsed_ms_used, "lastTick": value._last_tick, "authorityClockId": id(value._authority_clock)}
def _budget_bytes(value: BudgetState) -> bytes: return _json_bytes(_budget_plain(value))
def _verify_budget(value: BudgetState) -> None: _issued("budget", value, BudgetState, _BUDGET_CAP, _budget_bytes(value))


_TOOL_FIELDS = {"search_products": {"query", "limit", "category"}, "get_product_details": {"productId"}, "compare_products": {"productIds"}}
def _tool_args(tool: object, args: object) -> dict[str, object]:
    if type(tool) is not str or tool not in READ_ONLY_TOOL_ALLOWLIST or type(args) is not dict: raise PermissionError("only allowlisted read tool plain arguments are accepted")
    if set(args) != _TOOL_FIELDS[tool] and not (tool == "search_products" and set(args) in ({"query", "limit"}, {"query", "category"}, {"query", "limit", "category"})): raise ValueError("tool arguments contain unknown or missing fields")
    if tool == "search_products":
        result: dict[str, object] = {"query": _strict_text(args["query"], "query", maximum=128)}
        if "limit" in args: result["limit"] = _strict_int(args["limit"], "limit", minimum=1, maximum=50)
        if "category" in args: result["category"] = _strict_text(args["category"], "category", maximum=64)
        return result
    if tool == "get_product_details": return {"productId": _strict_int(args["productId"], "productId", minimum=1, maximum=2**63 - 1)}
    ids = args["productIds"]
    if type(ids) is not list or not 2 <= len(ids) <= 5 or any(type(item) is not int or item <= 0 for item in ids) or len(set(ids)) != len(ids): raise ValueError("productIds must be a unique strict integer list")
    return {"productIds": list(ids)}


@dataclass(frozen=True, slots=True)
class BoundedToolRequest:
    tool_name: str; canonical_args: bytes; args_digest: str; state_revision: int; step_id: str; _nonce: object
    @classmethod
    def create(cls, *, tool_name: object, arguments: object, state_revision: object, step_id: object) -> BoundedToolRequest:
        args = _tool_args(tool_name, arguments); raw = _json_bytes(args); request = cls(tool_name, raw, _digest(raw), _strict_int(state_revision, "stateRevision", minimum=1, maximum=2**31 - 1), _opaque(step_id, "stepId"), _REQUEST_CAP); return _register("request", request, _request_bytes(request), _REQUEST_CAP)  # type: ignore[return-value]
    def canonical_args_text(self) -> str: return self.canonical_args.decode("utf-8")
def _request_bytes(value: BoundedToolRequest) -> bytes: return _json_bytes({"toolName": value.tool_name, "canonicalArgs": json.loads(value.canonical_args), "argsDigest": value.args_digest, "stateRevision": value.state_revision, "stepId": value.step_id})
def _request(value: object) -> BoundedToolRequest:
    if type(value) is not BoundedToolRequest: raise PermissionError("tool request was not runner-issued")
    _issued("request", value, BoundedToolRequest, _REQUEST_CAP, _request_bytes(value)); args = _tool_args(value.tool_name, _json_object(value.canonical_args, "canonicalArgs"))
    if _json_bytes(args) != value.canonical_args or _strict_digest(value.args_digest, "argsDigest") != _digest(value.canonical_args): raise ValueError("tool request canonical snapshot mismatch")
    _strict_int(value.state_revision, "stateRevision", minimum=1, maximum=2**31 - 1); _opaque(value.step_id, "stepId"); return value


def _patch_value(field: str, value: object) -> object:
    if field == "unknowns":
        if type(value) is not list or len(value) > 50 or any(type(item) is not str or not item or len(item.encode("utf-8")) > 128 or _SENSITIVE_TEXT.search(item) for item in value): raise ValueError("unknowns patch value is invalid")
        return list(value)
    if field == "candidateScope":
        if type(value) is not dict or set(value) != {"scopeId", "candidateIds"}: raise ValueError("candidateScope patch value has unknown fields")
        ids = value["candidateIds"]
        if type(ids) is not list or len(ids) > 50 or any(type(item) is not int or item <= 0 for item in ids) or len(set(ids)) != len(ids): raise ValueError("candidateScope candidateIds are invalid")
        return {"scopeId": _opaque(value["scopeId"], "scopeId"), "candidateIds": list(ids)}
    if type(value) is not dict or set(value) != {"kind", "reasonCode"} or value["kind"] not in {"clarify", "search", "compare", "recommend", "answer", "wait"}: raise ValueError("currentAction patch value is invalid")
    return {"kind": value["kind"], "reasonCode": _opaque(value["reasonCode"], "reasonCode")}
def _patch_operations(operations: object) -> list[dict[str, object]]:
    if type(operations) is not list or not operations: raise ValueError("StateDelta requires explicit patch operations")
    fields: set[str] = set(); result: list[dict[str, object]] = []
    for item in operations:
        if type(item) is not dict or type(item.get("op")) is not str or type(item.get("field")) is not str or item["field"] in fields or item["field"] not in _PATCH_FIELDS or item["op"] not in {"set", "remove"}: raise ValueError("StateDelta patch operation is invalid")
        if item["op"] == "set":
            if set(item) != {"op", "field", "value"}: raise ValueError("set patch has unknown fields")
            result.append({"op": "set", "field": item["field"], "value": _patch_value(item["field"], item["value"])})
        else:
            if set(item) != {"op", "field"}: raise ValueError("remove patch has unknown fields")
            result.append({"op": "remove", "field": item["field"]})
        fields.add(item["field"])
    return result


@dataclass(frozen=True, slots=True)
class StateDelta:
    base_revision: int; next_revision: int; canonical_patch: bytes; patch_digest: str; _nonce: object
    @classmethod
    def create(cls, *, base_revision: object, operations: object) -> StateDelta:
        base = _strict_int(base_revision, "baseRevision", minimum=1, maximum=2**31 - 2); raw = _json_bytes({"operations": _patch_operations(operations)}); delta = cls(base, base + 1, raw, _digest(raw), _DELTA_CAP); return _register("delta", delta, _delta_bytes(delta), _DELTA_CAP)  # type: ignore[return-value]
def _delta_bytes(value: StateDelta) -> bytes: return _json_bytes({"baseRevision": value.base_revision, "nextRevision": value.next_revision, "canonicalPatch": json.loads(value.canonical_patch), "patchDigest": value.patch_digest})
def _delta(value: object) -> StateDelta:
    if type(value) is not StateDelta: raise PermissionError("StateDelta was not factory-issued")
    _issued("delta", value, StateDelta, _DELTA_CAP, _delta_bytes(value)); parsed = _json_object(value.canonical_patch, "canonicalPatch")
    if set(parsed) != {"operations"} or _json_bytes({"operations": _patch_operations(parsed["operations"])}) != value.canonical_patch or value.next_revision != value.base_revision + 1 or _strict_digest(value.patch_digest, "patchDigest") != _digest(value.canonical_patch): raise ValueError("StateDelta snapshot is invalid")
    return value


@dataclass(frozen=True, slots=True)
class ToolReceipt:
    receipt_id: str; tool_name: str; step_id: str; args_digest: str; state_revision: int; outcome: str; error_code: ErrorCode | None; issued_at: datetime; _nonce: object
def _receipt_plain(value: ToolReceipt) -> dict[str, object]:
    if value.error_code is not None and type(value.error_code) is not ErrorCode: raise ValueError("receipt errorCode must be strict ErrorCode")
    return {"receiptId": _opaque(value.receipt_id, "receiptId"), "toolName": value.tool_name, "stepId": _opaque(value.step_id, "stepId"), "argsDigest": _strict_digest(value.args_digest, "argsDigest"), "stateRevision": _strict_int(value.state_revision, "stateRevision", minimum=1, maximum=2**31 - 1), "outcome": value.outcome, "errorCode": None if value.error_code is None else value.error_code.value, "issuedAt": _utc(value.issued_at, "issuedAt").isoformat()}
def _receipt_bytes(value: ToolReceipt) -> bytes: return _json_bytes(_receipt_plain(value))
_RECEIPT_FIELDS = frozenset({"receiptId", "toolName", "stepId", "argsDigest", "stateRevision", "outcome", "errorCode", "issuedAt"})


def _validate_receipt_value(value: ToolReceipt) -> None:
    _receipt_plain(value)
    _strict_text(value.tool_name, "toolName", maximum=64)
    if value.tool_name not in READ_ONLY_TOOL_ALLOWLIST or type(value.outcome) is not str or value.outcome not in {"succeeded", "failed"} or (value.outcome == "succeeded") != (value.error_code is None):
        raise ValueError("receipt contract is invalid")


def _parse_receipt_snapshot(raw: bytes) -> ToolReceipt:
    if type(raw) is not bytes:
        raise ValueError("receipt snapshot must be bytes")
    data = _json_object(raw, "receipt snapshot")
    if set(data) != _RECEIPT_FIELDS:
        raise ValueError("receipt snapshot has unknown or missing fields")
    code = data["errorCode"]
    try:
        error_code = None if code is None else ErrorCode(code)
        issued_at = datetime.fromisoformat(data["issuedAt"])
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt snapshot has invalid typed fields") from exc
    receipt = ToolReceipt(data["receiptId"], data["toolName"], data["stepId"], data["argsDigest"], data["stateRevision"], data["outcome"], error_code, issued_at, _RECEIPT_CAP)
    _validate_receipt_value(receipt)
    if _receipt_bytes(receipt) != raw:
        raise ValueError("receipt snapshot is not canonical")
    return receipt


def serialize_receipt(value: object) -> bytes:
    if type(value) is not ToolReceipt: raise PermissionError("receipt was not runner-issued")
    raw = _issued("receipt", value, ToolReceipt, _RECEIPT_CAP, _receipt_bytes(value)); _validate_receipt_value(value)
    return raw


class ReceiptIssuer:
    def __init__(self, *, clock: UtcClock) -> None: self._clock = clock
    def issue(self, request: object, *, receipt_id: object, outcome: object, error_code: ErrorCode | None = None) -> ToolReceipt:
        request = _request(request)
        if type(outcome) is not str or outcome not in {"succeeded", "failed"} or (error_code is not None and type(error_code) is not ErrorCode) or (outcome == "succeeded") != (error_code is None): raise ValueError("receipt outcome/errorCode mismatch")
        now = _utc(self._clock.now_utc(), "authoritative UTC now")
        if now > datetime.now(UTC) + _MAX_FUTURE_SKEW: raise ValueError("authoritative UTC now exceeds future skew")
        receipt = ToolReceipt(_opaque(receipt_id, "receiptId"), request.tool_name, request.step_id, request.args_digest, request.state_revision, outcome, error_code, now, _RECEIPT_CAP); return _register("receipt", receipt, _receipt_bytes(receipt), _RECEIPT_CAP)  # type: ignore[return-value]


def _restore_receipt(raw: bytes) -> ToolReceipt:
    receipt = _parse_receipt_snapshot(raw)
    _register("receipt", receipt, raw, _RECEIPT_CAP)
    serialize_receipt(receipt)
    return receipt  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class StepCommit:
    state_delta: StateDelta; receipt: ToolReceipt; _nonce: object
    @classmethod
    def commit(cls, *, current_server_revision: object, delta: object, receipt: object) -> StepCommit:
        current = _strict_int(current_server_revision, "currentServerRevision", minimum=1, maximum=2**31 - 2); delta = _delta(delta); receipt = receipt if serialize_receipt(receipt) else receipt
        if delta.base_revision != current or receipt.state_revision != current or delta.next_revision != current + 1: raise ValueError("OCC revision mismatch")
        commit = cls(delta, receipt, _COMMIT_CAP); return _register("commit", commit, _commit_bytes(commit), _COMMIT_CAP)  # type: ignore[return-value]
def _commit_bytes(value: StepCommit) -> bytes: return _json_bytes({"baseRevision": value.state_delta.base_revision, "nextRevision": value.state_delta.next_revision, "patchDigest": value.state_delta.patch_digest, "receipt": json.loads(_receipt_bytes(value.receipt))})
def serialize_step_commit(value: object) -> bytes:
    if type(value) is not StepCommit: raise PermissionError("StepCommit was not runner-issued")
    raw = _issued("commit", value, StepCommit, _COMMIT_CAP, _commit_bytes(value)); _delta(value.state_delta); serialize_receipt(value.receipt)
    if value.state_delta.base_revision != value.receipt.state_revision: raise ValueError("StepCommit revisions mismatch")
    return raw


class ReplayLedger:
    __slots__ = ("_snapshots",)
    def __init__(self, _private: object | None = None) -> None:
        if _private is not None: raise TypeError("ReplayLedger constructor does not accept caller snapshots")
        self._snapshots: dict[tuple[str, int], bytes] = {}; _register("ledger", self, _ledger_bytes(self), _LEDGER_CAP)
    def _verify(self) -> None:
        try:
            raw = _ledger_bytes(self)
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("replay ledger snapshot mutated") from exc
        _issued("ledger", self, ReplayLedger, _LEDGER_CAP, raw)
    def record(self, receipt: object) -> ReplayLedger:
        self._verify(); raw = serialize_receipt(receipt); slot = (receipt.step_id, receipt.state_revision); _validate_ledger_entry(slot, raw); existing = self._snapshots.get(slot)
        if existing is not None:
            if existing != raw: raise ValueError("replay slot conflict")
            return self
        updated = dict(self._snapshots); updated[slot] = bytes(raw); self._snapshots = updated
        _register("ledger", self, _ledger_bytes(self), _LEDGER_CAP)
        return self
    def exact_replay(self, *, step_id: object, args_digest: object, state_revision: object) -> ToolReceipt:
        self._verify(); slot = (_opaque(step_id, "stepId"), _strict_int(state_revision, "stateRevision", minimum=1, maximum=2**31 - 1)); raw = self._snapshots.get(slot)
        if raw is None: raise KeyError("exact replay receipt does not exist")
        expected_digest = _strict_digest(args_digest, "argsDigest")
        receipt = _parse_receipt_snapshot(raw)
        if (receipt.step_id, receipt.state_revision, receipt.args_digest) != (slot[0], slot[1], expected_digest):
            raise ValueError("replay slot conflict")
        return _restore_receipt(bytes(raw))


def _validate_ledger_entry(slot: object, raw: bytes) -> ToolReceipt:
    if type(slot) is not tuple or len(slot) != 2:
        raise ValueError("replay slot is invalid")
    step_id = _opaque(slot[0], "stepId")
    state_revision = _strict_int(slot[1], "stateRevision", minimum=1, maximum=2**31 - 1)
    receipt = _parse_receipt_snapshot(raw)
    if (receipt.step_id, receipt.state_revision) != (step_id, state_revision):
        raise ValueError("replay slot does not match receipt snapshot")
    return receipt


def _ledger_bytes(value: ReplayLedger) -> bytes:
    entries: list[dict[str, object]] = []
    for slot, raw in value._snapshots.items():
        _validate_ledger_entry(slot, raw)
        entries.append({"stepId": slot[0], "stateRevision": slot[1], "receipt": json.loads(raw)})
    entries.sort(key=lambda item: (item["stepId"], item["stateRevision"]))
    return _json_bytes({"snapshots": entries})


@dataclass(frozen=True, slots=True)
class BoundedReactTerminal:
    reason: TerminalReason; state_revision: int; _nonce: object
    @classmethod
    def create(cls, *, reason: TerminalReason, state_revision: object) -> BoundedReactTerminal:
        if type(reason) is not TerminalReason: raise ValueError("terminal reason must be exact TerminalReason enum")
        terminal = cls(reason, _strict_int(state_revision, "stateRevision", minimum=1, maximum=2**31 - 1), _TERMINAL_CAP); return _register("terminal", terminal, _terminal_bytes(terminal), _TERMINAL_CAP)  # type: ignore[return-value]
def _terminal_bytes(value: BoundedReactTerminal) -> bytes: return _json_bytes({"reason": value.reason.value, "stateRevision": value.state_revision})
def serialize_terminal(value: object) -> bytes:
    if type(value) is not BoundedReactTerminal: raise PermissionError("terminal was not runner-issued")
    return _issued("terminal", value, BoundedReactTerminal, _TERMINAL_CAP, _terminal_bytes(value))


def _no_pickle(self: object, protocol: int) -> None:
    raise TypeError("runner-issued contract objects cannot be pickled")


for _formal_type in (RouteDecision, BudgetState, BoundedToolRequest, StateDelta, ToolReceipt, StepCommit, ReplayLedger, BoundedReactTerminal):
    _formal_type.__reduce_ex__ = _no_pickle  # type: ignore[attr-defined]


__all__ = ["BoundedReactTerminal", "BoundedToolRequest", "BudgetLimits", "BudgetState", "ErrorCode", "MonotonicClock", "READ_ONLY_TOOL_ALLOWLIST", "ReasonCode", "ReceiptIssuer", "ReplayLedger", "RunnerRouteAuthority", "StateDelta", "StepCommit", "Strategy", "TerminalReason", "UtcClock", "serialize_receipt", "serialize_route_decision", "serialize_step_commit", "serialize_terminal"]
