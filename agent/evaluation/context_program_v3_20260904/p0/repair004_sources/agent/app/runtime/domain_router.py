from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..domains import (
    DomainHint,
    DomainId,
    get_domain,
    get_domain_for_task_type,
    registered_domains,
)

RoutingReason = Literal[
    "explicit_hint",
    "detected_switch",
    "active_task",
    "detected_message",
    "ambiguous_message",
    "default_local_life",
]


@dataclass(frozen=True, slots=True)
class DomainRoutingDecision:
    domain_id: DomainId
    task_type: str
    reason: RoutingReason
    confidence: float
    ambiguous: bool = False
    clarification_question: str | None = None


def _decision(
    domain_id: DomainId,
    reason: RoutingReason,
    confidence: float,
    *,
    ambiguous: bool = False,
    clarification_question: str | None = None,
) -> DomainRoutingDecision:
    spec = get_domain(domain_id)
    return DomainRoutingDecision(
        domain_id=domain_id,
        task_type=spec.task_type,
        reason=reason,
        confidence=confidence,
        ambiguous=ambiguous,
        clarification_question=clarification_question,
    )


def resolve_domain(
    message: str,
    *,
    domain_hint: DomainHint = "auto",
    active_task_type: str | None = None,
) -> DomainRoutingDecision:
    """Resolve one turn without allowing tools from two domains to mix."""

    if domain_hint != "auto":
        return _decision(domain_hint, "explicit_hint", 1.0)

    active = get_domain_for_task_type(active_task_type)
    detected = [spec for spec in registered_domains() if spec.matches(message)]

    if len(detected) > 1:
        fallback = active.domain_id if active is not None else "local_life"
        return _decision(
            fallback,
            "ambiguous_message",
            0.5,
            ambiguous=True,
            clarification_question=(
                "你这次是想继续本地生活查询，还是切换到 3C 商品导购？"
            ),
        )

    if detected:
        selected = detected[0]
        if active is not None and selected.domain_id != active.domain_id:
            return _decision(selected.domain_id, "detected_switch", 0.95)
        return _decision(selected.domain_id, "detected_message", 0.95)

    if active is not None:
        return _decision(active.domain_id, "active_task", 0.9)

    return _decision("local_life", "default_local_life", 0.6)
