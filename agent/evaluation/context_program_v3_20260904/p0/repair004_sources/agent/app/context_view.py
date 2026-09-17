"""ContextProjector: deterministically project a ContextPack into typed ContextViews.

Each lifecycle phase (Planner, Executor, Validator, Replanner, FinalAnswer)
receives a minimal, typed context — not the full ContextPack, and not the raw
TaskState. All Views carry two revision fields:

  - taskRevision (baseContextRevision): frozen ContextPack revision — traceability
    baseline only; NEVER used for pre-validation or boundary gating.
  - phaseTaskRevision: current TaskState.revision at projection time — this is the
    authoritative revision for boundary pre-validation.

The projector is pure (no LLM, no I/O) so the same ContextPack always produces
the same Views.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_pack import (
    ContextPack,
    PHASE_TOKEN_BUDGETS,
    _estimate_dict_tokens,
    _normalize_base_context_revision,
    validate_context_pack_contract,
)
from .context_policy import (
    CONTEXT_CONTRACT_VERSION,
    GENERIC_CONTEXT_POLICY_ID,
)
from .domains.context_skill import (
    CONTEXT_CONTRACT_VERSION as SKILL_CONTRACT_VERSION,
    GENERIC_CONTEXT_SKILL_ID,
)
from .planning import TaskPlan
from .memory.context_projection import LongTermMemoryContext
from .memory.v3_runtime import MemoryRunBinding

# ── View base ──────────────────────────────────────────────────────────────────

VIEW_SCHEMA_VERSION = "1.0"


class ContextViewBudgetExceeded(ValueError):
    """Raised when a phase projection exceeds its explicit model-input budget."""


def _enforce_view_budget(view: BaseModel, phase: str) -> None:
    budget = PHASE_TOKEN_BUDGETS[phase]
    actual = _estimate_dict_tokens(
        view.model_dump(by_alias=True, mode="json", exclude={"context_hash", "contextHash"})
    )
    if actual > budget:
        raise ContextViewBudgetExceeded(
            f"{phase} ContextView exceeds token budget: {actual} > {budget}"
        )


def _view_hash(view: BaseModel) -> str:
    """Deterministic content hash for any ContextView (excludes runId, phaseTaskRevision, baseContextRevision)."""
    payload = json.dumps(
        view.model_dump(by_alias=True, mode="json", exclude={
            "run_id", "runId",
            "phase_task_revision", "phaseTaskRevision",
            "base_context_revision", "baseContextRevision",
            "context_hash", "contextHash",
        }),
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _derive_view_hash(parent_hash: str, view_type: str, payload_hash: str) -> str:
    """Derive a view hash from the parent ContextPack hash and view content."""
    joined = "|".join([parent_hash, view_type, payload_hash])
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


# ── PlannerContextView ────────────────────────────────────────────────────────


class PlannerContextView(BaseModel):
    """Minimal context for the initial Planner phase.

    Contains user message, goal, confirmed facts, hard constraints, soft preferences,
    unknowns, pending questions, current task progress, allowed tool names,
    full candidate tool schemas, and system policies.
    Does NOT contain full tool result bodies.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    base_context_revision: int = Field(ge=1, alias="baseContextRevision")
    phase_task_revision: int = Field(ge=1, alias="phaseTaskRevision")
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _normalize_base_context_revision(data)
        return data
    context_hash: str = Field(alias="contextHash")

    goal: str
    user_message: str = Field(alias="userMessage")
    task_type: str = Field(default="generic", alias="taskType")
    task_status: str = Field(alias="taskStatus")
    confirmed_facts: list[dict[str, Any]] = Field(default_factory=list, alias="confirmedFacts")
    hard_constraints: list[dict[str, Any]] = Field(default_factory=list, alias="hardConstraints")
    soft_preferences: list[dict[str, Any]] = Field(default_factory=list, alias="softPreferences")
    unknowns: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list, alias="pendingQuestions")
    allowed_tool_names: list[str] = Field(default_factory=list, alias="allowedToolNames")
    candidate_tools: list[dict[str, Any]] = Field(default_factory=list, alias="candidateTools")
    system_policies: dict[str, Any] = Field(default_factory=dict, alias="systemPolicies")
    # Server-validated shopping-guide argument sources (category label + exact
    # requirements), None for non-ecommerce tasks. Same dict the ExecutorView carries.
    shopping_guide_sources: dict[str, Any] | None = Field(
        default=None, alias="shoppingGuideSources"
    )
    # An independently issued, low-priority channel.  It deliberately contains
    # neither memory revision/provenance nor owner/entry identifiers.
    long_term_memory: list[dict[str, str]] = Field(
        default_factory=list,
        alias="longTermMemory",
        repr=False,
        exclude_if=lambda value: value == [],
    )


# ── ExecutorContextView ────────────────────────────────────────────────────────


class ExecutorToolSpec(BaseModel):
    """Safe tool schema for the Executor phase."""

    name: str
    description: str = ""
    parameters: dict[str, Any]


class ExecutorContextView(BaseModel):
    """Minimal context for one Executor step.

    Contains the current plan step, constraints relevant to this step,
    the tool schema for this step, facts needed for this step, and
    prior step outputs required by this step.
    Does NOT contain unrelated history.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    base_context_revision: int = Field(ge=1, alias="baseContextRevision")
    phase_task_revision: int = Field(ge=1, alias="phaseTaskRevision")
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _normalize_base_context_revision(data)
        return data

    context_hash: str = Field(alias="contextHash")

    plan_id: str = Field(alias="planId")
    step_id: str = Field(alias="stepId")
    step_description: str = Field(alias="stepDescription")
    tool_name: str = Field(alias="toolName")
    tool_schema: ExecutorToolSpec | None = Field(default=None, alias="toolSchema")
    task_goal: str = Field(default="", alias="taskGoal")
    resolved_arguments: dict[str, Any] = Field(default_factory=dict, alias="resolvedArguments")
    relevant_facts: list[dict[str, Any]] = Field(default_factory=list, alias="relevantFacts")
    relevant_constraints: list[dict[str, Any]] = Field(default_factory=list, alias="relevantConstraints")
    system_policies: dict[str, Any] = Field(default_factory=dict, alias="systemPolicies")
    prior_step_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict, alias="priorStepOutputs")
    # Server-validated shopping-guide argument sources — the Executor resolver
    # re-derives category/requirements from this same dict (never from the plan).
    shopping_guide_sources: dict[str, Any] | None = Field(
        default=None, alias="shoppingGuideSources"
    )


# ── ValidatorContextView ───────────────────────────────────────────────────────


class ValidatorStepEvidence(BaseModel):
    """Evidence summary for one step in the Validator."""

    step_id: str = Field(alias="stepId")
    tool_name: str = Field(alias="toolName")
    outcome: str  # tool_succeeded | tool_failed | tool_error
    resolved_arguments: dict[str, Any] = Field(
        default_factory=dict,
        alias="resolvedArguments",
    )
    evidence_refs: list[str] = Field(default_factory=list, alias="evidenceRefs")
    detail_keys: list[str] = Field(default_factory=list, alias="detailKeys")
    evidence_values: dict[str, Any] = Field(default_factory=dict, alias="evidenceValues")
    normalized_output: dict[str, Any] = Field(
        default_factory=dict,
        alias="normalizedOutput",
    )


class ValidatorContextView(BaseModel):
    """Context for the Validator phase.

    Contains original goal, hard constraints, executed steps with evidence,
    and unmet/unknown items.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    base_context_revision: int = Field(ge=1, alias="baseContextRevision")
    phase_task_revision: int = Field(ge=1, alias="phaseTaskRevision")
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _normalize_base_context_revision(data)
        return data

    context_hash: str = Field(alias="contextHash")

    goal: str
    # Optional only for decoding old stored traces. New runtime projections
    # always populate it, and Validator rejects a missing Plan fail-closed.
    plan: TaskPlan | None = None
    hard_constraints: list[dict[str, Any]] = Field(default_factory=list, alias="hardConstraints")
    executed_steps: list[ValidatorStepEvidence] = Field(default_factory=list, alias="executedSteps")
    evidence_refs: list[str] = Field(default_factory=list, alias="evidenceRefs")
    unknowns: list[str] = Field(default_factory=list)
    unmet_constraints: list[str] = Field(default_factory=list, alias="unmetConstraints")


# ── ReplannerContextView ──────────────────────────────────────────────────────


class ReplannerContextView(BaseModel):
    """Context for the Replanner recovery phase.

    Contains the failed plan, failure reason, unsatisfied constraints,
    remaining available tools, and the current task facts.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    base_context_revision: int = Field(ge=1, alias="baseContextRevision")
    phase_task_revision: int = Field(ge=1, alias="phaseTaskRevision")
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _normalize_base_context_revision(data)
        return data

    context_hash: str = Field(alias="contextHash")

    goal: str
    failed_plan_summary: dict[str, Any] = Field(alias="failedPlanSummary")
    failed_plan: TaskPlan | None = Field(default=None, alias="failedPlan")
    failure: dict[str, Any] = Field(default_factory=dict)
    failure_reason: str = Field(alias="failureReason")
    unsatisfied_constraints: list[str] = Field(default_factory=list, alias="unsatisfiedConstraints")
    remaining_tool_names: list[str] = Field(default_factory=list, alias="remainingToolNames")
    candidate_tool_schemas: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="candidateToolSchemas",
    )
    system_policies: dict[str, Any] = Field(
        default_factory=dict,
        alias="systemPolicies",
    )
    confirmed_facts: list[dict[str, Any]] = Field(default_factory=list, alias="confirmedFacts")
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    user_message: str = Field(default="", alias="userMessage")
    reusable_step_outputs: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        alias="reusableStepOutputs",
    )
    replan_attempt: int = Field(ge=1, alias="replanAttempt")
    max_replan_attempts: int = Field(default=3, ge=1, le=5, alias="maxReplanAttempts")
    # Same shopping-guide source contract as Planner/Executor views; the Replanner
    # must reuse it verbatim, never a looser set of rules.
    shopping_guide_sources: dict[str, Any] | None = Field(
        default=None, alias="shoppingGuideSources"
    )
    long_term_memory: list[dict[str, str]] = Field(
        default_factory=list,
        alias="longTermMemory",
        repr=False,
        exclude_if=lambda value: value == [],
    )


# ── FinalAnswerContextView ─────────────────────────────────────────────────────


class FinalAnswerContextView(BaseModel):
    """Context for the FinalAnswer generation phase.

    Contains only validated results, authoritative evidence, unknowns,
    allowed facts, and answer format requirements.
    Does NOT contain full chat history.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(alias="runId")
    task_id: str = Field(alias="taskId")
    base_context_revision: int = Field(ge=1, alias="baseContextRevision")
    phase_task_revision: int = Field(ge=1, alias="phaseTaskRevision")
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

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _normalize_base_context_revision(data)
        return data

    context_hash: str = Field(alias="contextHash")

    goal: str
    validated_results: list[dict[str, Any]] = Field(default_factory=list, alias="validatedResults")
    evidence_refs: list[str] = Field(default_factory=list, alias="evidenceRefs")
    unknowns: list[str] = Field(default_factory=list)
    allowed_facts: list[dict[str, Any]] = Field(default_factory=list, alias="allowedFacts")
    answer_category: str | None = Field(default=None, alias="answerCategory")
    answer_constraints: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="answerConstraints",
    )
    answer_format: dict[str, Any] = Field(default_factory=dict, alias="answerFormat")
    long_term_memory: list[dict[str, str]] = Field(
        default_factory=list,
        alias="longTermMemory",
        repr=False,
        exclude_if=lambda value: value == [],
    )


# ── ContextProjector ──────────────────────────────────────────────────────────


class ContextProjector:
    """Deterministic projector: ContextPack → typed ContextViews.
    相同输入得到相同输出view
    不允许模型自行总结、选择字段
    不得访问数据库 redis 网络，处理内存中对象。
    Pure functions — no LLM, no I/O. Each method extracts only the fields
    needed by the target lifecycle phase.
    """

    def __init__(
        self,
        pack: ContextPack,
        *,
        long_term_memory_context: LongTermMemoryContext | MemoryRunBinding | None = None,
        long_term_memory_enabled: bool = False,
    ) -> None:
        from .context_pack import context_pack_hash

        # Own a deep snapshot so later accidental mutations of the caller's
        # nested lists cannot detach projected content from its recorded hash.
        self._pack = pack.model_copy(deep=True)
        # model_copy/model_construct bypass Pydantic validators.  Re-run the
        # complete task/domain/skill/policy and structured-payload contract
        # here, before a single phase can publish a View.
        self._skill = validate_context_pack_contract(self._pack)
        self._pack_hash: str = context_pack_hash(self._pack)
        # This seam is intentionally opt-in and process-local.  It never
        # modifies the ContextPack snapshot (or its canonical hash), TaskState,
        # checkpoint or trace.  B3a's issuer validates the value again at every
        # visible-phase access below.
        self._long_term_memory_context = (
            long_term_memory_context
            if long_term_memory_enabled
            and type(long_term_memory_context) in {LongTermMemoryContext, MemoryRunBinding}
            else None
        )

    @property
    def pack_hash(self) -> str:
        """Lazy-computed ContextPack hash."""
        return self._pack_hash

    def verifies(self, view_type: str, view: BaseModel) -> bool:
        """Verify that a View still matches its frozen Pack and payload."""
        expected = _derive_view_hash(
            self.pack_hash,
            view_type,
            _view_hash(view),
        )
        return expected == getattr(view, "context_hash", None)

    def _domain_context(self) -> dict[str, Any]:
        """Return only the four skill fields, never raw TaskState/domainState."""

        values = {
            "shoppingGuide": self._pack.shopping_guide_state,
            "candidateScope": self._pack.candidate_scope_state,
            "scopeRerankRequest": self._pack.scope_rerank_request,
        }
        return {key: value for key, value in values.items() if value is not None}

    def _phase_context(self, phase: str) -> dict[str, Any]:
        """Query the registered skill/policy for one phase's domain fields."""

        return self._skill.context_for_phase(phase, self._domain_context())

    def _shopping_sources(self, phase: str) -> dict[str, Any] | None:
        value = self._phase_context(phase).get("shoppingGuideSources")
        return value if isinstance(value, dict) else None

    def _current_requirement_keys(self, phase: str) -> set[str] | None:
        """Return current task fact/requirement keys that override memory.

        The keys are metadata only; neither user message nor preference value
        is copied into this filter.  Current-turn/task requirements win over a
        long-term value sharing the same semantic key.
        """

        # Keep the same normalization used by ShoppingTaskStateV2 for all
        # overlays.  A lazy import avoids making this optional B3b seam part of
        # the base ContextPack import graph.
        from .domains.ecommerce.shopping_task_state_v2 import canonical_requirement_key

        keys: set[str] = set()

        def collect(items: object) -> None:
            if type(items) is not list:
                return
            for item in items:
                if type(item) is not dict:
                    continue
                for field in ("semanticKey", "key"):
                    if field not in item:
                        continue
                    try:
                        keys.add(canonical_requirement_key(item[field]))
                    except ValueError:
                        # A malformed current requirement must never make a
                        # long-term value applicable by accident.
                        return None

        collect(self._pack.soft_preferences)
        collect([_serialize_fact(fact) for fact in self._pack.confirmed_facts])
        collect([_serialize_constraint(item) for item in self._pack.hard_constraints])
        sources = self._shopping_sources(phase)
        if sources is not None:
            collect(sources.get("requirements"))
        return keys

    def _long_term_memory_channel(self, phase: str) -> list[dict[str, str]]:
        """Return a fresh, validated no-ID channel for a model-visible phase.

        Invalid, forged or unavailable B3a contexts are intentionally treated
        as absent.  ContextPack/skill errors are deliberately outside this
        catch and continue to fail as before.
        """

        context = self._long_term_memory_context
        if context is None:
            return []
        if type(context) is MemoryRunBinding:
            guide = self._pack.shopping_guide_state
            if (
                type(guide) is not dict
                or guide.get("category") != context.category_id
            ):
                return []
        try:
            payload = context.payload_for_phase(phase)  # revalidates issuance
        except ValueError:
            return []
        if payload is None or type(payload) is not dict:
            return []
        preferences = payload.get("preferences")
        if type(preferences) is not list or len(preferences) > 8:
            return []
        overridden = self._current_requirement_keys(phase)
        if overridden is None:
            return []
        channel: list[dict[str, str]] = []
        schema: str | None = None
        for item in preferences:
            if type(item) is not dict:
                return []
            if set(item) == {"category", "semanticKey", "value"}:
                if schema not in {None, "v2"}:
                    return []
                schema = "v2"
                category = item["category"]
                semantic_key = item["semanticKey"]
                value = item["value"]
                if (
                    type(category) is not str
                    or type(semantic_key) is not str
                    or type(value) is not str
                    or category != "shopping_preference"
                ):
                    return []
                published = {
                    "category": category,
                    "semanticKey": semantic_key,
                    "value": value,
                }
            elif set(item) == {
                "categoryId", "preferenceKind", "attributeKey", "normalizedValue"
            }:
                if schema not in {None, "v3"}:
                    return []
                schema = "v3"
                semantic_key = item["attributeKey"]
                if any(type(item[key]) is not str for key in item):
                    return []
                if item["preferenceKind"] not in {"prefer", "avoid", "indifferent"}:
                    return []
                published = dict(item)
            else:
                return []
            try:
                from .domains.ecommerce.shopping_task_state_v2 import canonical_requirement_key
                canonical_key = canonical_requirement_key(semantic_key)
            except ValueError:
                return []
            if canonical_key not in overridden:
                channel.append(published)
        # B3a guarantees sorted/bounded canonical inputs.  Repeat both here so
        # this boundary cannot broaden if that contract is later changed.
        sort_key = (
            (lambda item: (item["category"], item["semanticKey"], item["value"]))
            if schema != "v3"
            else (lambda item: (
                item["categoryId"], item["attributeKey"],
                item["preferenceKind"], item["normalizedValue"],
            ))
        )
        if channel != sorted(channel, key=sort_key):
            return []
        encoded = json.dumps(channel, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(channel) > 8 or len(encoded) > 4096 or any(
            len(json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")) > 512
            for item in channel
        ):
            return []
        return channel

    # ── Planner ──────────────────────────────────────────────────────────────

    def planner_view(
        self,
        tool_names: list[str],
        task_status: str,
        *,
        user_message: str = "",
        candidate_tool_schemas: list[dict[str, Any]] | None = None,
        system_policies: dict[str, Any] | None = None,
        phase_task_revision: int | None = None,
    ) -> PlannerContextView:
        """Project PlannerContextView from ContextPack.

        phase_task_revision is the current TaskState.revision at projection time
        — the authoritative revision for boundary pre-validation.
        """
        view = PlannerContextView(
            runId=self._pack.run_id,
            taskId=self._pack.task_id,
            baseContextRevision=self._pack.base_context_revision,
            phaseTaskRevision=phase_task_revision if phase_task_revision is not None else self._pack.base_context_revision,
            contextPolicyId=self._pack.context_policy_id,
            contextPolicyVersion=self._pack.context_policy_version,
            contextSkillId=self._pack.context_skill_id,
            contextSkillVersion=self._pack.context_skill_version,
            contextHash="",  # filled below
            goal=self._pack.goal,
            userMessage=user_message or self._pack.goal,
            taskType=self._pack.task_type,
            taskStatus=task_status,
            confirmedFacts=[
                _serialize_fact(f) for f in self._pack.confirmed_facts
            ],
            hardConstraints=[
                _serialize_constraint(c) for c in self._pack.hard_constraints
            ],
            softPreferences=self._pack.soft_preferences,
            unknowns=self._pack.unknowns,
            pendingQuestions=self._pack.pending_questions,
            allowedToolNames=tool_names,
            candidateTools=candidate_tool_schemas or [],
            systemPolicies=system_policies or {},
            shoppingGuideSources=self._shopping_sources("planner"),
            longTermMemory=self._long_term_memory_channel("planner"),
        )
        view = view.model_copy(update={"context_hash": _derive_view_hash(self.pack_hash, "planner", _view_hash(view))})
        _enforce_view_budget(view, "planner")
        return view

    # ── Executor ─────────────────────────────────────────────────────────────

    def executor_view(
        self,
        *,
        plan_id: str,
        step_id: str,
        step_description: str,
        tool_name: str,
        tool_schema: dict[str, Any] | None = None,
        resolved_arguments: dict[str, Any] | None = None,
        required_fact_keys: list[str] | None = None,
        required_constraint_keys: list[str] | None = None,
        prior_step_outputs: dict[str, dict[str, Any]] | None = None,
        system_policies: dict[str, Any] | None = None,
        phase_task_revision: int | None = None,
    ) -> ExecutorContextView:
        """Project ExecutorContextView — one step at a time, minimal context.

        phase_task_revision is the current TaskState.revision at projection time.
        """
        # ``None`` means the caller omitted a projection filter and preserves
        # the legacy "show all" behavior.  An explicit empty list is a real
        # allowlist with zero members and must not silently widen to all state.
        fact_keys = None if required_fact_keys is None else set(required_fact_keys)
        constraint_keys = (
            None
            if required_constraint_keys is None
            else set(required_constraint_keys)
        )

        relevant_facts = [
            _serialize_fact(f)
            for f in self._pack.confirmed_facts
            if f.key in fact_keys
        ] if fact_keys is not None else [
            _serialize_fact(f) for f in self._pack.confirmed_facts
        ]

        relevant_constraints = [
            _serialize_constraint(c)
            for c in self._pack.hard_constraints
            if _constraint_key(c) in constraint_keys
        ] if constraint_keys is not None else [
            _serialize_constraint(c) for c in self._pack.hard_constraints
        ]

        tool_spec = None
        if tool_schema is not None:
            func = tool_schema.get("function", tool_schema)
            tool_spec = ExecutorToolSpec(
                name=func.get("name", tool_name),
                description=func.get("description", ""),
                parameters=func.get("parameters", {}),
            )

        view = ExecutorContextView(
            runId=self._pack.run_id,
            taskId=self._pack.task_id,
            baseContextRevision=self._pack.base_context_revision,
            phaseTaskRevision=phase_task_revision if phase_task_revision is not None else self._pack.base_context_revision,
            contextPolicyId=self._pack.context_policy_id,
            contextPolicyVersion=self._pack.context_policy_version,
            contextSkillId=self._pack.context_skill_id,
            contextSkillVersion=self._pack.context_skill_version,
            contextHash="",
            planId=plan_id,
            stepId=step_id,
            stepDescription=step_description,
            toolName=tool_name,
            toolSchema=tool_spec,
            taskGoal=self._pack.goal,
            resolvedArguments=resolved_arguments or {},
            relevantFacts=relevant_facts,
            relevantConstraints=relevant_constraints,
            systemPolicies=system_policies or {},
            priorStepOutputs=prior_step_outputs or {},
            shoppingGuideSources=self._shopping_sources("executor"),
        )
        view = view.model_copy(update={"context_hash": _derive_view_hash(self.pack_hash, "executor", _view_hash(view))})
        _enforce_view_budget(view, "executor")
        return view

    # ── Validator ────────────────────────────────────────────────────────────

    def validator_view(
        self,
        *,
        plan: TaskPlan | None = None,
        executed_steps: list[dict[str, Any]],
        unmet_constraints: list[str] | None = None,
        phase_task_revision: int | None = None,
    ) -> ValidatorContextView:
        """Project ValidatorContextView with step evidence summaries.

        phase_task_revision is the current TaskState.revision at projection time.
        """
        steps = [
            ValidatorStepEvidence(
                stepId=s.get("stepId", ""),
                toolName=s.get("toolName", ""),
                outcome=s.get("outcome", "tool_succeeded"),
                resolvedArguments=s.get("resolvedArguments", {}),
                evidenceRefs=s.get("evidenceRefs", []),
                detailKeys=s.get("detailKeys", []),
                evidenceValues=s.get("evidenceValues", {}),
                normalizedOutput=s.get("normalizedOutput", {}),
            )
            for s in executed_steps
        ]

        current_evidence_refs = list(dict.fromkeys(
            ref
            for step in steps
            for ref in step.evidence_refs
        ))
        # Validator currently has no ecommerce domain fields in its View, but
        # still queries the policy so an invalid/unknown phase contract cannot
        # silently bypass this lifecycle boundary.
        self._phase_context("validator")

        view = ValidatorContextView(
            runId=self._pack.run_id,
            taskId=self._pack.task_id,
            baseContextRevision=self._pack.base_context_revision,
            phaseTaskRevision=phase_task_revision if phase_task_revision is not None else self._pack.base_context_revision,
            contextPolicyId=self._pack.context_policy_id,
            contextPolicyVersion=self._pack.context_policy_version,
            contextSkillId=self._pack.context_skill_id,
            contextSkillVersion=self._pack.context_skill_version,
            contextHash="",
            goal=self._pack.goal,
            plan=plan.model_copy(deep=True) if plan is not None else None,
            hardConstraints=[
                _serialize_constraint(c) for c in self._pack.hard_constraints
            ],
            executedSteps=steps,
            evidenceRefs=current_evidence_refs,
            unknowns=self._pack.unknowns,
            unmetConstraints=unmet_constraints or [],
        )
        view = view.model_copy(update={"context_hash": _derive_view_hash(self.pack_hash, "validator", _view_hash(view))})
        _enforce_view_budget(view, "validator")
        return view

    # ── Replanner ────────────────────────────────────────────────────────────

    def replanner_view(
        self,
        *,
        failed_plan_summary: dict[str, Any],
        failed_plan: TaskPlan | None = None,
        failure: dict[str, Any] | None = None,
        failure_reason: str,
        unsatisfied_constraints: list[str] | None = None,
        remaining_tool_names: list[str] | None = None,
        candidate_tool_schemas: list[dict[str, Any]] | None = None,
        system_policies: dict[str, Any] | None = None,
        replan_attempt: int = 1,
        max_replan_attempts: int = 3,
        user_message: str = "",
        reusable_step_outputs: dict[str, dict[str, Any]] | None = None,
        phase_task_revision: int | None = None,
    ) -> ReplannerContextView:
        """Project ReplannerContextView for recovery.

        phase_task_revision is the current TaskState.revision at projection time.
        """
        view = ReplannerContextView(
            runId=self._pack.run_id,
            taskId=self._pack.task_id,
            baseContextRevision=self._pack.base_context_revision,
            phaseTaskRevision=phase_task_revision if phase_task_revision is not None else self._pack.base_context_revision,
            contextPolicyId=self._pack.context_policy_id,
            contextPolicyVersion=self._pack.context_policy_version,
            contextSkillId=self._pack.context_skill_id,
            contextSkillVersion=self._pack.context_skill_version,
            contextHash="",
            goal=self._pack.goal,
            failedPlanSummary=failed_plan_summary,
            failedPlan=failed_plan,
            failure=failure or {},
            failureReason=failure_reason,
            unsatisfiedConstraints=unsatisfied_constraints or [],
            remainingToolNames=remaining_tool_names or [],
            candidateToolSchemas=candidate_tool_schemas or [],
            systemPolicies=system_policies or {},
            confirmedFacts=[
                _serialize_fact(f) for f in self._pack.confirmed_facts
            ],
            constraints=[
                _serialize_constraint(c) for c in self._pack.hard_constraints
            ],
            unknowns=self._pack.unknowns,
            userMessage=user_message or self._pack.goal,
            reusableStepOutputs=reusable_step_outputs or {},
            replanAttempt=replan_attempt,
            maxReplanAttempts=max_replan_attempts,
            shoppingGuideSources=self._shopping_sources("replanner"),
            longTermMemory=self._long_term_memory_channel("replanner"),
        )
        view = view.model_copy(update={"context_hash": _derive_view_hash(self.pack_hash, "replanner", _view_hash(view))})
        _enforce_view_budget(view, "replanner")
        return view

    # ── FinalAnswer ──────────────────────────────────────────────────────────

    def final_answer_view(
        self,
        *,
        validated_results: list[dict[str, Any]],
        evidence_refs: list[str] | None = None,
        phase_task_revision: int | None = None,
    ) -> FinalAnswerContextView:
        """Project FinalAnswerContextView — only validated results and evidence.

        phase_task_revision is the current TaskState.revision at projection time.
        """
        final_context = self._phase_context("final_answer")
        final_guide = final_context.get("shoppingGuide")
        answer_format: dict[str, Any] = {
            "maxProducts": 3,
            "requireEvidenceRefs": True,
            "requireUnknownsDisclosure": bool(self._pack.unknowns),
        }
        if isinstance(final_guide, dict) and final_guide.get("mode") == "compare":
            candidate_ids = final_guide.get("candidateIds")
            compared_ids = final_guide.get("comparedIds")
            if (
                isinstance(candidate_ids, list)
                and isinstance(compared_ids, list)
                and len(compared_ids) in {2, 3}
                and len(set(compared_ids)) == len(compared_ids)
                and all(type(item) is int and item > 0 for item in candidate_ids)
                and all(type(item) is int and item in candidate_ids for item in compared_ids)
            ):
                answer_format["comparisonSelection"] = {
                    "selectedProductIds": list(compared_ids),
                    "sourceDisplayOrdinals": [
                        candidate_ids.index(item) + 1 for item in compared_ids
                    ],
                }
        view = FinalAnswerContextView(
            runId=self._pack.run_id,
            taskId=self._pack.task_id,
            baseContextRevision=self._pack.base_context_revision,
            phaseTaskRevision=phase_task_revision if phase_task_revision is not None else self._pack.base_context_revision,
            contextPolicyId=self._pack.context_policy_id,
            contextPolicyVersion=self._pack.context_policy_version,
            contextSkillId=self._pack.context_skill_id,
            contextSkillVersion=self._pack.context_skill_version,
            contextHash="",
            goal=self._pack.goal,
            validatedResults=validated_results,
            evidenceRefs=list(dict.fromkeys(evidence_refs or [])),
            unknowns=self._pack.unknowns,
            allowedFacts=[
                _serialize_fact(f) for f in self._pack.confirmed_facts
            ],
            answerCategory=(
                final_guide.get("category")
                if isinstance(final_guide, dict)
                else None
            ),
            answerConstraints=(
                list(final_guide.get("requirements", []))
                if isinstance(final_guide, dict)
                and isinstance(final_guide.get("requirements"), list)
                else []
            ),
            answerFormat=answer_format,
            longTermMemory=self._long_term_memory_channel("final_answer"),
        )
        view = view.model_copy(update={"context_hash": _derive_view_hash(self.pack_hash, "final_answer", _view_hash(view))})
        _enforce_view_budget(view, "final_answer")
        return view


# ── Serialization helpers ──────────────────────────────────────────────────────


def _serialize_fact(fact: Any) -> dict[str, Any]:
    """Serialize a TaskFact to a stable dict for views."""
    if isinstance(fact, dict):
        return {
            "key": fact.get("key", ""),
            "value": fact.get("value"),
            "certainty": fact.get("certainty", "confirmed"),
            "source": fact.get("source", ""),
        }
    return {
        "key": getattr(fact, "key", ""),
        "value": getattr(fact, "value", None),
        "certainty": getattr(fact, "certainty", "confirmed"),
        "source": getattr(fact, "source", ""),
    }


def _serialize_constraint(constraint: Any) -> dict[str, Any]:
    """Serialize a TaskConstraint to a stable dict for views."""
    if isinstance(constraint, dict):
        return {
            "key": constraint.get("key", ""),
            "operator": constraint.get("operator", ""),
            "value": constraint.get("value"),
            "source": constraint.get("source", ""),
        }
    return {
        "key": getattr(constraint, "key", ""),
        "operator": getattr(constraint, "operator", ""),
        "value": getattr(constraint, "value", None),
        "source": getattr(constraint, "source", ""),
    }


def _constraint_key(constraint: Any) -> str:
    if isinstance(constraint, dict):
        return constraint.get("key", "")
    return getattr(constraint, "key", "")
