"""Evaluation-only V3 treatment server preserving exact requirement provenance."""

from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException

from agent.app.domains.ecommerce.models import ShoppingRequirement
from agent.app.settings import settings
from evaluation import shopping_task_state_context_ab_v2_server as _base


def _requirement(requirement: Any, *, category: str) -> ShoppingRequirement:
    legacy = _base._requirement_original(requirement, category=category)
    return legacy.model_copy(
        update={"source": requirement.source_provenance or requirement.source}
    )


if not hasattr(_base, "_requirement_original"):
    _base._requirement_original = _base._requirement
_base._requirement = _requirement
app = _base.app


@app.get("/internal/evaluation/taskstate-context-ab-v3")
async def treatment_status_v3(
    x_agent_debug_key: str | None = Header(default=None, alias="X-Agent-Debug-Key"),
) -> dict[str, Any]:
    if not settings.agent_trace_debug_enabled or x_agent_debug_key != settings.agent_trace_debug_key:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "experimentId": "shopping-task-state-context-ab-v3-20260827",
        "arm": "treatment",
        "runtime": settings.agent_control_runtime,
        "projectionCount": _base._projection_count,
        "projectionFailures": _base._projection_failures,
        "productionContractChangedDuringRun": False,
        "productionContractRevision": "shopping-task-state-v2-source-provenance-v1",
    }
