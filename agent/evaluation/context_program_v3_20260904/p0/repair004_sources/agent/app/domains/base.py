from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

DomainId = Literal["local_life", "ecommerce"]
DomainHint = Literal["auto", "local_life", "ecommerce"]


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """Stable registration contract for one user-facing Agent domain."""

    domain_id: DomainId
    task_type: str
    label: str
    tool_names: tuple[str, ...]
    matches: Callable[[str], bool]
    context_skill_id: str
    context_skill_version: str


_DOMAINS: dict[DomainId, DomainSpec] = {}
_TASK_TYPES: dict[str, DomainSpec] = {}


def register_domain(spec: DomainSpec) -> DomainSpec:
    from .context_skill import get_context_skill

    skill = get_context_skill(spec.context_skill_id, spec.context_skill_version)
    if skill.domain_id != spec.domain_id:
        raise ValueError(
            f"context skill {skill.key} is not bound to domain {spec.domain_id}"
        )
    if spec.domain_id in _DOMAINS:
        raise ValueError(f"duplicate domain id: {spec.domain_id}")
    if spec.task_type in _TASK_TYPES:
        raise ValueError(f"duplicate task type: {spec.task_type}")
    _DOMAINS[spec.domain_id] = spec
    _TASK_TYPES[spec.task_type] = spec
    return spec


def get_domain(domain_id: DomainId) -> DomainSpec:
    return _DOMAINS[domain_id]


def get_domain_for_task_type(task_type: str | None) -> DomainSpec | None:
    if task_type is None:
        return None
    return _TASK_TYPES.get(task_type)


def registered_domains() -> tuple[DomainSpec, ...]:
    return tuple(_DOMAINS.values())
