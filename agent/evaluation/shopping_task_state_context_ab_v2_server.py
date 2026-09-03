"""Evaluation-only ASGI bootstrap for the V2 task-semantic treatment arm.

Launch from the repository root as::

    python -m uvicorn agent.evaluation.shopping_task_state_context_ab_v2_server:app

Production modules and default settings remain unchanged.  This bootstrap
patches only the process-local ``agent.app.llm.build_context_pack`` reference.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from fastapi import Header, HTTPException

from agent.app import llm as llm_module
from agent.app.context_pack import build_context_pack as production_build_context_pack
from agent.app.domains.ecommerce.models import (
    SPEC_REGISTRY,
    BrandAvoidance,
    CandidateScope,
    ScopeRerankRequest,
    ShoppingGuideState,
    ShoppingRequirement,
    compiled_shopping_requirements,
)
from agent.app.domains.ecommerce.shopping_task_state_v2 import ShoppingTaskStateV2
from agent.app.settings import settings
from agent.app.task_state import TaskState


_projection_lock = asyncio.Lock()
_projection_count = 0
_projection_failures = 0


class TreatmentProjectionError(ValueError):
    pass


def _requirement(requirement: Any, *, category: str) -> ShoppingRequirement:
    spec = SPEC_REGISTRY.get(category, {}).get(requirement.key)
    if requirement.status != "active" or spec is None:
        raise TreatmentProjectionError("invalid V2 terminal requirement")
    _value_type, unit, _operators = spec
    value = list(requirement.value) if isinstance(requirement.value, tuple) else requirement.value
    return ShoppingRequirement(
        key=requirement.key,
        operator=requirement.operator,
        value=value,
        unit=unit,
        priority=requirement.priority,
        source=requirement.source,
    )


def _project(state: TaskState) -> TaskState:
    snapshot = ShoppingTaskStateV2.model_validate(
        state.domain_state.get("shoppingTaskStateV2")
    )
    shared_guide = ShoppingGuideState.model_validate(
        state.domain_state.get("shoppingGuide")
    )
    if shared_guide.category is None:
        raise TreatmentProjectionError("shared category missing")
    requirements: list[ShoppingRequirement] = []
    avoidances: list[BrandAvoidance] = []
    for item in snapshot.requirements:
        legacy = _requirement(item, category=shared_guide.category)
        if legacy.key == "brand" and legacy.operator in {"neq", "not_in"}:
            values = legacy.value if isinstance(legacy.value, list) else [legacy.value]
            if item.source != "user" or not all(isinstance(value, str) for value in values):
                raise TreatmentProjectionError("invalid V2 brand avoidance")
            avoidances.append(BrandAvoidance(
                values=values,
                strength=legacy.priority,
                source="user",
            ))
        else:
            requirements.append(legacy)
    projected_guide = ShoppingGuideState.model_validate(
        shared_guide.model_copy(
            deep=True,
            update={
                "use_cases": [] if snapshot.use_case == "shopping" else [snapshot.use_case],
                "requirements": requirements,
                "brand_avoidances": avoidances,
                "candidate_ids": [],
            },
        ).model_dump(by_alias=True, mode="json")
    )

    raw_scope = state.domain_state.get("candidateScope")
    try:
        scope = CandidateScope.model_validate(raw_scope)
    except (TypeError, ValueError):
        scope = None
    v2_scope = snapshot.candidate_scope
    if v2_scope is not None:
        if scope is None or (
            scope.status != "active"
            or scope.task_id != state.task_id
            or scope.scope_id != v2_scope.scope_id
            or scope.category != v2_scope.category
            or scope.category != projected_guide.category
            or tuple(scope.visible_product_ids or scope.ranked_item_ids)
            != tuple(v2_scope.candidate_ids)
            or list(scope.requirements_snapshot)
            != list(compiled_shopping_requirements(projected_guide))
        ):
            raise TreatmentProjectionError("shared CandidateScope mismatch")
    elif scope is not None and scope.status == "active":
        raise TreatmentProjectionError("active CandidateScope lacks V2 link")

    raw_rerank = state.domain_state.get("scopeRerankRequest")
    try:
        rerank = ScopeRerankRequest.model_validate(raw_rerank)
    except (TypeError, ValueError):
        rerank = None
    if rerank is not None and (
        scope is None
        or scope.status != "active"
        or v2_scope is None
        or rerank.scope_id != scope.scope_id
    ):
        raise TreatmentProjectionError("shared ScopeRerankRequest mismatch")

    if scope is not None and scope.status == "active":
        projected_guide = projected_guide.model_copy(
            update={"candidate_ids": list(scope.visible_product_ids or scope.ranked_item_ids)}
        )
    if projected_guide.mode == "compare":
        if scope is None or scope.status != "active":
            raise TreatmentProjectionError("comparison scope missing")
        if not set(projected_guide.compared_ids).issubset(set(scope.ranked_item_ids)):
            raise TreatmentProjectionError("comparison IDs outside CandidateScope")

    domain = deepcopy(state.domain_state)
    domain["shoppingGuide"] = projected_guide.model_dump(by_alias=True, mode="json")
    if scope is None:
        domain.pop("candidateScope", None)
    else:
        domain["candidateScope"] = scope.model_dump(by_alias=True, mode="json")
    if rerank is None:
        domain.pop("scopeRerankRequest", None)
    else:
        domain["scopeRerankRequest"] = rerank.model_dump(by_alias=True, mode="json")
    return state.model_copy(
        deep=True,
        update={
            "goal": snapshot.goal,
            "unknowns": [item.reason for item in snapshot.unknowns],
            "pending_questions": [item.reason for item in snapshot.unknowns if item.blocking],
            "domain_state": domain,
        },
    )


async def treatment_build_context_pack(
    state: TaskState,
    **kwargs: Any,
) -> Any:
    global _projection_count, _projection_failures
    if "shoppingTaskStateV2" not in state.domain_state:
        return await production_build_context_pack(state, **kwargs)
    try:
        projected = _project(state)
    except Exception:
        async with _projection_lock:
            _projection_failures += 1
        raise
    async with _projection_lock:
        _projection_count += 1
    return await production_build_context_pack(projected, **kwargs)


llm_module.build_context_pack = treatment_build_context_pack

from agent.app import main as main_module  # noqa: E402

main_module.build_context_pack = treatment_build_context_pack
app = main_module.app


@app.get("/internal/evaluation/taskstate-context-ab-v2")
async def treatment_status(
    x_agent_debug_key: str | None = Header(default=None, alias="X-Agent-Debug-Key"),
) -> dict[str, Any]:
    if not settings.agent_trace_debug_enabled or x_agent_debug_key != settings.agent_trace_debug_key:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "experimentId": "shopping-task-state-context-ab-v2-20260827",
        "arm": "treatment",
        "runtime": settings.agent_control_runtime,
        "projectionCount": _projection_count,
        "projectionFailures": _projection_failures,
        "productionContractChanged": False,
    }
