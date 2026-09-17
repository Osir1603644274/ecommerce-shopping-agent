"""Deterministic context construction layer built on top of TaskState and SessionMemory.

This module does NOT introduce a second memory system; it reads the existing TaskState,
SessionMemory history, and domain tools to produce a bounded, prioritised context snapshot
that the Harness, Planner, Executor, Validator, Replanner, and Critic can all consume
without repeatedly re-deriving the same structure from raw chat history.

Budget enforcement uses deterministic hybrid compression: protected structured
facts stay verbatim, recent dialogue stays verbatim, older dialogue is compacted,
duplicates are removed, and only low-priority material may be dropped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_policy import (
    CONTEXT_CONTRACT_VERSION,
    GENERIC_CONTEXT_POLICY_ID,
)
from .task_state import TaskConstraint, TaskFact, TaskState
from .domains import get_domain_for_task_type
from .domains.context_skill import (
    CONTEXT_CONTRACT_VERSION as SKILL_CONTRACT_VERSION,
    GENERIC_CONTEXT_SKILL_ID,
    ContextSkillError,
    context_skill_for_task_type,
    get_context_skill,
    validate_context_binding,
)
from .domains.ecommerce.models import (
    CATEGORY_LABELS,
    CandidateScope,
    ScopeRerankRequest,
    ShoppingGuideState,
    compiled_shopping_requirements,
)
from .domains.ecommerce.shopping_state_authority import select_shopping_state_authority
from .settings import settings

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_BUDGET = 6_000
MAX_HISTORY_SUMMARIES = 6
RECENT_HISTORY_MESSAGES = 4

# A ContextPack is the common bounded source. Views are smaller projections and
# use these explicit ceilings during quality/cost evaluation; they are recorded
# here so a single undocumented 6000-token number is not treated as universal.
PHASE_TOKEN_BUDGETS = {
    "planner": 6_000,
    "executor": 3_000,
    # Temporary development ceiling: three-card real-query comparisons may
    # carry the complete deterministic proof while Harness work is still in
    # progress.  Context compaction is evaluated later, not allowed to block
    # the current architecture/demo pass.
    "validator": 10_000,
    "replanner": 4_000,
    # Temporary development ceiling: unblock real multi-turn architecture
    # testing while FinalAnswer projection compaction remains follow-up work.
    "final_answer": 10_000,
}

# Priority tiers used by the truncation algorithm — lower tier = evicted first.
_PRIORITY_SAFETY = 0
_PRIORITY_USER_CORRECTIONS = 1
_PRIORITY_CONFIRMED_GOALS = 2
_PRIORITY_UNKNOWNS = 3
_PRIORITY_RECENT_HISTORY = 4
_PRIORITY_SOFT_PREFS = 5
_PRIORITY_EVIDENCE = 6


class HistorySummary(BaseModel):
    """A compact summary of one historical turn; kept bounded at most 6 entries."""

    role: Literal["user", "assistant"] = "user"
    summary: str = Field(max_length=300)
    at_turn: int | None = None
    kind: Literal["recent_verbatim", "older_summary"] = "recent_verbatim"
    source_turns: list[int] = Field(default_factory=list, alias="sourceTurns")


class TruncationTrace(BaseModel):
    """Audit trail for deterministic hybrid compression and final trimming."""

    budget_tokens: int
    initial_token_count: int
    final_token_count: int
    strategy: Literal["hybrid"] = "hybrid"
    deduplicated_history_messages: int = 0
    compressed_history_groups: int = 0
    deduplicated_soft_preferences: int = 0
    preserved_evidence_refs: int = 0
    dropped_history_summaries: int = 0
    dropped_soft_preferences: int = 0
    dropped_evidence_refs: int = 0
    truncation_reason: str = ""


class ContextPackBudgetExceeded(ValueError):
    """Protected context alone cannot fit the configured model budget."""


# All names a revision may arrive under (camelCase alias / snake_case field,
# canonical / legacy).  With ``populate_by_name=True`` Pydantic accepts every one.
_BASE_REVISION_KEYS = (
    "baseContextRevision",
    "base_context_revision",
    "taskRevision",
    "task_revision",
)


def _normalize_base_context_revision(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize the accepted revision input names to a single canonical value.

    - Copies the input: the caller's dict is never mutated.
    - Presence is judged by key existence, never by truthiness — a provided
      ``0`` is a provided value and is left for ``ge=1`` to reject.
    - Any two provided values that differ fail closed; every provided value
      must agree, otherwise there is no single truth to normalize to.
    - Legacy ``taskRevision``/``task_revision`` keys are removed and exactly one
      canonical ``baseContextRevision`` is written.
    """
    data = dict(data)
    provided = {key: data[key] for key in _BASE_REVISION_KEYS if key in data}
    values = list(provided.values())
    if len(values) >= 2:
        first = values[0]
        for value in values[1:]:
            if value != first:
                raise ValueError(
                    "conflicting baseContextRevision inputs: "
                    f"{sorted(provided.items())} — all provided names must agree"
                )
    for key in _BASE_REVISION_KEYS:
        data.pop(key, None)
    if values:
        data["baseContextRevision"] = values[0]
    return data


_CONTEXT_METADATA_KEYS = {
    "contextPolicyId": ("contextPolicyId", "context_policy_id"),
    "contextPolicyVersion": ("contextPolicyVersion", "context_policy_version"),
    "contextSkillId": ("contextSkillId", "context_skill_id"),
    "contextSkillVersion": ("contextSkillVersion", "context_skill_version"),
}


def _normalize_context_contract(data: dict[str, Any]) -> dict[str, Any]:
    """Attach one registered domain skill to legacy/direct Pack callers."""

    data = dict(data)
    task_type = data.get("taskType", data.get("task_type", "generic"))
    present: dict[str, Any] = {}
    for canonical, aliases in _CONTEXT_METADATA_KEYS.items():
        values = [data[key] for key in aliases if key in data]
        if len(values) > 1 and any(value != values[0] for value in values[1:]):
            raise ValueError(f"conflicting context contract inputs for {canonical}")
        if values:
            present[canonical] = values[0]
        for key in aliases:
            data.pop(key, None)

    if present and set(present) != set(_CONTEXT_METADATA_KEYS):
        raise ValueError(
            "context policy/skill id and version must be supplied together"
        )

    if not present:
        skill = context_skill_for_task_type(task_type)
        present = {
            "contextPolicyId": skill.policy_id,
            "contextPolicyVersion": skill.policy_version,
            "contextSkillId": skill.skill_id,
            "contextSkillVersion": skill.version,
        }
    data.update(present)
    try:
        validate_context_binding(
            task_type=task_type,
            skill_id=data["contextSkillId"],
            skill_version=data["contextSkillVersion"],
            policy_id=data["contextPolicyId"],
            policy_version=data["contextPolicyVersion"],
        )
    except ContextSkillError as exc:
        raise ValueError(str(exc)) from exc
    return data


class ContextPack(BaseModel):
    """Deterministic, bounded context snapshot consumed by agent lifecycle phases.

    All fields are derived from existing TaskState / SessionMemory / tool schemas;
    nothing in ContextPack is a second source of truth. One Pack is created
    after the current user turn updates TaskState and remains immutable in
    meaning for that run. Planner/Executor/Validator changes advance
    ``phaseTaskRevision`` in their Views; they do not mutate this base snapshot.
    The next user turn always builds a new Pack.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"

    history_repair_version: Literal["history-v1"] | None = Field(
        default=None, alias="historyRepairVersion", exclude_if=lambda value: value is None,
    )

    # Identity
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    base_context_revision: int = Field(ge=1, alias="baseContextRevision")

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        """Accept legacy ``taskRevision`` / ``task_revision`` as input,
        store in canonical ``baseContextRevision``.  Fail closed when any two
        provided revision values differ."""
        if not isinstance(data, dict):
            return data
        return _normalize_context_contract(_normalize_base_context_revision(data))

    @model_validator(mode="after")
    def _validate_context_boundary(self) -> "ContextPack":
        validate_context_pack_contract(self)
        return self

    # Core intent
    goal: str
    task_type: str = Field(default="generic", alias="taskType")
    context_policy_id: str = Field(
        default=GENERIC_CONTEXT_POLICY_ID,
        alias="contextPolicyId",
    )
    context_policy_version: str = Field(
        default=CONTEXT_CONTRACT_VERSION,
        alias="contextPolicyVersion",
    )
    context_skill_id: str = Field(
        default=GENERIC_CONTEXT_SKILL_ID,
        alias="contextSkillId",
    )
    context_skill_version: str = Field(
        default=SKILL_CONTRACT_VERSION,
        alias="contextSkillVersion",
    )

    # Structured world model (from TaskState)
    confirmed_facts: list[TaskFact] = Field(
        default_factory=list, alias="confirmedFacts"
    )
    hard_constraints: list[TaskConstraint] = Field(
        default_factory=list, alias="hardConstraints"
    )
    soft_preferences: list[dict[str, Any]] = Field(
        default_factory=list, alias="softPreferences"
    )
    unknowns: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(
        default_factory=list, alias="pendingQuestions"
    )

    # Domain-specific structured state
    shopping_guide_state: dict[str, Any] | None = Field(
        default=None, alias="shoppingGuideState"
    )
    candidate_scope_state: dict[str, Any] | None = Field(
        default=None, alias="candidateScopeState"
    )
    scope_rerank_request: dict[str, Any] | None = Field(
        default=None, alias="scopeRerankRequest"
    )
    shopping_state_authority_source: str | None = Field(
        default=None, alias="shoppingStateAuthoritySource"
    )
    shopping_state_degraded_reason: str | None = Field(
        default=None, alias="shoppingStateDegradedReason"
    )
    shopping_state_semantic_hash: str | None = Field(
        default=None, alias="shoppingStateSemanticHash"
    )

    # History summaries (at most 6)
    history_summaries: list[HistorySummary] = Field(
        default_factory=list, alias="historySummaries"
    )

    # Tool surface
    allowed_tools: list[str] = Field(default_factory=list, alias="allowedTools")

    # Evidence trail
    evidence_refs: list[str] = Field(default_factory=list, alias="evidenceRefs")

    # Budget accounting
    truncation_trace: TruncationTrace | None = Field(
        default=None, alias="truncationTrace"
    )


def validate_context_pack_contract(pack: ContextPack):
    """Revalidate a Pack's identity and published domain payload.

    Pydantic's ``model_copy(update=...)`` and ``model_construct()`` are
    intentionally available APIs, but they bypass before/after validators.
    This helper is therefore also called by ContextProjector immediately before
    any View can be emitted.  The value comparison makes the boundary strict:
    a generic/local skill cannot carry ecommerce fields, and a forged ecommerce
    field must equal the skill's validated serialization.
    """

    if not isinstance(pack, ContextPack):
        raise ContextSkillError("ContextPack boundary requires a ContextPack")

    required = (
        "run_id",
        "task_id",
        "base_context_revision",
        "goal",
        "task_type",
        "context_policy_id",
        "context_policy_version",
        "context_skill_id",
        "context_skill_version",
    )
    missing = [field for field in required if not hasattr(pack, field)]
    if missing:
        raise ContextSkillError(
            f"ContextPack is incomplete at the projection boundary: {missing}"
        )

    skill = validate_context_binding(
        task_type=pack.task_type,
        skill_id=pack.context_skill_id,
        skill_version=pack.context_skill_version,
        policy_id=pack.context_policy_id,
        policy_version=pack.context_policy_version,
    )
    context = {
        "shoppingGuide": getattr(pack, "shopping_guide_state", None),
        "candidateScope": getattr(pack, "candidate_scope_state", None),
        "scopeRerankRequest": getattr(pack, "scope_rerank_request", None),
    }
    context = {key: value for key, value in context.items() if value is not None}
    try:
        published = skill.publish_context(context)
    except ContextSkillError:
        raise
    for field in ("shoppingGuide", "candidateScope", "scopeRerankRequest"):
        supplied = context.get(field)
        if supplied is not None and field not in published:
            raise ContextSkillError(
                f"ContextPack contains unvalidated or cross-domain field: {field}"
            )
    return skill


def _stable_hash(*values: str) -> str:
    """Return a deterministic hex digest of the concatenated string values."""
    joined = "|".join(values)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def _estimate_tokens(text: str) -> int:
    """Quick conservative token estimator (≈ chars/2 for CJK; chars/3.5 for ASCII).

    This is ONLY for budget trimming — it does not need to match the model tokeniser
    exactly.  A 5-10 % error is harmless because the budget is already padded.
    """
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "〿")
    ascii_chars = len(text) - cjk
    return (cjk // 2) + (ascii_chars // 3) + 1


def _estimate_dict_tokens(obj: Any) -> int:
    """Conservative token estimate for a JSON-serialisable object."""
    return _estimate_tokens(json.dumps(obj, ensure_ascii=False, default=str))


_CONTEXT_IDENTITY_FIELDS = frozenset({
    "contextPolicyId",
    "contextPolicyVersion",
    "contextSkillId",
    "contextSkillVersion",
    "shoppingStateAuthoritySource",
    "shoppingStateDegradedReason",
    "shoppingStateSemanticHash",
})


def _budget_payload(pack: "ContextPack") -> dict[str, Any]:
    """Exclude fixed non-semantic contract labels from legacy Pack budgeting.

    Phase Views carry these labels and hash them, but they are not domain
    context. Keeping them outside the historical Pack budget preserves the
    existing 450-token compatibility boundary without permitting any
    variable/secret state to bypass fail-closed trimming.
    """

    payload = pack.model_dump(by_alias=True, mode="json")
    return {
        key: value
        for key, value in payload.items()
        if key not in _CONTEXT_IDENTITY_FIELDS
    }


def _build_history_summaries(
    history: list[dict[str, Any]] | None,
    max_summaries: int = MAX_HISTORY_SUMMARIES,
) -> list[HistorySummary]:
    """Keep recent messages verbatim and deterministically summarize older ones."""
    summaries, _ = _prepare_history_summaries(history, max_summaries=max_summaries)
    return summaries


def _prepare_history_summaries(
    history: list[dict[str, Any]] | None,
    *,
    max_summaries: int = MAX_HISTORY_SUMMARIES,
) -> tuple[list[HistorySummary], dict[str, int]]:
    """Return bounded history plus deterministic compression metrics.

    This is deliberately model-free.  TaskState keeps hard constraints and
    confirmed facts separately, so an older-dialogue summary can never rewrite
    those protected structures.
    """
    if not history:
        return [], {"deduplicated": 0, "compressedGroups": 0}

    messages: list[tuple[Literal["user", "assistant"], str, int]] = []
    turn_index = 0
    for message in history:
        role = message.get("role")
        if role not in {"user", "assistant"}:
            continue
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if role == "user":
            turn_index += 1
        normalized = re.sub(r"\s+", " ", content.strip())
        messages.append((role, normalized, turn_index))

    # Keep the most recent occurrence of an exact repeated message.
    deduplicated_reversed: list[tuple[Literal["user", "assistant"], str, int]] = []
    seen: set[tuple[str, str]] = set()
    for role, content, turn in reversed(messages):
        identity = (role, content.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        deduplicated_reversed.append((role, content, turn))
    unique_messages = list(reversed(deduplicated_reversed))

    recent_count = min(RECENT_HISTORY_MESSAGES, max_summaries, len(unique_messages))
    recent = unique_messages[-recent_count:] if recent_count else []
    older = unique_messages[:-recent_count] if recent_count else unique_messages
    older_slots = max(0, max_summaries - len(recent))
    older_groups: list[list[tuple[Literal["user", "assistant"], str, int]]] = []
    if older and older_slots:
        group_count = min(older_slots, 2, len(older))
        for index in range(group_count):
            start = index * len(older) // group_count
            end = (index + 1) * len(older) // group_count
            older_groups.append(older[start:end])

    summaries: list[HistorySummary] = []
    for group in older_groups:
        excerpts: list[str] = []
        for role, content, _turn in group:
            label = "用户" if role == "user" else "助手"
            first_sentence = re.split(r"(?<=[。！？.!?])\s*", content, maxsplit=1)[0]
            excerpt = f"{label}:{first_sentence[:90]}"
            if excerpt not in excerpts:
                excerpts.append(excerpt)
        summary = "较早对话：" + "；".join(excerpts)
        group_turns = sorted({turn for _role, _content, turn in group})
        source_turns = (
            group_turns
            if len(group_turns) <= 2
            else [group_turns[0], group_turns[-1]]
        )
        summaries.append(HistorySummary(
            role="user",
            summary=summary[:300],
            at_turn=max(turn for _role, _content, turn in group),
            kind="older_summary",
            sourceTurns=source_turns,
        ))

    summaries.extend(
        HistorySummary(
            role=role,
            summary=content[:300],
            at_turn=turn,
            kind="recent_verbatim",
            sourceTurns=[turn],
        )
        for role, content, turn in recent
    )
    return summaries, {
        "deduplicated": len(messages) - len(unique_messages),
        "compressedGroups": len(older_groups),
    }


def _deduplicate_soft_preferences(
    preferences: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Remove exact repeated soft preferences while preserving stable order."""
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for preference in preferences:
        identity = json.dumps(preference, ensure_ascii=False, sort_keys=True, default=str)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(preference)
    return unique, len(preferences) - len(unique)


def _extract_soft_preferences(
    state: TaskState,
    guide_state: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Collect soft preferences from TaskState facts and shopping guide requirements."""
    prefs: list[dict[str, Any]] = []
    for fact in state.facts:
        if fact.source.startswith("inferred:"):
            prefs.append({
                "key": fact.key,
                "value": fact.value,
                "source": fact.source,
                "certainty": fact.certainty,
            })
    if guide_state:
        requirements = guide_state.get("requirements", [])
        if isinstance(requirements, list):
            for req in requirements:
                if isinstance(req, dict) and req.get("priority") == "soft":
                    prefs.append({
                        "key": req.get("key"),
                        "operator": req.get("operator", "eq"),
                        "value": req.get("value"),
                        "source": req.get("source", ""),
                    })
        avoidances = guide_state.get("brandAvoidances", [])
        if isinstance(avoidances, list):
            for avoidance in avoidances:
                if (
                    isinstance(avoidance, dict)
                    and avoidance.get("strength") == "soft"
                ):
                    prefs.append({
                        "key": "brand",
                        "operator": "not_in",
                        "value": avoidance.get("values"),
                        "source": avoidance.get("source", ""),
                    })
    return prefs


def _collect_evidence_refs(domain_state: dict[str, Any]) -> list[str]:
    """Collect evidenceRefs from the last tool result and any persisted trace."""
    refs: list[str] = []
    last_tool = domain_state.get("lastToolResult")
    if isinstance(last_tool, dict):
        detail = last_tool.get("detail", {})
        if isinstance(detail, dict):
            for candidate in detail.get("candidates", []) or []:
                if isinstance(candidate, dict):
                    for ref in candidate.get("evidenceRefs", []) or []:
                        if isinstance(ref, str) and ref not in refs:
                            refs.append(ref)
    return refs


def shopping_guide_argument_sources(
    guide: dict[str, Any] | None,
    scope: dict[str, Any] | None = None,
    pending_rerank: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Derive the minimal, server-owned shopping-guide argument sources.

    Returns ``None`` when no valid shopping guide is present — a non-ecommerce
    task must never expose these references.  ``category`` is the deterministic
    server mapping of the validated internal code (phone/laptop/headphones) to the
    tool-schema label (手机/笔记本/耳机); the model cannot infer it.  ``requirements``
    is the validated ``ShoppingRequirement`` serialization with reproducible
    order/fields/values.  Planner and Executor compare against these exact values
    and must agree because they both derive from this same server snapshot.

    When a valid active ``CandidateScope`` AND a server-owned one-turn
    ``ScopeRerankRequest`` referencing that scope are present, the sources also
    expose the exact in-scope rerank references (``scopeId``,
    ``scopeRankedItemIds``, ``rankingIntent``, ``categoryCode``).  The Planner
    only ever copies these — it never invents them.
    """
    if not isinstance(guide, dict) or not guide:
        return None
    try:
        validated = ShoppingGuideState.model_validate(guide)
    except ValueError:
        return None
    if validated.category not in CATEGORY_LABELS:
        return None
    serialized = validated.model_dump(by_alias=True, mode="json")
    sources: dict[str, Any] = {"category": CATEGORY_LABELS[validated.category]}
    if settings.product_knowledge_enabled:
        # The server, never the model, maps the search label to the comparison code.
        sources["categoryCode"] = validated.category
    sources["requirements"] = [
        item.model_dump(mode="json")
        for item in compiled_shopping_requirements(validated)
    ]
    compared_ids = serialized.get("comparedIds")
    if (
        isinstance(compared_ids, list)
        and len(compared_ids) in {2, 3}
        and len(set(compared_ids)) == len(compared_ids)
        and all(type(item_id) is int and item_id > 0 for item_id in compared_ids)
    ):
        sources["comparedIds"] = compared_ids
        # ``search_products`` accepts the localized label while
        # ``compare_products`` accepts the internal category code.  Publish the
        # latter only for a validated comparison set so the model never has to
        # translate between the two contracts.
        sources["categoryCode"] = validated.category
    if isinstance(scope, dict) and isinstance(pending_rerank, dict):
        try:
            scope_model = CandidateScope.model_validate(scope)
            rerank_model = ScopeRerankRequest.model_validate(pending_rerank)
        except ValueError:
            return sources
        if (
            scope_model.status != "active"
            or rerank_model.scope_id != scope_model.scope_id
            or scope_model.category != validated.category
        ):
            return sources
        sources["scopeId"] = scope_model.scope_id
        sources["scopeRankedItemIds"] = list(scope_model.ranked_item_ids)
        if scope_model.source_query:
            sources["scopeSourceQuery"] = scope_model.source_query
        sources["rankingIntent"] = rerank_model.ranking_intent
        # The in-scope rerank tool consumes the internal category code, exactly
        # like ``compare_products``.
        sources["categoryCode"] = scope_model.category
    return sources


async def build_context_pack(
    state: TaskState,
    *,
    allowed_tools: list[str] | None = None,
    history: list[dict[str, Any]] | None = None,
    budget_tokens: int | None = None,
    run_id: str | None = None,
) -> ContextPack:
    """Construct a deterministic ContextPack from the current TaskState and history.

    This is the single entry point for context construction.  All lifecycle phases
    (Planner, Executor, Validator, Replanner, Critic) should receive their context
    via a ContextPack rather than re-deriving it from raw messages.
    """
    import uuid

    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"

    # Domain context comes only from the DomainSpec-bound versioned skill.  A
    # skill may return validated public fields, never raw domainState.
    domain = get_domain_for_task_type(state.task_type)
    if domain is None:
        skill = get_context_skill(GENERIC_CONTEXT_SKILL_ID, SKILL_CONTRACT_VERSION)
    else:
        skill = get_context_skill(domain.context_skill_id, domain.context_skill_version)
    authority_selection = None
    authority_domain_state = state.domain_state
    if state.task_type == "ecommerce_guide":
        authority_selection = select_shopping_state_authority(
            domain_state=state.domain_state,
            task_id=state.task_id,
            task_revision=state.revision,
            goal=state.goal,
            unknowns=state.unknowns,
            pending_questions=state.pending_questions,
            mode=settings.shopping_state_authority,
        )
        authority_domain_state = authority_selection.domain_state
    try:
        published_context = skill.publish_context(authority_domain_state)
    except ContextSkillError:
        # Invalid domain context is rejected without allowing a malformed or
        # cross-domain residual to enter the Pack.
        published_context = {}

    # --- History summaries ---
    from .context_input import is_experimental_context_input, experimental_pack_budget
    if budget_tokens is None:
        # Ordinary callers keep the legacy default. A declared isolated study
        # budget also governs this shared control Pack; explicit smaller
        # caller budgets still win. The complete model wire is checked later.
        budget_tokens = experimental_pack_budget() or DEFAULT_TOKEN_BUDGET
    if is_experimental_context_input():
        # New lanes read source-linked archive history at the explicit model
        # boundary. Do not mechanically clip it into legacy summaries first.
        history_summaries = []
        history_metrics = {"deduplicated": 0, "compressedGroups": 0}
    else:
        history_summaries, history_metrics = _prepare_history_summaries(history)

    # --- Soft preferences ---
    raw_soft_prefs = _extract_soft_preferences(
        state,
        published_context.get("shoppingGuide"),
    )
    soft_prefs, deduplicated_soft_preferences = _deduplicate_soft_preferences(
        raw_soft_prefs
    )

    # --- Evidence refs ---
    evidence_domain_state = dict(state.domain_state)
    if authority_selection is not None:
        for field in ("shoppingGuide", "candidateScope", "scopeRerankRequest"):
            evidence_domain_state.pop(field, None)
        evidence_domain_state.update(authority_domain_state)
    evidence_refs = _collect_evidence_refs(evidence_domain_state)

    # --- Allowed tools ---
    tools = allowed_tools or []

    pack = ContextPack(
        run_id=run_id,
        task_id=state.task_id,
        base_context_revision=state.revision,
        goal=(authority_selection.goal if authority_selection is not None else state.goal),
        task_type=state.task_type,
        context_policy_id=skill.policy_id,
        context_policy_version=skill.policy_version,
        context_skill_id=skill.skill_id,
        context_skill_version=skill.version,
        confirmed_facts=[
            fact for fact in state.facts if fact.certainty == "confirmed"
        ],
        hard_constraints=[
            constraint for constraint in state.constraints
            if (
                isinstance(constraint, TaskConstraint)
                or (
                    isinstance(constraint, dict)
                    and constraint.get("source", "").startswith("user")
                )
            )
        ],
        soft_preferences=soft_prefs,
        unknowns=(
            list(authority_selection.unknowns)
            if authority_selection is not None
            else state.unknowns
        ),
        pending_questions=(
            list(authority_selection.pending_questions)
            if authority_selection is not None
            else state.pending_questions
        ),
        shopping_guide_state=published_context.get("shoppingGuide"),
        candidate_scope_state=published_context.get("candidateScope"),
        scope_rerank_request=published_context.get("scopeRerankRequest"),
        shopping_state_authority_source=(
            authority_selection.source if authority_selection is not None else None
        ),
        shopping_state_degraded_reason=(
            authority_selection.degraded_reason if authority_selection is not None else None
        ),
        shopping_state_semantic_hash=(
            authority_selection.semantic_hash if authority_selection is not None else None
        ),
        history_summaries=history_summaries,
        history_repair_version="history-v1" if settings.context_history_v1_enabled else None,
        allowed_tools=tools,
        evidence_refs=evidence_refs,
    )

    # --- Token budget enforcement ---
    initial_tokens = _estimate_dict_tokens(_budget_payload(pack))
    if initial_tokens <= budget_tokens:
        if (
            history_metrics["deduplicated"]
            or history_metrics["compressedGroups"]
            or deduplicated_soft_preferences
        ):
            return pack.model_copy(update={"truncation_trace": TruncationTrace(
                budget_tokens=budget_tokens,
                initial_token_count=initial_tokens,
                final_token_count=initial_tokens,
                deduplicated_history_messages=history_metrics["deduplicated"],
                compressed_history_groups=history_metrics["compressedGroups"],
                deduplicated_soft_preferences=deduplicated_soft_preferences,
                preserved_evidence_refs=len(pack.evidence_refs),
                truncation_reason="Hybrid compression applied; no budget trimming required.",
            )}, deep=True)
        return pack

    # Hybrid compression already compacted/deduplicated history and soft prefs.
    # If the Pack is still too large, only low-priority material is trimmed.
    # Evidence refs, hard constraints and confirmed facts are never removed.
    trace = TruncationTrace(
        budget_tokens=budget_tokens,
        initial_token_count=initial_tokens,
        final_token_count=initial_tokens,
        deduplicated_history_messages=history_metrics["deduplicated"],
        compressed_history_groups=history_metrics["compressedGroups"],
        deduplicated_soft_preferences=deduplicated_soft_preferences,
        preserved_evidence_refs=len(pack.evidence_refs),
    )
    truncated = pack.model_copy(deep=True)

    # Tier 1: trim soft preferences. Confirmed facts/hard constraints remain.
    while truncated.soft_preferences and trace.final_token_count > budget_tokens:
        truncated.soft_preferences.pop()
        trace.dropped_soft_preferences += 1
        trace.final_token_count = _estimate_dict_tokens(
            _budget_payload(truncated)
        )

    # Tier 2: trim older summaries first, then oldest recent history if needed.
    while (
        truncated.history_summaries
        and trace.final_token_count > budget_tokens
    ):
        truncated.history_summaries.pop(0)
        trace.dropped_history_summaries += 1
        trace.final_token_count = _estimate_dict_tokens(
            _budget_payload(truncated)
        )

    trace.truncation_reason = (
        f"Hybrid budget {budget_tokens} tokens exceeded initial {initial_tokens}; "
        f"preserved all {trace.preserved_evidence_refs} evidence refs, "
        f"{trace.dropped_soft_preferences} soft prefs, "
        f"{trace.dropped_history_summaries} history summaries. "
        f"Final: {trace.final_token_count} tokens."
    )
    if trace.final_token_count > budget_tokens:
        raise ContextPackBudgetExceeded(
            "Protected ContextPack fields exceed the hard token budget: "
            f"{trace.final_token_count} > {budget_tokens}"
        )
    return truncated.model_copy(update={"truncation_trace": trace}, deep=True)


def context_pack_hash(pack: ContextPack) -> str:
    """Deterministic content hash for A/B comparison."""
    payload = json.dumps(
        pack.model_dump(
            by_alias=True,
            mode="json",
            exclude={"run_id", "truncation_trace", "base_context_revision", "baseContextRevision"},
        ),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return _stable_hash(payload)


def context_pack_token_count(pack: ContextPack) -> int:
    """Return the actual token count of a ContextPack (for trace recording)."""
    return _estimate_dict_tokens(
        pack.model_dump(by_alias=True, mode="json", exclude={"run_id"})
    )


def context_pack_system_message(pack: ContextPack) -> dict[str, str]:
    """Render a ContextPack as a single role=system message for the model."""
    from .context_input import phase_context
    return {
        "role": "system",
        "content": json.dumps(
            phase_context("extraction", pack.model_dump(by_alias=True, mode="json", exclude={"run_id"})),
            ensure_ascii=False,
            default=str,
        ),
    }
