"""Default-off authenticated bridge from Java memory projection to ContextPack.

The bridge owns no browser parsing and cannot create an authenticated request.
It receives an already-issued request capability, asks Java for the V2
projection, and returns an empty context for every unavailable or invalid path.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterable

from ..request_auth import RequestAuthContext, _memory_credential
from .context_projection import LongTermMemoryContext, derive_long_term_memory_context, empty_long_term_memory_context
from .governance import (
    MemoryApplicationContext,
    memory_snapshot_from_authenticated_projection_v2,
    projection_result_from_effective,
    resolve_effective_preferences,
)
from .long_term_memory import ShoppingPreference
from .projection_client import MemoryProjectionClient


async def load_authenticated_long_term_memory_context(
    *,
    enabled: bool,
    projection_client: MemoryProjectionClient,
    request_auth_context: RequestAuthContext | None,
    product_category: str,
    recipient_scope: str,
    current_turn: Iterable[ShoppingPreference] = (),
    task_state: Iterable[ShoppingPreference] = (),
    now: datetime | None = None,
) -> LongTermMemoryContext:
    """Return only a bounded governed context, otherwise the issued empty one.

    The bearer is read solely by the projection client.  It is never returned,
    logged, placed in a snapshot, or exposed to a model-facing object.
    """
    if enabled is not True or request_auth_context is None:
        return empty_long_term_memory_context()
    try:
        credential = _memory_credential(request_auth_context)
        projection = await projection_client.fetch_v2(credential)
        snapshot = memory_snapshot_from_authenticated_projection_v2(projection)
        application = MemoryApplicationContext(
            authenticated_owner_user_id=snapshot.owner_user_id,
            product_category=product_category,  # validated by the contract
            recipient_scope=recipient_scope,  # validated by the contract
            now=datetime.now(UTC) if now is None else now,
            current_turn=tuple(current_turn),
            task_state=tuple(task_state),
            memory_enabled=True,
        )
        effective = resolve_effective_preferences(snapshot, application)
        governed = projection_result_from_effective(effective)
        return derive_long_term_memory_context(
            governed,
            current_requirements=application.current_turn + application.task_state,
        )
    except (TypeError, ValueError, AttributeError):
        return empty_long_term_memory_context()


__all__ = ["load_authenticated_long_term_memory_context"]
