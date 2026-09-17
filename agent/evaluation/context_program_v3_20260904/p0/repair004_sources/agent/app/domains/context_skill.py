"""Explicit domain context skills backed by versioned Context Policies."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..context_policy import (
    CONTEXT_CONTRACT_VERSION,
    CONTEXT_FIELDS,
    ECOMMERCE_CONTEXT_POLICY_ID,
    GENERIC_CONTEXT_POLICY_ID,
    LOCAL_LIFE_CONTEXT_POLICY_ID,
    ContextPolicyError,
    get_context_policy,
)

GENERIC_CONTEXT_SKILL_ID = "generic-context"
LOCAL_LIFE_CONTEXT_SKILL_ID = "local-life-context"
ECOMMERCE_CONTEXT_SKILL_ID = "ecommerce-context"


class ContextSkillError(ValueError):
    """Raised when a context skill or its published context is invalid."""


def _empty_context(_domain_state: Mapping[str, Any] | None) -> dict[str, Any]:
    return {}


def _publish_ecommerce_context(
    domain_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(domain_state, Mapping):
        raise ContextSkillError("ecommerce domainState must be a mapping")

    # Imports stay lazy so the skill registry does not create an ecommerce
    # package cycle during DomainSpec registration.
    from .ecommerce.models import (
        CandidateScope,
        ScopeRerankRequest,
        ShoppingGuideState,
    )

    result: dict[str, Any] = {}
    model_fields = (
        ("shoppingGuide", ShoppingGuideState),
        ("candidateScope", CandidateScope),
        ("scopeRerankRequest", ScopeRerankRequest),
    )
    for field_name, model_type in model_fields:
        if field_name not in domain_state or domain_state[field_name] is None:
            continue
        raw_value = domain_state[field_name]
        try:
            result[field_name] = model_type.model_validate(raw_value).model_dump(
                by_alias=True,
                mode="json",
            )
        except (TypeError, ValueError) as exc:
            raise ContextSkillError(
                f"invalid ecommerce context field: {field_name}"
            ) from exc

    if "candidateScope" in result and "scopeRerankRequest" in result:
        scope = result["candidateScope"]
        request = result["scopeRerankRequest"]
        if request["scopeId"] != scope["scopeId"]:
            # Keep the validated source objects available to the pack, but do
            # not manufacture a rerank source for a mismatched server request.
            result.pop("scopeRerankRequest", None)

    if "shoppingGuide" in result:
        from ..context_pack import shopping_guide_argument_sources

        sources = shopping_guide_argument_sources(
            result["shoppingGuide"],
            scope=result.get("candidateScope"),
            pending_rerank=result.get("scopeRerankRequest"),
        )
        if sources is not None:
            result["shoppingGuideSources"] = sources
    return result


@dataclass(frozen=True, slots=True)
class ContextSkill:
    """Immutable domain skill with a versioned phase policy binding."""

    skill_id: str
    version: str
    domain_id: str
    policy_id: str
    policy_version: str
    publisher: Callable[[Mapping[str, Any] | None], dict[str, Any]]

    def __post_init__(self) -> None:
        if not isinstance(self.skill_id, str) or not self.skill_id.strip():
            raise ContextSkillError("skill_id must be a non-empty string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ContextSkillError("skill version must be a non-empty string")
        if not isinstance(self.domain_id, str) or not self.domain_id.strip():
            raise ContextSkillError("skill domain_id must be a non-empty string")
        try:
            policy = get_context_policy(self.policy_id, self.policy_version)
        except ContextPolicyError as exc:
            raise ContextSkillError(
                f"skill references unknown policy: {self.policy_id}@{self.policy_version}"
            ) from exc
        for phase in ("planner", "executor", "validator", "replanner", "final_answer"):
            unknown = policy.fields_for(phase) - CONTEXT_FIELDS
            if unknown:
                raise ContextSkillError(
                    f"policy publishes unknown field(s) for {phase}: {sorted(unknown)}"
                )

    @property
    def key(self) -> tuple[str, str]:
        return self.skill_id, self.version

    def publish_context(
        self,
        domain_state: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            raw = self.publisher(domain_state)
        except ContextSkillError:
            raise
        except (TypeError, ValueError) as exc:
            raise ContextSkillError(f"skill {self.key} rejected domain context") from exc
        if not isinstance(raw, Mapping):
            raise ContextSkillError(f"skill {self.key} publisher must return a mapping")
        unknown = sorted(set(raw) - CONTEXT_FIELDS)
        if unknown:
            raise ContextSkillError(
                f"skill {self.key} attempted to publish unknown field(s): {unknown}"
            )
        return deepcopy(dict(raw))

    def context_for_phase(
        self,
        phase: str,
        context: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        # Re-run the skill validator on the Pack's structured fields. This
        # rejects a manually forged/invalid Pack before any View is emitted.
        published = self.publish_context(context)
        allowed = get_context_policy(self.policy_id, self.policy_version).fields_for(phase)
        return {
            field: deepcopy(published[field])
            for field in sorted(allowed)
            if field in published
        }


class ContextSkillRegistry:
    """Copy-on-write registry keyed by explicit skill id and version."""

    def __init__(self) -> None:
        self._skills: Mapping[tuple[str, str], ContextSkill] = MappingProxyType({})

    def register(self, skill: ContextSkill) -> ContextSkill:
        if not isinstance(skill, ContextSkill):
            raise ContextSkillError("only ContextSkill objects may be registered")
        if skill.key in self._skills:
            raise ContextSkillError(f"duplicate context skill: {skill.key}")
        updated = dict(self._skills)
        updated[skill.key] = skill
        self._skills = MappingProxyType(updated)
        return skill

    def get(self, skill_id: str, version: str) -> ContextSkill:
        key = (skill_id, version)
        try:
            return self._skills[key]
        except KeyError as exc:
            raise ContextSkillError(f"unknown context skill: {key}") from exc

    def registered(self) -> tuple[ContextSkill, ...]:
        return tuple(self._skills[key] for key in sorted(self._skills))


_SKILLS = ContextSkillRegistry()


def register_context_skill(skill: ContextSkill) -> ContextSkill:
    return _SKILLS.register(skill)


def get_context_skill(skill_id: str, version: str) -> ContextSkill:
    return _SKILLS.get(skill_id, version)


def registered_context_skills() -> tuple[ContextSkill, ...]:
    return _SKILLS.registered()


def context_skill_for_task_type(task_type: str) -> ContextSkill:
    """Return the only skill allowed to serve ``task_type``.

    Domain routing is the authority for known task types.  Anything not in the
    registry is intentionally treated as the generic task surface, so an
    unrecognised task can never select a richer domain skill by identity alone.
    The import is local to keep the domain registry and skill registry free of a
    module-import cycle.
    """

    from .base import get_domain_for_task_type

    domain = get_domain_for_task_type(task_type)
    if domain is None:
        return get_context_skill(GENERIC_CONTEXT_SKILL_ID, CONTEXT_CONTRACT_VERSION)
    return get_context_skill(domain.context_skill_id, domain.context_skill_version)


def validate_context_binding(
    *,
    task_type: str,
    skill_id: str,
    skill_version: str,
    policy_id: str,
    policy_version: str,
) -> ContextSkill:
    """Validate the complete task/domain/skill/policy identity chain.

    This is the single identity contract used at both the Pack construction
    boundary and the independent Projector boundary.  It deliberately checks
    the supplied skill before comparing it with the task route so an unknown
    skill remains an explicit fail-closed error rather than being mistaken for
    a merely mismatched domain.
    """

    if not isinstance(task_type, str):
        raise ContextSkillError("task type must be a string")
    try:
        skill = get_context_skill(skill_id, skill_version)
    except ContextSkillError:
        raise

    expected = context_skill_for_task_type(task_type)
    expected_domain_id = expected.domain_id
    if skill.key != expected.key or skill.domain_id != expected_domain_id:
        raise ContextSkillError(
            "context skill is not bound to the task type's registered domain"
        )
    if (
        skill.policy_id != policy_id
        or skill.policy_version != policy_version
        or expected.policy_id != policy_id
        or expected.policy_version != policy_version
    ):
        raise ContextSkillError(
            "context policy identity does not match the task-bound skill"
        )
    return skill


def _register_builtin_skills() -> None:
    register_context_skill(ContextSkill(
        skill_id=GENERIC_CONTEXT_SKILL_ID,
        version=CONTEXT_CONTRACT_VERSION,
        domain_id="generic",
        policy_id=GENERIC_CONTEXT_POLICY_ID,
        policy_version=CONTEXT_CONTRACT_VERSION,
        publisher=_empty_context,
    ))
    register_context_skill(ContextSkill(
        skill_id=LOCAL_LIFE_CONTEXT_SKILL_ID,
        version=CONTEXT_CONTRACT_VERSION,
        domain_id="local_life",
        policy_id=LOCAL_LIFE_CONTEXT_POLICY_ID,
        policy_version=CONTEXT_CONTRACT_VERSION,
        publisher=_empty_context,
    ))
    register_context_skill(ContextSkill(
        skill_id=ECOMMERCE_CONTEXT_SKILL_ID,
        version=CONTEXT_CONTRACT_VERSION,
        domain_id="ecommerce",
        policy_id=ECOMMERCE_CONTEXT_POLICY_ID,
        policy_version=CONTEXT_CONTRACT_VERSION,
        publisher=_publish_ecommerce_context,
    ))


_register_builtin_skills()


__all__ = [
    "CONTEXT_CONTRACT_VERSION",
    "ECOMMERCE_CONTEXT_SKILL_ID",
    "GENERIC_CONTEXT_SKILL_ID",
    "LOCAL_LIFE_CONTEXT_SKILL_ID",
    "ContextSkill",
    "ContextSkillError",
    "ContextSkillRegistry",
    "context_skill_for_task_type",
    "get_context_skill",
    "register_context_skill",
    "registered_context_skills",
    "validate_context_binding",
]
