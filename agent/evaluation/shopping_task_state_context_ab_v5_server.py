"""Evaluation-only V5 treatment server under the current react_v1 runtime."""

from __future__ import annotations

from typing import Any

from fastapi import Header, HTTPException

from agent.app.settings import settings
from evaluation import shopping_task_state_context_ab_v3_server as _base


app = _base.app


@app.get("/internal/evaluation/taskstate-context-ab-v5")
async def treatment_status_v5(
    x_agent_debug_key: str | None = Header(default=None, alias="X-Agent-Debug-Key"),
) -> dict[str, Any]:
    if not settings.agent_trace_debug_enabled or x_agent_debug_key != settings.agent_trace_debug_key:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "experimentId": "shopping-task-state-context-ab-v5-20260829",
        "arm": "treatment",
        "runtime": settings.agent_control_runtime,
        "reactLiveEnabled": settings.agent_react_live_enabled,
        "projectionCount": _base._base._projection_count,
        "projectionFailures": _base._base._projection_failures,
        "productionContractChangedDuringRun": False,
        "productionContractRevision": "shopping-task-state-v2-source-provenance-v1",
    }
