"""Pure, server-owned contracts for transaction fulfillment.

This module intentionally contains no transport, persistence, HTTP, MCP, or
model code. The issuer/registry objects are an in-process seam only; they are
not a substitute for production signatures or durable idempotency storage.
"""
from __future__ import annotations

import hashlib
import json
import re
import math
import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Mapping

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "AuthorityClock", "BackendQueryResult", "ConfirmationEvidence", "ConfirmationIssuer",
    "CompensationStatus", "ErrorCode", "FulfillmentAction", "FulfillmentIntent",
    "FulfillmentStatus", "ImmutablePreviewBinding", "InvalidFulfillmentTransition",
    "OwnerPrincipal", "PaymentCompensationPolicyError", "QueryReceipt", "QueryReceiptIssuer",
    "SagaExecution", "SagaReceipt", "SagaReceiptIssuer", "SagaStep", "StepKind", "StepStatus",
    "build_command_idempotency_key", "command_idempotency_key",
]


class FulfillmentStatus(StrEnum):
    PREVIEWED = "PREVIEWED"; AWAITING_CONFIRMATION = "AWAITING_CONFIRMATION"; ORDER_CREATING = "ORDER_CREATING"; ORDER_CREATED = "ORDER_CREATED"
    PAYMENT_CREATING = "PAYMENT_CREATING"; PAYMENT_PENDING = "PAYMENT_PENDING"; PAID = "PAID"; FULFILLING = "FULFILLING"; COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"; COMPENSATING = "COMPENSATING"; COMPENSATED = "COMPENSATED"; MANUAL_REVIEW = "MANUAL_REVIEW"; FAILED = "FAILED"


class FulfillmentAction(StrEnum):
    ORDER_AND_PAY = "order_and_pay"; ORDER_ONLY = "order_only"; DO_NOT_PAY = "do_not_pay"


class StepStatus(StrEnum):
    PENDING = "PENDING"; CREATING = "CREATING"; SUCCEEDED = "SUCCEEDED"; FAILED = "FAILED"; UNKNOWN = "UNKNOWN"; COMPENSATING = "COMPENSATING"; COMPENSATED = "COMPENSATED"; MANUAL_REVIEW = "MANUAL_REVIEW"


class StepKind(StrEnum):
    ORDER = "order"; PAYMENT = "payment"; FULFILLMENT = "fulfillment"; CANCEL_ORDER = "cancel_order"


class ErrorCode(StrEnum):
    TIMEOUT = "TIMEOUT"; DECLINED = "DECLINED"; BACKEND_FAILED_AFTER_QUERY = "BACKEND_FAILED_AFTER_QUERY"; NOT_FOUND = "NOT_FOUND"; FOUND_NOT_COMMITTED = "FOUND_NOT_COMMITTED"; UNKNOWN = "UNKNOWN"


class CompensationStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"; REQUESTED = "REQUESTED"; IN_PROGRESS = "IN_PROGRESS"; COMPLETED = "COMPLETED"; FAILED = "FAILED"; PROHIBITED = "PROHIBITED"


class BackendQueryResult(StrEnum):
    NOT_FOUND = "NOT_FOUND"; FOUND_NOT_COMMITTED = "FOUND_NOT_COMMITTED"; COMMITTED = "COMMITTED"; FAILED = "FAILED"; UNKNOWN = "UNKNOWN"


class InvalidFulfillmentTransition(ValueError):
    pass


class PaymentCompensationPolicyError(ValueError):
    pass


_SENSITIVE_KEYS = frozenset({"token", "access_token", "authorization", "bearer", "prompt", "raw_prompt", "raw_message", "user_message", "session_id", "sessionid"})
_SENSITIVE_VALUE_RE = re.compile(r"(?:bearer\s+|raw[_ -]?prompt|authorization|access[_ -]?token|\btoken\b)", re.I)
_SENSITIVE_REFERENCE_RE = re.compile(r"(?:diagnosis|allerg|medical|credential|secret|bearer|token|prompt|session)", re.I)
_OPAQUE_REFERENCE_RE = re.compile(r"^[a-z][a-z0-9]{0,31}-[0-9a-f]{1,64}$")
_CANCEL_REASONS = frozenset({"PAYMENT_FAILED", "USER_REQUESTED", "ORDER_EXPIRED", "FULFILLMENT_FAILED"})
_AUTHORITY_CLOCK_CAP = object(); _OWNER_CAP = object(); _CONFIRMATION_CAP = object(); _CONFIRMATION_REGISTRY_CAP = object(); _QUERY_RECEIPT_CAP = object(); _SAGA_RECEIPT_CAP = object(); _EXECUTION_CAP = object(); _EXECUTION_TRANSITION_CAP = object()
_OWNER_REGISTRY_BY_ID: dict[int, Any] = {}; _CLOCK_REGISTRY_BY_ID: dict[int, Any] = {}
_CONFIRMATION_REGISTRY_BY_ID: dict[int, Any] = {}; _QUERY_RECEIPT_REGISTRY_BY_ID: dict[int, Any] = {}; _SAGA_RECEIPT_REGISTRY_BY_ID: dict[int, Any] = {}; _EXECUTION_REGISTRY_BY_ID: dict[int, Any] = {}


def _reject_sensitive_fields(value: Any) -> Any:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if str(key).replace("-", "_").lower() in _SENSITIVE_KEYS: raise ValueError("履约合同禁止敏感字段")
            _reject_sensitive_fields(nested)
    elif isinstance(value, (list, tuple, set)):
        for item in value: _reject_sensitive_fields(item)
    elif isinstance(value, str) and _SENSITIVE_VALUE_RE.search(value): raise ValueError("履约合同禁止敏感字符串")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"{name}不能为空")
    return value.strip()


def _owner(value: Any) -> str: return _text(value, "server owner userId")
def _digest(value: Any) -> str: return _text(value, "digest")


def _amount(value: Any) -> Decimal:
    if type(value) is bool or type(value) not in {int, float, Decimal}:
        raise ValueError("amount必须是严格Decimal number")
    if type(value) is float and not math.isfinite(value):
        raise ValueError("amount不得是NaN或Infinity")
    try:
        decimal = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount不是有效Decimal") from exc
    if not decimal.is_finite():
        raise ValueError("amount包含非有限或异常指数")
    # Decimal preserves the sign bit on zero (``-0.00`` normalizes to ``-0``).
    # The contract has one canonical zero so all preview/confirmation/command
    # digests and signed snapshots agree on 0, regardless of input spelling.
    if decimal == 0:
        return Decimal("0")
    if decimal < 0 or abs(decimal.as_tuple().exponent) > 18 or abs(decimal.adjusted()) > 38:
        raise ValueError("amount包含非有限或异常指数")
    return decimal.normalize()


def _aware_utc(value: datetime, name: str = "datetime") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None: raise ValueError(f"{name}必须带时区")
    return value.astimezone(UTC)


def _opaque(value: str | None, name: str = "backendReference") -> str | None:
    if value is None: return None
    if not isinstance(value, str) or not _OPAQUE_REFERENCE_RE.fullmatch(value) or _SENSITIVE_REFERENCE_RE.search(value): raise ValueError(f"{name}必须是opaque reference")
    return value


def _strict_json(value: Any) -> Any:
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value): raise ValueError("canonical args不得包含NaN或Infinity")
        return value
    if type(value) is str:
        if unicodedata.normalize("NFKC", value) != value: raise ValueError("canonical args包含非规范Unicode")
        return value
    if type(value) is list:
        return [_strict_json(item) for item in value]
    if type(value) is dict:
        result: dict[str, Any] = {}
        for key, nested in value.items():
            if type(key) is not str or not key.isascii() or unicodedata.normalize("NFKC", key) != key: raise ValueError("canonical args键必须是规范ASCII字符串")
            if key in result: raise ValueError("canonical args包含重复键")
            result[key] = _strict_json(nested)
        return result
    raise ValueError("canonical args只接受strict plain JSON")


def _canonical_args_value(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None: value = {}
    if type(value) is not dict: raise ValueError("canonical args必须是strict JSON object")
    _reject_sensitive_fields(value)
    return _strict_json(value)


def _canonical_args(value: Mapping[str, Any] | None) -> str:
    try:
        return json.dumps(_canonical_args_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc: raise ValueError("canonical args必须是strict JSON") from exc


def _amount_json(value: Any) -> str:
    return format(_amount(value), "f")


def _deep_plain_copy(value: Any) -> Any:
    if type(value) is dict: return {key: _deep_plain_copy(nested) for key, nested in value.items()}
    if type(value) is list: return [_deep_plain_copy(item) for item in value]
    return value


class AuthorityClock:
    __slots__ = ()
    def __new__(cls, _capability: object | None = None):
        if _capability is not _AUTHORITY_CLOCK_CAP: raise TypeError("AuthorityClock必须由server签发")
        return super().__new__(cls)
    @classmethod
    def _issue(cls) -> AuthorityClock: return cls(_AUTHORITY_CLOCK_CAP)
    def now(self) -> datetime: return datetime.now(UTC)
    def __copy__(self): raise TypeError("AuthorityClock不可copy")
    __deepcopy__ = __copy__
    def __reduce_ex__(self, protocol: int): raise TypeError("AuthorityClock不可pickle")


_AUTHORITY_CLOCK = AuthorityClock._issue()
_CLOCK_REGISTRY_BY_ID[id(_AUTHORITY_CLOCK)] = _AUTHORITY_CLOCK


def _authority_now() -> datetime:
    if _CLOCK_REGISTRY_BY_ID.get(id(_AUTHORITY_CLOCK)) is not _AUTHORITY_CLOCK:
        raise PermissionError("AuthorityClock identity不合法")
    return _AUTHORITY_CLOCK.now()


class OwnerPrincipal:
    __slots__ = ("user_id",)
    def __new__(cls, user_id: str, _capability: object | None = None):
        if _capability is not _OWNER_CAP: raise TypeError("OwnerPrincipal必须由server签发")
        obj = super().__new__(cls); obj.user_id = _owner(user_id); _OWNER_REGISTRY_BY_ID[id(obj)] = obj.user_id; return obj
    @classmethod
    def issue(cls, user_id: str) -> OwnerPrincipal: return cls(user_id, _OWNER_CAP)
    def __copy__(self): raise TypeError("OwnerPrincipal不可copy")
    __deepcopy__ = __copy__
    def __reduce_ex__(self, protocol: int): raise TypeError("OwnerPrincipal不可pickle")


def _principal(value: Any, expected: str) -> OwnerPrincipal:
    if not isinstance(value, OwnerPrincipal) or _OWNER_REGISTRY_BY_ID.get(id(value)) != value.user_id or value.user_id != expected: raise PermissionError("履约合同要求匹配的server OwnerPrincipal")
    return value


def _step_kind(value: Any) -> StepKind:
    if type(value) is not StepKind: raise ValueError("step必须是StepKind枚举")
    return value


def command_idempotency_key(*, owner_user_id: str | None = None, server_owner_user_id: str | None = None, task_id: str = "task-unknown", execution_id: str, action: FulfillmentAction = FulfillmentAction.ORDER_AND_PAY, confirmation_id: str = "confirmation-unknown", confirmation_digest: str = "confirmation-unknown", step: str, preview_digest: str, amount: int | float = 0, currency: str = "CNY", ordered_items_digest: str = "items-unknown", canonical_args: Mapping[str, Any] | None = None, session_id: str | None = None, **unexpected: Any) -> str:
    if unexpected: raise TypeError("command idempotency key不接受额外参数")
    if session_id is not None: raise ValueError("command idempotency key不得绑定sessionId")
    if owner_user_id is not None and server_owner_user_id is not None and _owner(owner_user_id) != _owner(server_owner_user_id): raise ValueError("server owner userId不一致")
    if type(action) is not FulfillmentAction: raise ValueError("action必须是FulfillmentAction枚举")
    if not isinstance(step, str) or step != step.strip(): raise ValueError("step不得含首尾空白")
    payload = {"ownerUserId": _owner(owner_user_id or server_owner_user_id or ""), "taskId": _text(task_id, "taskId"), "executionId": _text(execution_id, "executionId"), "action": action.value, "confirmationId": _text(confirmation_id, "confirmationId"), "confirmationDigest": _digest(confirmation_digest), "step": StepKind(_text(step, "step")).value, "previewDigest": _digest(preview_digest), "amount": _amount_json(amount), "currency": _text(currency, "currency").upper(), "orderedItemsDigest": _digest(ordered_items_digest), "canonicalArgs": _canonical_args_value(canonical_args)}
    return "fulfillment-v1-" + hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


build_command_idempotency_key = command_idempotency_key


class _ContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True, validate_assignment=True)
    @model_validator(mode="before")
    @classmethod
    def reject_sensitive_input(cls, value: Any) -> Any:
        _reject_sensitive_fields(value)
        if isinstance(value, Mapping):
            if "amount" in value: _amount(value["amount"])
            enum_fields = {"action": FulfillmentAction, "status": (StepStatus, FulfillmentStatus), "step": StepKind, "errorCode": ErrorCode, "error_code": ErrorCode, "compensationStatus": CompensationStatus, "compensation_status": CompensationStatus, "result": BackendQueryResult}
            for field, enum_type in enum_fields.items():
                if field in value and value[field] is not None and (type(value[field]) not in enum_type if isinstance(enum_type, tuple) else type(value[field]) is not enum_type):
                    raise ValueError(f"{field}必须是严格枚举")
        return value
    def clone(self, **update: Any):
        values = self.model_dump(); values.update(update); return type(self).model_validate(values)
    def copy(self, *args: Any, **kwargs: Any):
        # Pydantic's legacy copy() can bypass validators and issuer markers.
        # No contract object may become a formally issued clone through it.
        raise TypeError("合同对象禁止legacy copy")
    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False): return self.clone(**dict(update or {}))
    @classmethod
    def model_construct(cls, *_: Any, **__: Any): raise TypeError("合同禁止model_construct绕过验证")


class FulfillmentIntent(_ContractModel):
    owner_user_id: str = Field(alias="userId", validation_alias=AliasChoices("userId", "ownerUserId", "owner_user_id")); execution_id: str = Field(alias="executionId"); task_id: str = Field(default="task-unknown", alias="taskId"); preview_digest: str = Field(alias="previewDigest"); action: FulfillmentAction = FulfillmentAction.ORDER_AND_PAY; amount: Decimal = Decimal("0"); currency: str = "CNY"; ordered_items_digest: str = Field(default="items-unknown", alias="orderedItemsDigest"); preview_expires_at: datetime | None = Field(default=None, alias="expiresAt")
    @model_validator(mode="before")
    @classmethod
    def strict_action_input(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "action" in value and type(value["action"]) is not FulfillmentAction: raise ValueError("action必须是FulfillmentAction枚举")
        return value
    @model_validator(mode="after")
    def validate_contract(self) -> FulfillmentIntent:
        _owner(self.owner_user_id); _text(self.execution_id, "executionId"); _text(self.task_id, "taskId"); _digest(self.preview_digest); object.__setattr__(self, "amount", _amount(self.amount)); _text(self.currency, "currency"); _digest(self.ordered_items_digest)
        if self.preview_expires_at is not None and _aware_utc(self.preview_expires_at, "expiresAt") <= _authority_now(): raise ValueError("preview已过期")
        return self
    @property
    def user_id(self) -> str: return self.owner_user_id


class ImmutablePreviewBinding(_ContractModel):
    owner_user_id: str = Field(alias="userId", validation_alias=AliasChoices("userId", "ownerUserId", "owner_user_id")); execution_id: str = Field(alias="executionId"); task_id: str = Field(default="task-unknown", alias="taskId"); preview_digest: str = Field(alias="previewDigest"); action: FulfillmentAction = FulfillmentAction.ORDER_AND_PAY; amount: Decimal = Decimal("0"); currency: str = "CNY"; ordered_items_digest: str = Field(default="items-unknown", alias="orderedItemsDigest"); expires_at: datetime | None = Field(default=None, alias="expiresAt")
    @model_validator(mode="before")
    @classmethod
    def strict_action_input(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and "action" in value and type(value["action"]) is not FulfillmentAction: raise ValueError("action必须是FulfillmentAction枚举")
        return value
    @model_validator(mode="after")
    def validate_contract(self) -> ImmutablePreviewBinding:
        _owner(self.owner_user_id); _text(self.execution_id, "executionId"); _text(self.task_id, "taskId"); _digest(self.preview_digest); object.__setattr__(self, "amount", _amount(self.amount)); _text(self.currency, "currency"); _digest(self.ordered_items_digest)
        if self.expires_at is not None and _aware_utc(self.expires_at, "expiresAt") <= _authority_now(): raise ValueError("expiresAt必须是未来aware时间")
        return self
    def assert_matches(self, intent: FulfillmentIntent) -> None:
        self.assert_current()
        if (self.owner_user_id, self.execution_id, self.task_id, self.preview_digest, self.action, self.amount, self.currency.upper(), self.ordered_items_digest) != (intent.owner_user_id, intent.execution_id, intent.task_id, intent.preview_digest, intent.action, intent.amount, intent.currency.upper(), intent.ordered_items_digest): raise ValueError("confirmation preview binding不匹配")
        if (self.expires_at is None) != (intent.preview_expires_at is None) or (self.expires_at is not None and _aware_utc(self.expires_at) != _aware_utc(intent.preview_expires_at)): raise ValueError("confirmation preview binding不匹配")
    def assert_current(self) -> None:
        if self.expires_at is not None and _authority_now() >= _aware_utc(self.expires_at): raise ValueError("confirmation preview已过期")
    def assert_owner(self, server_owner_user_id: str) -> None:
        if self.owner_user_id != _owner(server_owner_user_id): raise PermissionError("履约合同owner不匹配")


def _issuer_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False): raise TypeError("server issuer对象不允许model_copy")
def _issuer_pickle(self, protocol: int): raise TypeError("server issuer对象不可pickle")
def _issuer_shallow_copy(self): raise TypeError("server issuer对象不可copy")
def _issuer_deep_copy(self, memo: dict[int, Any]): raise TypeError("server issuer对象不可deepcopy")


class ConfirmationEvidence(_ContractModel):
    confirmation_id: str = Field(alias="confirmationId"); owner_user_id: str = Field(alias="userId"); task_id: str = Field(alias="taskId"); execution_id: str = Field(alias="executionId"); action: FulfillmentAction; amount: Decimal; currency: str; ordered_items_digest: str = Field(alias="orderedItemsDigest"); preview_digest: str = Field(alias="previewDigest"); confirmation_digest: str = Field(alias="confirmationDigest"); expires_at: datetime = Field(alias="expiresAt")
    @model_validator(mode="before")
    @classmethod
    def require_issuer(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or value.get("_issuer") is not _CONFIRMATION_CAP: raise ValueError("ConfirmationEvidence必须由server issuer签发")
        value = dict(value); value.pop("_issuer", None); return value
    @classmethod
    def model_construct(cls, *_: Any, **__: Any): raise TypeError("ConfirmationEvidence禁止model_construct")
    model_copy = _issuer_copy; __reduce_ex__ = _issuer_pickle; __copy__ = _issuer_shallow_copy; __deepcopy__ = _issuer_deep_copy
    @model_validator(mode="after")
    def validate_evidence(self) -> ConfirmationEvidence:
        _owner(self.owner_user_id); _text(self.confirmation_id, "confirmationId"); _text(self.task_id, "taskId"); _text(self.execution_id, "executionId"); object.__setattr__(self, "amount", _amount(self.amount)); _text(self.currency, "currency"); _digest(self.ordered_items_digest); _digest(self.preview_digest); _digest(self.confirmation_digest); expiry = _aware_utc(self.expires_at, "expiresAt")
        if expiry <= _authority_now(): raise ValueError("confirmation已过期")
        if self.confirmation_digest != _confirmation_digest(self): raise ValueError("confirmation digest不匹配")
        return self


def _confirmation_digest(evidence: ConfirmationEvidence) -> str:
    data = {"confirmationId": evidence.confirmation_id, "userId": evidence.owner_user_id, "taskId": evidence.task_id, "executionId": evidence.execution_id, "action": evidence.action.value, "amount": _amount_json(evidence.amount), "currency": evidence.currency.upper(), "orderedItemsDigest": evidence.ordered_items_digest, "previewDigest": evidence.preview_digest, "expiresAt": _aware_utc(evidence.expires_at).isoformat()}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ConfirmationRegistry:
    def __init__(self, evidence: ConfirmationEvidence, _capability: object | None = None):
        if _capability is not _CONFIRMATION_REGISTRY_CAP: raise TypeError("confirmation registry必须由server创建")
        self.identity = id(evidence); self.evidence = evidence; self.snapshot = evidence.model_dump(mode="json"); self.used = False; self.execution_id: str | None = None; _CONFIRMATION_REGISTRY_BY_ID[self.identity] = self
    def consume(self, evidence: ConfirmationEvidence, execution: SagaExecution, owner: OwnerPrincipal) -> None:
        if id(evidence) != self.identity or evidence is not self.evidence or self.used or evidence.model_dump(mode="json") != self.snapshot: raise PermissionError("confirmation identity/replay不合法")
        _principal(owner, execution.owner_user_id); evidence.validate_evidence()
        if (evidence.owner_user_id, evidence.task_id, evidence.execution_id, evidence.action, evidence.amount, evidence.currency.upper(), evidence.ordered_items_digest, evidence.preview_digest) != (execution.owner_user_id, execution.task_id, execution.execution_id, execution.action, execution.amount, execution.currency.upper(), execution.ordered_items_digest, execution.preview_digest): raise PermissionError("confirmation绑定不匹配")
        self.used = True; self.execution_id = execution.execution_id


class ConfirmationIssuer:
    @classmethod
    def issue(cls, owner: OwnerPrincipal, intent: FulfillmentIntent, *, confirmation_id: str, expires_at: datetime | None = None) -> ConfirmationEvidence:
        _principal(owner, intent.owner_user_id); expiry = _aware_utc(expires_at or (_authority_now() + timedelta(minutes=5)), "expiresAt")
        data = {"confirmationId": _text(confirmation_id, "confirmationId"), "userId": owner.user_id, "taskId": intent.task_id, "executionId": intent.execution_id, "action": intent.action, "amount": intent.amount, "currency": intent.currency.upper(), "orderedItemsDigest": intent.ordered_items_digest, "previewDigest": intent.preview_digest, "expiresAt": expiry}
        temp = object.__new__(ConfirmationEvidence)
        for name, value in (("confirmation_id", data["confirmationId"]), ("owner_user_id", data["userId"]), ("task_id", data["taskId"]), ("execution_id", data["executionId"]), ("action", data["action"]), ("amount", data["amount"]), ("currency", data["currency"]), ("ordered_items_digest", data["orderedItemsDigest"]), ("preview_digest", data["previewDigest"]), ("expires_at", expiry)): object.__setattr__(temp, name, value)
        data["confirmationDigest"] = _confirmation_digest(temp); evidence = ConfirmationEvidence.model_validate({"_issuer": _CONFIRMATION_CAP, **data}); ConfirmationRegistry(evidence, _CONFIRMATION_REGISTRY_CAP); return evidence


class SagaStep(_ContractModel):
    step: StepKind; status: StepStatus = StepStatus.PENDING; owner_user_id: str = Field(alias="userId"); task_id: str = Field(default="task-unknown", alias="taskId"); execution_id: str = Field(alias="executionId"); action: FulfillmentAction = FulfillmentAction.ORDER_AND_PAY; confirmation_id: str = Field(default="confirmation-unknown", alias="confirmationId"); confirmation_digest: str = Field(default="confirmation-unknown", alias="confirmationDigest"); amount: Decimal = Decimal("0"); currency: str = "CNY"; ordered_items_digest: str = Field(default="items-unknown", alias="orderedItemsDigest"); preview_digest: str = Field(alias="previewDigest"); canonical_args: dict[str, Any] = Field(default_factory=dict, alias="canonicalArgs"); command_idempotency_key: str | None = Field(default=None, alias="commandIdempotencyKey"); backend_reference: str | None = Field(default=None, alias="backendReference"); error_code: ErrorCode | None = Field(default=None, alias="errorCode"); query_required: bool = Field(default=False, alias="queryRequired"); query_sequence: int = Field(default=0, alias="querySequence")
    model_copy = _issuer_copy; __reduce_ex__ = _issuer_pickle; __copy__ = _issuer_shallow_copy; __deepcopy__ = _issuer_deep_copy
    def __getattribute__(self, name: str):
        if name == "canonical_args":
            try: return _deep_plain_copy(object.__getattribute__(self, "__dict__").get("canonical_args", {}))
            except (AttributeError, TypeError): pass
        return super().__getattribute__(name)
    @model_validator(mode="after")
    def validate_step(self) -> SagaStep:
        _owner(self.owner_user_id); _text(self.task_id, "taskId"); _text(self.execution_id, "executionId"); object.__setattr__(self, "amount", _amount(self.amount)); _text(self.currency, "currency"); _digest(self.ordered_items_digest); _digest(self.preview_digest); object.__setattr__(self, "canonical_args", _canonical_args_value(self.canonical_args))
        if self.query_sequence < 0: raise ValueError("querySequence不能为负")
        expected = command_idempotency_key(owner_user_id=self.owner_user_id, task_id=self.task_id, execution_id=self.execution_id, action=self.action, confirmation_id=self.confirmation_id, confirmation_digest=self.confirmation_digest, step=self.step.value, preview_digest=self.preview_digest, amount=self.amount, currency=self.currency, ordered_items_digest=self.ordered_items_digest, canonical_args=self.canonical_args)
        if self.command_idempotency_key is None: object.__setattr__(self, "command_idempotency_key", expected)
        elif self.command_idempotency_key != expected: raise ValueError("command idempotency key未绑定完整合同输入")
        _opaque(self.backend_reference)
        if self.status is StepStatus.SUCCEEDED and (not self.backend_reference or self.error_code or self.query_required): raise ValueError("SUCCEEDED step证据不一致")
        if self.status is StepStatus.FAILED and (not self.error_code or self.backend_reference or self.query_required): raise ValueError("FAILED step必须有errorCode且不得带success reference")
        if self.status is StepStatus.UNKNOWN and (not self.query_required or self.backend_reference): raise ValueError("UNKNOWN step必须queryRequired且不得带success reference")
        if self.query_required and self.status is not StepStatus.UNKNOWN: raise ValueError("queryRequired只能绑定UNKNOWN")
        return self


class QueryReceipt(_ContractModel):
    aggregate_id: str = Field(alias="aggregateId"); execution_id: str = Field(alias="executionId"); step: StepKind; command_idempotency_key: str = Field(alias="commandIdempotencyKey"); query_sequence: int = Field(alias="querySequence"); result: BackendQueryResult; observed_at: datetime = Field(alias="observedAt"); backend_reference: str | None = Field(default=None, alias="backendReference")
    @model_validator(mode="before")
    @classmethod
    def require_issuer(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or value.get("_issuer") is not _QUERY_RECEIPT_CAP: raise ValueError("QueryReceipt必须由runner issuer签发")
        value = dict(value); value.pop("_issuer", None); return value
    @classmethod
    def model_construct(cls, *_: Any, **__: Any): raise TypeError("QueryReceipt禁止model_construct")
    model_copy = _issuer_copy; __reduce_ex__ = _issuer_pickle; __copy__ = _issuer_shallow_copy; __deepcopy__ = _issuer_deep_copy
    @model_validator(mode="after")
    def validate_receipt(self) -> QueryReceipt:
        _text(self.aggregate_id, "aggregateId"); _text(self.execution_id, "executionId")
        if self.aggregate_id != self.execution_id: raise ValueError("QueryReceipt aggregate/execution必须一致")
        if self.query_sequence <= 0: raise ValueError("querySequence必须为正整数")
        _opaque(self.backend_reference); _aware_utc(self.observed_at, "observedAt")
        if self.result is BackendQueryResult.COMMITTED and not self.backend_reference: raise ValueError("COMMITTED必须有backendReference")
        if self.result is not BackendQueryResult.COMMITTED and self.backend_reference: raise ValueError("非COMMITTED不得有backendReference")
        return self


class QueryReceiptRegistry:
    def __init__(self, receipt: QueryReceipt): self.identity = id(receipt); self.receipt = receipt; self.snapshot = receipt.model_dump(mode="json"); self.used = False; _QUERY_RECEIPT_REGISTRY_BY_ID[self.identity] = self
    def consume(self, receipt: QueryReceipt, *, execution_id: str, step: StepKind, command_key: str, sequence: int) -> BackendQueryResult:
        if id(receipt) != self.identity or receipt is not self.receipt or self.used or receipt.model_dump(mode="json") != self.snapshot: raise PermissionError("QueryReceipt identity/replay不合法")
        if receipt.execution_id != execution_id or receipt.aggregate_id != execution_id or receipt.step is not step or receipt.command_idempotency_key != command_key or receipt.query_sequence != sequence: raise PermissionError("QueryReceipt binding/sequence不匹配")
        self.used = True; return receipt.result


class QueryReceiptIssuer:
    @classmethod
    def issue(cls, execution: SagaExecution, step_name: StepKind, *, result: BackendQueryResult, backend_reference: str | None = None) -> QueryReceipt:
        _verify_execution(execution)
        kind = _step_kind(step_name); step = execution.step(kind)
        if step is None or step.status is not StepStatus.UNKNOWN or not step.query_required: raise InvalidFulfillmentTransition("只有UNKNOWN step可发行QueryReceipt")
        if type(result) is not BackendQueryResult: raise ValueError("result必须是BackendQueryResult枚举")
        receipt = QueryReceipt.model_validate({"_issuer": _QUERY_RECEIPT_CAP, "aggregateId": execution.execution_id, "executionId": execution.execution_id, "step": kind, "commandIdempotencyKey": step.command_idempotency_key, "querySequence": step.query_sequence + 1, "result": result, "observedAt": _authority_now(), "backendReference": backend_reference}); QueryReceiptRegistry(receipt); return receipt


class SagaReceipt(_ContractModel):
    execution_id: str = Field(alias="executionId"); owner_user_id: str = Field(alias="userId"); task_id: str = Field(default="task-unknown", alias="taskId"); action: FulfillmentAction = FulfillmentAction.ORDER_AND_PAY; confirmation_id: str = Field(default="confirmation-unknown", alias="confirmationId"); confirmation_digest: str = Field(default="confirmation-unknown", alias="confirmationDigest"); amount: Decimal = Decimal("0"); currency: str = "CNY"; ordered_items_digest: str = Field(default="items-unknown", alias="orderedItemsDigest"); canonical_args: dict[str, Any] = Field(default_factory=dict, alias="canonicalArgs"); step: StepKind; status: StepStatus; preview_digest: str = Field(alias="previewDigest"); command_idempotency_key: str = Field(alias="commandIdempotencyKey"); backend_reference: str | None = Field(default=None, alias="backendReference"); error_code: ErrorCode | None = Field(default=None, alias="errorCode"); query_required: bool = Field(default=False, alias="queryRequired"); compensation_status: CompensationStatus = Field(default=CompensationStatus.NOT_REQUIRED, alias="compensationStatus")
    def __getattribute__(self, name: str):
        if name == "canonical_args":
            try: return _deep_plain_copy(object.__getattribute__(self, "__dict__").get("canonical_args", {}))
            except (AttributeError, TypeError): pass
        return super().__getattribute__(name)
    @model_validator(mode="before")
    @classmethod
    def require_issuer(cls, value: Any) -> Any:
        if not isinstance(value, Mapping) or value.get("_issuer") is not _SAGA_RECEIPT_CAP: raise ValueError("SagaReceipt必须由runner issuer签发")
        value = dict(value); value.pop("_issuer", None); return value
    @classmethod
    def model_construct(cls, *_: Any, **__: Any): raise TypeError("SagaReceipt禁止model_construct")
    model_copy = _issuer_copy; __reduce_ex__ = _issuer_pickle; __copy__ = _issuer_shallow_copy; __deepcopy__ = _issuer_deep_copy
    @model_validator(mode="after")
    def validate_receipt(self) -> SagaReceipt:
        object.__setattr__(self, "amount", _amount(self.amount)); object.__setattr__(self, "canonical_args", _canonical_args_value(self.canonical_args))
        expected = command_idempotency_key(owner_user_id=self.owner_user_id, task_id=self.task_id, execution_id=self.execution_id, action=self.action, confirmation_id=self.confirmation_id, confirmation_digest=self.confirmation_digest, step=self.step.value, preview_digest=self.preview_digest, amount=self.amount, currency=self.currency, ordered_items_digest=self.ordered_items_digest, canonical_args=self.canonical_args)
        if self.command_idempotency_key != expected: raise ValueError("receipt command idempotency key不匹配")
        _opaque(self.backend_reference)
        if self.status is StepStatus.SUCCEEDED and (not self.backend_reference or self.error_code or self.query_required): raise ValueError("SUCCEEDED receipt证据不一致")
        if self.status is StepStatus.FAILED and (not self.error_code or self.backend_reference or self.query_required): raise ValueError("FAILED receipt必须有errorCode且不得带success reference")
        if self.status is StepStatus.UNKNOWN and (not self.query_required or self.backend_reference): raise ValueError("UNKNOWN receipt必须queryRequired且不得带success reference")
        if self.query_required and self.status is not StepStatus.UNKNOWN: raise ValueError("queryRequired只能绑定UNKNOWN")
        if self.compensation_status is CompensationStatus.PROHIBITED and not (self.step is StepKind.PAYMENT and self.status is StepStatus.SUCCEEDED): raise ValueError("PROHIBITED只能绑定成功payment")
        if self.compensation_status is not CompensationStatus.NOT_REQUIRED and self.compensation_status is not CompensationStatus.PROHIBITED:
            if self.step is not StepKind.CANCEL_ORDER: raise ValueError("补偿状态只能绑定cancel_order")
            if self.compensation_status in {CompensationStatus.REQUESTED, CompensationStatus.IN_PROGRESS} and self.status not in {StepStatus.CREATING, StepStatus.UNKNOWN, StepStatus.COMPENSATING}: raise ValueError("未完成cancel补偿状态不匹配")
            if self.compensation_status is CompensationStatus.COMPLETED and self.status is not StepStatus.SUCCEEDED: raise ValueError("完成补偿必须绑定成功cancel")
            if self.compensation_status is CompensationStatus.FAILED and self.status is not StepStatus.FAILED: raise ValueError("失败补偿必须绑定失败cancel")
        if self.step is StepKind.CANCEL_ORDER and self.status is StepStatus.SUCCEEDED and self.compensation_status is not CompensationStatus.COMPLETED: raise ValueError("成功cancel必须COMPLETED")
        return self


class SagaReceiptRegistry:
    def __init__(self, receipt: SagaReceipt, _capability: object | None = None):
        if _capability is not _SAGA_RECEIPT_CAP: raise TypeError("SagaReceipt registry必须由server创建")
        self.identity = id(receipt); self.receipt = receipt; self.snapshot = receipt.model_dump(mode="json"); _SAGA_RECEIPT_REGISTRY_BY_ID[self.identity] = self
    def verify(self, receipt: SagaReceipt, execution: SagaExecution, step: SagaStep) -> None:
        if id(receipt) != self.identity or receipt is not self.receipt or receipt.model_dump(mode="json") != self.snapshot: raise PermissionError("SagaReceipt identity不合法")
        if receipt.execution_id != execution.execution_id or receipt.owner_user_id != execution.owner_user_id or receipt.step is not step.step or receipt.command_idempotency_key != step.command_idempotency_key: raise PermissionError("SagaReceipt cross-aggregate注入")


class SagaReceiptIssuer:
    @classmethod
    def issue(cls, execution: SagaExecution, step: SagaStep) -> SagaReceipt:
        _verify_execution(execution)
        owned = next((item for item in execution.steps if item.step is step.step and item.command_idempotency_key == step.command_idempotency_key), None)
        if owned is None: raise PermissionError("step不属于execution")
        step = owned
        receipt = SagaReceipt.model_validate({"_issuer": _SAGA_RECEIPT_CAP, "executionId": execution.execution_id, "userId": execution.owner_user_id, "taskId": execution.task_id, "action": execution.action, "confirmationId": execution.confirmation_id, "confirmationDigest": execution.confirmation_digest, "amount": execution.amount, "currency": execution.currency, "orderedItemsDigest": execution.ordered_items_digest, "canonicalArgs": step.canonical_args, "step": step.step, "status": step.status, "previewDigest": execution.preview_digest, "commandIdempotencyKey": step.command_idempotency_key, "backendReference": step.backend_reference, "errorCode": step.error_code, "queryRequired": step.query_required, "compensationStatus": execution.compensation_status}); SagaReceiptRegistry(receipt, _SAGA_RECEIPT_CAP); return receipt


_ALLOWED_TRANSITIONS = {
    FulfillmentStatus.PREVIEWED: frozenset({FulfillmentStatus.AWAITING_CONFIRMATION}), FulfillmentStatus.AWAITING_CONFIRMATION: frozenset({FulfillmentStatus.ORDER_CREATING}), FulfillmentStatus.ORDER_CREATING: frozenset({FulfillmentStatus.ORDER_CREATED, FulfillmentStatus.UNKNOWN, FulfillmentStatus.FAILED}), FulfillmentStatus.ORDER_CREATED: frozenset({FulfillmentStatus.PAYMENT_CREATING, FulfillmentStatus.FULFILLING, FulfillmentStatus.COMPENSATING}), FulfillmentStatus.PAYMENT_CREATING: frozenset({FulfillmentStatus.PAYMENT_PENDING, FulfillmentStatus.PAID, FulfillmentStatus.UNKNOWN, FulfillmentStatus.FAILED}), FulfillmentStatus.PAYMENT_PENDING: frozenset({FulfillmentStatus.PAID, FulfillmentStatus.UNKNOWN, FulfillmentStatus.FAILED}), FulfillmentStatus.PAID: frozenset({FulfillmentStatus.FULFILLING, FulfillmentStatus.MANUAL_REVIEW}), FulfillmentStatus.FULFILLING: frozenset({FulfillmentStatus.COMPLETED, FulfillmentStatus.UNKNOWN, FulfillmentStatus.FAILED}), FulfillmentStatus.UNKNOWN: frozenset({FulfillmentStatus.MANUAL_REVIEW, FulfillmentStatus.FAILED, FulfillmentStatus.ORDER_CREATING, FulfillmentStatus.ORDER_CREATED, FulfillmentStatus.PAYMENT_CREATING, FulfillmentStatus.PAID, FulfillmentStatus.FULFILLING, FulfillmentStatus.COMPENSATING, FulfillmentStatus.COMPLETED}), FulfillmentStatus.COMPENSATING: frozenset({FulfillmentStatus.COMPENSATED, FulfillmentStatus.MANUAL_REVIEW, FulfillmentStatus.FAILED, FulfillmentStatus.UNKNOWN}), FulfillmentStatus.COMPENSATED: frozenset(), FulfillmentStatus.MANUAL_REVIEW: frozenset(), FulfillmentStatus.COMPLETED: frozenset(), FulfillmentStatus.FAILED: frozenset(),
}


def _register_execution(execution: Any) -> Any:
    _EXECUTION_REGISTRY_BY_ID[id(execution)] = (execution, execution.model_dump(mode="json"))
    return execution


def _verify_execution(execution: Any) -> None:
    record = _EXECUTION_REGISTRY_BY_ID.get(id(execution))
    if record is None or record[0] is not execution or execution.model_dump(mode="json") != record[1]:
        raise PermissionError("SagaExecution identity/snapshot不合法")


class SagaExecution(_ContractModel):
    execution_id: str = Field(alias="executionId"); owner_user_id: str = Field(alias="userId"); task_id: str = Field(default="task-unknown", alias="taskId"); action: FulfillmentAction = FulfillmentAction.ORDER_AND_PAY; amount: Decimal = Decimal("0"); currency: str = "CNY"; ordered_items_digest: str = Field(default="items-unknown", alias="orderedItemsDigest"); preview_digest: str = Field(alias="previewDigest"); preview_expires_at: datetime | None = Field(default=None, alias="expiresAt"); confirmation_id: str = Field(default="confirmation-unknown", alias="confirmationId"); confirmation_digest: str = Field(default="confirmation-unknown", alias="confirmationDigest"); confirmed: bool = False; status: FulfillmentStatus = FulfillmentStatus.PREVIEWED; steps: tuple[SagaStep, ...] = (); compensation_status: CompensationStatus = Field(default=CompensationStatus.NOT_REQUIRED, alias="compensationStatus"); error_code: ErrorCode | None = Field(default=None, alias="errorCode"); compensation_reason: str | None = Field(default=None, alias="compensationReason"); created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), alias="createdAt"); updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), alias="updatedAt")
    __reduce_ex__ = _issuer_pickle; __copy__ = _issuer_shallow_copy; __deepcopy__ = _issuer_deep_copy

    def __getattribute__(self, name: str):
        # Detach nested steps before exposing them; object.__setattr__ on a
        # returned step must not mutate this execution's internal snapshot.
        if name == "steps":
            try:
                raw = object.__getattribute__(self, "__dict__").get("steps")
                if isinstance(raw, tuple):
                    return tuple(item.clone() for item in raw)
            except (AttributeError, TypeError):
                pass
        return super().__getattribute__(name)
    @model_validator(mode="before")
    @classmethod
    def strict_action_input(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            internal = value.get("_executionIssuer") is _EXECUTION_CAP
            if value.get("confirmed") is True and not internal: raise ValueError("confirmed只能由已消费server confirmation产生")
            steps = value.get("steps", ())
            if not internal and any((getattr(item, "status", None) in {StepStatus.CREATING, StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.UNKNOWN, StepStatus.COMPENSATING, StepStatus.COMPENSATED, StepStatus.MANUAL_REVIEW}) or (isinstance(item, Mapping) and item.get("status") not in {None, StepStatus.PENDING}) for item in (steps or ())): raise ValueError("非preview execution不得直接构造已执行step")
            if internal:
                value = dict(value); value.pop("_executionIssuer", None)
        return value
    @model_validator(mode="after")
    def validate_identity(self) -> SagaExecution:
        _owner(self.owner_user_id); _text(self.execution_id, "executionId"); _text(self.task_id, "taskId"); object.__setattr__(self, "amount", _amount(self.amount)); _text(self.currency, "currency"); _digest(self.ordered_items_digest); _digest(self.preview_digest)
        if self.preview_expires_at is not None and _aware_utc(self.preview_expires_at, "expiresAt") <= _authority_now(): raise ValueError("preview已过期")
        _aware_utc(self.created_at); _aware_utc(self.updated_at)
        if self.confirmed and (self.confirmation_id == "confirmation-unknown" or self.confirmation_digest == "confirmation-unknown"): raise ValueError("confirmed必须绑定confirmation")
        for step in self.steps:
            if (step.owner_user_id, step.task_id, step.execution_id, step.action, step.confirmation_id, step.confirmation_digest, step.amount, step.currency.upper(), step.ordered_items_digest, step.preview_digest) != (self.owner_user_id, self.task_id, self.execution_id, self.action, self.confirmation_id, self.confirmation_digest, self.amount, self.currency.upper(), self.ordered_items_digest, self.preview_digest): raise ValueError("SagaStep identity必须继承execution合同")
        if len({step.step for step in self.steps}) != len(self.steps): raise ValueError("SagaStep不得重复")
        by = {step.step: step for step in self.steps}; order = by.get(StepKind.ORDER); payment = by.get(StepKind.PAYMENT); fulfill = by.get(StepKind.FULFILLMENT); cancel = by.get(StepKind.CANCEL_ORDER)
        if self.action is not FulfillmentAction.ORDER_AND_PAY and (payment is not None or self.status in {FulfillmentStatus.PAYMENT_CREATING, FulfillmentStatus.PAYMENT_PENDING, FulfillmentStatus.PAID}): raise ValueError("do_not_pay不得进入payment/paid状态")
        if self.status in {FulfillmentStatus.ORDER_CREATED, FulfillmentStatus.PAYMENT_CREATING, FulfillmentStatus.PAYMENT_PENDING, FulfillmentStatus.PAID, FulfillmentStatus.FULFILLING, FulfillmentStatus.COMPLETED, FulfillmentStatus.COMPENSATING, FulfillmentStatus.COMPENSATED} and (not order or order.status is not StepStatus.SUCCEEDED or not order.backend_reference): raise ValueError("订单后续状态必须有order成功证据")
        if self.action is FulfillmentAction.ORDER_AND_PAY and self.status in {FulfillmentStatus.PAID, FulfillmentStatus.FULFILLING} and (not payment or payment.status is not StepStatus.SUCCEEDED or not payment.backend_reference): raise ValueError("支付后续状态必须有payment成功证据")
        if self.status is FulfillmentStatus.COMPLETED:
            if not fulfill or fulfill.status is not StepStatus.SUCCEEDED or not fulfill.backend_reference: raise ValueError("COMPLETED必须有fulfillment成功证据")
            if self.action is FulfillmentAction.ORDER_AND_PAY and (not payment or payment.status is not StepStatus.SUCCEEDED): raise ValueError("支付完成必须有payment成功证据")
            if cancel or self.error_code or self.compensation_status is not CompensationStatus.NOT_REQUIRED: raise ValueError("COMPLETED终态互斥")
        if self.status is FulfillmentStatus.COMPENSATING and (not self.compensation_reason or self.compensation_status not in {CompensationStatus.REQUESTED, CompensationStatus.IN_PROGRESS}): raise ValueError("COMPENSATING必须有取消原因和进行中证据")
        if self.status is FulfillmentStatus.COMPENSATED:
            payment_failed = payment is not None and payment.status is StepStatus.FAILED and payment.error_code is not None
            if (not order or order.status is not StepStatus.SUCCEEDED or not cancel or cancel.status is not StepStatus.SUCCEEDED or not cancel.backend_reference or self.compensation_status is not CompensationStatus.COMPLETED or not self.compensation_reason or (payment is not None and not payment_failed) or (fulfill is not None and fulfill.status is StepStatus.SUCCEEDED) or (self.compensation_reason == "PAYMENT_FAILED" and not payment_failed)): raise ValueError("COMPENSATED必须有order、适用payment failure和cancel成功证据且不得已履约")
        if self.status is FulfillmentStatus.FAILED and (not self.error_code or not any(step.status is StepStatus.FAILED and step.error_code is not None for step in self.steps)): raise ValueError("FAILED必须绑定FAILED step和ErrorCode")
        if self.status is FulfillmentStatus.UNKNOWN and not any(step.status is StepStatus.UNKNOWN and step.query_required for step in self.steps): raise ValueError("UNKNOWN必须绑定query-required step")
        if self.status in {FulfillmentStatus.COMPLETED, FulfillmentStatus.COMPENSATED, FulfillmentStatus.FAILED, FulfillmentStatus.MANUAL_REVIEW} and not self.steps: raise ValueError("终态禁止0-step")
        if self.status is FulfillmentStatus.PAID and (cancel or self.compensation_status is not CompensationStatus.NOT_REQUIRED): raise ValueError("PAID不得带cancel/compensation")
        return self
    @classmethod
    def from_intent(cls, intent: FulfillmentIntent, *, owner: OwnerPrincipal) -> SagaExecution:
        _principal(owner, intent.owner_user_id); return _register_execution(cls(userId=owner.user_id, executionId=intent.execution_id, taskId=intent.task_id, action=intent.action, amount=intent.amount, currency=intent.currency, orderedItemsDigest=intent.ordered_items_digest, previewDigest=intent.preview_digest, expiresAt=intent.preview_expires_at))
    def _clone(self, *, _capability: object | None = None, **update: Any):
        if _capability is not _EXECUTION_TRANSITION_CAP:
            raise PermissionError("SagaExecution transition必须由runner authority产生")
        _verify_execution(self)
        immutable = (
            "owner_user_id", "execution_id", "task_id", "action", "amount",
            "currency", "ordered_items_digest", "preview_digest",
            "preview_expires_at", "created_at",
        )
        for field in immutable:
            if field in update:
                raise PermissionError("SagaExecution identity字段不可变更")
        if self.confirmed:
            for field in ("confirmation_id", "confirmation_digest", "confirmed"):
                if field in update:
                    raise PermissionError("SagaExecution confirmation binding不可变更")
        values = self.model_dump()
        values.update(update)
        if values.get("confirmed") is True or values.get("steps"):
            values["_executionIssuer"] = _EXECUTION_CAP
        return _register_execution(type(self).model_validate(values))
    def clone(self, **update: Any):
        raise TypeError("SagaExecution不可由public clone生成；只能由runner authority transition生成")
    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False):
        raise TypeError("SagaExecution不可由public model_copy生成")
    @property
    def user_id(self) -> str: return self.owner_user_id
    def _check(self, owner: OwnerPrincipal) -> None: _verify_execution(self); _principal(owner, self.owner_user_id)
    def assert_owner(self, server_owner_user_id: str) -> None:
        if self.owner_user_id != _owner(server_owner_user_id): raise PermissionError("履约execution owner不匹配")
    def _assert_preview_current(self) -> None:
        if self.preview_expires_at is not None and _authority_now() >= _aware_utc(self.preview_expires_at, "expiresAt"): raise ValueError("preview已过期")
    def assert_preview(self, preview_digest: str) -> None:
        self._assert_preview_current()
        if self.preview_digest != _digest(preview_digest): raise ValueError("preview digest发生漂移")
    def confirm(self, evidence: ConfirmationEvidence, *, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current(); registry = _CONFIRMATION_REGISTRY_BY_ID.get(id(evidence))
        if registry is None: raise PermissionError("confirmation不是server-issued evidence")
        registry.consume(evidence, self, owner); return self._clone(_capability=_EXECUTION_TRANSITION_CAP, status=FulfillmentStatus.AWAITING_CONFIRMATION, confirmed=True, confirmation_id=evidence.confirmation_id, confirmation_digest=evidence.confirmation_digest, updated_at=_authority_now())
    def transition(self, target: FulfillmentStatus | str, *, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current()
        if not isinstance(target, FulfillmentStatus): raise ValueError("状态必须是FulfillmentStatus枚举")
        if target is self.status: return self
        if target not in _ALLOWED_TRANSITIONS[self.status]: raise InvalidFulfillmentTransition(f"不允许Fulfillment从 {self.status} 转换到 {target}")
        if target is FulfillmentStatus.ORDER_CREATING and not self.confirmed: raise InvalidFulfillmentTransition("ORDER_CREATING必须先消费confirmation")
        if self.action is FulfillmentAction.DO_NOT_PAY and target in {FulfillmentStatus.PAYMENT_CREATING, FulfillmentStatus.PAYMENT_PENDING, FulfillmentStatus.PAID}: raise InvalidFulfillmentTransition("do_not_pay不得进入payment/paid状态")
        compensation = CompensationStatus.REQUESTED if target is FulfillmentStatus.COMPENSATING else (CompensationStatus.COMPLETED if target is FulfillmentStatus.COMPENSATED else self.compensation_status)
        return self._clone(_capability=_EXECUTION_TRANSITION_CAP, status=target, compensation_status=compensation, updated_at=_authority_now())
    def _replace_step(self, step: SagaStep) -> SagaExecution:
        return self._clone(_capability=_EXECUTION_TRANSITION_CAP, steps=tuple(item for item in self.steps if item.step is not step.step) + (step,), updated_at=_authority_now())
    def step(self, step_name: StepKind) -> SagaStep | None:
        kind = _step_kind(step_name); found = next((item for item in self.steps if item.step is kind), None); return found.clone() if found is not None else None
    def start_step(self, step_name: StepKind, *, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current()
        if not self.confirmed: raise InvalidFulfillmentTransition("step必须先消费confirmation")
        name = _step_kind(step_name)
        if self.step(name) is not None: raise InvalidFulfillmentTransition("step不得重复执行")
        if name is StepKind.ORDER: target = FulfillmentStatus.ORDER_CREATING
        elif name is StepKind.PAYMENT:
            if self.action is not FulfillmentAction.ORDER_AND_PAY or self.status is not FulfillmentStatus.ORDER_CREATED: raise InvalidFulfillmentTransition("当前状态/action禁止payment")
            target = FulfillmentStatus.PAYMENT_CREATING
        elif name is StepKind.FULFILLMENT:
            if self.status not in {FulfillmentStatus.ORDER_CREATED, FulfillmentStatus.PAID, FulfillmentStatus.FULFILLING}: raise InvalidFulfillmentTransition("当前状态禁止fulfillment")
            if self.action is FulfillmentAction.ORDER_AND_PAY and self.status is not FulfillmentStatus.PAID: raise InvalidFulfillmentTransition("fulfillment必须在payment成功后执行")
            target = FulfillmentStatus.FULFILLING
        elif name is StepKind.CANCEL_ORDER:
            if self.status is not FulfillmentStatus.COMPENSATING: raise InvalidFulfillmentTransition("cancel必须在COMPENSATING")
            target = FulfillmentStatus.COMPENSATING
        else: raise InvalidFulfillmentTransition("未知履约step")
        updated = self.transition(target, owner=owner); return updated._replace_step(SagaStep(step=name, status=StepStatus.CREATING, userId=self.owner_user_id, taskId=self.task_id, executionId=self.execution_id, action=self.action, confirmationId=self.confirmation_id, confirmationDigest=self.confirmation_digest, amount=self.amount, currency=self.currency, orderedItemsDigest=self.ordered_items_digest, previewDigest=self.preview_digest))
    def record_step_success(self, step_name: StepKind, *, backend_reference: str, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current(); kind = _step_kind(step_name); current = self.step(kind)
        if current is None or current.status is not StepStatus.CREATING: raise InvalidFulfillmentTransition("step未处于可成功确认状态")
        succeeded = current.clone(status=StepStatus.SUCCEEDED, query_required=False, backend_reference=_opaque(backend_reference), error_code=None); updated = self._replace_step(succeeded)
        if kind is StepKind.ORDER: return updated.transition(FulfillmentStatus.ORDER_CREATED, owner=owner)
        if kind is StepKind.PAYMENT: return updated.transition(FulfillmentStatus.PAID, owner=owner)
        if kind is StepKind.FULFILLMENT: return updated.transition(FulfillmentStatus.COMPLETED, owner=owner)
        return updated.transition(FulfillmentStatus.COMPENSATED, owner=owner)
    def record_step_failure(self, step_name: StepKind, *, error_code: ErrorCode, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current(); kind = _step_kind(step_name); current = self.step(kind)
        if type(error_code) is not ErrorCode: raise ValueError("error_code必须是ErrorCode枚举")
        if current is None or current.status is not StepStatus.CREATING: raise InvalidFulfillmentTransition("step未处于可失败确认状态")
        updated = self._replace_step(current.clone(status=StepStatus.FAILED, error_code=error_code, query_required=False, backend_reference=None))
        if kind is StepKind.PAYMENT and self.status in {FulfillmentStatus.PAYMENT_CREATING, FulfillmentStatus.PAYMENT_PENDING}: return updated
        return updated._clone(_capability=_EXECUTION_TRANSITION_CAP, status=FulfillmentStatus.FAILED, error_code=error_code, compensation_status=CompensationStatus.FAILED if kind is StepKind.CANCEL_ORDER else self.compensation_status, updated_at=_authority_now())
    def mark_unknown(self, step_name: StepKind, *, reason: ErrorCode, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current(); kind = _step_kind(step_name); current = self.step(kind)
        if type(reason) is not ErrorCode: raise ValueError("reason必须是ErrorCode枚举")
        if current is None or current.status not in {StepStatus.CREATING, StepStatus.UNKNOWN}: raise InvalidFulfillmentTransition("只有执行中的step可以进入UNKNOWN")
        return self._replace_step(current.clone(status=StepStatus.UNKNOWN, error_code=reason, query_required=True, backend_reference=None))._clone(_capability=_EXECUTION_TRANSITION_CAP, status=FulfillmentStatus.UNKNOWN, updated_at=_authority_now())
    def resolve_unknown(self, step_name: StepKind, receipt: QueryReceipt, *, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current(); kind = _step_kind(step_name); current = self.step(kind)
        if current is None or current.status is not StepStatus.UNKNOWN or not current.query_required: raise InvalidFulfillmentTransition("step不在UNKNOWN状态")
        registry = _QUERY_RECEIPT_REGISTRY_BY_ID.get(id(receipt))
        if registry is None: raise PermissionError("必须消费server-issued QueryReceipt")
        result = registry.consume(receipt, execution_id=self.execution_id, step=kind, command_key=current.command_idempotency_key, sequence=current.query_sequence + 1); current = current.clone(query_sequence=receipt.query_sequence)
        if result is BackendQueryResult.COMMITTED:
            succeeded = current.clone(status=StepStatus.SUCCEEDED, query_required=False, error_code=None, backend_reference=receipt.backend_reference)
            target = {StepKind.ORDER: FulfillmentStatus.ORDER_CREATED, StepKind.PAYMENT: FulfillmentStatus.PAID, StepKind.FULFILLMENT: FulfillmentStatus.COMPLETED, StepKind.CANCEL_ORDER: FulfillmentStatus.COMPENSATED}[kind]
            return self._clone(_capability=_EXECUTION_TRANSITION_CAP, steps=tuple(succeeded if item.step is kind else item for item in self.steps), status=target, compensation_status=CompensationStatus.COMPLETED if kind is StepKind.CANCEL_ORDER else self.compensation_status, updated_at=_authority_now())
        if result is BackendQueryResult.FAILED:
            failed_step = current.clone(status=StepStatus.FAILED, error_code=ErrorCode.BACKEND_FAILED_AFTER_QUERY, query_required=False)
            return self._clone(_capability=_EXECUTION_TRANSITION_CAP, steps=tuple(failed_step if item.step is kind else item for item in self.steps), status=FulfillmentStatus.PAYMENT_CREATING if kind is StepKind.PAYMENT else FulfillmentStatus.FAILED, error_code=ErrorCode.BACKEND_FAILED_AFTER_QUERY, compensation_status=CompensationStatus.FAILED if kind is StepKind.CANCEL_ORDER else self.compensation_status, updated_at=_authority_now())
        if result in {BackendQueryResult.UNKNOWN, BackendQueryResult.FOUND_NOT_COMMITTED}:
            held = current.clone(status=StepStatus.UNKNOWN, query_required=True, error_code=ErrorCode.UNKNOWN if result is BackendQueryResult.UNKNOWN else ErrorCode.FOUND_NOT_COMMITTED)
            return self._clone(_capability=_EXECUTION_TRANSITION_CAP, steps=tuple(held if item.step is kind else item for item in self.steps), status=FulfillmentStatus.UNKNOWN, updated_at=_authority_now())
        retry = current.clone(status=StepStatus.CREATING, query_required=False, error_code=None)
        target = {StepKind.ORDER: FulfillmentStatus.ORDER_CREATING, StepKind.PAYMENT: FulfillmentStatus.PAYMENT_CREATING, StepKind.FULFILLMENT: FulfillmentStatus.FULFILLING, StepKind.CANCEL_ORDER: FulfillmentStatus.COMPENSATING}[kind]
        return self._clone(_capability=_EXECUTION_TRANSITION_CAP, steps=tuple(retry if item.step is kind else item for item in self.steps), status=target, updated_at=_authority_now())
    def request_cancel_after_payment_failure(self, *, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current(); payment = self.step(StepKind.PAYMENT)
        if payment is None or payment.status is not StepStatus.FAILED: raise InvalidFulfillmentTransition("只有支付明确失败后才能请求取消订单")
        if self.status not in {FulfillmentStatus.PAYMENT_CREATING, FulfillmentStatus.PAYMENT_PENDING}: raise InvalidFulfillmentTransition("当前状态不允许支付失败补偿")
        return self._clone(_capability=_EXECUTION_TRANSITION_CAP, status=FulfillmentStatus.COMPENSATING, compensation_status=CompensationStatus.REQUESTED, compensation_reason="PAYMENT_FAILED", updated_at=_authority_now())
    def request_cancel(self, *, reason: str, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current()
        if reason not in _CANCEL_REASONS: raise ValueError("compensationReason不在允许枚举")
        order = self.step(StepKind.ORDER)
        if not order or order.status is not StepStatus.SUCCEEDED or not order.backend_reference: raise InvalidFulfillmentTransition("取消必须绑定已提交订单")
        if self.status not in {FulfillmentStatus.ORDER_CREATED, FulfillmentStatus.PAYMENT_CREATING, FulfillmentStatus.PAYMENT_PENDING}: raise InvalidFulfillmentTransition("当前状态不允许请求取消")
        return self._clone(_capability=_EXECUTION_TRANSITION_CAP, status=FulfillmentStatus.COMPENSATING, compensation_status=CompensationStatus.REQUESTED, compensation_reason=reason, updated_at=_authority_now())
    def request_auto_refund(self, *, owner: OwnerPrincipal) -> SagaExecution:
        self._check(owner); self._assert_preview_current(); payment = self.step(StepKind.PAYMENT)
        if self.status is not FulfillmentStatus.PAID or not payment or payment.status is not StepStatus.SUCCEEDED: raise PaymentCompensationPolicyError("当前没有已成功支付可供退款")
        return self._clone(_capability=_EXECUTION_TRANSITION_CAP, status=FulfillmentStatus.MANUAL_REVIEW, compensation_status=CompensationStatus.PROHIBITED, updated_at=_authority_now())
    def receipt(self, step_name: StepKind, *, owner: OwnerPrincipal) -> SagaReceipt:
        self._check(owner); self._assert_preview_current(); step = self.step(step_name)
        if step is None: raise KeyError("SagaStep不存在")
        return SagaReceiptIssuer.issue(self, step)
