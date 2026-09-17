"""Default-off durable memory receipt; never deserialize a model-authored binding.

A run stores only an identity-bound fingerprint in Redis, not credentials or a
second copy of authority. On recovery the BFF reissues from Java, then matches
the original fingerprint. Any authority change stops NEW effects. A completed
publication replay does not call this guard and does not regenerate an answer.
"""
from __future__ import annotations

import hashlib
import json
import weakref
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .v3_runtime import MemoryRunBinding, ProjectionV3Result, _validated_projection
from ..settings import settings


class MemorySnapshotRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class _Authority:
    ref: weakref.ReferenceType
    owner: str
    browser_binding: str
    refresh: Callable[[], Awaitable[tuple[MemoryRunBinding, str]]]


_ISSUED: dict[int, _Authority] = {}


def register_authority(binding: MemoryRunBinding, projection: ProjectionV3Result,
                       browser_binding: str,
                       refresh: Callable[[], Awaitable[tuple[MemoryRunBinding, str]]]) -> None:
    binding.payload_for_phase("planner")
    if not _validated_projection(projection) or not projection.owner_binding:
        raise MemorySnapshotRejected("memory_authority_unissued")
    identity = id(binding)
    _ISSUED[identity] = _Authority(
        weakref.ref(binding, lambda _ref: _ISSUED.pop(identity, None)),
        projection.owner_binding, browser_binding, refresh)


def has_durable_authority(binding: MemoryRunBinding | None) -> bool:
    if binding is None:
        return False
    authority = _ISSUED.get(id(binding))
    return authority is not None and authority.ref() is binding


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _fingerprint(binding: MemoryRunBinding | None, owner: str = "") -> str:
    payload = binding.payload_for_phase("planner") if binding is not None else None
    return hashlib.sha256(_canonical({
        "owner": owner,
        "revision": binding.memory_revision if binding is not None and payload else 0,
        "category": binding.category_id if payload else None,
        "catalog": binding.catalog_revision if payload else None,
        "preferences": payload,
    }).encode()).hexdigest()


async def prepare_memory_guard(*, redis: Any, binding: MemoryRunBinding | None,
                               task_id: str, run_id: str, session_id: str | None,
                               resuming: bool) -> Callable[..., Awaitable[None]]:
    from ..graph.resume import session_owner_hash

    if not session_id:
        raise MemorySnapshotRejected("memory_session_missing")
    authority = _ISSUED.get(id(binding)) if has_durable_authority(binding) else None
    retained = binding is not None and bool(binding.payload_for_phase("planner"))
    if retained and authority is None:
        raise MemorySnapshotRejected("memory_authority_unissued")
    expected = _fingerprint(binding, authority.owner if retained else "")
    owner_hash = session_owner_hash(session_id)
    key = "memory:durable:v1:" + hashlib.sha256(
        _canonical([task_id, run_id]).encode()).hexdigest()
    receipt = _canonical({"schemaVersion": 1, "taskId": task_id, "runId": run_id,
        "sessionOwnerHash": owner_hash,
        "browserBinding": authority.browser_binding if retained else None,
        "fingerprint": expected})
    existing = await redis.get(key)
    if isinstance(existing, bytes):
        existing = existing.decode("utf-8")
    if existing is None:
        if resuming:
            if not retained and not settings.memory_durable_snapshot_enabled:
                async def legacy_empty_guard(*_args: Any) -> None:
                    return None
                return legacy_empty_guard
            raise MemorySnapshotRejected("memory_resume_snapshot_missing")
        await redis.set(key, receipt, nx=True)
        existing = await redis.get(key)
        if isinstance(existing, bytes):
            existing = existing.decode("utf-8")
    if existing != receipt:
        raise MemorySnapshotRejected("memory_snapshot_identity_or_revision_changed")

    async def guard(actual_task: str = task_id, actual_run: str = run_id,
                    actual_owner: str = owner_hash) -> None:
        if (actual_task, actual_run, actual_owner) != (task_id, run_id, owner_hash):
            raise MemorySnapshotRejected("memory_runtime_identity_changed")
        if not retained:
            return  # Empty run stays empty even when a new preference is saved.
        if not (settings.memory_durable_snapshot_enabled and settings.memory_bff_enabled
                and settings.memory_projection_client_enabled):
            raise MemorySnapshotRejected("memory_disabled_during_run")
        if binding.catalog_revision != settings.memory_active_catalog_revision:
            raise MemorySnapshotRejected("memory_catalog_changed_during_run")
        assert authority is not None
        try:
            refreshed, owner = await authority.refresh()
            if _fingerprint(refreshed, owner) != expected:
                raise MemorySnapshotRejected("memory_changed_during_run")
        except MemorySnapshotRejected:
            raise
        except Exception as exc:
            raise MemorySnapshotRejected("memory_authority_unavailable") from exc

    await guard()
    return guard
