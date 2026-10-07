"""Server-owned, version-bound catalog evidence for unified guide turns."""
from __future__ import annotations

import json
from typing import Any

from .catalog_service import fingerprint, verify_scope
from .guide_state import bind_guide_write
from .task_state import TaskState, TaskStatePatchRequest, update_task_state


def _key(task_id: str, scope_id: str, base_revision: int) -> str:
    return f"guide-evidence:{task_id}:{base_revision}:{scope_id}"


async def publish_scope(task: TaskState, scope: dict[str, Any], *, run_id: str) -> TaskState:
    """Stage immutable evidence, then publish only its reference by TaskState CAS."""
    from .task_state import TASK_STATE_TTL_SECONDS, _get_client

    verify_scope(scope)
    scope_id = scope["scopeId"]
    key = _key(task.task_id, scope_id, task.revision)
    body = {"taskId": task.task_id, "baseRevision": task.revision,
            "scopeId": scope_id, "scopeSha256": fingerprint(scope), "scope": scope}
    client = _get_client()
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False)
    existing = await client.get(key)
    if existing is not None and json.loads(existing) != body:
        raise ValueError("guide evidence key collision")
    await client.set(key, encoded, ex=TASK_STATE_TTL_SECONDS)
    reference = {k: body[k] for k in ("taskId", "baseRevision", "scopeId", "scopeSha256")}
    reference["runId"] = run_id
    patch = TaskStatePatchRequest(expectedRevision=task.revision, actor="tool",
                                  domainStatePatch={"guideEvidenceRef": reference})
    with bind_guide_write():
        return await update_task_state(task.task_id, patch)


async def read_scope(task: TaskState) -> dict[str, Any] | None:
    from .task_state import _get_client

    ref = task.domain_state.get("guideEvidenceRef")
    if not isinstance(ref, dict) or ref.get("taskId") != task.task_id:
        return None
    base_revision = ref.get("baseRevision")
    if type(base_revision) is not int:
        return None
    raw = await _get_client().get(_key(task.task_id, ref.get("scopeId", ""), base_revision))
    if raw is None:
        return None
    body = json.loads(raw)
    scope = body.get("scope")
    verify_scope(scope)
    if (body.get("taskId") != task.task_id or body.get("scopeId") != ref.get("scopeId")
            or fingerprint(scope) != ref.get("scopeSha256")
            or body.get("baseRevision") != ref.get("baseRevision")):
        raise ValueError("guide evidence reference mismatch")
    return scope
