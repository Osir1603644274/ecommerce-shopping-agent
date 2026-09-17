import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import time
import uuid
from typing import Any, Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context_pack import shopping_guide_argument_sources
from .domains.ecommerce import CandidateScope
from .domains.ecommerce.ranking_contract import (
    TwoStageRankingContractError,
    normalize_search_products_detail,
    normalize_scope_rerank_detail,
)
from .planning import (
    PlanStep,
    TaskPlan,
    transition_plan_status,
    transition_plan_step_status,
)
from .schemas import ToolTrace
from .settings import settings
from .task_state import TaskState, TaskStatePatchRequest, update_task_state
from .tools import call_tool
from .validation_contracts import (
    ExpectedOutputContractError,
    normalized_output_fields_for_tool,
    validate_normalized_output_values,
)

logger = logging.getLogger(__name__)


class ExecutorSelectionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ExecutorClaimError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ExecutorArgumentResolutionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ExecutorToolValidationError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ExecutorToolDispatchError(RuntimeError):
    pass


class ExecutorResultPersistenceError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ExecutorOutputExtractionError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


ToolCaller = Callable[[str, dict[str, Any]], Awaitable[ToolTrace]]
MAX_STEP_EXECUTION_RESULTS = 100
StepExecutionOutcome = Literal[
    "tool_succeeded",
    "tool_failed",
    "tool_error",
]
ExecutorBlockStage = Literal[
    "argument_resolution",
    "tool_validation",
    "output_extraction",
]
ExecutorRunOutcome = Literal[
    "step_executed",
    "step_failed",
    "step_blocked",
]


class ExecutorStepContext(BaseModel):
    """A revision-bound snapshot of the next linear PlanStep to execute.

    When executor_view_data is present (context_pack mode), the Executor
    must prefer the view-provided resolved arguments, facts, constraints,
    and prior step outputs over deriving them from raw TaskState.
    """

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    task_id: str = Field(alias="taskId", min_length=1)
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    plan_id: str = Field(alias="planId", min_length=1)
    step: PlanStep
    executor_view_data: dict[str, Any] | None = Field(
        default=None, alias="executorViewData"
    )


class ExecutorViewData(BaseModel):
    """View-provided authoritative input for one Executor step."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    resolved_arguments: dict[str, Any] = Field(default_factory=dict, alias="resolvedArguments")
    relevant_facts: list[dict[str, Any]] = Field(default_factory=list, alias="relevantFacts")
    relevant_constraints: list[dict[str, Any]] = Field(default_factory=list, alias="relevantConstraints")
    prior_step_outputs: dict[str, dict[str, Any]] = Field(default_factory=dict, alias="priorStepOutputs")


class ResolvedExecutorArguments(BaseModel):
    """Concrete arguments resolved from an accepted PlanStep's declared sources."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    task_id: str = Field(alias="taskId", min_length=1)
    plan_id: str = Field(alias="planId", min_length=1)
    step_id: str = Field(alias="stepId", min_length=1)
    resolved_arguments: dict[str, Any] = Field(alias="resolvedArguments")


class ExecutorBlockRecord(BaseModel):
    """Temporary persisted evidence explaining why one claimed step blocked."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    plan_id: str = Field(alias="planId", min_length=1)
    step_id: str = Field(alias="stepId", min_length=1)
    stage: ExecutorBlockStage = "argument_resolution"
    error_code: str = Field(alias="errorCode", min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    blocked_at: datetime = Field(alias="blockedAt")


class ValidatedExecutorToolCall(BaseModel):
    """A whitelisted, schema-validated tool call that is safe to dispatch."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    task_id: str = Field(alias="taskId", min_length=1)
    plan_id: str = Field(alias="planId", min_length=1)
    step_id: str = Field(alias="stepId", min_length=1)
    tool_name: str = Field(alias="toolName", min_length=1)
    arguments: dict[str, Any]


def executor_tool_idempotency_key(
    *,
    task_id: str,
    plan_id: str,
    step_id: str,
    state_revision: int,
    tool_name: str,
) -> str:
    """Return the server-owned identity of one logical read-only tool slot.

    This is deliberately keyed by the immutable accepted Plan step rather than
    caller text.  The executor validates the resolved arguments against that
    step before the call crosses the tool boundary.
    """
    if type(state_revision) is not int or state_revision < 1:
        raise ValueError("state_revision must be a positive integer")
    body = {
        "taskId": task_id,
        "planId": plan_id,
        "stepId": step_id,
        "stateRevision": state_revision,
        "toolName": tool_name,
    }
    return hashlib.sha256(
        json.dumps(body, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


class StepExecutionResult(BaseModel):
    """The immutable technical record of exactly one dispatched tool call."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    task_id: str = Field(alias="taskId", min_length=1)
    plan_id: str = Field(alias="planId", min_length=1)
    step_id: str = Field(alias="stepId", min_length=1)
    tool_name: str = Field(alias="toolName", min_length=1)
    resolved_arguments: dict[str, Any] = Field(alias="resolvedArguments")
    outcome: StepExecutionOutcome
    tool_trace: ToolTrace | None = Field(default=None, alias="toolTrace")
    error_type: str | None = Field(default=None, alias="errorType", min_length=1)
    error_message: str | None = Field(
        default=None,
        alias="errorMessage",
        min_length=1,
        max_length=500,
    )
    started_at: datetime = Field(alias="startedAt")
    finished_at: datetime = Field(alias="finishedAt")
    duration_ms: float = Field(alias="durationMs", ge=0)

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "StepExecutionResult":
        if self.finished_at < self.started_at:
            raise ValueError("finishedAt不能早于startedAt")
        if self.outcome == "tool_succeeded":
            if self.tool_trace is None or not self.tool_trace.ok:
                raise ValueError("tool_succeeded必须包含ok=True的ToolTrace")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("tool_succeeded不能包含异常信息")
        elif self.outcome == "tool_failed":
            if self.tool_trace is None or self.tool_trace.ok:
                raise ValueError("tool_failed必须包含ok=False的ToolTrace")
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("tool_failed不能包含异常信息")
        else:
            if self.tool_trace is not None:
                raise ValueError("tool_error不能包含ToolTrace")
            if self.error_type is None or self.error_message is None:
                raise ValueError("tool_error必须包含异常类型和消息")
        if self.tool_trace is not None and self.tool_trace.tool != self.tool_name:
            raise ValueError("ToolTrace.tool必须等于toolName")
        return self


class NormalizedStepOutput(BaseModel):
    """A small, stable output contract exposed to later PlanSteps."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    task_id: str = Field(alias="taskId", min_length=1)
    plan_id: str = Field(alias="planId", min_length=1)
    step_id: str = Field(alias="stepId", min_length=1)
    values: dict[str, Any] = Field(min_length=1)


class ExecutorRunResult(BaseModel):
    """The observable result of executing at most one claimed PlanStep."""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    outcome: ExecutorRunOutcome
    context: ExecutorStepContext
    resolved_arguments: ResolvedExecutorArguments | None = Field(
        default=None,
        alias="resolvedArguments",
    )
    tool_call: ValidatedExecutorToolCall | None = Field(
        default=None,
        alias="toolCall",
    )
    execution_result: StepExecutionResult | None = Field(
        default=None,
        alias="executionResult",
    )
    # Runner-owned inbox receipt.  It is deliberately carried only across this
    # in-memory node hand-off; the executor node projects it to TaskState with
    # OCC after the normal step result has been persisted.
    durable_tool_receipt: dict[str, Any] | None = Field(
        default=None,
        alias="durableToolReceipt",
    )
    step_output: NormalizedStepOutput | None = Field(
        default=None,
        alias="stepOutput",
    )
    error_code: str | None = Field(default=None, alias="errorCode")
    reason: str | None = None
    task_state: TaskState = Field(alias="taskState")


def select_next_plan_step(plan: TaskPlan) -> PlanStep:
    """Select the first pending step whose earlier linear steps executed."""

    if plan.status != "active":
        raise ExecutorSelectionError(
            "plan_not_active",
            f"只有active Plan可以执行，当前状态为{plan.status}",
        )

    for index, step in enumerate(plan.steps):
        if step.status == "executed":
            continue
        if step.status == "pending":
            later_steps = plan.steps[index + 1 :]
            if any(later.status != "pending" for later in later_steps):
                raise ExecutorSelectionError(
                    "invalid_step_order",
                    "线性Plan中，待执行步骤之后的步骤必须仍为pending",
                )
            return step.model_copy(deep=True)
        if step.status == "executing":
            raise ExecutorSelectionError(
                "step_already_executing",
                f"PlanStep已经处于executing，不能重复领取：{step.step_id}",
            )
        raise ExecutorSelectionError(
            "step_not_runnable",
            f"PlanStep处于{step.status}，不能跳过后继续执行：{step.step_id}",
        )

    raise ExecutorSelectionError(
        "plan_has_no_pending_step",
        "Plan中已经没有pending步骤",
    )


def build_executor_step_context(
    state: TaskState,
    executor_view: Any | None = None,
) -> ExecutorStepContext:
    """Validate the task boundary and build the next Executor input.

    When executor_view (ExecutorContextView) is provided, the view is the
    authoritative source for step identity, resolved arguments, relevant facts,
    relevant constraints, and prior step outputs.  Any mismatch between view
    and state MUST fail closed — the Executor must not silently substitute
    TaskState data when the view disagrees.
    """

    if state.status not in {"ready", "executing"}:
        raise ExecutorSelectionError(
            "task_not_executable",
            f"TaskState状态为{state.status}，不能领取PlanStep",
        )
    if state.active_plan is None:
        raise ExecutorSelectionError(
            "active_plan_missing",
            "TaskState中不存在activePlan",
        )

    step = select_next_plan_step(state.active_plan)

    # When in context_pack mode, the view is the authoritative input.
    view_data: dict[str, Any] | None = None
    if executor_view is not None:
        # ── Identity checks: fail closed on any mismatch ────────────────
        view_step_id = getattr(executor_view, "step_id", None)
        view_tool = getattr(executor_view, "tool_name", None)
        view_plan_id = getattr(executor_view, "plan_id", None)

        if view_step_id and view_step_id != step.step_id:
            raise ExecutorSelectionError(
                "view_state_step_mismatch",
                f"ExecutorView step_id={view_step_id} != state step_id={step.step_id}",
            )
        if view_tool and view_tool != step.tool_name:
            raise ExecutorSelectionError(
                "view_state_tool_mismatch",
                f"ExecutorView tool_name={view_tool} != state tool_name={step.tool_name}",
            )
        if view_plan_id and view_plan_id != state.active_plan.plan_id:
            raise ExecutorSelectionError(
                "view_state_plan_mismatch",
                f"ExecutorView plan_id={view_plan_id} != state plan_id={state.active_plan.plan_id}",
            )

        # ── Extract view-provided authoritative data ─────────────────
        resolved_args = getattr(executor_view, "resolved_arguments", None)
        task_goal = getattr(executor_view, "task_goal", None)
        rel_facts = getattr(executor_view, "relevant_facts", None)
        rel_constraints = getattr(executor_view, "relevant_constraints", None)
        view_policies = getattr(executor_view, "system_policies", None)
        prior_outputs = getattr(executor_view, "prior_step_outputs", None)

        view_data = {
            "resolved_arguments": (
                dict(resolved_args) if resolved_args else {}
            ),
            "task_goal": task_goal if isinstance(task_goal, str) else "",
            "relevant_facts": (
                list(rel_facts) if rel_facts else []
            ),
            "relevant_constraints": (
                list(rel_constraints) if rel_constraints else []
            ),
            "system_policies": (
                dict(view_policies) if view_policies else {}
            ),
            "prior_step_outputs": (
                dict(prior_outputs) if prior_outputs else {}
            ),
            "shopping_guide_sources": deepcopy(
                getattr(executor_view, "shopping_guide_sources", None) or {}
            ),
        }

    return ExecutorStepContext(
        taskId=state.task_id,
        expectedRevision=state.revision,
        planId=state.active_plan.plan_id,
        step=step,
        executorViewData=view_data,
    )


async def claim_executor_step(
    state: TaskState,
    context: ExecutorStepContext,
    *,
    durable: bool = False,
) -> TaskState:
    """Claim one selected pending step with a revision-checked TaskState patch."""

    if context.task_id != state.task_id:
        raise ExecutorClaimError(
            "task_mismatch",
            "ExecutorStepContext与TaskState不属于同一个任务",
        )
    if context.expected_revision != state.revision:
        raise ExecutorClaimError(
            "revision_mismatch",
            "ExecutorStepContext与传入TaskState的revision不一致",
        )
    if state.active_plan is None or context.plan_id != state.active_plan.plan_id:
        raise ExecutorClaimError(
            "plan_mismatch",
            "ExecutorStepContext与TaskState中的activePlan不一致",
        )

    current_step = next(
        (
            step
            for step in state.active_plan.steps
            if step.step_id == context.step.step_id
        ),
        None,
    )
    if current_step is None or current_step != context.step:
        raise ExecutorClaimError(
            "step_mismatch",
            "ExecutorStepContext中的PlanStep与当前Plan不一致",
        )

    claimed_plan = transition_plan_step_status(
        state.active_plan,
        context.step.step_id,
        "executing",
    )
    now = datetime.now(timezone.utc)
    lease_seconds = max(int(settings.agent_executor_lease_seconds), 1)
    lease = {
        "planId": state.active_plan.plan_id,
        "stepId": context.step.step_id,
        "toolName": context.step.tool_name,
        "idempotencyKey": executor_tool_idempotency_key(
            task_id=state.task_id,
            plan_id=state.active_plan.plan_id,
            step_id=context.step.step_id,
            state_revision=state.revision,
            tool_name=context.step.tool_name,
        ),
        # Before the Inbox claim exists, a crashed worker has provably not
        # crossed the tool boundary and recovery may safely put this step back
        # to pending.  Non-durable callers retain the legacy lease semantics.
        "inboxStatus": "ABSENT" if durable else "RUNNING",
        "claimToken": uuid.uuid4().hex,
        "claimedAt": now.isoformat(),
        "expiresAt": (now + timedelta(seconds=lease_seconds)).isoformat(),
    }
    patch = TaskStatePatchRequest(
        expectedRevision=context.expected_revision,
        actor="agent",
        status="executing",
        activePlan=claimed_plan,
        domainStatePatch={"executorLease": lease},
    )
    return await update_task_state(state.task_id, patch)


async def recover_expired_executor_claim(
    state: TaskState,
    *,
    durable_inbox: Any | None = None,
    force_unknown: bool = False,
) -> TaskState:
    """Fail closed when an executor lease expires without a durable result.

    An expired lease says only that this process no longer owns the slot.  It
    does *not* prove that the read-only tool was never reached: a worker can
    die after the remote call but before ``persist_step_execution_result``.
    Returning that step to ``pending`` would therefore make recovery dispatch
    the same logical call twice.  Keep the ambiguity visible as an inbox
    ``UNKNOWN`` terminal and require a fresh user turn / new Plan instead.
    """

    plan = state.active_plan
    if state.status != "executing" or plan is None:
        return state
    executing = [step for step in plan.steps if step.status == "executing"]
    if not executing:
        return state
    step = executing[0]
    lease = state.domain_state.get("executorLease")
    expires_at: datetime | None = None
    if (
        isinstance(lease, dict)
        and lease.get("planId") == plan.plan_id
        and lease.get("stepId") == step.step_id
    ):
        raw_expiry = lease.get("expiresAt")
        if isinstance(raw_expiry, str):
            try:
                expires_at = datetime.fromisoformat(raw_expiry)
            except ValueError:
                expires_at = None
    now = datetime.now(timezone.utc)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at > now and not force_unknown:
            return state

    # A TaskState lease can be durable before any Inbox record has been
    # created.  That exact crash window is safe: business work was impossible,
    # so restore the original pending step rather than manufacturing UNKNOWN.
    if isinstance(lease, dict) and lease.get("inboxStatus") == "ABSENT":
        recovered_plan = transition_plan_step_status(plan, step.step_id, "pending")
        return await update_task_state(
            state.task_id,
            TaskStatePatchRequest(
                expectedRevision=state.revision,
                actor="system",
                status="ready",
                activePlan=recovered_plan,
                domainStatePatch={
                    "executorLease": None,
                    "executorRecovery": {
                        "planId": plan.plan_id,
                        "stepId": step.step_id,
                        "status": "SAFE_RETRY",
                        "reason": "task_claim_before_inbox_claim",
                        "recoveredAt": now.isoformat(),
                    },
                },
            ),
        )

    # ``PREPARED`` has an immutable argument hash, so recovery can query the
    # runner-owned Inbox without resolving model-controlled arguments again.
    # A surviving CLAIMED record has not crossed the caller boundary and is
    # therefore safe to relinquish/rebuild; IN_FLIGHT/UNKNOWN/SUCCEEDED are
    # deliberately not retried by changing TaskState revision.
    observed = None
    if durable_inbox is not None and isinstance(lease, dict) and lease.get("inboxStatus") in {"PREPARED", "IN_FLIGHT"}:
        try:
            from .graph.tool_inbox_v2 import InboxStatus, ToolInboxSlot

            input_hash = lease.get("canonicalArgsSha256")
            state_revision = lease.get("stateRevision")
            slot = ToolInboxSlot.create(
                task_id=state.task_id,
                plan_id=plan.plan_id,
                step_id=step.step_id,
                state_revision=state_revision,
                tool_name=step.tool_name,
                canonical_args_sha256=input_hash,
            )
            observed = await durable_inbox.inspect(slot)
            safely_unreached = observed.status in {InboxStatus.ABSENT, InboxStatus.CLAIMED}
        except Exception:
            safely_unreached = False
        if safely_unreached:
            recovered_plan = transition_plan_step_status(plan, step.step_id, "pending")
            return await update_task_state(
                state.task_id,
                TaskStatePatchRequest(
                    expectedRevision=state.revision,
                    actor="system",
                    status="ready",
                    activePlan=recovered_plan,
                    domainStatePatch={
                        "executorLease": None,
                        "executorRecovery": {
                            "planId": plan.plan_id,
                            "stepId": step.step_id,
                            "status": "SAFE_RETRY",
                            "reason": "inbox_claim_before_tool_boundary",
                            "recoveredAt": now.isoformat(),
                        },
                    },
                ),
            )

        if observed is not None and observed.status is InboxStatus.SUCCEEDED:
            projected = await _recover_succeeded_durable_inbox(
                state, plan=plan, step=step, lease=lease, observed=observed
            )
            if projected is not None:
                return projected

    unknown_plan = transition_plan_step_status(plan, step.step_id, "failed")
    stale_plan = transition_plan_status(unknown_plan, "stale")
    inbox_key = lease.get("idempotencyKey") if isinstance(lease, dict) else None
    patch = TaskStatePatchRequest(
        expectedRevision=state.revision,
        actor="system",
        status="ready",
        activePlan=stale_plan,
        domainStatePatch={
            "executorLease": None,
            "executorRecovery": {
                "planId": plan.plan_id,
                "stepId": step.step_id,
                "status": "UNKNOWN",
                "idempotencyKey": inbox_key if isinstance(inbox_key, str) else None,
                "reason": "lease_expired" if expires_at is not None else "lease_missing",
                "recoveredAt": now.isoformat(),
            },
        },
    )
    return await update_task_state(state.task_id, patch)


async def _recover_succeeded_durable_inbox(
    state: TaskState,
    *,
    plan: TaskPlan,
    step: PlanStep,
    lease: dict[str, Any],
    observed: Any,
) -> TaskState | None:
    """Project an authoritative Inbox success without another tool call."""
    try:
        from .graph.tool_inbox_v2 import InboxStatus, sha256

        receipt = observed.receipt
        trace = observed.trace
        state_revision = lease.get("stateRevision")
        input_hash = lease.get("canonicalArgsSha256")
        marker = state.domain_state.get("v2RunMarker")
        if (
            observed.status is not InboxStatus.SUCCEEDED
            or not isinstance(receipt, dict)
            or trace is None
            or lease.get("inboxStatus") != "IN_FLIGHT"
            or type(state_revision) is not int
            or state_revision < 1
            or state.revision != state_revision + 3
            or not isinstance(input_hash, str)
            or sha256(step.arguments) != input_hash
            or not isinstance(marker, dict)
            or receipt.get("taskId") != state.task_id
            or receipt.get("runId") != marker.get("runId")
            or receipt.get("threadId") != marker.get("threadId")
            or receipt.get("sessionOwnerHash") != marker.get("sessionOwnerHash")
            or receipt.get("planId") != plan.plan_id
            or receipt.get("stepId") != step.step_id
            or receipt.get("toolName") != step.tool_name
            or receipt.get("stateRevision") != state_revision
            or receipt.get("inputHash") != input_hash
            or receipt.get("resultHash") != sha256(trace.model_dump(by_alias=True, mode="json"))
            or receipt.get("executionId") != observed.execution_id
            or receipt.get("fence") != observed.fence
            or receipt.get("inboxStatus") != InboxStatus.SUCCEEDED.value
            or receipt.get("toolOutcome") != (
                "tool_succeeded" if trace.ok else "tool_failed"
            )
        ):
            return None
        context = ExecutorStepContext(
            taskId=state.task_id,
            expectedRevision=state_revision,
            planId=plan.plan_id,
            step=step,
        )
        tool_call = ValidatedExecutorToolCall(
            taskId=state.task_id,
            planId=plan.plan_id,
            stepId=step.step_id,
            toolName=step.tool_name,
            arguments=deepcopy(step.arguments),
        )
        now = datetime.now(timezone.utc)
        result = StepExecutionResult(
            taskId=state.task_id,
            planId=plan.plan_id,
            stepId=step.step_id,
            toolName=step.tool_name,
            resolvedArguments=deepcopy(step.arguments),
            outcome="tool_succeeded" if trace.ok else "tool_failed",
            toolTrace=trace,
            startedAt=now,
            finishedAt=now,
            durationMs=max(float(trace.duration_ms or 0), 0),
        )
        output = (
            extract_normalized_step_output(context, result)
            if trace.ok and _step_output_is_required(plan, step.step_id)
            else None
        )
        persisted = await persist_step_execution_result(
            state,
            context,
            tool_call,
            result,
            output,
            durable_tool_receipt=receipt,
        )
        if persisted.revision != state_revision + 4:
            return None
        receipt_hash = hashlib.sha256(
            json.dumps(
                receipt, ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        return await update_task_state(
            persisted.task_id,
            TaskStatePatchRequest(
                expectedRevision=persisted.revision,
                actor="agent",
                domain_state_patch={
                    "v2ExecReceipt": receipt,
                    "v2ExecReceiptProjection": {
                        "receiptHash": receipt_hash,
                        "projectionRevision": persisted.revision + 1,
                    },
                },
            ),
        )
    except Exception:
        logger.warning("unable to project succeeded durable Inbox recovery", exc_info=True)
        return None


async def _mark_durable_inbox(
    state: TaskState,
    context: ExecutorStepContext,
    *,
    status: str,
    canonical_args_sha256: str,
) -> TaskState:
    """OCC-project Inbox progress so the crash boundary is auditable."""
    lease = state.domain_state.get("executorLease")
    if not isinstance(lease, dict) or lease.get("planId") != context.plan_id or lease.get("stepId") != context.step.step_id:
        raise ExecutorClaimError("executor_inbox_lease_mismatch", "durable Inbox lease drifted")
    if status not in {"PREPARED", "IN_FLIGHT"}:
        raise ValueError("invalid durable inbox status")
    updated = dict(lease)
    updated.update({
        "inboxStatus": status,
        "canonicalArgsSha256": canonical_args_sha256,
        "stateRevision": context.expected_revision,
    })
    return await update_task_state(
        state.task_id,
        TaskStatePatchRequest(
            expectedRevision=state.revision,
            actor="agent",
            domainStatePatch={"executorLease": updated},
        ),
    )


async def abandon_cancelled_executor_claim(
    state: TaskState,
    context: ExecutorStepContext,
) -> TaskState:
    """Close a claimed step when the enclosing request is cancelled.

    Cancellation can happen after a tool has crossed an external boundary, so
    the step is not made pending for an automatic retry.  The accepted Plan is
    instead failed and made stale, while the lease is cleared atomically.  A
    later user turn can then retire the stale receipt and build a fresh Plan
    without duplicating an ambiguously completed tool call.
    """

    plan = state.active_plan
    lease = state.domain_state.get("executorLease")
    if (
        state.status != "executing"
        or plan is None
        or plan.plan_id != context.plan_id
        or not isinstance(lease, dict)
        or lease.get("planId") != context.plan_id
        or lease.get("stepId") != context.step.step_id
    ):
        return state
    step = next(
        (item for item in plan.steps if item.step_id == context.step.step_id),
        None,
    )
    if step is None or step.status != "executing":
        return state

    failed_plan = transition_plan_step_status(plan, step.step_id, "failed")
    stale_plan = transition_plan_status(failed_plan, "stale")
    now = datetime.now(timezone.utc)
    patch = TaskStatePatchRequest(
        expectedRevision=state.revision,
        actor="system",
        status="ready",
        activePlan=stale_plan,
        domainStatePatch={
            "executorLease": None,
            "executorRecovery": {
                "planId": plan.plan_id,
                "stepId": step.step_id,
                "reason": "execution_cancelled",
                "recoveredAt": now.isoformat(),
            },
        },
    )
    return await update_task_state(state.task_id, patch)


def _task_state_argument_value(state: TaskState, reference: str) -> Any:
    scope, separator, key = reference.partition(".")
    if not separator or not key:
        raise ExecutorArgumentResolutionError(
            "invalid_task_state_reference",
            f"TaskState参数来源格式无效：{reference}",
        )
    if scope == "facts":
        fact = next((item for item in state.facts if item.key == key), None)
        if fact is None:
            raise ExecutorArgumentResolutionError(
                "argument_source_missing",
                f"TaskState中不存在fact：{key}",
            )
        if fact.certainty != "confirmed":
            raise ExecutorArgumentResolutionError(
                "argument_source_unconfirmed",
                f"Executor只能使用confirmed fact：{key}",
            )
        return fact.value
    if scope == "constraints":
        constraint = next(
            (item for item in state.constraints if item.key == key),
            None,
        )
        if constraint is None:
            raise ExecutorArgumentResolutionError(
                "argument_source_missing",
                f"TaskState中不存在constraint：{key}",
            )
        return constraint.value
    raise ExecutorArgumentResolutionError(
        "invalid_task_state_reference",
        f"不支持的TaskState参数来源：{reference}",
    )


def _claimed_step(state: TaskState, context: ExecutorStepContext) -> PlanStep:
    if state.task_id != context.task_id:
        raise ExecutorArgumentResolutionError(
            "task_mismatch",
            "ExecutorStepContext与TaskState不属于同一个任务",
        )
    if state.status != "executing":
        raise ExecutorArgumentResolutionError(
            "task_not_executing",
            f"TaskState状态为{state.status}，不能解析执行参数",
        )
    if state.active_plan is None or state.active_plan.plan_id != context.plan_id:
        raise ExecutorArgumentResolutionError(
            "plan_mismatch",
            "ExecutorStepContext与TaskState中的activePlan不一致",
        )

    current_step = next(
        (
            step
            for step in state.active_plan.steps
            if step.step_id == context.step.step_id
        ),
        None,
    )
    if current_step is None:
        raise ExecutorArgumentResolutionError(
            "step_missing",
            f"PlanStep不存在：{context.step.step_id}",
        )
    if current_step.status != "executing":
        raise ExecutorArgumentResolutionError(
            "step_not_claimed",
            f"PlanStep尚未成功领取：{current_step.step_id}",
        )
    current_contract = current_step.model_dump(exclude={"status"}, mode="python")
    selected_contract = context.step.model_dump(exclude={"status"}, mode="python")
    if current_contract != selected_contract:
        raise ExecutorArgumentResolutionError(
            "step_contract_changed",
            "领取后的PlanStep执行契约与选择时不一致",
        )
    return current_step


def _persisted_prior_step_output(
    state: TaskState,
    step_id: str,
) -> dict[str, Any] | None:
    raw_outputs = state.domain_state.get("stepOutputs", {})
    if not isinstance(raw_outputs, dict):
        raise ExecutorArgumentResolutionError(
            "invalid_prior_step_outputs",
            "TaskState.domainState.stepOutputs 必须是对象",
        )
    raw_output = raw_outputs.get(step_id)
    if raw_output is None:
        return None
    try:
        output = NormalizedStepOutput.model_validate(raw_output)
    except ValueError as exc:
        raise ExecutorArgumentResolutionError(
            "invalid_prior_step_output",
            f"前序步骤输出记录无法校验：{step_id}",
        ) from exc
    if (
        output.task_id != state.task_id
        or state.active_plan is None
        or output.plan_id != state.active_plan.plan_id
        or output.step_id != step_id
    ):
        raise ExecutorArgumentResolutionError(
            "prior_step_output_mismatch",
            f"前序步骤输出不属于当前任务和Plan：{step_id}",
        )
    referenced_step = next(
        item for item in state.active_plan.steps if item.step_id == step_id
    )
    try:
        validate_normalized_output_values(referenced_step.tool_name, output.values)
    except ExpectedOutputContractError as exc:
        raise ExecutorArgumentResolutionError(exc.code, str(exc)) from exc
    return deepcopy(output.values)


def resolve_executor_arguments(
    state: TaskState,
    context: ExecutorStepContext,
    *,
    prior_step_outputs: dict[str, dict[str, Any]] | None = None,
    system_policies: dict[str, Any] | None = None,
) -> ResolvedExecutorArguments:
    """Resolve concrete tool arguments from the claimed step's declared sources.

    When context.executor_view_data is present (context_pack mode), the view's
    pre-resolved arguments and prior step outputs take precedence over deriving
    values from raw TaskState.
    """

    step = _claimed_step(state, context)

    # ── context_pack mode: view-provided data is authoritative ─────────
    view_data = context.executor_view_data
    if view_data is not None:
        # Use view-provided prior_step_outputs if available, else fall back
        view_prior = view_data.get("prior_step_outputs", {})
        effective_prior = view_prior if view_prior else (prior_step_outputs or {})
        # View-provided resolved_arguments are a frozen expectation, not a
        # shortcut around source verification. Every argument is re-derived
        # from the bounded View below.
        view_resolved = view_data.get("resolved_arguments", {})
        available_outputs = effective_prior
    else:
        available_outputs = prior_step_outputs or {}

    policies = system_policies or {}
    resolved: dict[str, Any] = {}

    for argument_name, source in step.argument_sources.items():
        if source.kind == "task_goal":
            value = (
                view_data.get("task_goal", "")
                if view_data is not None
                else state.goal
            )
        elif source.kind == "task_state":
            if view_data is None:
                value = _task_state_argument_value(state, source.reference)
            else:
                scope, separator, key = source.reference.partition(".")
                if not separator or not key:
                    raise ExecutorArgumentResolutionError(
                        "invalid_task_state_reference",
                        f"ExecutorView参数来源格式无效：{source.reference}",
                    )
                collection_name = (
                    "relevant_facts" if scope == "facts"
                    else "relevant_constraints" if scope == "constraints"
                    else ""
                )
                if not collection_name:
                    raise ExecutorArgumentResolutionError(
                        "invalid_task_state_reference",
                        f"ExecutorView不支持参数来源：{source.reference}",
                    )
                item = next(
                    (
                        candidate
                        for candidate in view_data.get(collection_name, [])
                        if isinstance(candidate, dict)
                        and candidate.get("key") == key
                    ),
                    None,
                )
                if item is None:
                    raise ExecutorArgumentResolutionError(
                        "argument_source_missing",
                        f"ExecutorView中不存在参数来源：{source.reference}",
                    )
                if scope == "facts" and item.get("certainty") != "confirmed":
                    raise ExecutorArgumentResolutionError(
                        "argument_source_unconfirmed",
                        f"ExecutorView中的fact尚未确认：{key}",
                    )
                value = item.get("value")
        elif source.kind == "system_policy":
            effective_policies = (
                view_data.get("system_policies", {})
                if view_data is not None
                else policies
            )
            if source.reference not in effective_policies:
                raise ExecutorArgumentResolutionError(
                    "argument_source_missing",
                    f"系统策略中不存在：{source.reference}",
                )
            value = effective_policies[source.reference]
        elif source.kind == "shopping_guide":
            # Same server snapshot the Planner validated against: the view carries
            # the mapped category label + exact requirements; legacy mode derives
            # them from TaskState through the identical validator+mapper.  Only
            # the ecommerce_guide task type may resolve these references — any
            # other task type must fail closed here, even with a format-valid
            # residual shoppingGuide (not just in ContextPack/View).
            if state.task_type != "ecommerce_guide":
                sources = {}
            elif view_data is not None:
                sources = view_data.get("shopping_guide_sources", {})
            else:
                sources = (
                    shopping_guide_argument_sources(
                        state.domain_state.get("shoppingGuide"),
                        scope=state.domain_state.get("candidateScope"),
                        pending_rerank=state.domain_state.get("scopeRerankRequest"),
                    )
                    or {}
                )
            if not sources or source.reference not in sources:
                raise ExecutorArgumentResolutionError(
                    "argument_source_missing",
                    f"购物导购来源中不存在：{source.reference}",
                )
            value = sources[source.reference]
        else:
            referenced_step_id, output_field = source.reference.split(".", 1)
            referenced_step = next(
                (
                    item
                    for item in state.active_plan.steps
                    if item.step_id == referenced_step_id
                ),
                None,
            )
            if referenced_step is None or referenced_step.status != "executed":
                raise ExecutorArgumentResolutionError(
                    "prior_step_not_ready",
                    f"前序步骤尚未executed：{referenced_step_id}",
                )
            output = available_outputs.get(referenced_step_id)
            if output is None:
                output = _persisted_prior_step_output(
                    state,
                    referenced_step_id,
                )
            if output is None or output_field not in output:
                raise ExecutorArgumentResolutionError(
                    "prior_step_output_missing",
                    f"前序步骤输出不存在：{source.reference}",
                )
            value = output[output_field]

        if source.kind != "prior_step" and value != step.arguments[argument_name]:
            raise ExecutorArgumentResolutionError(
                "argument_source_changed",
                f"参数来源已变化，不能沿用旧Plan：{argument_name}",
            )
        resolved[argument_name] = deepcopy(value)

    if view_data is not None and view_resolved and resolved != view_resolved:
        raise ExecutorArgumentResolutionError(
            "view_resolved_arguments_mismatch",
            "ExecutorView中的resolvedArguments与逐项来源复核结果不一致",
        )

    return ResolvedExecutorArguments(
        taskId=state.task_id,
        planId=state.active_plan.plan_id,
        stepId=step.step_id,
        resolvedArguments=resolved,
    )


def _validate_scope_rerank_resolved(
    state: TaskState,
    resolved: dict[str, Any],
) -> None:
    """Fail closed unless the resolved rerank inputs match the live scope.

    The Planner copied ``scopeRankedItemIds`` verbatim and the Executor
    re-derived them from the same server snapshot, but neither is trusted on
    its own: the authoritative CandidateScope persisted under the current task
    is the only legal universe, in exact ranked order.  Any mismatch — extra ID,
    missing ID, reorder, wrong category, invalid intent — blocks the step.
    """
    if state.task_type != "ecommerce_guide":
        raise ExecutorArgumentResolutionError(
            "scope_rerank_wrong_task",
            "范围重排只能属于 ecommerce_guide 任务",
        )
    raw_scope = state.domain_state.get("candidateScope")
    if not isinstance(raw_scope, dict):
        raise ExecutorArgumentResolutionError(
            "scope_rerank_scope_missing",
            "范围重排必须绑定 server-owned CandidateScope",
        )
    try:
        scope = CandidateScope.model_validate(raw_scope)
    except ValueError as exc:
        raise ExecutorArgumentResolutionError(
            "scope_rerank_scope_invalid",
            "CandidateScope 不符合运行时契约",
        ) from exc
    if scope.status != "active" or scope.task_id != state.task_id:
        raise ExecutorArgumentResolutionError(
            "scope_rerank_scope_invalid",
            "范围重排引用的 scope 已失效或跨任务",
        )
    if resolved.get("scopeId") != scope.scope_id:
        raise ExecutorArgumentResolutionError(
            "scope_rerank_scope_mismatch",
            "范围重排 scopeId 与当前 CandidateScope 不一致",
        )
    if resolved.get("productIds") != list(scope.ranked_item_ids):
        raise ExecutorArgumentResolutionError(
            "scope_rerank_input_outside_scope",
            "范围重排输入必须与当前 scope 的 rankedItemIds 完全一致",
        )
    if resolved.get("category") != scope.category:
        raise ExecutorArgumentResolutionError(
            "scope_rerank_category_mismatch",
            "范围重排 category 与当前 scope 不一致",
        )
    if resolved.get("rankingIntent") not in {
        "camera_title_claim", "gaming_title_claim",
    }:
        raise ExecutorArgumentResolutionError(
            "scope_rerank_intent_invalid",
            "范围重排意图不在受控契约内",
        )


async def block_executor_step(
    state: TaskState,
    context: ExecutorStepContext,
    error: ExecutorArgumentResolutionError | ExecutorToolValidationError,
    *,
    stage: Literal["argument_resolution", "tool_validation"] = (
        "argument_resolution"
    ),
) -> TaskState:
    """Persist a pre-dispatch block and return control to the Runtime."""

    step = _claimed_step(state, context)
    blocked_plan = transition_plan_step_status(
        state.active_plan,
        step.step_id,
        "blocked",
    )
    record = ExecutorBlockRecord(
        planId=state.active_plan.plan_id,
        stepId=step.step_id,
        stage=stage,
        errorCode=error.code,
        reason=str(error)[:500],
        blockedAt=datetime.now(timezone.utc),
    )
    patch = TaskStatePatchRequest(
        expectedRevision=state.revision,
        actor="agent",
        status="ready",
        activePlan=blocked_plan,
        domainStatePatch={
            "executorBlock": record.model_dump(by_alias=True, mode="json"),
            "executorLease": None,
        },
    )
    return await update_task_state(state.task_id, patch)


def validate_executor_tool_call(
    context: ExecutorStepContext,
    resolved: ResolvedExecutorArguments,
    allowed_tool_schemas: list[dict[str, Any]],
) -> ValidatedExecutorToolCall:
    """Enforce the Executor tool allowlist and validate resolved arguments."""

    if (
        resolved.task_id != context.task_id
        or resolved.plan_id != context.plan_id
        or resolved.step_id != context.step.step_id
    ):
        raise ExecutorToolValidationError(
            "execution_context_mismatch",
            "resolvedArguments与ExecutorStepContext不属于同一次执行",
        )

    allowed_by_name: dict[str, dict[str, Any]] = {}
    for schema in allowed_tool_schemas:
        function = schema.get("function") if schema.get("type") == "function" else None
        if not isinstance(function, dict):
            raise ExecutorToolValidationError(
                "invalid_tool_schema",
                "Executor白名单中存在非法function Schema",
            )
        name = function.get("name")
        parameters = function.get("parameters")
        if not isinstance(name, str) or not name.strip() or not isinstance(parameters, dict):
            raise ExecutorToolValidationError(
                "invalid_tool_schema",
                "Executor工具Schema缺少合法name或parameters",
            )
        normalized_name = name.strip()
        if normalized_name in allowed_by_name:
            raise ExecutorToolValidationError(
                "duplicate_allowed_tool",
                f"Executor工具白名单名称重复：{normalized_name}",
            )
        allowed_by_name[normalized_name] = parameters

    tool_name = context.step.tool_name
    parameters = allowed_by_name.get(tool_name)
    if parameters is None:
        raise ExecutorToolValidationError(
            "tool_not_allowed",
            f"PlanStep工具不在Executor白名单中：{tool_name}",
        )

    strict_parameters = deepcopy(parameters)
    if strict_parameters.get("type") == "object":
        strict_parameters.setdefault("additionalProperties", False)
    try:
        Draft202012Validator.check_schema(strict_parameters)
        Draft202012Validator(strict_parameters).validate(
            resolved.resolved_arguments
        )
    except SchemaError as exc:
        raise ExecutorToolValidationError(
            "invalid_tool_schema",
            f"工具 {tool_name} 的参数Schema不合法",
        ) from exc
    except JsonSchemaValidationError as exc:
        location = ".".join(str(item) for item in exc.absolute_path)
        suffix = f"（字段：{location}）" if location else ""
        raise ExecutorToolValidationError(
            "invalid_tool_arguments",
            f"工具 {tool_name} 的resolvedArguments未通过Schema校验{suffix}",
        ) from exc

    return ValidatedExecutorToolCall(
        taskId=context.task_id,
        planId=context.plan_id,
        stepId=context.step.step_id,
        toolName=tool_name,
        arguments=deepcopy(resolved.resolved_arguments),
    )


async def execute_validated_tool_call(
    tool_call: ValidatedExecutorToolCall,
    *,
    tool_caller: ToolCaller = call_tool,
) -> ToolTrace:
    """Dispatch exactly one validated tool call and return its raw ToolTrace."""

    trace = await tool_caller(
        tool_call.tool_name,
        deepcopy(tool_call.arguments),
    )
    if not isinstance(trace, ToolTrace):
        raise ExecutorToolDispatchError("工具调用层没有返回ToolTrace")
    if trace.tool != tool_call.tool_name:
        raise ExecutorToolDispatchError(
            "ToolTrace.tool与已校验的toolName不一致"
        )
    return trace


async def execute_and_record_tool_call(
    tool_call: ValidatedExecutorToolCall,
    *,
    tool_caller: ToolCaller = call_tool,
) -> StepExecutionResult:
    """Execute one validated call and always return its technical result record."""

    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    try:
        trace = await execute_validated_tool_call(
            tool_call,
            tool_caller=tool_caller,
        )
    except Exception as exc:
        return StepExecutionResult(
            taskId=tool_call.task_id,
            planId=tool_call.plan_id,
            stepId=tool_call.step_id,
            toolName=tool_call.tool_name,
            resolvedArguments=deepcopy(tool_call.arguments),
            outcome="tool_error",
            errorType=type(exc).__name__,
            errorMessage=(str(exc) or "工具调用抛出未说明异常")[:500],
            startedAt=started_at,
            finishedAt=datetime.now(timezone.utc),
            durationMs=round((time.perf_counter() - started) * 1000, 2),
        )

    return StepExecutionResult(
        taskId=tool_call.task_id,
        planId=tool_call.plan_id,
        stepId=tool_call.step_id,
        toolName=tool_call.tool_name,
        resolvedArguments=deepcopy(tool_call.arguments),
        outcome="tool_succeeded" if trace.ok else "tool_failed",
        toolTrace=trace,
        startedAt=started_at,
        finishedAt=datetime.now(timezone.utc),
        durationMs=round((time.perf_counter() - started) * 1000, 2),
    )


def extract_normalized_step_output(
    context: ExecutorStepContext,
    result: StepExecutionResult,
) -> NormalizedStepOutput:
    """Convert a raw successful ToolTrace into a stable prior-step contract."""

    expected_identity = (
        context.task_id,
        context.plan_id,
        context.step.step_id,
        context.step.tool_name,
    )
    result_identity = (
        result.task_id,
        result.plan_id,
        result.step_id,
        result.tool_name,
    )
    if result_identity != expected_identity:
        raise ExecutorOutputExtractionError(
            "execution_result_mismatch",
            "StepExecutionResult 与本次 ExecutorStepContext 不匹配",
        )
    if result.outcome != "tool_succeeded" or result.tool_trace is None:
        raise ExecutorOutputExtractionError(
            "execution_not_successful",
            "只有技术执行成功的工具结果才能提取步骤输出",
        )
    if context.step.tool_name == "search_products":
        if context.step.expected_output.get("requiresProductCandidates") is not True:
            raise ExecutorOutputExtractionError(
                "unsupported_output_contract",
                "search_products 步骤必须声明 requiresProductCandidates=true",
            )
        try:
            ranking_output = normalize_search_products_detail(
                result.tool_trace.detail,
                requirements=result.resolved_arguments.get("requirements"),
                category=result.resolved_arguments.get("category"),
            )
        except TwoStageRankingContractError as exc:
            raise ExecutorOutputExtractionError(
                exc.code, str(exc)
            ) from exc
        return _registered_normalized_output(
            context.step.tool_name,
            taskId=context.task_id, planId=context.plan_id,
            stepId=context.step.step_id,
            values=ranking_output.normalized_values(),
        )
    if context.step.tool_name == "get_product_details":
        if context.step.expected_output.get("requiresProductDetails") is not True:
            raise ExecutorOutputExtractionError(
                "unsupported_output_contract",
                "get_product_details 步骤必须声明 requiresProductDetails=true",
            )
        detail = result.tool_trace.detail
        product_ids = detail.get("productIds") if isinstance(detail, dict) else None
        if not isinstance(product_ids, list) or not product_ids:
            raise ExecutorOutputExtractionError(
                "product_details_missing", "商品详情步骤没有返回商品"
            )
        return _registered_normalized_output(
            context.step.tool_name,
            taskId=context.task_id, planId=context.plan_id,
            stepId=context.step.step_id, values={"productIds": product_ids},
        )
    if context.step.tool_name == "compare_products":
        if context.step.expected_output.get("requiresGuideDecision") is not True:
            raise ExecutorOutputExtractionError(
                "unsupported_output_contract",
                "compare_products 步骤必须声明 requiresGuideDecision=true",
            )
        detail = result.tool_trace.detail
        products = detail.get("products") if isinstance(detail, dict) else None
        if not isinstance(products, list):
            raise ExecutorOutputExtractionError(
                "guide_decision_missing", "比较步骤没有返回决选结果"
            )
        return _registered_normalized_output(
            context.step.tool_name,
            taskId=context.task_id, planId=context.plan_id,
            stepId=context.step.step_id,
            values={"productIds": [
                item["product"]["id"] for item in products
                if isinstance(item, dict) and isinstance(item.get("product"), dict)
                and type(item["product"].get("id")) is int
            ]},
        )
    if context.step.tool_name == "rerank_products_in_scope":
        if context.step.expected_output.get("requiresScopeRerank") is not True:
            raise ExecutorOutputExtractionError(
                "unsupported_output_contract",
                "rerank_products_in_scope 步骤必须声明 requiresScopeRerank=true",
            )
        try:
            rerank_output = normalize_scope_rerank_detail(
                result.tool_trace.detail,
                requirements=result.resolved_arguments.get("requirements"),
                category=result.resolved_arguments.get("category"),
            )
        except TwoStageRankingContractError as exc:
            raise ExecutorOutputExtractionError(
                exc.code, str(exc)
            ) from exc
        return _registered_normalized_output(
            context.step.tool_name,
            taskId=context.task_id, planId=context.plan_id,
            stepId=context.step.step_id,
            values=rerank_output.normalized_values(),
        )
    if context.step.tool_name == "search_places":
        if context.step.expected_output.get("requiresPlaceCandidates") is not True:
            raise ExecutorOutputExtractionError(
                "unsupported_output_contract",
                "search_places step must declare requiresPlaceCandidates=true",
            )
        detail = result.tool_trace.detail
        items = detail.get("items") if isinstance(detail, dict) else None
        if not isinstance(items, list) or len(items) != 1:
            raise ExecutorOutputExtractionError(
                "place_ambiguous",
                "search_places must return exactly one place before a later step can use placeId",
            )
        item = items[0]
        place_id = item.get("id") if isinstance(item, dict) else None
        if not isinstance(place_id, str) or not place_id.strip():
            raise ExecutorOutputExtractionError(
                "invalid_place_id",
                "The unique place candidate is missing a valid id",
            )
        return _registered_normalized_output(
            context.step.tool_name,
            taskId=context.task_id,
            planId=context.plan_id,
            stepId=context.step.step_id,
            values={"placeId": place_id},
        )
    if context.step.tool_name != "search_shops":
        raise ExecutorOutputExtractionError(
            "unsupported_output_extractor",
            f"尚未为工具定义输出适配器：{context.step.tool_name}",
        )
    if context.step.expected_output.get("requiresShopId") is not True:
        raise ExecutorOutputExtractionError(
            "unsupported_output_contract",
            "search_shops 步骤必须显式声明 requiresShopId=true",
        )

    detail = result.tool_trace.detail
    if not isinstance(detail, dict) or not isinstance(detail.get("shops"), list):
        raise ExecutorOutputExtractionError(
            "invalid_tool_output",
            "search_shops 的 ToolTrace.detail.shops 必须是列表",
        )
    shops = detail["shops"]
    if not shops:
        raise ExecutorOutputExtractionError(
            "shop_not_found",
            "search_shops 没有返回可供下一步使用的商户",
        )
    if len(shops) != 1:
        raise ExecutorOutputExtractionError(
            "shop_ambiguous",
            "search_shops 返回多个候选，不能擅自选择 shopId",
        )
    shop = shops[0]
    shop_id = shop.get("id") if isinstance(shop, dict) else None
    if not isinstance(shop_id, int) or isinstance(shop_id, bool):
        raise ExecutorOutputExtractionError(
            "invalid_shop_id",
            "唯一商户候选缺少合法的整数 id",
        )
    return _registered_normalized_output(
        context.step.tool_name,
        taskId=context.task_id,
        planId=context.plan_id,
        stepId=context.step.step_id,
        values={"shopId": shop_id},
    )


def _registered_normalized_output(
    tool_name: str,
    **payload: Any,
) -> NormalizedStepOutput:
    """Enforce that Executor output keys match the central runtime contract."""

    output = NormalizedStepOutput(**payload)
    registered_fields = normalized_output_fields_for_tool(tool_name)
    actual_fields = frozenset(output.values)
    if not registered_fields or actual_fields != registered_fields:
        raise ExecutorOutputExtractionError(
            "normalized_output_contract_mismatch",
            f"工具 {tool_name} 的规范化输出字段与运行时契约不一致："
            f"expected={sorted(registered_fields)}, actual={sorted(actual_fields)}",
        )
    return output


async def persist_step_execution_result(
    state: TaskState,
    context: ExecutorStepContext,
    tool_call: ValidatedExecutorToolCall,
    result: StepExecutionResult,
    step_output: NormalizedStepOutput | None = None,
    output_error: ExecutorOutputExtractionError | None = None,
    durable_tool_receipt: dict[str, Any] | None = None,
) -> TaskState:
    """Persist one tool result and release the claimed step back to the Runtime."""

    try:
        step = _claimed_step(state, context)
    except ExecutorArgumentResolutionError as exc:
        raise ExecutorResultPersistenceError(exc.code, str(exc)) from exc

    expected_identity = (
        context.task_id,
        context.plan_id,
        context.step.step_id,
        context.step.tool_name,
    )
    tool_call_identity = (
        tool_call.task_id,
        tool_call.plan_id,
        tool_call.step_id,
        tool_call.tool_name,
    )
    result_identity = (
        result.task_id,
        result.plan_id,
        result.step_id,
        result.tool_name,
    )
    if tool_call_identity != expected_identity:
        raise ExecutorResultPersistenceError(
            "tool_call_context_mismatch",
            "ValidatedExecutorToolCall 与本次 ExecutorStepContext 不匹配",
        )
    if result_identity != tool_call_identity:
        raise ExecutorResultPersistenceError(
            "execution_result_mismatch",
            "StepExecutionResult 与本次工具调用不匹配",
        )
    if result.resolved_arguments != tool_call.arguments:
        raise ExecutorResultPersistenceError(
            "execution_arguments_mismatch",
            "StepExecutionResult 中的参数与已校验工具参数不一致",
        )
    if step_output is not None and output_error is not None:
        raise ExecutorResultPersistenceError(
            "conflicting_output_result",
            "规范化步骤输出与输出提取错误不能同时持久化",
        )
    if step_output is not None:
        output_identity = (
            step_output.task_id,
            step_output.plan_id,
            step_output.step_id,
        )
        if output_identity != expected_identity[:3]:
            raise ExecutorResultPersistenceError(
                "step_output_mismatch",
                "NormalizedStepOutput 与本次执行不匹配",
            )
        if result.outcome != "tool_succeeded":
            raise ExecutorResultPersistenceError(
                "step_output_for_failed_execution",
                "失败的工具执行不能持久化规范化步骤输出",
            )
    if output_error is not None and result.outcome != "tool_succeeded":
        raise ExecutorResultPersistenceError(
            "output_error_for_failed_execution",
            "只有技术执行成功的结果才可能发生输出提取错误",
        )

    if output_error is not None:
        target_status = "blocked"
    elif result.outcome == "tool_succeeded":
        target_status = "executed"
    else:
        target_status = "failed"
    updated_plan = transition_plan_step_status(
        state.active_plan,
        step.step_id,
        target_status,
    )

    raw_history = state.domain_state.get("stepExecutionResults", [])
    if not isinstance(raw_history, list):
        raise ExecutorResultPersistenceError(
            "invalid_execution_history",
            "TaskState.domainState.stepExecutionResults 必须是列表",
        )
    try:
        history = [
            StepExecutionResult.model_validate(item).model_dump(
                by_alias=True,
                mode="json",
            )
            for item in raw_history
        ]
    except ValueError as exc:
        raise ExecutorResultPersistenceError(
            "invalid_execution_history",
            "TaskState 中已有的步骤执行记录无法校验",
        ) from exc

    history.append(result.model_dump(by_alias=True, mode="json"))
    history = history[-MAX_STEP_EXECUTION_RESULTS:]
    domain_state_patch: dict[str, Any] = {
        "stepExecutionResults": history,
        "executorBlock": None,
        "executorLease": None,
    }
    lease = state.domain_state.get("executorLease")
    if not isinstance(lease, dict):
        raise ExecutorResultPersistenceError(
            "executor_inbox_missing",
            "已领取的工具步骤缺少持久化 Inbox 记录",
        )
    inbox_key = lease.get("idempotencyKey")
    if not isinstance(inbox_key, str) or len(inbox_key) != 64:
        raise ExecutorResultPersistenceError(
            "executor_inbox_invalid",
            "已领取的工具步骤 Inbox 标识无效",
        )
    expected_inbox_key = executor_tool_idempotency_key(
        task_id=context.task_id,
        plan_id=context.plan_id,
        step_id=context.step.step_id,
        state_revision=context.expected_revision,
        tool_name=context.step.tool_name,
    )
    if durable_tool_receipt is not None:
        trace = result.tool_trace
        expected_tool_outcome = (
            "tool_succeeded" if trace is not None and trace.ok
            else "tool_failed" if trace is not None
            else None
        )
        trace_hash = (
            hashlib.sha256(json.dumps(
                trace.model_dump(by_alias=True, mode="json"), ensure_ascii=True,
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")).hexdigest()
            if trace is not None else None
        )
        hex64 = lambda value: isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
        if (
            inbox_key != expected_inbox_key
            or lease.get("inboxStatus") != "IN_FLIGHT"
            or lease.get("canonicalArgsSha256") != hashlib.sha256(json.dumps(tool_call.arguments, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
            or expected_tool_outcome is None
            or result.outcome != expected_tool_outcome
            or durable_tool_receipt.get("taskId") != context.task_id
            or durable_tool_receipt.get("planId") != context.plan_id
            or durable_tool_receipt.get("stepId") != context.step.step_id
            or durable_tool_receipt.get("toolName") != context.step.tool_name
            or durable_tool_receipt.get("stateRevision") != context.expected_revision
            or durable_tool_receipt.get("inputHash") != lease.get("canonicalArgsSha256")
            or durable_tool_receipt.get("resultHash") != trace_hash
            or durable_tool_receipt.get("inboxStatus") != "SUCCEEDED"
            or durable_tool_receipt.get("toolOutcome") != expected_tool_outcome
            or not hex64(durable_tool_receipt.get("executionId"))
            or not hex64(durable_tool_receipt.get("logicalSlotKey"))
            or type(durable_tool_receipt.get("fence")) is not int
            or durable_tool_receipt.get("fence") < 1
        ):
            raise ExecutorResultPersistenceError(
                "durable_inbox_receipt_mismatch",
                "runner-owned Inbox receipt 与执行结果不一致",
            )
        # The authoritative receipt stays in Redis.  TaskState only receives
        # its immutable summary later in the executor node's v2ExecReceipt.
    else:
        if inbox_key != expected_inbox_key or lease.get("inboxStatus") != "RUNNING":
            raise ExecutorResultPersistenceError(
                "executor_inbox_mismatch",
                "已领取的工具步骤 Inbox 与执行上下文不一致",
            )
        raw_inbox = state.domain_state.get("executorToolInbox", {})
        if not isinstance(raw_inbox, dict):
            raise ExecutorResultPersistenceError(
                "executor_inbox_invalid",
                "TaskState.domainState.executorToolInbox 必须是对象",
            )
        inbox = dict(raw_inbox)
        inbox[inbox_key] = {
            "status": "COMPLETED" if result.outcome == "tool_succeeded" else "FAILED",
            "taskId": context.task_id,
            "planId": context.plan_id,
            "stepId": context.step.step_id,
            "toolName": context.step.tool_name,
            "resultDigest": hashlib.sha256(
                result.model_dump_json(by_alias=True).encode("utf-8")
            ).hexdigest(),
        }
        domain_state_patch["executorToolInbox"] = inbox
    if output_error is not None:
        block_record = ExecutorBlockRecord(
            planId=context.plan_id,
            stepId=context.step.step_id,
            stage="output_extraction",
            errorCode=output_error.code,
            reason=str(output_error)[:500],
            blockedAt=datetime.now(timezone.utc),
        )
        domain_state_patch["executorBlock"] = block_record.model_dump(
            by_alias=True,
            mode="json",
        )
    if step_output is not None or output_error is not None:
        raw_outputs = state.domain_state.get("stepOutputs", {})
        if not isinstance(raw_outputs, dict):
            raise ExecutorResultPersistenceError(
                "invalid_step_outputs",
                "TaskState.domainState.stepOutputs 必须是对象",
            )
        outputs: dict[str, Any] = {}
        try:
            for stored_step_id, raw_output in raw_outputs.items():
                stored = NormalizedStepOutput.model_validate(raw_output)
                if stored_step_id != stored.step_id:
                    raise ValueError("stepOutputs键与记录stepId不一致")
                stored_plan_step = next(
                    (
                        item for item in state.active_plan.steps
                        if item.step_id == stored_step_id
                    ),
                    None,
                )
                if stored_plan_step is None:
                    raise ValueError("stepOutputs包含当前Plan之外的步骤")
                validate_normalized_output_values(
                    stored_plan_step.tool_name, stored.values
                )
                outputs[stored_step_id] = stored.model_dump(
                    by_alias=True,
                    mode="json",
                )
        except (ValueError, ExpectedOutputContractError) as exc:
            raise ExecutorResultPersistenceError(
                "invalid_step_outputs",
                "TaskState 中已有的规范化步骤输出无法校验",
            ) from exc
        if step_output is not None:
            outputs[step_output.step_id] = step_output.model_dump(
                by_alias=True,
                mode="json",
            )
        else:
            outputs.pop(context.step.step_id, None)
        domain_state_patch["stepOutputs"] = outputs

    patch = TaskStatePatchRequest(
        expectedRevision=state.revision,
        actor="agent",
        status="ready",
        activePlan=updated_plan,
        domainStatePatch=domain_state_patch,
    )
    return await update_task_state(state.task_id, patch)


def _step_output_is_referenced(plan: TaskPlan, step_id: str) -> bool:
    """Return whether a later linear step consumes this step's output."""

    current_index = next(
        index
        for index, step in enumerate(plan.steps)
        if step.step_id == step_id
    )
    prefix = f"{step_id}."
    return any(
        source.kind == "prior_step"
        and source.reference is not None
        and source.reference.startswith(prefix)
        for later_step in plan.steps[current_index + 1 :]
        for source in later_step.argument_sources.values()
    )


def _step_output_is_required(plan: TaskPlan, step_id: str) -> bool:
    """Return whether Runtime contracts require a persisted normalized output.

    Later-step consumption is one reason to persist an output, but it is not
    the only one: Validator contracts also consume normalized outputs for
    terminal steps such as a single-step ``search_products`` plan.  Restrict
    the latter path to tools with server-registered normalized fields so raw
    ToolTrace detail is never promoted as an ad-hoc contract.
    """

    step = next(item for item in plan.steps if item.step_id == step_id)
    validator_requires_output = bool(
        step.expected_output
        and normalized_output_fields_for_tool(step.tool_name)
    )
    return validator_requires_output or _step_output_is_referenced(plan, step_id)


async def run_executor_step(
    state: TaskState,
    allowed_tool_schemas: list[dict[str, Any]],
    *,
    system_policies: dict[str, Any] | None = None,
    tool_caller: ToolCaller = call_tool,
    executor_view: Any | None = None,
    durable_tool_boundary: Any | None = None,
    durable_run_id: str | None = None,
    durable_thread_id: str | None = None,
    durable_session_owner_hash: str | None = None,
) -> ExecutorRunResult:
    """Execute and persist at most one linear PlanStep.

    When executor_view (ExecutorContextView) is provided, the Executor uses it
    as authoritative context for the current step instead of building fresh.
    """

    state = await recover_expired_executor_claim(
        state,
        durable_inbox=(
            durable_tool_boundary.inbox if durable_tool_boundary is not None else None
        ),
    )
    context = build_executor_step_context(state, executor_view=executor_view)
    claimed_state = await claim_executor_step(
        state, context, durable=durable_tool_boundary is not None,
    )

    try:
        try:
            resolved = resolve_executor_arguments(
                claimed_state,
                context,
                system_policies=system_policies,
            )
            if context.step.tool_name == "rerank_products_in_scope":
                try:
                    _validate_scope_rerank_resolved(
                        claimed_state, resolved.resolved_arguments
                    )
                except ExecutorArgumentResolutionError as exc:
                    blocked_state = await block_executor_step(
                        claimed_state,
                        context,
                        exc,
                        stage="argument_resolution",
                    )
                    return ExecutorRunResult(
                        outcome="step_blocked",
                        context=context,
                        errorCode=exc.code,
                        reason=str(exc),
                        taskState=blocked_state,
                    )
        except ExecutorArgumentResolutionError as exc:
            blocked_state = await block_executor_step(
                claimed_state,
                context,
                exc,
                stage="argument_resolution",
            )
            return ExecutorRunResult(
                outcome="step_blocked",
                context=context,
                errorCode=exc.code,
                reason=str(exc),
                taskState=blocked_state,
            )

        try:
            tool_call = validate_executor_tool_call(
                context,
                resolved,
                allowed_tool_schemas,
            )
        except ExecutorToolValidationError as exc:
            blocked_state = await block_executor_step(
                claimed_state,
                context,
                exc,
                stage="tool_validation",
            )
            return ExecutorRunResult(
                outcome="step_blocked",
                context=context,
                resolvedArguments=resolved,
                errorCode=exc.code,
                reason=str(exc),
                taskState=blocked_state,
            )

        durable_tool_receipt: dict[str, Any] | None = None
        if durable_tool_boundary is not None:
            # This is an explicit durable branch, not callable-signature
            # inspection.  The boundary constructs the canonical slot and
            # sends ToolExecutionContext to the actual V2 caller.
            if not all(isinstance(value, str) and value for value in (
                durable_run_id, durable_thread_id, durable_session_owner_hash,
            )):
                raise RuntimeError("durable V2 tool boundary missing server identity")
            from .graph.tool_inbox_v2 import ToolInboxSlot, sha256

            slot = ToolInboxSlot.create(
                task_id=context.task_id,
                plan_id=context.plan_id,
                step_id=context.step.step_id,
                state_revision=context.expected_revision,
                tool_name=tool_call.tool_name,
                canonical_args_sha256=sha256(tool_call.arguments),
            )
            claimed_state = await _mark_durable_inbox(
                claimed_state,
                context,
                status="PREPARED",
                canonical_args_sha256=slot.canonical_args_sha256,
            )

            async def _mark_in_flight(_context: Any) -> None:
                nonlocal claimed_state
                claimed_state = await _mark_durable_inbox(
                    claimed_state,
                    context,
                    status="IN_FLIGHT",
                    canonical_args_sha256=slot.canonical_args_sha256,
                )

            delivery = await durable_tool_boundary.execute(
                slot=slot,
                run_id=durable_run_id,
                thread_id=durable_thread_id,
                session_owner_hash=durable_session_owner_hash,
                tool_name=tool_call.tool_name,
                arguments=tool_call.arguments,
                on_in_flight=_mark_in_flight,
            )
            durable_tool_receipt = dict(delivery.receipt)

            async def _ledger_replay_caller(
                _name: str, _arguments: dict[str, Any],
            ) -> ToolTrace:
                return delivery.trace

            execution_result = await execute_and_record_tool_call(
                tool_call,
                tool_caller=_ledger_replay_caller,
            )
        else:
            execution_result = await execute_and_record_tool_call(
                tool_call,
                tool_caller=tool_caller,
            )
    except asyncio.CancelledError:
        try:
            await asyncio.shield(
                abandon_cancelled_executor_claim(claimed_state, context)
            )
        except Exception:
            logger.exception(
                "Failed to close cancelled Executor claim for task=%s plan=%s step=%s",
                claimed_state.task_id,
                context.plan_id,
                context.step.step_id,
            )
        raise
    step_output: NormalizedStepOutput | None = None
    output_error: ExecutorOutputExtractionError | None = None
    if (
        execution_result.outcome == "tool_succeeded"
        and claimed_state.active_plan is not None
        and _step_output_is_required(
            claimed_state.active_plan,
            context.step.step_id,
        )
    ):
        try:
            step_output = extract_normalized_step_output(
                context,
                execution_result,
            )
        except ExecutorOutputExtractionError as exc:
            output_error = exc

    updated_state = await persist_step_execution_result(
        claimed_state,
        context,
        tool_call,
        execution_result,
        step_output,
        output_error=output_error,
        durable_tool_receipt=durable_tool_receipt,
    )
    if output_error is not None:
        return ExecutorRunResult(
            outcome="step_blocked",
            context=context,
            resolvedArguments=resolved,
            toolCall=tool_call,
            executionResult=execution_result,
            durableToolReceipt=durable_tool_receipt,
            errorCode=output_error.code,
            reason=str(output_error),
            taskState=updated_state,
        )
    if execution_result.outcome != "tool_succeeded":
        return ExecutorRunResult(
            outcome="step_failed",
            context=context,
            resolvedArguments=resolved,
            toolCall=tool_call,
            executionResult=execution_result,
            durableToolReceipt=durable_tool_receipt,
            taskState=updated_state,
        )
    return ExecutorRunResult(
        outcome="step_executed",
        context=context,
        resolvedArguments=resolved,
        toolCall=tool_call,
        executionResult=execution_result,
        durableToolReceipt=durable_tool_receipt,
        stepOutput=step_output,
        taskState=updated_state,
    )
