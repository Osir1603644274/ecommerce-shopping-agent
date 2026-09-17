"""Pure memory-domain contracts; production authorization is intentionally absent."""

from .long_term_memory import (
    DurableConsentLedger,
    LongTermMemoryEntry,
    LongTermMemoryProjection,
    MemoryCommandDraft,
    PRODUCTION_AUTHORIZATION_UNAVAILABLE,
    ShoppingPreference,
    applicable_preferences,
    serialize_projection,
)
from .context_projection import (
    LongTermMemoryContext,
    LongTermShoppingPreference,
    derive_long_term_memory_context,
    empty_long_term_memory_context,
)
from .governance import (
    EffectivePreferenceSnapshot,
    MemoryApplicationContext,
    MemoryApplicabilityDecision,
    MemorySnapshot,
    ScopedMemoryRecord,
    memory_snapshot_from_authenticated_projection_v2,
    memory_snapshot_from_projection_v2,
    projection_result_from_effective,
    resolve_effective_preferences,
)
from .historical_events import (
    EligibleHistoricalClick,
    HistoricalEvent,
    HistoricalEventApplicationContext,
    HistoricalEventDecision,
    HistoricalEventSnapshot,
    HistoricalSignalSnapshot,
    resolve_historical_clicks,
)

__all__ = [
    "DurableConsentLedger",
    "LongTermMemoryEntry",
    "LongTermMemoryContext",
    "LongTermMemoryProjection",
    "LongTermShoppingPreference",
    "MemoryCommandDraft",
    "MemoryApplicationContext",
    "MemoryApplicabilityDecision",
    "MemorySnapshot",
    "memory_snapshot_from_authenticated_projection_v2",
    "memory_snapshot_from_projection_v2",
    "PRODUCTION_AUTHORIZATION_UNAVAILABLE",
    "EffectivePreferenceSnapshot",
    "EligibleHistoricalClick",
    "HistoricalEvent",
    "HistoricalEventApplicationContext",
    "HistoricalEventDecision",
    "HistoricalEventSnapshot",
    "HistoricalSignalSnapshot",
    "ScopedMemoryRecord",
    "ShoppingPreference",
    "applicable_preferences",
    "derive_long_term_memory_context",
    "empty_long_term_memory_context",
    "projection_result_from_effective",
    "resolve_effective_preferences",
    "resolve_historical_clicks",
    "serialize_projection",
]
