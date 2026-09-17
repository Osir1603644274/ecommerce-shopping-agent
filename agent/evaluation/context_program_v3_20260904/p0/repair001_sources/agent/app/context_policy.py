"""Versioned, fail-closed context policy registration.

Policies describe which *validated* domain-context fields may be considered by
each lifecycle phase.  They are deliberately smaller than a prompt or a raw
TaskState: the public contract exposes only the policy identity, never this
registry's complete declaration.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

CONTEXT_PHASES = (
    "planner",
    "executor",
    "validator",
    "replanner",
    "final_answer",
)

# These are the only domain-context fields a registered skill may publish.
# The aliases intentionally describe the source contract, while ContextPack's
# legacy ``shoppingGuideState`` attribute remains an internal compatibility
# name for existing callers.
CONTEXT_FIELDS = frozenset({
    "shoppingGuide",
    "candidateScope",
    "scopeRerankRequest",
    "shoppingGuideSources",
})

GENERIC_CONTEXT_POLICY_ID = "generic-context-policy"
LOCAL_LIFE_CONTEXT_POLICY_ID = "local-life-context-policy"
ECOMMERCE_CONTEXT_POLICY_ID = "ecommerce-context-policy"
CONTEXT_CONTRACT_VERSION = "1.0"


class ContextPolicyError(ValueError):
    """Raised when a policy declaration or query is not fail-closed valid."""


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Immutable policy declaration keyed by ``policy_id`` and ``version``."""

    policy_id: str
    version: str
    phase_fields: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ContextPolicyError("policy_id must be a non-empty string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ContextPolicyError("policy version must be a non-empty string")

        try:
            pairs = (
                tuple(self.phase_fields.items())
                if isinstance(self.phase_fields, Mapping)
                else tuple(self.phase_fields)
            )
        except TypeError as exc:
            raise ContextPolicyError("phase_fields must be a phase mapping") from exc
        phase_names = [item[0] for item in pairs if isinstance(item, tuple) and len(item) == 2]
        if len(phase_names) != len(pairs) or len(set(phase_names)) != len(phase_names):
            raise ContextPolicyError("phase_fields must contain unique phase entries")
        if set(phase_names) != set(CONTEXT_PHASES):
            missing = sorted(set(CONTEXT_PHASES) - set(phase_names))
            unknown = sorted(set(phase_names) - set(CONTEXT_PHASES))
            raise ContextPolicyError(
                f"policy phases must cover {CONTEXT_PHASES}; missing={missing}, unknown={unknown}"
            )

        normalized: list[tuple[str, tuple[str, ...]]] = []
        for phase in CONTEXT_PHASES:
            fields = dict(pairs)[phase]
            if not isinstance(fields, (tuple, list, set, frozenset)):
                raise ContextPolicyError(f"fields for phase {phase!r} must be a sequence")
            field_list = list(fields)
            if any(not isinstance(field, str) for field in field_list):
                raise ContextPolicyError(f"fields for phase {phase!r} must be strings")
            if len(set(field_list)) != len(field_list):
                raise ContextPolicyError(f"duplicate context field in phase {phase!r}")
            unknown = sorted(set(field_list) - CONTEXT_FIELDS)
            if unknown:
                raise ContextPolicyError(
                    f"unknown context field(s) in phase {phase!r}: {unknown}"
                )
            normalized.append((phase, tuple(sorted(field_list))))
        object.__setattr__(self, "phase_fields", tuple(normalized))

    @property
    def key(self) -> tuple[str, str]:
        return self.policy_id, self.version

    def fields_for(self, phase: str) -> frozenset[str]:
        if phase not in CONTEXT_PHASES:
            raise ContextPolicyError(f"unknown context phase: {phase!r}")
        fields = dict(self.phase_fields)[phase]
        return frozenset(fields)

    def allows(self, phase: str, field: str) -> bool:
        if field not in CONTEXT_FIELDS:
            raise ContextPolicyError(f"unknown context field: {field!r}")
        return field in self.fields_for(phase)

    @classmethod
    def from_declaration(cls, declaration: Mapping[str, Any]) -> "ContextPolicy":
        if not isinstance(declaration, Mapping):
            raise ContextPolicyError("policy declaration must be a mapping")
        allowed_keys = {"policyId", "version", "phases"}
        unknown = sorted(set(declaration) - allowed_keys)
        if unknown:
            raise ContextPolicyError(f"unknown policy declaration field(s): {unknown}")
        if set(declaration) != allowed_keys:
            raise ContextPolicyError("policy declaration requires policyId, version, and phases")
        phases = declaration["phases"]
        if not isinstance(phases, Mapping):
            raise ContextPolicyError("policy phases must be a mapping")
        entries: list[tuple[str, tuple[str, ...]]] = []
        for phase, phase_declaration in phases.items():
            if not isinstance(phase, str):
                raise ContextPolicyError("policy phase names must be strings")
            if not isinstance(phase_declaration, Mapping):
                raise ContextPolicyError(f"phase {phase!r} must be a mapping")
            phase_unknown = sorted(set(phase_declaration) - {"fields"})
            if phase_unknown:
                raise ContextPolicyError(
                    f"unknown policy field(s) in phase {phase!r}: {phase_unknown}"
                )
            if set(phase_declaration) != {"fields"}:
                raise ContextPolicyError(f"phase {phase!r} requires fields")
            fields = phase_declaration["fields"]
            if not isinstance(fields, (list, tuple)):
                raise ContextPolicyError(f"fields for phase {phase!r} must be a list")
            entries.append((phase, tuple(fields)))
        return cls(
            policy_id=declaration["policyId"],
            version=declaration["version"],
            phase_fields=tuple(entries),
        )


class ContextPolicyRegistry:
    """Copy-on-write registry; registered policy objects remain immutable."""

    def __init__(self) -> None:
        self._policies: Mapping[tuple[str, str], ContextPolicy] = MappingProxyType({})

    def register(
        self,
        policy: ContextPolicy | Mapping[str, Any],
    ) -> ContextPolicy:
        item = (
            policy
            if isinstance(policy, ContextPolicy)
            else ContextPolicy.from_declaration(policy)
        )
        if item.key in self._policies:
            raise ContextPolicyError(f"duplicate context policy: {item.key}")
        updated = dict(self._policies)
        updated[item.key] = item
        self._policies = MappingProxyType(updated)
        return item

    def get(self, policy_id: str, version: str) -> ContextPolicy:
        key = (policy_id, version)
        try:
            return self._policies[key]
        except KeyError as exc:
            raise ContextPolicyError(f"unknown context policy: {key}") from exc

    def registered(self) -> tuple[ContextPolicy, ...]:
        return tuple(self._policies[key] for key in sorted(self._policies))


_POLICIES = ContextPolicyRegistry()


def register_context_policy(
    policy: ContextPolicy | Mapping[str, Any],
) -> ContextPolicy:
    return _POLICIES.register(policy)


def get_context_policy(policy_id: str, version: str) -> ContextPolicy:
    return _POLICIES.get(policy_id, version)


def registered_context_policies() -> tuple[ContextPolicy, ...]:
    return _POLICIES.registered()


def _register_builtin_policies() -> None:
    empty = {phase: {"fields": []} for phase in CONTEXT_PHASES}
    register_context_policy({
        "policyId": GENERIC_CONTEXT_POLICY_ID,
        "version": CONTEXT_CONTRACT_VERSION,
        "phases": empty,
    })
    register_context_policy({
        "policyId": LOCAL_LIFE_CONTEXT_POLICY_ID,
        "version": CONTEXT_CONTRACT_VERSION,
        "phases": empty,
    })
    ecommerce = {
        "planner": {"fields": sorted(CONTEXT_FIELDS)},
        "executor": {"fields": sorted(CONTEXT_FIELDS)},
        "validator": {"fields": []},
        "replanner": {"fields": sorted(CONTEXT_FIELDS)},
        "final_answer": {"fields": ["shoppingGuide"]},
    }
    register_context_policy({
        "policyId": ECOMMERCE_CONTEXT_POLICY_ID,
        "version": CONTEXT_CONTRACT_VERSION,
        "phases": ecommerce,
    })


_register_builtin_policies()


__all__ = [
    "CONTEXT_CONTRACT_VERSION",
    "CONTEXT_FIELDS",
    "CONTEXT_PHASES",
    "ECOMMERCE_CONTEXT_POLICY_ID",
    "GENERIC_CONTEXT_POLICY_ID",
    "LOCAL_LIFE_CONTEXT_POLICY_ID",
    "ContextPolicy",
    "ContextPolicyError",
    "ContextPolicyRegistry",
    "get_context_policy",
    "register_context_policy",
    "registered_context_policies",
]
