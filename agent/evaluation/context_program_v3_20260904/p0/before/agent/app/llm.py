import asyncio
import contextvars
import hashlib
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

# Per-request observation of TaskManager / TaskState model calls.  The endpoint
# opens one span per turn; the deterministic fast paths intentionally never
# observe a call, which is exactly how the observation proves model bypass.
_llm_call_span_var: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("agent_llm_call_span", default=None)
)


def begin_agent_llm_call_span(
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    handoff_id: str | None = None,
    context_receipt_id: str | None = None,
    context_binding_hash: str | None = None,
) -> str:
    """Open a request-scoped model-call observation span (returns span id)."""
    span_id = uuid.uuid4().hex
    _llm_call_span_var.set({
        "id": span_id,
        "runId": run_id or span_id,
        "parentRunId": parent_run_id,
        "handoffId": handoff_id,
        "contextReceiptId": context_receipt_id,
        "contextBindingHash": context_binding_hash,
        "calls": {},
        "failures": {},
        "durations": {},
        "purposeCounts": {},
        "receipts": [],
    })
    return span_id


def bind_agent_llm_call_span(
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    handoff_id: str | None = None,
    context_receipt_id: str | None = None,
    context_binding_hash: str | None = None,
) -> None:
    """Bind later server-owned identities without reopening the request span."""

    span = _llm_call_span_var.get()
    if span is None:
        return
    for key, value in (
        ("runId", run_id),
        ("parentRunId", parent_run_id),
        ("handoffId", handoff_id),
        ("contextReceiptId", context_receipt_id),
        ("contextBindingHash", context_binding_hash),
    ):
        if value is not None:
            span[key] = value


def end_agent_llm_call_span() -> dict[str, Any]:
    """Close the current span and return its model-call snapshot."""
    span = _llm_call_span_var.get()
    if span is None:
        return {
            "modelCalls": {},
            "modelFailures": {},
            "llmDurationMs": {},
            "modelCallReceipts": [],
        }
    _llm_call_span_var.set(None)
    return {
        "modelCalls": dict(span["calls"]),
        "modelFailures": dict(span["failures"]),
        "llmDurationMs": {
            stage: round(duration, 3)
            for stage, duration in span["durations"].items()
        },
        "modelCallReceipts": list(span["receipts"]),
    }


def _observe_llm_call(
    stage: str,
    duration_ms: float,
    *,
    failed: bool = False,
    response: Any | None = None,
    model_call_id: str | None = None,
    parent_run_id: str | None = None,
    handoff_id: str | None = None,
    context_receipt_id: str | None = None,
    context_binding_hash: str | None = None,
    error_code: str | None = None,
    record_aggregate: bool = True,
    record_receipt: bool = True,
) -> None:
    span = _llm_call_span_var.get()
    if span is None:
        return
    if record_aggregate:
        span["calls"][stage] = span["calls"].get(stage, 0) + 1
        if failed:
            span["failures"][stage] = span["failures"].get(stage, 0) + 1
        span["durations"][stage] = span["durations"].get(stage, 0.0) + duration_ms

    if not record_receipt:
        return

    purpose = _MODEL_CALL_PURPOSE_BY_STAGE.get(stage)
    if purpose is None:
        return
    retry_ordinal = span["purposeCounts"].get(purpose, 0)
    span["purposeCounts"][purpose] = retry_ordinal + 1
    receipt = build_model_call_receipt(
        run_id=span["runId"],
        parent_run_id=(
            parent_run_id if parent_run_id is not None else span["parentRunId"]
        ),
        handoff_id=handoff_id if handoff_id is not None else span["handoffId"],
        context_receipt_id=(
            context_receipt_id
            if context_receipt_id is not None
            else span["contextReceiptId"]
        ),
        context_binding_hash=(
            context_binding_hash
            if context_binding_hash is not None
            else span["contextBindingHash"]
        ),
        call_purpose=purpose,
        agent_role=_MODEL_CALL_ROLE_BY_STAGE.get(stage),
        provider="deepseek",
        model=settings.deepseek_model,
        duration_ms=duration_ms,
        retry_ordinal=retry_ordinal,
        failed=failed,
        response=response,
        model_call_id=model_call_id,
        error_code=error_code,
    )
    span["receipts"].append(receipt_json(receipt))

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)

from .harness import (
    HarnessStepResult,
    _build_validated_evidence_refs,
    _build_validated_results,
    _build_persisted_comparison_results,
    _build_validated_scope_results,
    _record_view,
    apply_memory_rerank_to_validated_results,
    build_validated_guide_result,
    run_harness_step,
)
from .schemas import ToolTrace
from .settings import settings
from .model_call_observability import (
    CallPurpose,
    build_model_call_receipt,
    persist_model_call_receipts,
    receipt_json,
)

_MODEL_CALL_PURPOSE_BY_STAGE: dict[str, CallPurpose] = {
    "task_manager": "task_manager_relation",
    "task_state": "task_state_extraction",
    "react_decision": "shopping_policy_decision",
    "shopping_policy_decision": "shopping_policy_decision",
    "research_decision": "research_policy_decision",
    "research_synthesis": "research_synthesis",
    "final_answer": "final_answer",
    "memory_candidate": "memory_candidate_extraction",
    "contract_authoring": "contract_authoring",
}
_MODEL_CALL_ROLE_BY_STAGE = {
    "task_manager": "SYSTEM",
    "task_state": "SYSTEM",
    "react_decision": "SHOPPING_AGENT",
    "shopping_policy_decision": "SHOPPING_AGENT",
    "research_decision": "EVIDENCE_RESEARCH_AGENT",
    "research_synthesis": "EVIDENCE_RESEARCH_AGENT",
    "final_answer": "SHOPPING_AGENT",
    "memory_candidate": "SYSTEM",
    "contract_authoring": "SYSTEM",
}


async def persist_agent_llm_call_snapshot(
    snapshot: dict[str, Any],
    *,
    force: bool = False,
    evaluation_mode: bool | None = None,
) -> str:
    """Persist redacted receipts; evaluation mode is fail-closed."""

    if not force and not settings.model_call_receipts_enabled:
        snapshot["modelCallReceiptPersistenceStatus"] = "DISABLED"
        return "DISABLED"
    status = await persist_model_call_receipts(
        list(snapshot.get("modelCallReceipts") or []),
        evaluation_mode=(
            settings.model_call_receipt_evaluation_mode
            if evaluation_mode is None
            else evaluation_mode
        ),
    )
    snapshot["modelCallReceiptPersistenceStatus"] = status
    return status

from .model_compat import tool_choice_kwargs
from .agent_trace import (
    AgentRunTrace,
    RecommendationDraftTrace,
    TraceBuilder,
    TraceSummary,
)
from .context_pack import (
    ContextPack,
    DEFAULT_TOKEN_BUDGET,
    build_context_pack,
    context_pack_hash,
    context_pack_system_message,
    context_pack_token_count,
)
from .memory.v3_runtime import MemoryRunBinding
from .reference_context import ResolvedReferenceContext
from .context_view import ContextProjector, PlannerContextView, ExecutorContextView, ValidatorContextView, FinalAnswerContextView
from .transport_resolver import get_tool_transport
from .orchestrator import AgentOrchestrator
from .react_graph import ReActGraphRuntime, run_controlled_react_graph
from .validation_contracts import harness_contract_tool_names
from .domains.ecommerce import (
    BrandAvoidance,
    CandidateScope,
    DOMESTIC_PHONE_BRANDS,
    ECOMMERCE_GUIDE_TOOL_NAMES,
    ScopeRerankRequest,
    ShoppingGuideState,
    ShoppingRequirement,
    canonicalize_brand,
    compiled_shopping_requirements,
    detect_product_category,
    detect_product_brands,
    extract_order_reference,
    parse_brand_negations,
    product_brand_aliases,
    explicit_confirmation_action,
    is_ecommerce_message,
    is_order_status_query,
    is_unsafe_shopping_use,
    render_order_status_result,
    render_transaction_result,
)
from .transaction_agent.runtime import dispatch_confirmed_handoff, dispatch_order_status
from .domains.ecommerce.used_phone_attributes import USED_PHONE_ATTRIBUTE_REGISTRY
from .domains.ecommerce.shopping_state_update import (
    build_shopping_state_transition_patch,
    clear_stale_guide_references,
)
from .task_state import (
    TaskRelationDecision,
    TaskState,
    TaskStatePatchRequest,
    TaskStateRevisionConflictError,
    TaskStateTransitionError,
    get_task_state,
    update_task_state,
)
from .tools import (
    SHOPPING_REQUIREMENT_KEYS,
    SHOPPING_REQUIREMENT_UNITS,
    TOOL_SCHEMAS,
    USED_PHONE_REQUIREMENT_DESCRIPTION,
    call_tool,
    get_place_catalog,
)

# 系统提示词：告诉模型它的身份和回答风格。后续接工具时还会在这里补充规则。
SYSTEM_PROMPT = "你是“本地生活智能体”的助手，用简洁、友好的中文回答用户的本地生活相关问题。"
AnswerDeltaCallback = Callable[[str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class TransactionHandoff:
    """Typed boundary to the separate trusted transaction service."""

    action: str
    status: Literal["awaiting_trusted_transaction_agent"] = (
        "awaiting_trusted_transaction_agent"
    )

    def detail(self) -> dict[str, str]:
        return {"status": self.status, "action": self.action}


def _deterministic_smalltalk_answer(message: str) -> str | None:
    """Answer unambiguous greetings without spending a model round trip."""

    normalized = re.sub(r"[\s！!。,.，？?～~]+", "", message).casefold()
    if normalized not in {"你好", "你好呀", "您好", "嗨", "哈喽", "hello", "hi"}:
        return None
    return "你好呀！你想了解哪类商品？我可以帮你检索和比较手机、笔记本或耳机。"
TaskStateCallback = Callable[[TaskState, str], Awaitable[None]]
TOOL_PLANNING_PROMPT = (
    "当前仅进行工具规划，不要向用户撰写最终回答。"
    "如果还需要数据，只返回完整的工具调用和完整JSON参数；"
    "如果不需要工具或现有工具结果已经足够，只回复 READY_TO_ANSWER。"
)
TASK_STATE_TOOL_NAME = "update_task_state"
TASK_RELATION_TOOL_NAME = "route_session_task"
TASK_RELATION_PLANNING_PROMPT = (
    "你是会话TaskManager，只判断本轮消息应交给哪个任务，不回答用户问题。"
    "普通补充、追问和代词承接都应continue_current；"
    "明确提出独立目标时start_new，但这只暂停旧任务，不代表放弃；"
    "明确回到最近暂停任务时resume_previous，并原样填写其taskId；"
    "只有明确取消当前任务且没有替代目标时cancel_current；"
    "无法可靠判断时ambiguous并给出一句澄清问题。"
)
DEFERRED_TASK_RETENTION_REASON = (
    "用户要求保留历史导购任务供以后恢复，当前不切换任务"
)
TASK_STATE_PLANNING_PROMPT = (
    "你必须先维护当前 TaskState，再规划任何业务工具。"
    "只调用 update_task_state 一次，不要在这一轮调用其他工具，也不要回答用户。"
    "从本轮用户原话中提取目标、事实和约束；用户明确提供的内容source=user、certainty=confirmed。"
    "缺少且会阻止当前任务继续的条件写入unknowns，并把一句最必要的追问写入pendingQuestions；"
    "不要把非必要信息列为unknown。信息足够时将collecting_information改为ready。"
    "只有用户明确表示结束或取消任务时才设置completed或cancelled。"
)
TASK_STATE_PLANNING_PROMPT += (
    "\n当 taskType=ecommerce_guide 时，把导购状态写入 "
    "domainStatePatch.shoppingGuide：mode 使用 recommend/compare，category 使用 phone/laptop/headphones，"
    "requirements 每项必须包含 key/operator/value/unit/priority/source。"
    "只有用户明确原话可标 priority=hard；推断项必须标 soft 且 source 以 inferred: 开头。"
    "只有缺失信息确实使当前检索或比较无法执行时，才保持 collecting_information。"
    "当用户条件能够映射到当前工具支持的字段时，预算、品牌、型号或用途不应被机械地视为必填；"
    "但缺少比较对象身份、目标品类、必要安全边界，或条件无法映射到证据档位时仍须追问。"
    "二手手机可执行受控字段仅限：os、battery_health、screen_originality、"
    "motherboard_repair、battery_originality、scratch_level、shell_condition；"
    "值域必须服从工具 schema，unit 都是 enum。eq 接受单个枚举值，"
    "in/not_in 接受非空且无重复的枚举值数组；不得从标题、品牌、卖家、"
    "子串或孤立百分比伪造这些事实。"
    "用户明确说不要/排除某个取值时必须保留否定语义，使用 not_in + 取值数组；"
    "不要把它改写成另一个值的 eq，即使当前枚举看起来只有两个值。"
    "用户说“A或B都可以/都行/均可”表示不限制这两个选项，不得把 A、B 分别写成"
    "同时成立的 eq；例如“苹果或者安卓都可以”不得生成 brand=apple 与 os=android。"
    "当已有可执行 shoppingGuide 时，预算、品牌、具体型号、用途等非阻塞偏好不要写入 addUnknowns/pendingQuestions；"
    "如确有助于后续个性化，可写入 optionalShoppingQuestions，且每项只写 kind，"
    "kind 只能是 budget/brand/model/use_case，问题文本由服务端生成，"
    "并把任务设为 ready。比较对象身份、目标品类、安全边界和无法映射的证据条件绝不能写入该可选字段。"
    "shoppingGuide 尚无 requirements 时可用 requirements 提交初始完整集合；"
    "已有 requirements 的后续轮只能用 upsertRequirements/removeRequirementKeys 做增量更新，"
    "不得重写完整 requirements。电商受控约束以 shoppingGuide 为真源，"
    "通用 upsertConstraints/removeConstraintKeys 由服务端从它投影，不要重复提交。"
)
TASK_STATE_PLANNING_PROMPT += (
    "\n提交的 update_task_state 状态形状必须二选一，二者互斥，不得混用："
    "\n- 可执行形状：status=ready，且 addUnknowns 必须为空、pendingQuestions 必须为 []；"
    "非阻塞的预算/品牌/型号/用途偏好只能写入 optionalShoppingQuestions"
    "（每项只写 kind，问题文本由服务端生成），绝不能写入 addUnknowns/pendingQuestions。"
    "\n- 必须澄清形状：status=collecting_information，且同时存在真正的阻塞 addUnknowns"
    "与一句 pendingQuestions；此时不得把任务伪装成 ready，也不得把真正阻塞的条件静默降级为 optional。"
    "同一 payload 若同时出现 ready/executing 与阻塞 unknown 或 pending question，会被服务端拒绝。"
    "每次 update_task_state 调用必须显式声明 status（collecting_information/ready/executing/"
    "paused/completed/cancelled 之一）；不得省略 status，服务端不会自动把省略的 status 置为 ready。"
)
TASK_STATE_REPAIR_PROMPT = (
    "你的上一条 update_task_state 调用未通过服务端纯校验，且尚未写入任何状态（persist=0）。"
    "请只调用 update_task_state 一次提交修正后的合法 payload；不要调用其他工具，不要回答用户。"
    "工具返回的 JSON 中包含精确原因：code、field_path、message 与 persist=0。"
    "function.arguments 直接包含业务字段，不得再套 arguments/name/function/tools 外壳。"
    "若输出截断，只提交本轮必要的最小更新；不重复未变化事实，不添加解释文字。"
    "状态形状必须二选一且互斥："
    "可执行形状=status=ready 且 addUnknowns 为空、pendingQuestions=[]，非阻塞偏好只进 optionalShoppingQuestions；"
    "澄清形状=status=collecting_information 且同时存在真正的阻塞 addUnknowns 与一句 pendingQuestions。"
    "若错误列出多个 path，必须在这唯一一次修复中同时关闭全部错误："
    "删除 evidenceStatus/candidateIds/comparedIds/useCases 等服务端拥有的 shoppingGuide 字段；"
    "所有中文必须提交正常 Unicode，不得提交 GBK-over-Latin-1 乱码；"
    "已有 category 和可执行 requirements 的 recommend 任务中，预算、品牌、型号、用途不得继续阻塞。"
    "修正后的 payload 必须显式声明 status（collecting_information/ready/executing/"
    "paused/completed/cancelled 之一）；不得省略 status，服务端不会自动补 status 为 ready。"
    "不要删除用户明确提供的 hard 事实，不要为通过校验而把真正阻塞的问题降级为 optional，也不要伪造证据。"
    "若已有 shoppingGuide.requirements，删除完整 requirements，改用 "
    "upsertRequirements/removeRequirementKeys；不要同时 upsert 和 remove 同一个 key。"
)

_MODEL_TASK_STATE_WRITABLE_KEYS = frozenset({
    "status",
    "goal",
    "upsertFacts",
    "removeFactKeys",
    "upsertConstraints",
    "removeConstraintKeys",
    "addUnknowns",
    "resolveUnknowns",
    "pendingQuestions",
    "optionalShoppingQuestions",
    "domainStatePatch",
})
_MODEL_TASK_STATE_REQUIRED_KEYS = frozenset({"status"})
_MODEL_SHOPPING_GUIDE_WRITABLE_KEYS = frozenset({
    "mode",
    "category",
    "requirements",
    "upsertRequirements",
    "removeRequirementKeys",
})
_MODEL_SHOPPING_GUIDE_STATE_FIELDS = frozenset({
    "mode",
    "category",
    "requirements",
})
_SERVER_SHOPPING_GUIDE_OWNED_KEYS = frozenset({
    "brandAvoidances",
    "useCases",
    "candidateIds",
    "comparedIds",
    "evidenceStatus",
})
_MODEL_TASK_STATE_ITEM_CONTRACTS = {
    "upsertFacts": (
        frozenset({"key", "value", "certainty", "source"}),
        frozenset({"key", "value", "source"}),
    ),
    "upsertConstraints": (
        frozenset({"key", "operator", "value", "source"}),
        frozenset({"key", "operator", "value", "source"}),
    ),
}

TASK_STATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TASK_STATE_TOOL_NAME,
        "description": "根据当前用户消息提交对既有TaskState的局部更新；revision和actor由服务端填写。",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": [
                        "collecting_information",
                        "ready",
                        "executing",
                        "paused",
                        "completed",
                        "cancelled",
                    ],
                },
                "goal": {"type": "string"},
                "upsertFacts": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["key", "value", "source"],
                        "properties": {
                            "key": {"type": "string"},
                            "value": {},
                            "certainty": {
                                "type": "string",
                                "enum": ["confirmed", "predicted", "inferred"],
                            },
                            "source": {
                                "type": "string",
                                "enum": ["user", "agent", "tool", "system"],
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "removeFactKeys": {"type": "array", "items": {"type": "string"}},
                "upsertConstraints": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["key", "operator", "value", "source"],
                        "properties": {
                            "key": {"type": "string"},
                            "operator": {
                                "type": "string",
                                "enum": ["eq", "lte", "gte", "in", "not_in"],
                            },
                            "value": {},
                            "source": {
                                "type": "string",
                                "enum": ["user", "agent", "tool", "system"],
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "removeConstraintKeys": {"type": "array", "items": {"type": "string"}},
                "addUnknowns": {"type": "array", "items": {"type": "string"}},
                "resolveUnknowns": {"type": "array", "items": {"type": "string"}},
                "pendingQuestions": {"type": "array", "items": {"type": "string"}},
                "optionalShoppingQuestions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["kind"],
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["budget", "brand", "model", "use_case"],
                            },
                        },
                        "additionalProperties": False,
                    },
                },
                "domainStatePatch": {
                    "type": "object",
                    "properties": {
                        "shoppingGuide": {
                            "type": "object",
                            "properties": {
                                "mode": {
                                    "type": "string",
                                    "enum": ["recommend", "compare"],
                                },
                                "category": {
                                    "type": "string",
                                    "enum": ["phone", "laptop", "headphones"],
                                },
                                "requirements": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": [
                                            "key",
                                            "operator",
                                            "value",
                                            "unit",
                                            "priority",
                                            "source",
                                        ],
                                        "properties": {
                                            "key": {
                                                "type": "string",
                                                "enum": SHOPPING_REQUIREMENT_KEYS,
                                                "description": (
                                                    "Supported fact keys. Controlled "
                                                    "used-phone values: "
                                                    + USED_PHONE_REQUIREMENT_DESCRIPTION
                                                ),
                                            },
                                            "operator": {
                                                "type": "string",
                                                "enum": ["eq", "lte", "gte", "in", "not_in"],
                                            },
                                            "value": {},
                                            "unit": {
                                                "type": "string",
                                                "enum": SHOPPING_REQUIREMENT_UNITS,
                                            },
                                            "priority": {
                                                "type": "string",
                                                "enum": ["hard", "soft"],
                                            },
                                            "source": {"type": "string"},
                                        },
                                        "additionalProperties": False,
                                    },
                                },
                                "upsertRequirements": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": [
                                            "key", "operator", "value", "unit",
                                            "priority", "source",
                                        ],
                                        "properties": {
                                            "key": {
                                                "type": "string",
                                                "enum": SHOPPING_REQUIREMENT_KEYS,
                                            },
                                            "operator": {
                                                "type": "string",
                                                "enum": ["eq", "lte", "gte", "in", "not_in"],
                                            },
                                            "value": {},
                                            "unit": {
                                                "type": "string",
                                                "enum": SHOPPING_REQUIREMENT_UNITS,
                                            },
                                            "priority": {
                                                "type": "string",
                                                "enum": ["hard", "soft"],
                                            },
                                            "source": {"type": "string"},
                                        },
                                        "additionalProperties": False,
                                    },
                                },
                                "removeRequirementKeys": {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "enum": SHOPPING_REQUIREMENT_KEYS,
                                    },
                                },
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "required": ["status"],
            "additionalProperties": False,
        },
    },
}


def _assert_model_task_state_schema_contract() -> None:
    parameters = TASK_STATE_TOOL_SCHEMA["function"]["parameters"]
    schema_keys = frozenset(parameters["properties"])
    if schema_keys != _MODEL_TASK_STATE_WRITABLE_KEYS:
        raise RuntimeError("update_task_state schema/runtime top-level allowlists differ")
    if frozenset(parameters.get("required", [])) != _MODEL_TASK_STATE_REQUIRED_KEYS:
        raise RuntimeError("update_task_state schema/runtime required keys differ")
    guide_schema = parameters["properties"]["domainStatePatch"]["properties"][
        "shoppingGuide"
    ]
    if frozenset(guide_schema["properties"]) != _MODEL_SHOPPING_GUIDE_WRITABLE_KEYS:
        raise RuntimeError("shoppingGuide schema/runtime allowlists differ")
    state_keys = frozenset(
        field.alias or name
        for name, field in ShoppingGuideState.model_fields.items()
    )
    if (
        _MODEL_SHOPPING_GUIDE_STATE_FIELDS
        | _SERVER_SHOPPING_GUIDE_OWNED_KEYS
    ) != state_keys:
        raise RuntimeError("ShoppingGuideState fields need an explicit owner")
    for field_name, (runtime_keys, runtime_required) in (
        _MODEL_TASK_STATE_ITEM_CONTRACTS.items()
    ):
        item_schema = parameters["properties"][field_name]["items"]
        if frozenset(item_schema["properties"]) != runtime_keys:
            raise RuntimeError(
                f"{field_name} schema/runtime item allowlists differ"
            )
        if frozenset(item_schema["required"]) != runtime_required:
            raise RuntimeError(
                f"{field_name} schema/runtime required keys differ"
            )


_assert_model_task_state_schema_contract()

TASK_RELATION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": TASK_RELATION_TOOL_NAME,
        "description": "判断用户消息与当前及最近TaskState之间的关系。",
        "parameters": {
            "type": "object",
            "required": ["relation", "reason", "confidence"],
            "properties": {
                "relation": {
                    "type": "string",
                    "enum": [
                        "continue_current",
                        "start_new",
                        "resume_previous",
                        "cancel_current",
                        "ambiguous",
                    ],
                },
                "targetTaskId": {"type": "string"},
                "reason": {"type": "string"},
                "clarificationQuestion": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "additionalProperties": False,
        },
    },
}


def get_client() -> AsyncOpenAI:
    # DeepSeek 兼容 OpenAI 接口，所以直接用 openai 的客户端，只把 base_url 指向 DeepSeek。
    return AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        timeout=30.0,
        max_retries=2,
    )


def _task_relation_summary(state: TaskState) -> dict[str, Any]:
    return {
        "taskId": state.task_id,
        "status": state.status,
        "goal": state.goal,
        "facts": [
            {"key": fact.key, "value": fact.value}
            for fact in state.facts
        ],
        "pendingQuestions": state.pending_questions,
        "updatedAt": state.updated_at.isoformat(),
    }


async def classify_task_relation(
    message: str,
    active_task: TaskState,
    recent_tasks: list[TaskState],
) -> TaskRelationDecision:
    """Ask the model for a bounded proposal; invalid output safely continues."""
    if active_task.task_type == "ecommerce_guide":
        shopping_state = active_task.domain_state.get("shoppingGuide")
        current_category = (
            shopping_state.get("category")
            if isinstance(shopping_state, dict)
            else None
        )
        requested_category = detect_product_category(message)
        compact_message = re.sub(r"\s+", "", message).strip("。！!？?")
        if re.fullmatch(
            r"(?:算了|不买了|不要了|取消(?:当前)?(?:任务|导购)?|"
            r"停止(?:当前)?(?:任务|导购)?|结束(?:当前)?(?:任务|导购)?)",
            compact_message,
        ):
            return TaskRelationDecision(
                relation="cancel_current",
                reason="用户使用了无歧义的当前导购取消表达",
                confidence=1.0,
            )
        # Mentioning a paused category is not itself a request to resume it.
        # For example, "手机任务先保留，我说继续手机时再回来" explicitly
        # defers the switch.  Require both the defer wording and a real paused
        # task for that category before taking this deterministic no-switch path.
        deferred_switch = (
            requested_category is not None
            and requested_category != current_category
            and "先保留" in compact_message
            and "继续" in compact_message
            and any(cue in compact_message for cue in ("再回来", "再恢复", "再继续"))
        )
        paused_requested_task = next(
            (
                state
                for state in recent_tasks
                if state.task_id != active_task.task_id
                and state.status == "paused"
                and isinstance(state.domain_state.get("shoppingGuide"), dict)
                and state.domain_state["shoppingGuide"].get("category")
                == requested_category
            ),
            None,
        )
        if deferred_switch and paused_requested_task is not None:
            return TaskRelationDecision(
                relation="continue_current",
                targetTaskId=active_task.task_id,
                reason=DEFERRED_TASK_RETENTION_REASON,
                confidence=1.0,
            )
        if (
            current_category is not None
            and requested_category is not None
            and requested_category != current_category
        ):
            return TaskRelationDecision(
                relation="start_new",
                reason="用户明确切换了商品品类，创建独立导购任务以隔离约束",
                confidence=1.0,
            )
        explicit_new_task = re.search(
            r"(?:新任务|另一个任务|另外一个任务|重新开始)", compact_message
        )
        current_scope_followup = any(
            cue in compact_message
            for cue in (
                *_INVALIDATED_SCOPE_REFERENCE_CUES,
                "根据已有属性",
                "根据已知属性",
                "根据你确实知道的属性",
                "根据确实知道的属性",
                "各自适合谁",
                "各自有什么特点",
                "怎么选",
                "第一个",
                "第一款",
                "第二个",
                "第二款",
                "第三个",
                "第三款",
                "这两个",
                "这两款",
                "这三个",
                "这三款",
            )
        )
        if (
            current_category == "phone"
            and requested_category is None
            and explicit_new_task is None
            and current_scope_followup
        ):
            return TaskRelationDecision(
                relation="continue_current",
                reason="用户明确引用当前导购候选或已验证属性",
                confidence=1.0,
            )
        if (
            current_category is not None
            and requested_category == current_category
            and explicit_new_task is None
        ):
            return TaskRelationDecision(
                relation="continue_current",
                reason="用户明确提到当前导购的同一商品品类",
                confidence=1.0,
            )
        explicit_patch = re.search(
            r"(?:其他要求不变|其余要求不变|降为偏好|改为偏好|"
            r"改成硬条件|作为硬条件|预算(?:改|降|提高|增加|放宽)|"
            r"价格(?:改|降|提高|增加|放宽)|不再要求)",
            compact_message,
        )
        if current_category is not None and requested_category is None and explicit_patch:
            return TaskRelationDecision(
                relation="continue_current",
                reason="用户使用了无歧义的当前导购约束修改表达",
                confidence=1.0,
            )

    client = get_client()
    candidates = [
        _task_relation_summary(state)
        for state in recent_tasks
        if state.task_id != active_task.task_id
    ]
    try:
        relation_started = time.perf_counter()
        relation_call_failed = True
        response = None
        try:
            response = await client.chat.completions.create(
                model=settings.deepseek_model,
                messages=[
                    {"role": "system", "content": TASK_RELATION_PLANNING_PROMPT},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "message": message,
                                "activeTask": _task_relation_summary(active_task),
                                "recentTasks": candidates,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                tools=[TASK_RELATION_TOOL_SCHEMA],
                **tool_choice_kwargs(
                    settings.deepseek_model,
                    {
                        "type": "function",
                        "function": {"name": TASK_RELATION_TOOL_NAME},
                    },
                ),
            )
            relation_call_failed = False
        finally:
            _observe_llm_call(
                "task_manager",
                (time.perf_counter() - relation_started) * 1000.0,
                failed=relation_call_failed,
                response=response,
            )
        reply = response.choices[0].message
        call = next(
            (
                item
                for item in (reply.tool_calls or [])
                if item.function.name == TASK_RELATION_TOOL_NAME
            ),
            None,
        )
        if call is None:
            raise ValueError("TaskManager模型没有返回关系工具调用")
        arguments = json.loads(call.function.arguments or "{}")
        decision = TaskRelationDecision.model_validate(arguments)
    except Exception:
        return TaskRelationDecision(
            relation="continue_current",
            reason="任务关系判断失败，保守地继续当前任务",
            confidence=0.0,
        )

    known_tasks = {state.task_id: state for state in recent_tasks}
    if decision.relation == "resume_previous":
        target = known_tasks.get(decision.target_task_id or "")
        if target is None or target.task_id == active_task.task_id:
            return TaskRelationDecision(
                relation="ambiguous",
                reason="模型指定的恢复目标不属于当前会话的历史任务",
                clarificationQuestion="你是想继续当前事情，还是回到之前的任务？",
                confidence=0.0,
            )
        if target.status != "paused":
            return TaskRelationDecision(
                relation="ambiguous",
                reason="模型指定的历史任务当前不可恢复",
                clarificationQuestion="你想回到哪个之前暂停的任务？",
                confidence=0.0,
            )
    return decision


async def ask_llm(message: str) -> str:
    """把用户的一句话发给 DeepSeek，拿到一段普通文字回答（这一步还不带工具）。"""
    client = get_client()
    response = await client.chat.completions.create(
        model=settings.deepseek_model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
    )
    return response.choices[0].message.content or ""


# 带工具时的系统提示词：明确要求模型用工具查真实数据，不要瞎编。
AGENT_SYSTEM_PROMPT = (
    "你是“本地生活智能体”的助手。涉及商户或地点的数据问题必须调用工具获取真实数据，"
    "再用简洁友好的中文回答，绝不要编造商户或地点信息。"
    "用户询问北京公园或博物馆的名称、行政区、官方类型、等级、地址、电话、坐标或无障碍设施记录时，"
    "先调用地点搜索工具；需要完整详情时，使用搜索返回的placeId调用地点详情工具。"
    "地点目录当前只是100个公园和190个备案开放博物馆的演示快照，不代表北京实时全量数据；"
    "回答搜索数量时必须说“当前演示目录中找到N个”，不得说某行政区或北京市“共有N个”；"
    "开放状态、拥挤程度、安静程度和是否适合老人等主观或实时结论不在证据范围内，不能根据等级或设施自行推断。"
    "地点搜索出现多个同名候选时，应列出行政区并请用户确认，不得擅自选择。"
    "当用户明确要求基于自己的历史行为、浏览记录或喜好做个性化推荐，并提供用户编号 userId 时，"
    "使用商户推荐工具；如果没有提供 userId，应先询问用户编号。"
    "当用户想找商户或问分类时，使用商户列表或分类工具；"
    "当用户询问某家店的电话、地址或基础详情时，使用商户 ID 调用详情工具；"
    "当用户询问口味、环境、排队、预算、营业时间、设施、适合人群或消费体验时，"
    "必须调用评论证据工具。用户没有点名具体商户时，调用统一知识检索工具，"
    "并传入 sources=[\"reviews\"]；用户明确点名某家商户时，调用定店评论检索工具，"
    "传入完整问题和用户说出的商户名称。"
    "定店评论检索如果返回多个候选，必须请用户确认具体门店，绝不能自行选择。"
    "‘哪家更适合办公/聊天/约会’属于评论体验问题，"
    "不需要再调用商户列表或详情工具。根据评论回答时，引用必须原样使用工具返回的 reviewId，"
    "格式为 [<reviewId>]；不得改写、缩写、重新编号，也不得自行添加 review- 前缀。"
    "不要把评论中的体验信息当成商户基础字段，也不要用商户列表工具猜测评论体验。"
    "Yelp评论是来源商户的历史体验证据，原文和译文不会伪装成北京本地评论；"
    "当工具返回evidenceNotice时，回答必须保留这条数据边界，不得把体验结论说成北京演示商户的现实事实。"
    "拿到工具返回的数据后，就直接用中文回答，不要重复调用同一个工具。"
    "如果工具调用失败或没查到结果，就如实告诉用户暂时查不到，不要编造。"
)

# 安全阀：最多允许模型连开几轮工具，防止它无限开单烧钱
# 地点能力的行为约束单独保留，便于针对 Agent 级评测迭代，而不把规则埋进长提示词中。
AGENT_SYSTEM_PROMPT += (
    "\n地点工具补充规则："
    "当用户询问某个已点名公园是否适合老人、是否安静或是否拥挤时，"
    "最多调用一次search_places查询这个公园的客观资料；不要重复搜索，"
    "不要用公园等级、面积、道路或绿化自行推断适合程度，必须明确说明目录无法支持该主观或实时结论。"
    "这类回答必须包含‘现有目录无法判断是否适合’；不要补充‘通常道路平整、绿化较好’等未经数据支持的常识推断。"
    "商家搜索没有结果时，直接如实说明当前数据未收录；"
    "不要为了改写同一个附近商家问题而继续调用list_shop_types后再次search_shops。"
    "遇到‘某公园附近的商家’时，使用地点目录中的placeId调用search_shops，"
    "在统一BD-09坐标系下按距离搜索，最多搜索一次。"
    "真实公园、博物馆等地点来自北京目录；商户名称、地址和位置是为了保留Yelp评论与行为关系而生成的"
    "北京化演示投影，不是真实登记商家。回答时必须明确称为‘演示商户’，不得暗示其现实存在。"
)
AGENT_SYSTEM_PROMPT += (
    "\n商品导购规则：只支持手机、笔记本和耳机。先用 search_products 召回候选，"
    "再用 get_product_details 读取权威快照，最后用 compare_products 做确定性比较；"
    "需要对已建立候选范围做服务端重排时，只能使用 rerank_products_in_scope。"
    "只有用户原话中的明确要求可标 hard；使用场景推断只能标 soft 且 source 以 inferred: 开头。"
    "未知规格不是满足。未验证价格不得展示或参与预算判断。最终最多展示 3 个商品；"
    "不足 3 个完全匹配时必须把其余结果标为最接近备选，并说明失败或未知条件。"
    "不得提供外部购买链接，并必须声明导购数据是历史公开快照、非实时价格或库存。"
    "用户要求交易时，当前导购只生成交易接管提示；订单、取消和支付必须交由独立的受信交易服务处理，"
    "本 Agent 不预览、不创建、不取消订单，也不发起支付。"
    "如果用途涉及窃听、未经授权访问、绕过监护或其他伤害，不得调用商品推荐工具；"
    "应简短拒绝该用途，并提供合法、安全的替代建议。"
)

MAX_TOOL_ROUNDS = 5
MAX_HARNESS_TRANSITIONS = 8

MERCHANT_DOC_ANSWER_GUARD = (
    "商户资料只证明工具明确返回的客观字段。"
    "只能回答这些字段，不得把WiFi、座位、评分、停车或营业时间推断成适合办公、适合约会、"
    "环境舒适等体验结论，也不要额外追加用户没有询问的推荐建议。"
)
POLICY_DOC_ANSWER_GUARD = (
    "政策资料只能用于回答用户当前的政策问题，并保留原文中的限定语。"
    "不得把“不一定需要”改写成绝对的“不会收集”或“完全不需要”；"
    "用户没有要求启动推荐时，不得主动索要userId、转入个性化推荐，或声称能够推荐地点。"
)

REVIEW_EXPERIENCE_KEYWORDS = (
    "适合",
    "安静",
    "办公",
    "带电脑",
    "插座",
    "聊天",
    "约会",
    "口味",
    "好吃",
    "环境",
    "氛围",
    "排队",
    "预算",
    "人均",
    "便宜",
    "贵",
    "设施",
    "体验",
    "感受",
    "评价",
    "评论",
    "推荐",
    "不推荐",
)
SHOP_TYPE_KEYWORDS = ("分类", "类型", "品类", "类别")
SHOP_DETAIL_KEYWORDS = ("电话", "地址", "位置", "在哪", "哪里")
SHOP_LIST_KEYWORDS = ("附近", "有什么", "有哪些", "商户", "店")
PERSONALIZED_RECOMMENDATION_KEYWORDS = (
    "个性化",
    "根据我的历史",
    "浏览记录",
    "历史行为",
    "用户行为",
    "我的喜好",
    "我喜欢",
    "我最近",
    "给我推荐",
    "为我推荐",
    "推荐一些",
    "demo-user",
    "user-",
)
POLICY_KNOWLEDGE_SUBJECT_KEYWORDS = (
    "个人信息",
    "隐私",
    "数据用途",
    "自动化决策",
    "消费者权益",
    "平台规则",
    "退款政策",
    "退款规则",
    "押金规则",
    "投诉",
)
POLICY_KNOWLEDGE_INTENT_KEYWORDS = (
    "为什么",
    "为何",
    "如何",
    "怎么",
    "是否",
    "能不能",
    "可以",
    "应该",
    "规则",
    "权利",
    "处理",
    "使用",
    "删除",
    "查询",
    "更正",
)
MERCHANT_PROFILE_KEYWORDS = (
    "营业时间",
    "几点开门",
    "几点关门",
    "几点营业",
    "周末营业",
    "WiFi",
    "wifi",
    "无线网络",
    "户外座位",
    "外带",
    "配送",
    "停车",
    "信用卡",
    "预约",
    "价格档位",
    "营业状态",
)
PLACE_ENTITY_KEYWORDS = (
    "公园", "游园", "名园", "绿地", "景区", "景点", "博物馆", "博物院", "纪念馆",
)
SHOP_ENTITY_KEYWORDS = ("咖啡店", "餐厅", "饭店", "火锅店", "商户", "酒店", "健身房", "电影院", "美食")
PLACE_DISTRICTS = (
    "东城区", "西城区", "朝阳区", "海淀区", "丰台区", "石景山区", "门头沟区", "房山区",
    "通州区", "顺义区", "昌平区", "大兴区", "怀柔区", "平谷区", "密云区", "延庆区", "经济开发区",
)
PARK_TYPES = ("历史名园", "综合公园", "自然（类）公园", "专类公园", "生态公园", "社区公园", "游园")
PARK_LEVELS = ("一级", "二级", "三级", "四级")
FACILITY_ALIASES = (
    ("无障碍厕所", "无障碍厕所/厕位"),
    ("无障碍旅游路线", "无障碍旅游路线"),
    ("无障碍停车位", "无障碍停车位"),
    ("低位服务设施", "低位服务设施"),
    ("出入口坡化", "出入口坡化"),
    ("接驳", "无障碍设施的接驳程度"),
)


def _tool_schema_name(schema: dict) -> str:
    return schema["function"]["name"]


def _tool_schemas_by_name(*names: str) -> list[dict]:
    allowed = set(names)
    return [schema for schema in TOOL_SCHEMAS if _tool_schema_name(schema) in allowed]


def _contains_any(message: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in message for keyword in keywords)


def _is_place_question(message: str) -> bool:
    return (
        _contains_any(message, PLACE_ENTITY_KEYWORDS)
        and not _contains_any(message, SHOP_ENTITY_KEYWORDS)
    )


def _is_landmark_shop_question(message: str) -> bool:
    return (
        "附近" in message
        and _contains_any(message, PLACE_ENTITY_KEYWORDS)
        and _contains_any(message, SHOP_ENTITY_KEYWORDS)
    )


def _is_policy_knowledge_question(message: str) -> bool:
    return (
        _contains_any(message, POLICY_KNOWLEDGE_SUBJECT_KEYWORDS)
        and _contains_any(message, POLICY_KNOWLEDGE_INTENT_KEYWORDS)
    )


def _is_merchant_profile_question(message: str) -> bool:
    """Identify factual merchant-profile fields stored in merchant_docs."""
    return _contains_any(message, MERCHANT_PROFILE_KEYWORDS)


def _forced_place_arguments(message: str) -> dict:
    kind = "museum" if _contains_any(message, ("博物馆", "博物院", "纪念馆")) else "park"
    arguments: dict[str, object] = {"kind": kind}
    for district in PLACE_DISTRICTS:
        if district in message:
            arguments["district"] = district
            break
    for park_type in PARK_TYPES:
        if park_type in message:
            arguments["parkType"] = park_type
            break
    for park_level in PARK_LEVELS:
        if park_level in message:
            arguments["parkLevel"] = park_level
            break
    for alias, facility_name in FACILITY_ALIASES:
        if alias in message:
            arguments["facilityName"] = facility_name
            if "不涉及" in message:
                arguments["facilityStatus"] = "不涉及"
            elif "良好" in message:
                arguments["facilityStatus"] = "良好"
            elif "标准" in message:
                status_key = "signageStatus" if "标识" in message else "facilityStatus"
                arguments[status_key] = "标准"
            break
    try:
        if get_place_catalog().find_name_mentions(message):
            arguments["query"] = message
    except Exception:
        # The actual tool will return the catalog availability error. Keep the
        # fallback arguments usable without exposing local file details here.
        pass
    return arguments


def _forced_landmark_shop_arguments(message: str) -> dict:
    arguments: dict[str, object] = {}
    try:
        matches = [
            record
            for record in get_place_catalog().find_name_mentions(message)
            if record.longitude is not None
            and record.latitude is not None
            and record.coordinate_system == "BD-09"
        ]
        if len(matches) == 1:
            arguments["nearPlaceId"] = matches[0].id
    except Exception:
        pass

    type_keywords = (
        (2, ("咖啡", "咖啡店", "咖啡馆")),
        (3, ("电影", "影院", "影城")),
        (4, ("酒店", "宾馆", "住宿")),
        (5, ("健身", "瑜伽", "运动")),
        (1, ("美食", "餐厅", "餐馆", "饭店", "吃饭", "小吃")),
    )
    for type_id, keywords in type_keywords:
        if _contains_any(message, keywords):
            arguments["typeId"] = type_id
            break

    radius_match = re.search(r"(\d+(?:\.\d+)?)\s*(公里|千米|米)", message)
    if radius_match:
        radius = float(radius_match.group(1))
        if radius_match.group(2) in {"公里", "千米"}:
            radius *= 1000
        arguments["radiusMeters"] = radius
    return arguments


def required_tool_name(message: str) -> str | None:
    """Return the safe fallback when an evidence-required turn skips tools."""
    normalized = message.strip()
    if is_ecommerce_message(normalized):
        return "search_products"
    if _is_landmark_shop_question(normalized):
        return "search_shops"
    if _is_place_question(normalized):
        return "search_places"
    if _is_policy_knowledge_question(normalized):
        return "search_knowledge"
    if _is_merchant_profile_question(normalized):
        return "search_knowledge"
    if _contains_any(normalized, REVIEW_EXPERIENCE_KEYWORDS):
        return "search_knowledge"
    return None


def _forced_tool_arguments(tool_name: str, message: str) -> dict:
    if tool_name == "search_shops" and _is_landmark_shop_question(message):
        return _forced_landmark_shop_arguments(message)
    if tool_name == "search_places":
        return _forced_place_arguments(message)
    if tool_name == "search_knowledge":
        if _is_policy_knowledge_question(message):
            sources = ["policy_docs"]
        elif _is_merchant_profile_question(message):
            sources = ["merchant_docs"]
        else:
            sources = ["reviews"]
        return {"query": message, "sources": sources}
    return {"query": message}


def _is_transaction_message(message: str) -> bool:
    return _contains_any(
        message,
        (
            "下单",
            "购买",
            "买这个",
            "订单",
            "取消订单",
            "付款",
            "支付",
        ),
    )


def select_tool_schemas(message: str) -> list[dict]:
    """根据用户问题缩小本轮工具菜单，减少模型误调无关工具。"""
    normalized = message.strip()
    if "ecommerce_guide" in normalized or is_ecommerce_message(normalized):
        # Shopping/legacy/Harness all receive the same read-only four-tool
        # menu. Transaction intent is a separate explicit handoff, never an
        # LLM preview or write capability.
        return shopping_tool_schemas()
    if _is_place_question(normalized):
        return _tool_schemas_by_name("search_places", "get_place_detail")
    if _is_landmark_shop_question(normalized):
        return _tool_schemas_by_name("search_shops")
    if _is_policy_knowledge_question(normalized):
        return _tool_schemas_by_name("search_knowledge")
    if _is_merchant_profile_question(normalized):
        return _tool_schemas_by_name("search_knowledge")
    if _contains_any(normalized, PERSONALIZED_RECOMMENDATION_KEYWORDS):
        return _tool_schemas_by_name("recommend_shops")
    if _contains_any(normalized, REVIEW_EXPERIENCE_KEYWORDS):
        return _tool_schemas_by_name("search_shop_reviews", "search_knowledge")
    if _contains_any(normalized, SHOP_TYPE_KEYWORDS):
        return _tool_schemas_by_name("list_shop_types")
    if _contains_any(normalized, SHOP_DETAIL_KEYWORDS):
        return _tool_schemas_by_name("search_shops", "get_shop_detail")
    if _contains_any(normalized, SHOP_LIST_KEYWORDS):
        return _tool_schemas_by_name("search_shops", "list_shop_types")
    return TOOL_SCHEMAS


def shopping_tool_schemas() -> list[dict]:
    """Shopping/PAE/BOUNDED_REACT public menu: proposal generation only.

    Transaction preview and write names are intentionally absent.  The
    Transaction Agent owns the separate server-issued capability entry.
    """
    # The public menu is exactly the four read-only shopping tools.  Visibility
    # of rerank does not grant execution: the executor/tool still requires a
    # server-owned CandidateScope and state-bound arguments.
    return _tool_schemas_by_name(
        "search_products", "get_product_details", "compare_products",
        "rerank_products_in_scope",
    )


@dataclass(frozen=True, slots=True)
class ToolRoutingDecision:
    kind: Literal["matched", "context_only", "unknown"]
    schemas: tuple[dict, ...]
    reason: str


def _is_explicit_context_only_message(message: str) -> bool:
    user_text = message
    if "本轮补充：" in user_text:
        user_text = user_text.rsplit("本轮补充：", 1)[1]
    elif user_text.startswith("[") and "\n" in user_text:
        user_text = user_text.split("\n", 1)[1]
    normalized = re.sub(r"[\s，。！？!?]+", "", user_text).casefold()
    exact = {
        "你好", "您好", "嗨", "hello", "hi", "谢谢", "感谢", "再见",
        "你是谁", "你能做什么", "介绍一下你自己", "帮助",
    }
    return normalized in exact


def route_tool_schemas(message: str) -> ToolRoutingDecision:
    """Return an explicit route; unknown intent is never treated as small talk."""

    selected = select_tool_schemas(message)
    selected_names = {_tool_schema_name(schema) for schema in selected}
    all_names = {_tool_schema_name(schema) for schema in TOOL_SCHEMAS}
    if selected_names != all_names:
        return ToolRoutingDecision("matched", tuple(selected), "rule_matched")
    if _is_explicit_context_only_message(message):
        return ToolRoutingDecision("context_only", (), "explicit_context_only")
    return ToolRoutingDecision("unknown", (), "no_route_matched")


def _assistant_message(reply) -> dict:
    message = {
        "role": "assistant",
        "content": reply.content,
    }
    if reply.tool_calls:
        message["tool_calls"] = [
            {
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            }
            for tool_call in reply.tool_calls
        ]
    return message


async def _generate_final_answer(
    client: AsyncOpenAI,
    *,
    messages: list[dict],
    tool_traces: list[ToolTrace],
    on_answer_delta: AnswerDeltaCallback | None,
    fallback: str,
    final_answer_view: Any | None = None,
    multi_agent_projection: dict[str, Any] | None = None,
) -> str:
    """Generate only the user-facing final answer; stream this call when requested.

    When final_answer_view (FinalAnswerContextView) is provided in context_pack
    mode, the model input is built from only the view's validated results and
    allowed facts — NOT from full chat history or raw TaskState.
    """
    async def create_non_stream_response(answer_messages: list[dict]):
        """Retry one transient final-answer failure within the outer deadline.

        This call is read-only and has no state or business side effects.  We
        deliberately do not apply the policy to extraction, planning, tools,
        or streaming output, where replay could duplicate mutations or deltas.
        """
        for attempt in range(2):
            started = time.perf_counter()
            answer_call_failed = True
            response = None
            try:
                response = await client.chat.completions.create(
                    model=settings.deepseek_model,
                    messages=answer_messages,
                )
                answer_call_failed = False
                return response
            except (
                APIConnectionError,
                APITimeoutError,
                RateLimitError,
                InternalServerError,
            ):
                if attempt:
                    raise
                await asyncio.sleep(0)
            finally:
                _observe_llm_call(
                    "final_answer",
                    (time.perf_counter() - started) * 1000.0,
                    failed=answer_call_failed,
                    response=response,
                )
        raise AssertionError("unreachable final-answer retry state")

    # ── context_pack mode: build input from FinalAnswerView only ──────
    if final_answer_view is not None:
        view = final_answer_view
        goal = view.goal
        validated = view.validated_results
        allowed = view.allowed_facts
        unknowns = view.unknowns
        evidence_refs = view.evidence_refs
        answer_format = view.answer_format
        long_term_memory = getattr(view, "long_term_memory", [])
        if (
            type(long_term_memory) is not list
            or len(long_term_memory) > 8
            or any(
                type(item) is not dict
                or not (
                    (
                        set(item) == {"category", "semanticKey", "value"}
                        and item.get("category") == "shopping_preference"
                        and all(type(item.get(key)) is str for key in ("category", "semanticKey", "value"))
                    )
                    or (
                        set(item) == {"categoryId", "preferenceKind", "attributeKey", "normalizedValue"}
                        and item.get("preferenceKind") in {"prefer", "avoid", "indifferent"}
                        and all(type(item.get(key)) is str for key in ("categoryId", "preferenceKind", "attributeKey", "normalizedValue"))
                    )
                )
                for item in long_term_memory
            )
        ):
            long_term_memory = []

        evidence_block = json.dumps(validated, ensure_ascii=False) if validated else ""
        facts_block = json.dumps(allowed, ensure_ascii=False) if allowed else ""

        comparison_instruction = ""
        if any(
            isinstance(item, dict) and item.get("tool") == "compare_products"
            for item in validated
        ):
            comparison_selection = answer_format.get("comparisonSelection")
            selection_instruction = ""
            if (
                isinstance(comparison_selection, dict)
                and isinstance(comparison_selection.get("selectedProductIds"), list)
                and isinstance(comparison_selection.get("sourceDisplayOrdinals"), list)
            ):
                selection_instruction = (
                    " comparisonSelection 是服务端绑定的上一轮展示序号："
                    "selectedProductIds 与 sourceDisplayOrdinals 一一对应。"
                    "回答必须保留这些原展示序号，不得把第 3 项重编号为第 2 项，"
                    "也不得声称已绑定的原序号不存在。"
                )
            comparison_instruction = (
                "\n\n## 比较任务\n结合用户目标、当前明确要求和已验证商品字段，"
                "解释差异并给出有条件的推荐。未知或冲突字段不得当作满足；"
                "标题中的营销描述不得当作性能或拍照事实。"
                "price_minor 使用人民币分，展示时必须除以 100。"
                + selection_instruction
            )
        multi_agent_block = ""
        if (
            isinstance(multi_agent_projection, dict)
            and multi_agent_projection.get("schemaVersion")
            == "multi-agent-parent-projection-v2"
            and isinstance(multi_agent_projection.get("report"), dict)
        ):
            # The child tool observation is intentionally absent.  Only the
            # parent-validated report and deterministic decision aid cross the
            # Agent boundary.
            prompt_projection = {
                "routeClass": multi_agent_projection.get("routeClass"),
                "report": multi_agent_projection.get("report"),
                "candidateDecisionSupport": multi_agent_projection.get(
                    "candidateDecisionSupport"
                ),
            }
            multi_agent_block = (
                "\n\n## EvidenceResearchAgent 已验证回执\n"
                + json.dumps(prompt_projection, ensure_ascii=False)
                + "\n只能把 findings 中带 evidenceRefs 的 SATISFIED/VIOLATED/CONFLICT "
                "当作核验证据；UNKNOWN 与 unresolved 必须明确写成未核实且不得借用其他字段引用。"
                "candidateDecisionSupport 只可辅助候选排序，不能证明缺失能力。"
            )

        view_messages: list[dict] = [
            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
            {
                "role": "system",
                "content": (
                    "显式Harness已完成并通过Validator。"
                    "只能依据下面的已验证证据和允许事实回答，不得编造：\n\n"
                    f"## 用户目标\n{goal}\n\n"
                    f"## 已验证证据\n{evidence_block}\n\n"
                    f"## 允许引用的事实\n{facts_block}\n\n"
                    + (f"## 尚不确定\n" + "\n".join(f"- {u}" for u in unknowns) + "\n\n" if unknowns else "")
                    + (f"## 证据引用\n{json.dumps(evidence_refs, ensure_ascii=False)}\n\n" if evidence_refs else "")
                    + ("## 低优先级长期购物偏好\n"
                       + json.dumps(long_term_memory, ensure_ascii=False)
                       + "\n当前任务的明确要求优先；不得把这些偏好当作当前事实。\n\n"
                       if long_term_memory else "")
                    + f"## 回答格式约束\n{json.dumps(answer_format, ensure_ascii=False)}"
                    + comparison_instruction
                    + multi_agent_block
                ),
            },
        ]

        streamed_text = ""
        if on_answer_delta is None:
            response = await create_non_stream_response(view_messages)
            raw_answer = response.choices[0].message.content or fallback
        else:
            stream_started = time.perf_counter()
            stream_call_failed = True
            parts: list[str] = []
            try:
                stream = await client.chat.completions.create(
                    model=settings.deepseek_model,
                    messages=view_messages,
                    stream=True,
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    content = getattr(chunk.choices[0].delta, "content", None)
                    if not content:
                        continue
                    parts.append(content)
                    await on_answer_delta(content)
                stream_call_failed = False
            finally:
                _observe_llm_call(
                    "final_answer",
                    (time.perf_counter() - stream_started) * 1000.0,
                    failed=stream_call_failed,
                )
            streamed_text = "".join(parts)
            raw_answer = streamed_text or fallback

        answer = _enforce_tool_answer_constraints(raw_answer, tool_traces)
        if on_answer_delta is not None:
            if not streamed_text:
                await on_answer_delta(answer)
            elif answer.startswith(streamed_text) and answer != streamed_text:
                await on_answer_delta(answer[len(streamed_text) :])
        return answer

    # ── Legacy mode: use full messages list ───────────────────────────
    streamed_text = ""
    if on_answer_delta is None:
        response = await create_non_stream_response(messages)
        raw_answer = response.choices[0].message.content or fallback
    else:
        stream_started = time.perf_counter()
        stream_call_failed = True
        parts: list[str] = []
        try:
            stream = await client.chat.completions.create(
                model=settings.deepseek_model,
                messages=messages,
                stream=True,
            )
            async for chunk in stream:
                if not chunk.choices:
                    continue
                content = getattr(chunk.choices[0].delta, "content", None)
                if not content:
                    continue
                parts.append(content)
                await on_answer_delta(content)
            stream_call_failed = False
        finally:
            _observe_llm_call(
                "final_answer",
                (time.perf_counter() - stream_started) * 1000.0,
                failed=stream_call_failed,
            )
        streamed_text = "".join(parts)
        raw_answer = streamed_text or fallback

    answer = _enforce_tool_answer_constraints(raw_answer, tool_traces)
    if on_answer_delta is not None:
        if not streamed_text:
            await on_answer_delta(answer)
        elif answer.startswith(streamed_text) and answer != streamed_text:
            await on_answer_delta(answer[len(streamed_text) :])
    return answer


_USED_PHONE_VALUE_LABELS = {
    "ios": "iOS", "android": "Android", "90_plus": "电池健康 90%+",
    "80_90": "电池健康 80%-90%", "70_80": "电池健康 70%-80%",
    "lt70": "电池健康低于 70%", "original": "原装",
    "non_original": "非原装", "not_repaired": "未维修",
    "repaired": "有维修", "none": "无划痕", "light": "轻微划痕",
    "obvious": "明显划痕", "normal": "外壳正常",
    "damaged": "外壳磕碰或缺失",
    # Canonical brand identities from models.canonicalize_brand; keep the
    # display in Chinese so FinalAnswer renders "品牌排除苹果" not the token.
    "apple": "苹果", "samsung": "三星", "huawei": "华为", "honor": "荣耀",
    "xiaomi": "小米", "redmi": "红米", "oppo": "OPPO", "vivo": "vivo",
    "iqoo": "iQOO",
}
_USED_PHONE_GROUP_LABELS = {
    "brand": "品牌",
    "battery_health": "电池健康", "battery_originality": "电池",
    "motherboard_repair": "主板", "os": "系统", "scratch_level": "划痕",
    "screen_originality": "屏幕", "shell_condition": "外壳",
    "price_minor": "商品快照价格",
}


def _used_phone_presentation_lines(presentations: object) -> list[str]:
    """Render the ordered presentation cards the Validator whitelisted."""
    lines: list[str] = []
    if not isinstance(presentations, list):
        return lines
    for index, card in enumerate(presentations[:3], start=1):
        if not isinstance(card, dict) or type(card.get("productId")) is not int:
            continue
        product_id = card["productId"]
        title = card.get("title")
        if not isinstance(title, str) or not title:
            title = f"商品 {product_id}"
        brand = card.get("brand")
        meta = f"品牌：{brand}" if isinstance(brand, str) and brand else "品牌：未知"
        if card.get("priceStatus") == "verified" and type(card.get("priceMinor")) is int:
            meta += f"；快照价：¥{card['priceMinor'] / 100:.2f}"
        elif card.get("priceStatus") == "synthetic" and type(card.get("priceMinor")) is int:
            meta += (
                f"；模拟参考价：¥{card['priceMinor'] / 100:.2f}"
                "（AI 合成，非真实报价）"
            )
            if card.get("pricePolicy") == "budget_and_ranking":
                meta += "；预算判断 policy：synthetic/budget_and_ranking"
        else:
            meta += "；价格：未核验"
        attributes = card.get("attributes")
        known: list[str] = []
        conflicts: list[str] = []
        unknown_count = 0
        if isinstance(attributes, list):
            for attribute in attributes:
                if not isinstance(attribute, dict):
                    continue
                key = attribute.get("key")
                if key not in _USED_PHONE_GROUP_LABELS:
                    continue
                status = attribute.get("status")
                if status == "known" and isinstance(attribute.get("value"), str):
                    value = attribute["value"]
                    known.append(
                        f"{_USED_PHONE_GROUP_LABELS[key]}："
                        f"{_USED_PHONE_VALUE_LABELS.get(value, value)}"
                    )
                elif status == "conflict":
                    conflicts.append(f"{_USED_PHONE_GROUP_LABELS[key]}：证据冲突")
                elif status == "unknown":
                    unknown_count += 1
        lines.append(f"\n### {index}. {title}")
        lines.append(f"{meta}；ID：{product_id}")
        if known:
            lines.append("- 已验证属性：" + "；".join(known[:5]))
        if conflicts:
            lines.append("- 需注意：" + "；".join(conflicts))
        if unknown_count:
            lines.append(f"- 其余受控属性有 {unknown_count} 项未知，未当作满足。")
        if card.get("selectionType") == "closest_alternative":
            lines.append("- 状态：最接近备选，仍有硬条件证据缺口。")
    return lines


def _render_validated_used_phone_answer(
    state: TaskState,
    final_answer_view: Any | None,
) -> str | None:
    """Render only Validator-whitelisted used-phone facts, with no model call."""

    if state.task_type != "ecommerce_guide" or final_answer_view is None:
        return None
    if (
        state.task_type != "ecommerce_guide"
        or getattr(final_answer_view, "answer_category", None) != "phone"
    ):
        return None
    results = getattr(final_answer_view, "validated_results", None)
    if not isinstance(results, list) or not results:
        return None

    def requirement_text(item: object) -> str | None:
        if not isinstance(item, dict) or item.get("key") not in _USED_PHONE_GROUP_LABELS:
            return None
        if (
            item.get("key") == "price_minor"
            and item.get("operator") == "lte"
            and isinstance(item.get("value"), (int, float))
        ):
            amount = f"{float(item['value']) / 100:.2f}".rstrip("0").rstrip(".")
            priority = "硬条件" if item.get("priority") == "hard" else "偏好"
            return f"{priority}：预算不超过 ¥{amount}"
        values = item.get("value")
        values = values if isinstance(values, list) else [values]
        labels = [_USED_PHONE_VALUE_LABELS.get(str(value), str(value)) for value in values]
        relation = "排除" if item.get("operator") == "not_in" else "要求"
        priority = "硬条件" if item.get("priority") == "hard" else "偏好"
        return (
            f"{priority}：{_USED_PHONE_GROUP_LABELS[item['key']]}"
            f"{relation}{'/'.join(labels)}"
        )

    requirements = [
        rendered for item in getattr(final_answer_view, "answer_constraints", [])
        if (rendered := requirement_text(item)) is not None
    ]
    compare = next(
        (item for item in results if item.get("tool") == "compare_products"), None
    )
    if isinstance(compare, dict):
        evidence = compare.get("evidence")
        products = (
            evidence.get("rankedFinalists")
            if isinstance(evidence, dict) else None
        )
        if not isinstance(products, list) or not products:
            return None
        risky = {"repaired", "non_original", "light", "obvious", "damaged", "lt70"}
        rendered_products: list[tuple[int, int, int, list[str]]] = []
        for row in products:
            if not isinstance(row, dict) or type(row.get("productId")) is not int:
                continue
            product_id = row["productId"]
            risk_count = 0
            unknown_count = 0
            fields: list[str] = []
            field_evidence = row.get("fieldEvidence")
            if not isinstance(field_evidence, list):
                return None
            for check in field_evidence:
                if not isinstance(check, dict):
                    continue
                key, value = check.get("key"), check.get("actual")
                if key not in _USED_PHONE_GROUP_LABELS:
                    continue
                status = check.get("status")
                if status in {"unknown", "conflict"}:
                    unknown_count += 1
                    label = "冲突" if status == "conflict" else "未知"
                    fields.append(f"{_USED_PHONE_GROUP_LABELS[key]}：{label}")
                    continue
                if status != "known":
                    return None
                if not isinstance(value, str):
                    return None
                risk_count += int(value in risky)
                ref = check.get("evidenceRef")
                suffix = f" [{ref}]" if isinstance(ref, str) else ""
                fields.append(
                    f"{_USED_PHONE_GROUP_LABELS[key]}："
                    f"{_USED_PHONE_VALUE_LABELS.get(value, value)}{suffix}"
                )
            rendered_products.append((unknown_count, risk_count, product_id, fields))
        if not rendered_products:
            return None
        best_quality = min((item[0], item[1]) for item in rendered_products)
        best_ids = [
            str(item[2]) for item in rendered_products
            if (item[0], item[1]) == best_quality
        ]
        lines = ["已完成两件商品的七属性证据对比："]
        for unknown_count, risk_count, product_id, fields in rendered_products:
            lines.append(
                f"\n商品 {product_id}（未知/冲突 {unknown_count} 项，"
                f"已知风险 {risk_count} 项）"
            )
            lines.extend(f"- {field}" for field in fields)
        if len(best_ids) == 1:
            lines.append(
                f"\n按证据缺口优先、再比较已知风险，商品 {best_ids[0]} 更适合作为当前选择。"
            )
        else:
            lines.append("\n两件商品风险标记数相同，现有证据不足以判定唯一更低风险者。")
        lines.append("数据来自冻结的历史公开快照，不代表实时价格或库存。")
        return "\n".join(lines)

    rerank = next(
        (item for item in results if item.get("tool") == "rerank_products_in_scope"),
        None,
    )
    if isinstance(rerank, dict):
        summary = rerank.get("validationSummary")
        rerank_summary = (
            summary.get("requiresScopeRerank")
            if isinstance(summary, dict) else None
        )
        if not isinstance(rerank_summary, dict):
            return None
        ranked_ids = rerank_summary.get("rankedItemIds")
        if not isinstance(ranked_ids, list) or not ranked_ids:
            return None
        evidence = rerank.get("evidence")
        ranking_signal = (
            evidence.get("rankingSignal")
            if isinstance(evidence, dict) else None
        )
        intent = (
            "gaming_title_claim"
            if ranking_signal == "scope_gaming_title_claim"
            else "camera_title_claim"
        )
        intent_label = "打游戏" if intent == "gaming_title_claim" else "拍照"
        claim_label = (
            "性能、帧率或流畅度" if intent == "gaming_title_claim"
            else "相机、实拍或成像能力"
        )
        # §5 capability-boundary disclosure: the order reflects seller title/text
        # relevance only, never a real camera/gaming capability conclusion.
        lines = [
            f"以下仅按上一轮候选的商品标题/公开文本与“{intent_label}”相关性排序，"
            f"不代表真实{claim_label}结论。"
        ]
        if requirements:
            lines.append("上一轮筛选条件保持不变：" + "；".join(requirements) + "。")
        presentations = rerank_summary.get("productPresentations")
        card_lines = _used_phone_presentation_lines(presentations)
        if card_lines:
            lines.append("重排后的前 3 个候选：")
            lines.extend(card_lines)
        else:
            lines.append("重排候选商品 ID（按重排顺序）：" + "、".join(
                str(item) for item in ranked_ids[:5]
            ) + "。")
        if len(ranked_ids) > 3:
            lines.append(f"其余 {len(ranked_ids) - 3} 个候选保留在同一 scope 中。")
        lines.append("本次没有重新全库检索；排序仅发生在上一轮可信候选范围内。")
        return "\n".join(lines)

    search = next(
        (item for item in results if item.get("tool") == "search_products"), None
    )
    if not isinstance(search, dict):
        return None
    memory_reranked = search.get("memoryOrderAdjusted") is True
    validation_summary = search.get("validationSummary")
    candidate_summary = (
        validation_summary.get("requiresProductCandidates")
        if isinstance(validation_summary, dict) else None
    )
    ids = candidate_summary.get("productIds") if isinstance(candidate_summary, dict) else None
    if not isinstance(ids, list):
        return None
    if not ids:
        conditions = "；".join(requirements) if requirements else "当前条件"
        return f"未找到满足这些条件的候选：{conditions}。建议一次只放宽一个非安全条件后重试。"
    presentations = candidate_summary.get("productPresentations")
    if not isinstance(presentations, list):
        presentations = []

    def presentation_lines() -> list[str]:
        return _used_phone_presentation_lines(presentations)

    has_complete_match = candidate_summary.get("hasCompleteMatch")
    if has_complete_match is False:
        unknowns = candidate_summary.get("hardUnknownsByProduct")
        groups = sorted({
            str(group)
            for values in (unknowns.values() if isinstance(unknowns, dict) else [])
            if isinstance(values, list)
            for group in values
        })
        if groups == ["price_minor"]:
            lines = [
                "你的预算下界已按 0 元处理。当前冻结商品数据没有可核验的售价，"
                "所以我不能确认这些候选是否真的在预算内，也不能把未知价格当成 0 元。",
                f"按其余条件找到 {len(ids)} 个最近候选；以下展示前 3 个的已验证商品摘要：",
            ]
            lines.extend(presentation_lines())
            if not presentations:
                lines.append("候选商品 ID：" + "、".join(str(item) for item in ids[:5]) + "。")
            lines.append("购买前需要另行核价。")
            return "\n".join(lines)
        labels = "、".join(_USED_PHONE_GROUP_LABELS.get(item, item) for item in groups)
        boundary = f"（缺少：{labels}）" if labels else ""
        lines = [
            "没有找到证据完整、可确认满足全部硬条件的商品。"
            f"检索仅返回 {len(ids)} 个最近候选{boundary}，不能把未知当作满足。"
        ]
        if memory_reranked:
            lines.append("以下展示顺序仅在放行候选内按已确认长期偏好做了软调整，没有过滤商品。")
        lines.extend(presentation_lines())
        lines.append("建议核验缺失字段，或明确选择一个可放宽的条件后重试。")
        return "\n".join(lines)
    lines = [f"已按受控条件检索到 {len(ids)} 个候选。"]
    extraction = state.domain_state.get("taskStateExtraction")
    raw_guide = state.domain_state.get("shoppingGuide")
    persisted_use_cases = (
        raw_guide.get("useCases", []) if isinstance(raw_guide, dict) else []
    )
    if (
        (
            isinstance(extraction, dict)
            and extraction.get("route") == "deterministic_text_claim_discovery"
        )
        or any(
            item in {"gaming_title_claim", "camera_title_claim"}
            for item in persisted_use_cases
            if isinstance(item, str)
        )
    ):
        lines.append(
            "本轮按 query 与商品标题/公开文本的相关性召回；标题中的“打游戏/拍照”"
            "属于卖家描述，不是已验证的芯片性能、流畅度或相机能力结论。"
        )
    if requirements:
        lines.append("已应用：" + "；".join(requirements) + "。")
    if memory_reranked:
        lines.append("以下展示顺序仅在 Validator 放行候选内按已确认长期偏好做了软调整，没有过滤商品。")
    lines.append("以下展示前 3 个由 Validator 放行的商品摘要：")
    lines.extend(presentation_lines())
    if not presentations:
        lines.append("候选商品 ID（按检索顺序）：" + "、".join(str(item) for item in ids[:5]) + "。")
    if len(ids) > 3:
        lines.append(f"其余 {len(ids) - 3} 个候选保留在本轮结果中。")
    lines.append("展示字段来自 Executor 规范化输出并已通过 Validator；未核验字段保持未知。")
    lines.append(
        "数据来自冻结的历史公开快照；模拟参考价为 AI 合成，非真实报价，"
        "不代表实时价格或库存。"
    )
    return "\n".join(lines)


def _render_react_final_answer_fallback(
    state: TaskState,
    final_answer_view: Any | None,
    reason_code: str,
) -> str:
    """Fail safely from a stalled answer model using Validator-owned data only."""

    prefix = (
        "回答模型本轮超时，已安全降级为服务端受控摘要；"
        "以下只使用 Validator 已放行的信息，不生成主观结论。"
    )
    if reason_code == "answer_evidence_boundary":
        return f"{prefix}\n{_render_used_phone_evidence_boundary_answer()}"
    validated = _render_validated_used_phone_answer(state, final_answer_view)
    if validated is not None:
        return f"{prefix}\n{validated}"
    return (
        f"{prefix}\n当前没有足够的已验证结果可供展示，"
        "请补充条件或重新检索。"
    )


def _render_used_phone_evidence_boundary_answer() -> str:
    """Return the stable server-owned boundary for unsupported capabilities."""

    return (
        "当前受控证据不含可靠的游戏帧率、散热或拍照质量测试数据，"
        "因此不能据此判断哪款在这些方面更好，也不会根据商品标题猜测。"
        "你可以继续按已验证的系统、电池健康、屏幕/电池原装性、"
        "主板维修和外观状态选择；缺失字段保持未知。"
    )


_MULTI_AGENT_GAP_LABELS = {
    "battery_health": "电池健康",
    "battery_originality": "电池原装性",
    "screen_originality": "屏幕原装性",
    "motherboard_repair": "主板维修状态",
    "scratch_level": "划痕状态",
    "shell_condition": "外壳状态",
    "camera_quality": "相机表现",
    "gaming_performance": "游戏表现",
    "thermal_performance": "散热表现",
}


def _multi_agent_candidate_titles(
    final_answer_view: FinalAnswerContextView,
) -> dict[int, str]:
    titles: dict[int, str] = {}
    for result in final_answer_view.validated_results:
        if not isinstance(result, dict):
            continue
        evidence = result.get("evidence")
        if not isinstance(evidence, dict):
            continue
        products = evidence.get("products")
        if not isinstance(products, list):
            continue
        for row in products:
            if not isinstance(row, dict):
                continue
            product = row.get("product") if isinstance(row.get("product"), dict) else row
            raw_id = product.get("id")
            title = product.get("title")
            try:
                candidate_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if isinstance(title, str) and title.strip():
                titles[candidate_id] = title.strip()[:80]
    return titles


def _render_multi_agent_parent_answer_v2(
    final_answer_view: FinalAnswerContextView,
    projection: dict[str, Any],
) -> str | None:
    """Deterministically render a validated child report.

    This removes the remaining parent-model window in which an UNKNOWN claim
    could borrow unrelated evidence references.  The function does not inspect
    raw child tool data and cannot create claims outside ResearchReport.
    """

    if projection.get("schemaVersion") != "multi-agent-parent-projection-v2":
        return None
    report = projection.get("report")
    support = projection.get("candidateDecisionSupport")
    if not isinstance(report, dict) or not isinstance(support, dict):
        return None
    findings = report.get("findings")
    unresolved = report.get("unresolved")
    if not isinstance(findings, list) or not isinstance(unresolved, list):
        return None

    by_candidate: dict[int, list[str]] = {}
    verified_count: dict[int, int] = {}
    unknown_keys: set[str] = set()
    for item in findings:
        if not isinstance(item, dict):
            continue
        try:
            candidate_id = int(item.get("candidateId"))
        except (TypeError, ValueError):
            continue
        gap = str(item.get("evidenceGapKey") or "unknown")
        verdict = str(item.get("verdict") or "UNKNOWN")
        refs = item.get("evidenceRefs")
        refs = refs if isinstance(refs, list) else []
        label = _MULTI_AGENT_GAP_LABELS.get(gap, gap)
        if verdict in {"SATISFIED", "VIOLATED"} and refs:
            summary = str(item.get("summary") or "已核验")[:120]
            status = "满足" if verdict == "SATISFIED" else "不满足"
            by_candidate.setdefault(candidate_id, []).append(
                f"{label}：{status}（{summary}）"
            )
            verified_count[candidate_id] = verified_count.get(candidate_id, 0) + 1
        elif verdict == "CONFLICT":
            by_candidate.setdefault(candidate_id, []).append(f"{label}：证据冲突")
            unknown_keys.add(gap)
        else:
            # UNKNOWN never displays or borrows evidenceRefs, even if a model
            # attempted to attach unrelated references.
            by_candidate.setdefault(candidate_id, []).append(f"{label}：未核实")
            unknown_keys.add(gap)
    for item in unresolved:
        if not isinstance(item, dict):
            continue
        try:
            candidate_id = int(item.get("candidateId"))
        except (TypeError, ValueError):
            continue
        gap = str(item.get("evidenceGapKey") or "unknown")
        label = _MULTI_AGENT_GAP_LABELS.get(gap, gap)
        line = f"{label}：未核实"
        rows = by_candidate.setdefault(candidate_id, [])
        if line not in rows:
            rows.append(line)
        unknown_keys.add(gap)

    support_rows = support.get("candidates")
    support_ids = [
        int(item["candidateId"])
        for item in (support_rows if isinstance(support_rows, list) else [])
        if isinstance(item, dict) and type(item.get("candidateId")) is int
    ]
    best_ids = [
        int(value)
        for value in support.get("bestVerifiedCandidateIds", [])
        if type(value) is int
    ]
    ordered_ids = list(dict.fromkeys([*best_ids, *support_ids, *by_candidate]))[:3]
    if not ordered_ids:
        return None
    titles = _multi_agent_candidate_titles(final_answer_view)
    lines = ["只读证据子 Agent 已完成核验："]
    for ordinal, candidate_id in enumerate(ordered_ids, start=1):
        title = titles.get(candidate_id, f"商品 {candidate_id}")
        lines.append(f"\n{ordinal}. {title}")
        rows = by_candidate.get(candidate_id) or ["本轮目标字段：未核实"]
        lines.extend(f"- {row}" for row in rows)
    if best_ids:
        preferred = titles.get(best_ids[0], f"商品 {best_ids[0]}")
        lines.append(
            f"\n按已核验的机况风险字段，优先考虑：{preferred}。"
            "这只是已验证字段的排序，不代表未知性能已经得到证明。"
        )
    if unknown_keys:
        labels = "、".join(
            _MULTI_AGENT_GAP_LABELS.get(key, key) for key in sorted(unknown_keys)
        )
        lines.append(f"\n证据边界：{labels}仍有缺失或冲突，不能据此作肯定结论。")
    return "\n".join(lines)


_UNSUPPORTED_USED_PHONE_CAPABILITY_CUES = (
    "性能", "处理器", "新款", "新机型", "更现代", "芯片", "骁龙",
    "帧率", "散热", "拍照", "相机",
)
_EVIDENCE_BOUNDARY_CUES = (
    "不能", "无法", "未知", "未验证", "未核验", "没有实测",
    "不作为", "不是已验证", "需要确认", "待确认",
)


def _has_unsupported_used_phone_capability_claim(answer: str) -> bool:
    """Detect positive capability advice unsupported by the seven-field catalog."""

    for line in answer.splitlines():
        normalized = re.sub(r"\s+", "", line).casefold()
        if not any(cue in normalized for cue in _UNSUPPORTED_USED_PHONE_CAPABILITY_CUES):
            continue
        if not any(cue in normalized for cue in _EVIDENCE_BOUNDARY_CUES):
            return True
    return False


def _enforce_tool_answer_constraints(answer: str, traces: list[ToolTrace]) -> str:
    """Apply deterministic wording guards that should not depend on model compliance."""
    guarded = answer
    for trace in traces:
        if trace.tool != "search_places" or not isinstance(trace.detail, dict):
            continue
        if trace.detail.get("scope") != "demo_snapshot_not_realtime":
            continue
        total = trace.detail.get("total")
        if not isinstance(total, int):
            continue
        filters = trace.detail.get("filters")
        district = filters.get("district") if isinstance(filters, dict) else None
        kind = filters.get("kind", "park") if isinstance(filters, dict) else "park"
        kind_label = {"park": "公园", "museum": "博物馆"}.get(kind, "地点")
        scope_labels = [label for label in (district, "北京市", "北京") if label]
        for label in scope_labels:
            scoped_pattern = (
                rf"当前演示目录中\s*[，,:：]?\s*{re.escape(label)}"
                rf"[^。；;\n]{{0,40}}?共有\s*(?:\*\*)?\s*{total}\s*个\s*(?:\*\*)?"
                rf"(?:[^，,。；;\n]{{0,20}}{kind_label})?"
            )
            guarded = re.sub(
                scoped_pattern,
                f"当前演示目录中找到{total}个符合条件的{kind_label}",
                guarded,
            )
            absolute_pattern = (
                rf"{re.escape(label)}[^。；;\n]{{0,40}}?共有\s*(?:\*\*)?\s*{total}\s*个\s*(?:\*\*)?"
                rf"(?:[^，,。；;\n]{{0,20}}{kind_label})?"
            )
            guarded = re.sub(
                absolute_pattern,
                f"当前演示目录中找到{total}个符合条件的{kind_label}",
                guarded,
            )
    guarded = guarded.replace("在当前的演示目录中，当前演示目录中", "当前演示目录中")
    evidence_notices = [
        str(trace.detail.get("evidenceNotice"))
        for trace in traces
        if isinstance(trace.detail, dict) and trace.detail.get("evidenceNotice")
    ]
    data_notices = [
        str(trace.detail.get("dataNotice"))
        for trace in traces
        if isinstance(trace.detail, dict) and trace.detail.get("dataNotice")
    ]
    if evidence_notices and "不证明北京演示商户的现实经营事实" not in guarded:
        guarded = (
            guarded.rstrip()
            + "\n\n数据边界：Yelp评论保留来源原文/译文，只能作为演示推荐证据，"
            "不证明北京演示商户的现实经营事实。"
        )
    elif data_notices and "不代表真实登记商家" not in guarded:
        guarded = guarded.rstrip() + f"\n\n数据边界：{data_notices[0]}"
    return guarded


def _render_landmark_shop_answer(trace: ToolTrace) -> str:
    if not trace.ok or not isinstance(trace.detail, dict):
        return "附近演示商户查询暂时不可用，请稍后再试。"
    detail = trace.detail
    place_name = detail.get("nearPlaceName")
    if not place_name:
        return "我暂时无法把你提到的地点唯一匹配到带坐标的北京地点，请说出完整地点名称。"
    shops = detail.get("shops")
    if not isinstance(shops, list) or not shops:
        return (
            f"以{place_name}为中心，当前演示数据在指定范围内没有找到符合条件的演示商户。"
            "这些商户数据是北京化演示投影，不代表真实登记商家。"
        )

    radius = detail.get("radiusMeters")
    radius_text = f"{float(radius):g}米内" if isinstance(radius, (int, float)) else "附近"
    lines = []
    for shop in shops:
        if not isinstance(shop, dict):
            continue
        distance = shop.get("distanceMeters")
        distance_text = (
            f"，约{float(distance):.0f}米"
            if isinstance(distance, (int, float))
            else ""
        )
        avg_price = shop.get("avgPrice")
        price_text = (
            f"，人均约{avg_price}元"
            if isinstance(avg_price, (int, float)) and avg_price > 0
            else ""
        )
        lines.append(
            f"- {shop.get('name', '未命名演示商户')}"
            f"（{shop.get('address', '暂无地址')}{distance_text}{price_text}）"
        )
    return (
        f"以{place_name}为中心，{radius_text}找到{len(lines)}家演示商户：\n"
        + "\n".join(lines)
        + "\n\n说明：地点来自北京真实地点目录；商户名称、地址和位置为北京化演示投影，"
        "用于保留Yelp评论与用户行为关系，不代表真实登记商家。"
    )


def _has_ambiguous_place_search(traces: list[ToolTrace]) -> bool:
    for trace in traces:
        if trace.tool != "search_places" or not isinstance(trace.detail, dict):
            continue
        items = trace.detail.get("items")
        if not isinstance(items, list) or len(items) < 2:
            continue
        names = {
            re.sub(r"[（(][^）)]*[）)]\s*$", "", item.get("name", "").strip()).strip()
            for item in items
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if len(names) == 1:
            return True
    return False


def _task_state_context(state: TaskState) -> dict[str, str]:
    """Render the structured task ledger as hidden context, not chat history."""
    snapshot = state.model_dump(by_alias=True, mode="json")
    return {
        "role": "system",
        "content": (
            "以下是当前任务的结构化TaskState。它比早期聊天文字更可靠；"
            "回答和工具规划应沿用其中已确认的目标、事实与约束，不要要求用户重复提供。\n"
            + json.dumps(snapshot, ensure_ascii=False)
        ),
    }


async def _generate_pending_task_question(
    client: AsyncOpenAI,
    *,
    messages: list[dict],
    state: TaskState,
    on_answer_delta: AnswerDeltaCallback | None,
) -> str:
    question = state.pending_questions[0]
    extraction = state.domain_state.get("taskStateExtraction")
    if (
        isinstance(extraction, dict)
        and extraction.get("route") == "deterministic_capability_boundary"
    ):
        if on_answer_delta is not None:
            await on_answer_delta(question)
        return question
    return await _generate_final_answer(
        client,
        messages=[
            *messages,
            {
                "role": "system",
                "content": (
                    "TaskState表明当前缺少用户才能提供的关键信息。"
                    f"本轮只用一句简洁中文追问：{question}"
                    "不要调用业务工具，不要假设答案，也不要提前推荐。"
                ),
            },
        ],
        tool_traces=[],
        on_answer_delta=on_answer_delta,
        fallback=question,
    )


def _task_routing_message(message: str, state: TaskState | None) -> str:
    if state is None:
        return message
    if state.goal.strip() == message.strip():
        return f"[{state.task_type}]\n{message}"
    return (
        f"[{state.task_type}]\n"
        f"当前任务目标：{state.goal}\n"
        f"本轮补充：{message}"
    )


def _normalized_task_status(state: TaskState, requested: Any) -> Any:
    """Keep model-proposed status changes inside the v1 state-machine contract."""
    if not isinstance(requested, str) or requested == state.status:
        return None
    if state.status == "collecting_information" and requested == "executing":
        return "ready"
    return requested


def _effective_unknowns(state: TaskState, payload: dict[str, Any]) -> list[str]:
    """Compute unresolved unknowns after a proposed patch without trusting status."""

    resolved = {
        item
        for item in payload.get("resolveUnknowns", [])
        if isinstance(item, str) and item.strip()
    }
    effective = [item for item in state.unknowns if item not in resolved]
    for item in payload.get("addUnknowns", []):
        if isinstance(item, str) and item.strip() and item not in effective:
            effective.append(item)
    return effective


_OPTIONAL_SHOPPING_KINDS = frozenset({"budget", "brand", "model", "use_case"})
_MODEL_DOMAIN_STATE_WRITABLE_KEYS: dict[str, frozenset[str]] = {
    "ecommerce_guide": frozenset({"shoppingGuide"}),
    "local_life": frozenset(),
}
_MODEL_DOMAIN_STATE_PATCH_MISSING = object()
_OPTIONAL_SHOPPING_QUESTION_TEXT = {
    "budget": "后续如需缩小范围，可补充预算。",
    "brand": "后续如有品牌偏好，可再补充。",
    "model": "后续如有具体型号偏好，可再补充。",
    "use_case": "后续如需更细排序，可补充主要用途。",
}
_RECOMMENDATION_INTENT_CUES = (
    "推荐", "想买", "想找", "帮我找", "选购", "购买",
    "recommend", "looking for", "want to buy", "find me",
)
_COMPARISON_INTENT_CUES = (
    "比较", "对比", "二选一", "哪个", "哪一个", "哪款", "哪台", "哪部",
    "谁更", "还是", "区别", "差异", "优缺点", "更适合", "更好",
    "两个", "两款", "两台", "两部", "其中", "各自", "分别", "相较", "相比",
    "compare", "comparison", " versus ", " vs ", "which", "better", "difference",
    "choose", "between", "both", "either", "among", "each",
)
_NONBLOCKING_SHOPPING_UNKNOWN_CUES: dict[str, tuple[str, ...]] = {
    "budget": ("预算", "价位", "价格范围", "budget", "price range"),
    "brand": ("品牌", "brand"),
    "model": ("机型", "型号", "哪款", "具体款", "model"),
    "use_case": ("用途", "使用场景", "用途场景", "use case", "usage", "purpose"),
}

_BLOCKING_SHOPPING_REFERENCE_CUES = (
    "没有绑定", "未绑定", "无法绑定", "指代不明", "指代不清",
    "没有明确", "未明确", "无法确定", "具体是哪个", "具体是哪一个",
)


class TaskStatePayloadValidationError(ValueError):
    """Pure-validation rejection of an untrusted ``update_task_state`` payload.

    Raised only before any persistence. ``code`` and ``field_path`` carry a
    stable structured summary so the bounded repair can echo the exact error
    back to the model without leaking credentials or user data.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        field_path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.field_path = field_path


def _decoded_mojibake_variant(value: str) -> str | None:
    """Return a likely GBK-over-Latin-1 recovery without mutating the payload."""

    if any("\u4e00" <= char <= "\u9fff" for char in value):
        return None
    try:
        decoded = value.encode("latin-1").decode("gbk")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return None
    if sum("\u4e00" <= char <= "\u9fff" for char in decoded) < 2:
        return None
    return decoded


def _model_text_mojibake_paths(value: object, path: str = "$") -> list[str]:
    paths: list[str] = []
    if isinstance(value, str):
        if _decoded_mojibake_variant(value) is not None:
            paths.append(path)
    elif isinstance(value, dict):
        for key, item in value.items():
            paths.extend(_model_text_mojibake_paths(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            paths.extend(_model_text_mojibake_paths(item, f"{path}[{index}]"))
    return paths


def _unknown_text_variants(value: str) -> tuple[str, ...]:
    decoded = _decoded_mojibake_variant(value)
    return (value.casefold(), decoded.casefold()) if decoded is not None else (value.casefold(),)


def _is_blocking_shopping_reference_unknown(value: object) -> bool:
    """Keep unresolved entity references blocking even when they mention a preference."""

    return isinstance(value, str) and any(
        cue in variant
        for variant in _unknown_text_variants(value)
        for cue in _BLOCKING_SHOPPING_REFERENCE_CUES
    )


def _prevalidate_strict_task_state_payload(
    state: TaskState,
    payload: dict[str, Any],
    *,
    require_status: bool,
) -> None:
    """Aggregate strict extractor violations so the only repair sees them all."""

    if not require_status:
        return
    violations: list[tuple[str, str]] = []
    mojibake_paths = _model_text_mojibake_paths(payload)
    if mojibake_paths:
        violations.append((
            ",".join(mojibake_paths),
            "strings are reversibly misdecoded GBK-over-Latin-1; resubmit normal Unicode text",
        ))
    raw_domain = payload.get("domainStatePatch")
    raw_guide = raw_domain.get("shoppingGuide") if isinstance(raw_domain, dict) else None
    if isinstance(raw_guide, dict):
        rejected = sorted(set(raw_guide) - _MODEL_SHOPPING_GUIDE_WRITABLE_KEYS)
        if rejected:
            violations.append((
                ",".join(f"domainStatePatch.shoppingGuide.{key}" for key in rejected),
                "remove server-owned shoppingGuide keys: " + ", ".join(rejected),
            ))
        executable_recommendation = (
            raw_guide.get("mode") == "recommend"
            and raw_guide.get("category") is not None
            and isinstance(raw_guide.get("requirements"), list)
            and bool(raw_guide["requirements"])
            and payload.get("status", state.status) == "collecting_information"
        )
        if executable_recommendation:
            optional_indices: list[int] = []
            optional_kinds: set[str] = set()
            for index, item in enumerate(payload.get("addUnknowns", [])):
                if not isinstance(item, str):
                    continue
                if _is_blocking_shopping_reference_unknown(item):
                    continue
                for kind, cues in _NONBLOCKING_SHOPPING_UNKNOWN_CUES.items():
                    if any(
                        cue in variant
                        for variant in _unknown_text_variants(item)
                        for cue in cues
                    ):
                        optional_indices.append(index)
                        optional_kinds.add(kind)
                        break
            if optional_indices:
                violations.append((
                    "addUnknowns[" + ",".join(map(str, optional_indices)) + "]",
                    "executable recommendation cannot block on optional preferences; "
                    "use status=ready, empty addUnknowns/pendingQuestions and "
                    "optionalShoppingQuestions for: " + ",".join(sorted(optional_kinds)),
                ))
    if not violations:
        return
    if len(violations) == 1:
        path, message = violations[0]
        if message.startswith("executable recommendation"):
            code = "nonblocking_shopping_preference_marked_unknown"
        elif message.startswith("remove server-owned"):
            code = "model_cannot_write_server_owned_shopping_guide_keys"
        else:
            code = "model_text_mojibake"
        raise TaskStatePayloadValidationError(message, code=code, field_path=path)
    raise TaskStatePayloadValidationError(
        "; ".join(f"{path}: {message}" for path, message in violations),
        code="multiple_task_state_payload_violations",
        field_path="$",
    )


def _validated_model_task_patch(arguments: object) -> dict[str, Any]:
    """Validate the complete untrusted model parameter surface at runtime."""

    if not isinstance(arguments, dict):
        raise ValueError("update_task_state arguments must be an object")
    if "arguments" in arguments:
        raise TaskStatePayloadValidationError(
            "model cannot write task state keys: arguments; function.arguments must "
            "contain the patch itself, not another arguments wrapper",
            code="unexpected_task_state_arguments_wrapper",
            field_path="arguments.arguments",
        )
    rejected_keys = sorted(set(arguments) - _MODEL_TASK_STATE_WRITABLE_KEYS)
    if rejected_keys:
        raise ValueError(
            "model cannot write task state keys: " + ", ".join(rejected_keys)
        )
    return dict(arguments)


def _validate_model_task_patch_items(payload: dict[str, Any]) -> None:
    """Enforce public array-item schemas before Pydantic can drop extra keys."""

    for field_name, (allowed_keys, required_keys) in (
        _MODEL_TASK_STATE_ITEM_CONTRACTS.items()
    ):
        if field_name not in payload:
            continue
        items = payload[field_name]
        if not isinstance(items, list):
            raise ValueError(f"{field_name} must be an array")
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"{field_name}[{index}] must be an object")
            item_keys = set(item)
            rejected_keys = sorted(item_keys - allowed_keys)
            if rejected_keys:
                raise ValueError(
                    f"model cannot write {field_name}[{index}] keys: "
                    + ", ".join(rejected_keys)
                )
            missing_keys = sorted(required_keys - item_keys)
            if missing_keys:
                raise ValueError(
                    f"{field_name}[{index}] missing required keys: "
                    + ", ".join(missing_keys)
                )


def _validated_model_domain_patch(
    state: TaskState,
    value: object,
) -> dict[str, Any]:
    """Return the model-owned domain patch after a task-type allowlist check.

    Internal runtime writers use ``TaskStatePatchRequest`` directly and are not
    constrained here. This boundary applies only to the untrusted
    ``update_task_state`` tool payload emitted by the model.
    """

    if value is _MODEL_DOMAIN_STATE_PATCH_MISSING:
        return {}
    if not isinstance(value, dict):
        raise ValueError("domainStatePatch must be an object")
    allowed_keys = _MODEL_DOMAIN_STATE_WRITABLE_KEYS.get(
        state.task_type,
        frozenset(),
    )
    rejected_keys = sorted(set(value) - allowed_keys)
    if rejected_keys:
        raise ValueError(
            "model cannot write domainStatePatch keys: "
            + ", ".join(rejected_keys)
        )
    result = dict(value)
    raw_guide = result.get("shoppingGuide", _MODEL_DOMAIN_STATE_PATCH_MISSING)
    if raw_guide is not _MODEL_DOMAIN_STATE_PATCH_MISSING:
        if not isinstance(raw_guide, dict):
            raise ValueError("domainStatePatch.shoppingGuide must be an object")
        rejected_guide_keys = sorted(
            set(raw_guide) - _MODEL_SHOPPING_GUIDE_WRITABLE_KEYS
        )
        if rejected_guide_keys:
            raise ValueError(
                "model cannot write shoppingGuide keys: "
                + ", ".join(rejected_guide_keys)
            )
        guide_patch = dict(raw_guide)
        if "requirements" in guide_patch and (
            "upsertRequirements" in guide_patch
            or "removeRequirementKeys" in guide_patch
        ):
            raise ValueError(
                "shoppingGuide requirements cannot be combined with incremental requirement operations"
            )
        result["shoppingGuide"] = guide_patch
    return result


def _materialize_requirements_increment(
    existing: list[Any],
    guide_patch: dict[str, Any],
) -> list[Any]:
    """Apply a key-addressed requirement delta without trusting a full rewrite."""

    has_existing = bool(existing)
    if has_existing and "requirements" in guide_patch:
        raise ValueError(
            "existing shoppingGuide requirements must be updated with "
            "upsertRequirements/removeRequirementKeys"
        )
    # A server-created guide legitimately starts with an empty requirement
    # collection.  Key-addressed upserts are safe on that empty collection and
    # avoid forcing a repair to switch back to the full-table surface.
    if "requirements" in guide_patch:
        return list(guide_patch["requirements"])

    upserts = guide_patch.get("upsertRequirements", [])
    removals = guide_patch.get("removeRequirementKeys", [])
    if not isinstance(upserts, list):
        raise ValueError("upsertRequirements must be an array")
    if not isinstance(removals, list) or not all(
        isinstance(key, str) and key for key in removals
    ):
        raise ValueError("removeRequirementKeys must be an array of keys")
    upsert_keys = [
        item.get("key") if isinstance(item, dict) else None for item in upserts
    ]
    if any(not isinstance(key, str) or not key for key in upsert_keys):
        raise ValueError("upsertRequirements items must contain a key")
    if len(upsert_keys) != len(set(upsert_keys)):
        raise ValueError("upsertRequirements keys must be unique")
    if len(removals) != len(set(removals)):
        raise ValueError("removeRequirementKeys must be unique")
    overlap = sorted(set(upsert_keys).intersection(removals))
    if overlap:
        raise ValueError(
            "the same shopping requirement cannot be upserted and removed: "
            + ", ".join(overlap)
        )

    by_key = {
        item.get("key"): item
        for item in upserts
        if isinstance(item, dict)
    }
    result: list[Any] = []
    for item in existing:
        key = item.get("key") if isinstance(item, dict) else None
        if key in removals:
            continue
        result.append(by_key.pop(key, item))
    result.extend(by_key[key] for key in upsert_keys if key in by_key)
    return result


def _materialize_model_domain_patch(
    state: TaskState,
    model_domain_patch: dict[str, Any],
) -> tuple[dict[str, Any], ShoppingGuideState | None]:
    """Merge model-owned guide fields while preserving the server-owned ledger."""

    if "shoppingGuide" not in model_domain_patch:
        return {}, None
    existing_guide = state.domain_state.get("shoppingGuide")
    merged_guide = dict(existing_guide) if isinstance(existing_guide, dict) else {}
    guide_patch = dict(model_domain_patch["shoppingGuide"])
    existing_requirements = merged_guide.get("requirements", [])
    if not isinstance(existing_requirements, list):
        existing_requirements = []
    merged_guide.update({
        key: value
        for key, value in guide_patch.items()
        if key not in {
            "requirements", "upsertRequirements", "removeRequirementKeys",
        }
    })
    merged_guide["requirements"] = _materialize_requirements_increment(
        existing_requirements,
        guide_patch,
    )
    try:
        guide_state = ShoppingGuideState.model_validate(merged_guide)
    except ValueError as exc:
        raise ValueError(
            "invalid shoppingGuide patch: unsupported or malformed requirement"
        ) from exc
    return {
        "shoppingGuide": guide_state.model_dump(by_alias=True, mode="json")
    }, guide_state


def _synchronize_ecommerce_constraint_projection(
    state: TaskState,
    payload: dict[str, Any],
    guide_state: ShoppingGuideState | None,
) -> None:
    """Make the validated shopping guide the only ecommerce constraint table."""

    if guide_state is None:
        return
    controlled = set(SHOPPING_REQUIREMENT_KEYS)
    model_upserts = payload.get("upsertConstraints", [])
    model_removals = payload.get("removeConstraintKeys", [])
    if not isinstance(model_upserts, list) or not isinstance(model_removals, list):
        return
    controlled_upserts = {
        item.get("key"): item
        for item in model_upserts
        if isinstance(item, dict) and item.get("key") in controlled
    }
    # TaskConstraint has no priority field and ContextPack publishes every row
    # in that table as a Planner ``hardConstraint``. Project only hard shopping
    # requirements here; soft requirements have their own authoritative path
    # through shoppingGuide.requirements -> ContextPack.softPreferences.
    projected = {
        requirement.key: {
            "key": requirement.key,
            "operator": requirement.operator,
            "value": requirement.value,
            "source": "user" if requirement.source == "user" else "agent",
        }
        for requirement in guide_state.requirements
        if requirement.priority == "hard"
    }
    for key, item in controlled_upserts.items():
        if projected.get(key) != item:
            raise ValueError(
                f"upsertConstraints.{key} disagrees with shoppingGuide requirements"
            )
    conflicting_removals = sorted(set(model_removals).intersection(projected))
    if conflicting_removals:
        raise ValueError(
            "removeConstraintKeys disagrees with shoppingGuide requirements: "
            + ", ".join(conflicting_removals)
        )
    payload["upsertConstraints"] = [
        item for item in model_upserts
        if not isinstance(item, dict) or item.get("key") not in controlled
    ] + list(projected.values())
    existing_controlled = {
        constraint.key
        for constraint in state.constraints
        if constraint.key in controlled
    }
    payload["removeConstraintKeys"] = [
        key for key in model_removals if key not in controlled
    ] + sorted(existing_controlled - set(projected))


def _is_explicit_used_phone_exclusion_clause(clause: str) -> bool:
    return (
        clause.startswith("不要")
        or clause.startswith("排除")
        or clause.endswith("的不要")
        or "不接受" in clause
        or "不能接受" in clause
        or "先排除" in clause
        or "排除掉" in clause
        # A scope-resolved negation (``屏幕不要非原装的``, ``非原装屏不要``,
        # ``屏幕不能是非原装``) is authoritative: the attribute phrase is the
        # rejected object, not a positive requirement.  Reusing the scoped
        # parser here keeps a single negation semantic instead of duplicating
        # keyword lists.
        or bool(_negation_scoped_used_phone_exclusions(clause))
    )


# Middle-position negation such as ``屏幕不要非原装的`` fixes the attribute
# scope on the left of the cue and the rejected value on the right.  ``非原装屏不要``
# uses a trailing cue and carries the rejected value inside the anchor itself.
_SCOPE_NEGATION_VERBS = ("不能是", "不可以是", "不能有", "不要", "不想要", "排除掉", "排除")

_SCOPE_ATTRIBUTE_ANCHORS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("非原装屏", "换过屏", "更换过屏幕", "原装屏", "原厂屏", "原装内屏", "屏幕", "屏"), "screen_originality"),
    (("非原装电池", "非原厂电池", "原装电池", "原厂电池", "电池"), "battery_originality"),
    (("主板",), "motherboard_repair"),
    (("划痕",), "scratch_level"),
    (("外壳", "机壳"), "shell_condition"),
)


def _scope_excluded_values(key: str, text: str) -> list[str] | None:
    """Return the finite-domain value rejected by a negation-scoped clause."""
    if key == "screen_originality":
        if any(token in text for token in ("非原装", "换过屏", "更换过屏幕", "换屏")):
            return ["non_original"]
        if any(token in text for token in ("原装", "原厂")):
            return ["original"]
    if key == "battery_originality":
        if any(token in text for token in ("非原装", "非原厂")):
            return ["non_original"]
        if any(token in text for token in ("原装", "原厂")):
            return ["original"]
    if key == "motherboard_repair":
        if any(token in text for token in ("修过", "维修", "修", "拆修")):
            return ["repaired"]
        if any(token in text for token in ("未修", "没修")):
            return ["not_repaired"]
    if key == "scratch_level":
        if "划痕" in text or "磕碰" in text:
            return ["light", "obvious"]
    if key == "shell_condition":
        if "磕碰" in text or "缺失" in text:
            return ["damaged"]
    return None


def _scope_anchor_key(text: str) -> str | None:
    """Return the most specific controlled attribute named by an anchor text."""
    for phrases, key in _SCOPE_ATTRIBUTE_ANCHORS:
        if any(phrase in text for phrase in phrases):
            return key
    return None


def _scope_anchor_and_value(after: str) -> tuple[str | None, list[str] | None]:
    """Find an attribute anchor plus its rejected value inside post-cue text."""
    for phrases, key in _SCOPE_ATTRIBUTE_ANCHORS:
        for phrase in phrases:
            index = after.find(phrase)
            if index == -1:
                continue
            descriptor = after[:index] + after[index + len(phrase):]
            values = _scope_excluded_values(key, descriptor)
            return key, values
    return None, None


def _negation_scoped_used_phone_exclusions(clause: str) -> dict[str, list[str]]:
    """Resolve attribute-scoped negation where the cue is not clause-initial.

    Handles ``屏幕不要非原装的``, ``不要非原装的屏幕``, ``非原装屏不要`` and
    ``屏幕不能是非原装`` without a single full-string patch.  Unbound or
    value-less negations fail closed (return nothing) so they never create a
    fabricated exclusion.
    """
    result: dict[str, list[str]] = {}

    def anchor_from_text(text: str) -> tuple[str, list[str] | None]:
        key = _scope_anchor_key(text)
        if key is None:
            return None, None
        return key, _scope_excluded_values(key, text)

    for verb in _SCOPE_NEGATION_VERBS:
        if not clause.endswith(verb):
            continue
        anchor_text = clause[: -len(verb)]
        if not anchor_text:
            continue
        key, values = anchor_from_text(anchor_text)
        if key is not None and values:
            result[key] = values
        return result
    for verb in _SCOPE_NEGATION_VERBS:
        index = clause.find(verb)
        if index == -1:
            continue
        before = clause[:index]
        after = clause[index + len(verb):]
        key = _scope_anchor_key(before)
        if key is not None:
            values = _scope_excluded_values(key, after)
            if values:
                result[key] = values
            return result
        key, values = _scope_anchor_and_value(after)
        if key is not None and values:
            result[key] = values
            return result
    return result


def _explicit_used_phone_exclusions(message: str) -> dict[str, list[str]]:
    """Return explicit enum values excluded by a direct negative request."""

    phrase_values = (
        (("非原装屏", "屏幕非原装", "换过屏", "更换过屏幕"), "screen_originality", ["non_original"]),
        (("主板修过", "主板维修过", "主板有维修"), "motherboard_repair", ["repaired"]),
        (("非原装电池", "电池不是原装", "非原厂电池"), "battery_originality", ["non_original"]),
        (("有划痕",), "scratch_level", ["light", "obvious"]),
        (("外壳磕碰或缺失", "外壳有磕碰或缺失"), "shell_condition", ["damaged"]),
        (("安卓系统", "安卓"), "os", ["android"]),
        (("ios系统", "ios"), "os", ["ios"]),
        (("电池健康70%到80%", "电池健康70%-80%"), "battery_health", ["70_80"]),
        (("电池健康80%到90%", "电池健康80%-90%"), "battery_health", ["80_90"]),
    )
    result: dict[str, list[str]] = {}
    for clause in _used_phone_clauses(message):
        if _is_explicit_used_phone_exclusion_clause(clause):
            for phrases, key, values in phrase_values:
                if any(phrase in clause for phrase in phrases):
                    result[key] = values
        # Scope-resolved middle/trailing negation runs on every clause but only
        # materializes a value when the attribute and rejected value are both
        # uniquely named, so it cannot invent an exclusion from bare text.
        scoped = _negation_scoped_used_phone_exclusions(clause)
        result.update(scoped)
    return result


def _canonicalize_explicit_used_phone_negation(
    message: str,
    guide_state: ShoppingGuideState | None,
) -> ShoppingGuideState | None:
    """Materialize direct exclusions in one auditable canonical form.

    The user utterance, not a model-proposed complement, is authoritative here.
    A recognized finite-domain exclusion is therefore persisted as hard/user
    ``not_in`` even when the extractor submitted an extensionally equivalent
    positive form such as ``eq original``.  Unrecognized text is untouched.
    """
    brand_negations = parse_brand_negations(message)
    if guide_state is not None:
        conflicting_operations = (
            brand_negations.negated_brands
            & frozenset(brand_negations.released_brands)
        )
        if conflicting_operations:
            raise TaskStatePayloadValidationError(
                "the same turn both rejects and accepts brands: "
                + ", ".join(sorted(conflicting_operations)),
                code="ambiguous_brand_negation_update",
                field_path="domainStatePatch.shoppingGuide.brandAvoidances",
            )

        strength_by_brand = {
            brand: avoidance.strength
            for avoidance in guide_state.brand_avoidances
            for brand in avoidance.values
        }
        for brand in brand_negations.released_brands:
            strength_by_brand.pop(brand, None)

        explicit = _explicit_used_phone_requirements(message).get("brand")
        if explicit is not None:
            raw_positive = (
                explicit.value if isinstance(explicit.value, list)
                else [explicit.value]
            )
            for value in raw_positive:
                strength_by_brand.pop(canonicalize_brand(str(value)), None)

        for target in brand_negations.targets:
            for brand in target.values:
                strength_by_brand[brand] = target.strength

        requirements: list[ShoppingRequirement] = []
        avoided = set(strength_by_brand)
        for requirement in guide_state.requirements:
            if requirement.key != "brand" or requirement.operator not in {"eq", "in"}:
                requirements.append(requirement)
                continue
            raw_values = (
                requirement.value
                if isinstance(requirement.value, list)
                else [requirement.value]
            )
            positive_brands = {
                canonicalize_brand(str(value))
                for value in raw_values
            }
            if not positive_brands.intersection(avoided):
                requirements.append(requirement)

        avoidances = [
            BrandAvoidance(
                values=sorted(
                    brand
                    for brand, strength in strength_by_brand.items()
                    if strength == target_strength
                ),
                strength=target_strength,
                source="user",
            )
            for target_strength in ("hard", "soft")
            if any(
                strength == target_strength
                for strength in strength_by_brand.values()
            )
        ]
        guide_state = guide_state.model_copy(update={
            "requirements": requirements,
            "brand_avoidances": avoidances,
        })

    exclusions = _explicit_used_phone_exclusions(message)
    if not exclusions or guide_state is None:
        return guide_state
    requirements = {item.key: item for item in guide_state.requirements}
    missing = sorted(set(exclusions) - set(requirements))
    if missing:
        raise TaskStatePayloadValidationError(
            "explicit negative preference is missing controlled requirements: "
            + ", ".join(missing),
            code="explicit_negation_semantics_lost",
            field_path="domainStatePatch.shoppingGuide.requirements",
        )
    normalized: list[ShoppingRequirement] = []
    for requirement in guide_state.requirements:
        values = exclusions.get(requirement.key)
        if values is None:
            normalized.append(requirement)
            continue
        normalized.append(ShoppingRequirement(
            key=requirement.key,
            operator="not_in",
            value=values,
            unit="enum",
            priority="hard",
            source="user",
        ))
    return guide_state.model_copy(update={"requirements": normalized})


def _used_phone_clauses(message: str) -> list[str]:
    """Return normalized local clauses for controlled used-phone matching.

    Punctuation and explicit additive conjunctions form priority boundaries.
    Plain ``和`` deliberately does not: phrases such as ``优先电池健康和主板没修过``
    apply one visible preference cue to both coordinated attributes.
    """

    return [
        re.sub(r"\s+", "", clause).casefold()
        for clause in re.split(
            r"[，,、；;。.!！?？]+|(?:并且|同时|还要|另外|再加上)",
            message,
        )
        if re.sub(r"\s+", "", clause)
    ]


def _nonbinding_used_phone_platform_acceptance_mentions(
    message: str,
) -> set[str]:
    """Return platform keys that the user explicitly leaves unrestricted.

    ``苹果或者安卓都可以`` coordinates an Apple/iPhone ecosystem with the
    Android ecosystem.  It is not the conjunction ``brand=apple AND
    os=android``.  Pure OS alternatives such as ``iOS 或 Android 均可`` only
    release ``os``; the cross-dimension Apple/Android wording releases both
    ``brand`` and ``os``.
    """

    connector = r"(?:或者|或是|还是|和|与|、|/|或)"
    acceptance = (
        r"(?:我)?(?:都)?(?:可以|都行|均可|皆可|都能接受|都可以接受|"
        r"没关系|都没关系|无所谓|不介意|随便)"
    )
    ios = r"(?:ios|苹果系统)"
    apple_ecosystem = r"(?:苹果(?!系统)|apple|iphone)"
    android = r"(?:安卓(?:系统|手机)?|android)"
    result: set[str] = set()
    for clause in _used_phone_clauses(message):
        os_pair = rf"(?:{ios}{connector}{android}|{android}{connector}{ios})"
        ecosystem_pair = (
            rf"(?:{apple_ecosystem}{connector}{android}|"
            rf"{android}{connector}{apple_ecosystem})"
        )
        if re.search(rf"{os_pair}.{{0,4}}{acceptance}$", clause):
            result.add("os")
        if re.search(rf"{ecosystem_pair}.{{0,4}}{acceptance}$", clause):
            result.update(("brand", "os"))
    return result


def _nonbinding_used_phone_result_brand_mentions(message: str) -> set[str]:
    """Return brands mentioned only as observations about prior results.

    A follow-up such as ``只有苹果的吗？有没有安卓的`` names Apple to
    question the displayed distribution; it does not request ``brand=apple``.
    Keep direct requests (``只要苹果``/``苹果优先``/``有没有苹果手机``)
    outside this guard so genuine positive brand constraints still bind.
    """

    observed: set[str] = set()
    result_context = (
        r"(?:推荐的|推荐结果|检索结果|搜索结果|显示的|给我的|出来的|"
        r"结果里|结果|候选里|候选|这些候选|这些|这几款)?"
    )
    for clause in _used_phone_clauses(message):
        for brand in detect_product_brands(clause):
            for alias in product_brand_aliases(brand):
                escaped = re.escape(alias)
                distribution_question = re.search(
                    rf"(?:怎么|为什么|难道)?{result_context}"
                    rf"(?:只有|都(?:是)?|全(?:部)?(?:是)?){escaped}"
                    rf"(?:的)?(?:吗|么|呢)?$",
                    clause,
                )
                excessive_result = re.search(
                    rf"{escaped}(?:的)?(?:怎么|为什么|也)?"
                    rf"(?:这么|那么|太)多(?:了)?$",
                    clause,
                )
                if distribution_question or excessive_result:
                    observed.add(brand)
                    break
    return observed


def _explicit_used_phone_requirement_removals(message: str) -> set[str]:
    """Recognize explicit key removals without treating value exclusion as deletion."""

    key_phrases: dict[str, tuple[str, ...]] = {
        "os": ("系统条件", "系统偏好", "操作系统条件", "ios条件", "安卓条件"),
        "battery_health": ("电池健康条件", "电池健康偏好", "电池条件"),
        "screen_originality": ("原装屏这个偏好", "屏幕原装偏好", "屏幕条件", "屏幕这项"),
        "motherboard_repair": ("主板维修条件", "主板条件", "主板偏好", "主板这项"),
        "battery_originality": ("原装电池这个偏好", "电池原装偏好"),
        "scratch_level": ("划痕条件", "划痕偏好", "无划痕偏好", "外观划痕偏好"),
        "shell_condition": ("外壳条件", "外壳偏好"),
        "price_minor": ("预算", "预算条件", "预算要求", "价格条件", "价位条件"),
    }
    removal_cues = ("取消", "去掉", "删掉", "不再要求", "不用保留", "不需要保留")
    result: set[str] = set()
    for clause in _used_phone_clauses(message):
        if not any(cue in clause for cue in removal_cues):
            continue
        for key, phrases in key_phrases.items():
            if any(phrase in clause for phrase in phrases):
                result.add(key)
    # Questioning why the previous result contains only one named brand is an
    # explicit release of any carried positive brand filter.  Without this,
    # retrying after a stale/spurious brand write would keep the contradiction.
    if _nonbinding_used_phone_result_brand_mentions(message):
        result.add("brand")
    # An explicit "either is fine" statement releases carried constraints.
    # A separate positive requirement in the same turn wins, e.g.
    # ``苹果或安卓都可以，安卓优先`` keeps the new soft Android preference.
    result.update(
        _nonbinding_used_phone_platform_acceptance_mentions(message)
        - set(_explicit_used_phone_requirements(message))
    )
    return result


def _explicit_used_phone_priority_changes(message: str) -> dict[str, str]:
    """Return key-addressed hard/soft changes that omit the existing value."""

    key_phrases: dict[str, tuple[str, ...]] = {
        "os": ("系统要求", "系统条件", "操作系统要求", "ios", "安卓"),
        "battery_health": ("电池健康要求", "电池健康条件", "电池档位"),
        "screen_originality": ("屏幕要求", "屏幕条件", "原装屏要求", "原厂屏要求"),
        "motherboard_repair": ("主板要求", "主板条件", "主板维修要求"),
        "battery_originality": ("电池原装要求", "原装电池要求", "原厂电池要求"),
        "scratch_level": ("划痕要求", "划痕条件", "外观划痕要求"),
        "shell_condition": ("外壳要求", "机壳要求", "外壳条件", "机壳条件"),
        "price_minor": ("预算要求", "预算条件", "价格要求", "价格条件"),
    }
    soft_cues = (
        "改为优先", "改成优先", "降为偏好", "降成偏好", "改为偏好",
        "改成偏好", "不再是硬条件", "只作为偏好", "仅作为偏好",
    )
    hard_cues = ("改为硬条件", "改成硬条件", "升为硬条件", "必须满足")
    result: dict[str, str] = {}
    for clause in _used_phone_clauses(message):
        priority = (
            "soft" if any(cue in clause for cue in soft_cues)
            else "hard" if any(cue in clause for cue in hard_cues)
            else None
        )
        if priority is None:
            continue
        for key, phrases in key_phrases.items():
            if any(phrase in clause for phrase in phrases):
                result[key] = priority
    return result


def _explicit_used_phone_retained_requirements(message: str) -> set[str]:
    """Recognize key-addressed retain/no-change references to existing state."""

    key_phrases: dict[str, tuple[str, ...]] = {
        "os": ("系统", "操作系统", "ios", "安卓"),
        "battery_health": ("电池健康", "电池档位"),
        "screen_originality": ("屏幕", "原装屏", "原厂屏"),
        "motherboard_repair": ("主板",),
        "battery_originality": ("原装电池", "原厂电池", "电池原装"),
        "scratch_level": ("划痕",),
        "shell_condition": ("外壳", "机壳"),
        "price_minor": ("预算", "价格", "价位"),
    }
    retain_cues = ("保留", "不变", "照旧", "继续", "维持")
    result: set[str] = set()
    for clause in _used_phone_clauses(message):
        if not any(cue in clause for cue in retain_cues):
            continue
        for key, phrases in key_phrases.items():
            if any(phrase in clause for phrase in phrases):
                result.add(key)
    return result


def _parse_chinese_integer(value: str) -> int | None:
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    units = {"十": 10, "百": 100, "千": 1000, "万": 10_000}
    if not value or any(char not in digits and char not in units for char in value):
        return None
    total = 0
    section = 0
    number = 0
    for char in value:
        if char in digits:
            number = digits[char]
            continue
        unit = units[char]
        if unit == 10_000:
            section = (section + number) * unit
            total += section
            section = 0
            number = 0
        else:
            section += (number or 1) * unit
            number = 0
    return total + section + number


def _explicit_phone_price_ceiling(message: str) -> int | None:
    """Return an explicit user-visible budget ceiling in CNY minor units."""

    normalized = re.sub(r"\s+", "", message).casefold()
    amount_pattern = r"(?P<amount>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+)"
    patterns = (
        amount_pattern + r"(?P<scale>[k千]?)(?:元|块)?(?:以内|以下|之内|封顶)",
        r"(?:预算|价格|价位)(?:不超过|最多|上限(?:是|为)?|控制在|"
        r"改成|改为|调整为|降到|降至|提高到|提高至)"
        + amount_pattern
        + r"(?P<scale>[k千]?)(?:元|块)?",
        r"(?:预算|价位)(?:是|为|大概|大约|约)?"
        + amount_pattern
        + r"(?P<scale>[k千]?)(?:元|块)?(?:左右|上下|吧)?",
    )
    match = next((match for pattern in patterns if (match := re.search(pattern, normalized))), None)
    bare_phone_amount = False
    if match is None:
        # Real users often end a short shopping request with the budget alone,
        # e.g. ``续航好手机500`` or ``手机推荐500左右``.  Bind only a trailing
        # amount immediately after the phone/recommendation phrase.  A lower
        # bound avoids turning model names such as ``苹果手机15`` into prices;
        # explicit 元/预算/以内 forms above remain unaffected.
        match = re.search(
            r"(?:二手手机|二手机|手机)(?:推荐)?[:：]?"
            + amount_pattern
            + r"(?P<scale>[k千]?)(?:左右|上下|吧)?$",
            normalized,
        )
        bare_phone_amount = match is not None
    if match is None:
        return None
    raw_amount = match.group("amount")
    if re.fullmatch(r"\d+(?:\.\d+)?", raw_amount):
        amount = float(raw_amount)
    else:
        colloquial_thousands = re.fullmatch(
            r"(?P<thousands>[一二两三四五六七八九])千"
            r"(?P<hundreds>[一二两三四五六七八九])",
            raw_amount,
        )
        if colloquial_thousands is not None:
            digit_values = {
                "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
                "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
            }
            parsed = (
                digit_values[colloquial_thousands.group("thousands")] * 1000
                + digit_values[colloquial_thousands.group("hundreds")] * 100
            )
        else:
            parsed = _parse_chinese_integer(raw_amount)
        if parsed is None:
            return None
        amount = float(parsed)
    scale = match.groupdict().get("scale") or ""
    if scale in {"k", "千"} and "千" not in raw_amount:
        amount *= 1000
    if bare_phone_amount and amount < 100:
        return None
    if not 1 <= amount <= 1_000_000:
        return None
    return int(round(amount * 100))


def _explicit_used_phone_requirements(message: str) -> dict[str, ShoppingRequirement]:
    """Parse exact user-visible used-phone requirements.

    This intentionally does not map vague descriptions such as ``成色好`` or
    use cases such as ``学生备用机`` to controlled facts.  Those phrases need
    clarification; turning them into exact catalog predicates would be an
    unsupported model inference.
    """

    text = re.sub(r"\s+", "", message).casefold().rstrip("。.!！?")

    def priority_for(clause: str, phrase: str, clause_key_count: int) -> str:
        soft_forms = (
            phrase + "更好", phrase + "优先", "优先" + phrase,
            phrase + "最好", "更看重" + phrase, "希望" + phrase,
            "偏好" + phrase, phrase + "就更好", phrase + "再加分",
            phrase + "改成优先", phrase + "改为优先", phrase + "降为偏好",
            phrase + "作为偏好", phrase + "偏好保留",
            phrase + "偏好继续保留", phrase + "偏好不变",
        )
        shared_soft_cues = (
            "都是加分项", "都只是加分项", "都只是加分", "都作为偏好",
            "都只作为偏好", "都仅作为偏好",
        )
        coordinated_suffix_soft_cues = (
            "只是加分项", "仅是加分项", "只算加分项", "只是加分",
        )
        single_mention_soft_cues = (
            "只是加分项", "仅是加分项", "只算加分项", "只是加分",
            "只作为偏好", "仅作为偏好", "列为偏好", "算作偏好",
            "只算偏好", "作为偏好", "锦上添花",
        )
        return "soft" if (
            any(form in clause for form in soft_forms)
            or clause.startswith(("优先", "偏好", "希望", "更看重", "尽量"))
            or any(cue in clause for cue in shared_soft_cues)
            or (
                clause_key_count > 1
                and any(cue in clause for cue in coordinated_suffix_soft_cues)
            )
            or (
                clause_key_count == 1
                and (
                    clause.startswith("最好")
                    or any(cue in clause for cue in single_mention_soft_cues)
                )
            )
        ) else "hard"

    specs: list[tuple[str, tuple[str, ...], str]] = [
        (
            "os",
            (
                "ios系统", "ios", "iphone", "苹果手机", "二手苹果手机",
                "苹果二手机", "苹果二手手机", "苹果系统",
            ),
            "ios",
        ),
        (
            "os",
            ("安卓系统", "android系统", "只看安卓二手机", "只看安卓", "安卓"),
            "android",
        ),
        (
            "battery_health",
            (
                "电池健康90%以上", "电池健康90%+", "电池健康九成以上",
                "电池健康要在90%以上", "电池健康必须90%以上",
                "电池健康至少90%以上", "电池90%以上", "电池90%+",
                "电池必须90%以上", "电池至少90%以上",
                "电池九成以上", "电池至少九成", "电池九成起",
                "九成以上电池", "九成起电池",
            ),
            "90_plus",
        ),
        ("battery_health", ("电池健康80%到90%", "电池健康80%-90%", "电池健康八成到九成", "电池健康八成至九成", "电池健康在八成到九成", "电池健康在八成至九成", "电池八成到九成", "电池八成至九成"), "80_90"),
        ("battery_health", ("电池健康70%到80%", "电池健康70%-80%"), "70_80"),
        ("screen_originality", ("屏幕要原装", "屏幕得是原装", "屏幕必须是原装", "原装屏", "原厂屏"), "original"),
        ("screen_originality", ("非原装屏", "换过屏", "更换过屏幕"), "non_original"),
        ("battery_originality", ("原装电池", "原厂电池", "电池也是原装"), "original"),
        ("battery_originality", ("非原装电池", "电池不是原装", "非原厂电池"), "non_original"),
        (
            "motherboard_repair",
            (
                "主板不能修过", "主板没修过", "主板未维修",
                "主板必须没修过", "主板从来没维修过", "未修主板",
                "不能有主板维修记录", "主板不能有维修记录", "主板无维修",
                "主板不能维修过",
            ),
            "not_repaired",
        ),
        ("motherboard_repair", ("主板修过", "主板有过维修", "主板必须修过", "主板有维修记录"), "repaired"),
        ("scratch_level", ("没有划痕", "无划痕", "没划痕", "一点划痕都没有"), "none"),
        ("scratch_level", ("只有轻微划痕", "轻微划痕"), "light"),
        ("shell_condition", ("外壳正常", "外壳要正常", "机壳正常", "机壳状态正常"), "normal"),
        ("shell_condition", ("外壳有磕碰或缺失", "外壳磕碰或缺失"), "damaged"),
    ]
    matches: dict[str, list[tuple[str, str, str]]] = {}
    for clause in _used_phone_clauses(message):
        if _is_explicit_used_phone_exclusion_clause(clause):
            continue
        nonbinding_platform_keys = (
            _nonbinding_used_phone_platform_acceptance_mentions(clause)
        )
        for key, phrases, value in specs:
            if key in nonbinding_platform_keys:
                continue
            matched_phrase = next(
                (phrase for phrase in phrases if phrase in clause), None
            )
            if matched_phrase is not None:
                if key in {"scratch_level", "shell_condition"} and any(
                    cue in clause for cue in ("可以接受", "能接受", "没关系", "不介意")
                ):
                    continue
                matches.setdefault(key, []).append((clause, matched_phrase, value))

    clause_key_counts = {
        clause: len({key for key, values in matches.items() for value in values if value[0] == clause})
        for values in matches.values() for clause, _phrase, _value in values
    }

    result: dict[str, ShoppingRequirement] = {}
    for key, values in matches.items():
        # A controlled phrase may contain another valid phrase, e.g.
        # ``非原装屏`` contains ``原装屏``.  Keep the longest surface within
        # each clause; independent values in separate clauses remain a real
        # contradiction and therefore fail closed below.
        longest_values = [
            item for item in values
            if not any(
                item[0] == other[0]
                and item[1] != other[1]
                and item[1] in other[1]
                for other in values
            )
        ]
        distinct = {value for _clause, _phrase, value in longest_values}
        if len(distinct) != 1:
            replacement_cues = ("改成", "改为", "换成", "换为")
            replacement_values = []
            for item in longest_values:
                clause_text, surface, _value = item
                cue_positions = [
                    clause_text.rfind(cue)
                    for cue in replacement_cues
                    if cue in clause_text
                ]
                if cue_positions and clause_text.rfind(surface) > max(cue_positions):
                    replacement_values.append(item)
            replacement_distinct = {
                value for _clause, _phrase, value in replacement_values
            }
            if len(replacement_distinct) == 1:
                longest_values = replacement_values
                distinct = replacement_distinct
        # "苹果还是安卓还没想好" is not an exact OS constraint.  In general,
        # contradictory exact values are left to clarification instead of
        # being converted into a permissive IN predicate.
        if len(distinct) != 1:
            continue
        clause, phrase, value = longest_values[-1]
        if key == "os" and any(
            cue in text for cue in ("还没想好", "没想好", "尚未决定", "未决定")
        ):
            continue
        priority = priority_for(clause, phrase, clause_key_counts.get(clause, 1))
        result[key] = ShoppingRequirement(
            key=key,
            operator="eq",
            value=value,
            unit="enum",
            priority=priority,
            source="user",
        )
    if "brand" not in result and any(
        phrase in text
        for phrase in ("国产手机", "国产二手手机", "国产品牌手机", "国产机")
    ):
        result["brand"] = ShoppingRequirement(
            key="brand",
            operator="in",
            value=list(DOMESTIC_PHONE_BRANDS),
            unit="text",
            priority="hard",
            source="user",
        )
    battery_quality_direction = any(
        phrase in text
        for phrase in (
            "电池质量要好", "电池质量好一点",
            "电池健康要好", "电池健康好一点",
        )
    )
    endurance_direction = any(
        phrase in text
        for phrase in (
            "续航好", "续航优先", "续航长", "长续航优先",
        )
    )
    if (
        "battery_health" not in result
        and (battery_quality_direction or endurance_direction)
    ):
        # This wording expresses direction, not an exact threshold. Preserve it
        # as a soft preference over the two healthy catalog bands; never turn
        # the inference into a hard filter or claim an exact battery percentage.
        result["battery_health"] = ShoppingRequirement(
            key="battery_health",
            operator="in",
            value=["90_plus", "80_90"],
            unit="enum",
            priority="soft",
            source=(
                "inferred: 续航好映射为较高电池健康度偏好"
                if endurance_direction
                else "inferred: 电池质量好映射为较高电池健康度偏好"
            ),
        )
    joint_battery_screen_quality = bool(re.search(
        r"(?:电池(?:和|与|、)?屏幕|屏幕(?:和|与|、)?电池|电池屏幕)"
        r".{0,8}(?:尽量|最好|要).{0,3}好",
        text,
    ))
    if joint_battery_screen_quality and "battery_health" not in result:
        result["battery_health"] = ShoppingRequirement(
            key="battery_health",
            operator="in",
            value=["90_plus", "80_90"],
            unit="enum",
            priority="soft",
            source="inferred: 电池好映射为较高电池健康度偏好",
        )
    if (
        joint_battery_screen_quality
        or re.search(r"屏幕.{0,6}(?:尽量|最好|要).{0,3}好", text)
    ) and "screen_originality" not in result:
        result["screen_originality"] = ShoppingRequirement(
            key="screen_originality",
            operator="eq",
            value="original",
            unit="enum",
            priority="soft",
            source="inferred: 屏幕好映射为原装屏偏好",
        )
    undecided_platform = any(
        cue in text for cue in ("还没想好", "没想好", "尚未决定", "未决定")
    ) and any(cue in text for cue in ("安卓", "android"))
    platform_comparison_pattern = (
        r"(?:(?:苹果|apple|iphone).*(?:或者|或是|还是|和|与|、|/|或).*"
        r"(?:安卓|android)|(?:安卓|android).*(?:或者|或是|还是|和|与|、|/|或).*"
        r"(?:苹果|apple|iphone))"
    )
    brand_negations = parse_brand_negations(message)
    result_observation_brands = _nonbinding_used_phone_result_brand_mentions(
        message
    )
    explicit_brands: list[str] = []
    for clause in _used_phone_clauses(message):
        if re.search(platform_comparison_pattern, clause):
            continue
        for brand in detect_product_brands(clause):
            if (
                brand not in brand_negations.guarded_brands
                and brand not in result_observation_brands
                and brand not in explicit_brands
            ):
                explicit_brands.append(brand)
    if not undecided_platform and len(explicit_brands) == 1:
        brand = explicit_brands[0]
        aliases = product_brand_aliases(brand)
        brand_is_soft = any(
            any(
                pattern in text
                for pattern in (
                    alias + "优先",
                    "优先" + alias,
                    "偏好" + alias,
                    "更喜欢" + alias,
                    alias + "最好",
                )
            )
            for alias in aliases
        )
        result["brand"] = ShoppingRequirement(
            key="brand",
            operator="eq",
            value=brand,
            unit="text",
            priority="soft" if brand_is_soft else "hard",
            source="user",
        )
    price_ceiling = _explicit_phone_price_ceiling(message)
    if price_ceiling is not None:
        result["price_minor"] = ShoppingRequirement(
            key="price_minor",
            operator="lte",
            value=price_ceiling,
            unit="CNY_MINOR",
            priority="hard",
            source="user",
        )
    return result


def _used_phone_controlled_mentions(message: str) -> set[str]:
    """Detect broad controlled-field mentions for deterministic coverage checks."""

    normalized = re.sub(r"\s+", "", message).casefold()
    cues: dict[str, tuple[str, ...]] = {
        "price_minor": ("预算", "价格", "价位"),
        "os": (
            "ios", "iphone", "安卓", "android", "苹果手机", "苹果系统",
        ),
        "battery_health": (
            "电池健康", "电池质量", "电池要好", "电池好", "电池九成",
            "电池八成", "九成以上电池", "九成起电池",
        ),
        "screen_originality": (
            "原装屏", "原厂屏", "非原装屏", "换过屏", "屏幕要", "屏幕得", "屏幕必须",
            "屏幕要求", "屏幕不要", "屏幕不能", "屏幕不可以", "屏不要", "屏不能",
            "原装的屏幕", "非原装的屏幕", "屏幕原装的", "屏幕非原装的",
        ),
        "motherboard_repair": ("主板",),
        "battery_originality": ("原装电池", "原厂电池", "非原装电池", "非原厂电池", "电池不是原装", "电池也是原装"),
        "scratch_level": ("划痕",),
        "shell_condition": ("外壳", "机壳"),
    }
    return {
        key for key, phrases in cues.items()
        if any(phrase in normalized for phrase in phrases)
    }


_INVALIDATED_SCOPE_REFERENCE_CUES = (
    "最开始那两个",
    "最开始的两个",
    "之前那两个",
    "原来那两个",
    "先前那两个",
)


def _references_invalidated_candidate_scope(state: TaskState, message: str) -> bool:
    """Recognize an old-pair reference before TaskState falls back to a model."""

    normalized = re.sub(r"\s+", "", message).casefold()
    if not any(cue in normalized for cue in _INVALIDATED_SCOPE_REFERENCE_CUES):
        return False
    invalidation = state.domain_state.get("candidateScopeInvalidation")
    return bool(
        isinstance(invalidation, dict)
        and invalidation.get("status") == "invalidated"
        and isinstance(invalidation.get("scopeId"), str)
        and invalidation.get("replacedByScopeId") != invalidation.get("scopeId")
    )


def _unsupported_used_phone_capability(
    message: str,
) -> tuple[str, str, str] | None:
    """Return a deterministic boundary for absent or undefined evidence."""

    normalized = re.sub(r"\s+", "", message).casefold()
    if any(cue in normalized for cue in ("销量最高", "销量排行", "最畅销", "卖得最多")):
        return (
            "unsupported_sales_ranking",
            "当前冻结商品快照不含销量字段，无法可靠判断销量最高或最畅销。",
            "当前数据没有销量字段，不能判断销量最高；你可以改按品牌、预算、电池健康、屏幕/电池是否原装、主板维修或外观条件筛选。",
        )
    if any(cue in normalized for cue in ("最新版", "最新款", "最近发布", "刚发布")):
        return (
            "unsupported_recency",
            "当前冻结商品快照无法验证发布时间或最新款身份。",
            "当前数据不能可靠判断最新版或最新款；你可以指定品牌、预算或受控质量条件，我再按可验证字段筛选。",
        )
    capability_mentions = (
        "打游戏", "大型游戏", "游戏性能", "游戏体验", "帧率", "发热", "散热",
        "拍照", "相机", "摄影",
    )
    # Broad discovery such as “有没有适合打游戏的手机” is allowed to
    # retrieve seller-title claims.  Only comparative/performance conclusions
    # are stopped here because the frozen catalog lacks trustworthy benchmarks.
    capability_evaluation_cues = (
        "哪个", "哪款", "哪个好", "更好", "最好", "最强", "对比", "比较",
        "帧率", "发热", "跑分", "画质", "实际性能", "真实体验",
    )
    if (
        any(cue in normalized for cue in capability_mentions)
        and any(cue in normalized for cue in capability_evaluation_cues)
    ):
        return (
            "unsupported_game_camera_evidence",
            "当前冻结商品快照不含可验证的芯片性能或相机指标。",
            "当前数据没有芯片性能和相机指标，不能可靠判断哪款更适合打游戏或拍照；我可以继续按已验证的系统、电池、屏幕、主板和外观条件比较。",
        )
    if (
        any(cue in normalized for cue in ("优质", "品质好", "质量好", "好一点"))
        and not _explicit_used_phone_requirements(message)
        # A quality adjective is non-blocking when the same turn already gives
        # a broad retrieval objective.  A standalone “质量好一点” remains
        # ambiguous and continues to fail closed.
        and not any(cue in normalized for cue in (
            "性价比", "高中生", "学生", "日常用", "平时用", "便宜",
        ))
    ):
        return (
            "ambiguous_quality_request",
            "“优质”尚未映射到明确的二手手机受控条件。",
            "“优质”可能指电池、原装屏、未维修主板或外观状态，我不会擅自替你展开；请至少指定一项你最看重的条件。",
        )
    return None


def _broad_used_phone_discovery(message: str) -> bool:
    """Allow exploratory shopping turns to retrieve before over-clarifying."""

    normalized = re.sub(
        r"\s+",
        "",
        _broad_used_phone_retrieval_goal(message),
    ).casefold()
    return any(cue in normalized for cue in (
        "推荐", "先看看", "看看", "有吗", "有没有", "适合", "性价比",
        "高中生", "学生", "日常用", "平时用", "续航", "便宜",
        "质量好", "品质好", "其他方面都可以", "其他主要方面都可以",
    ))


def _broad_used_phone_retrieval_goal(message: str) -> str:
    """Remove explicitly de-prioritized capabilities from the search query."""

    cleaned = message
    for pattern in (
        r"(?:也)?(?:不怎么|不太|基本不|很少)(?:打)?游戏",
        r"(?:也)?不(?:要求|需要|看重|在意)(?:拍照|相机|摄影)",
    ):
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[，,；;、]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or message


def _used_phone_text_claim_discovery(message: str) -> str | None:
    """Identify title/text recall without treating marketing text as a fact."""

    normalized = re.sub(
        r"\s+",
        "",
        _broad_used_phone_retrieval_goal(message),
    ).casefold()
    if _unsupported_used_phone_capability(message) is not None:
        return None
    if any(cue in normalized for cue in (
        "打游戏", "游戏", "电竞", "竞技", "吃鸡", "和平精英", "王者荣耀", "手游",
    )):
        return "gaming_title_claim"
    if any(cue in normalized for cue in ("拍照", "相机", "摄影")):
        return "camera_title_claim"
    return None


def _trusted_validator_presentation_ids(state: TaskState) -> list[int]:
    """Read ordered IDs only through the strict Validator publication boundary."""

    published = build_validated_guide_result(state)
    products = published.get("products") if isinstance(published, dict) else None
    if isinstance(products, list):
        product_ids: list[int] = []
        for item in products:
            product = item.get("product") if isinstance(item, dict) else None
            product_id = product.get("id") if isinstance(product, dict) else None
            if type(product_id) is int:
                trusted_product_id = product_id
            elif (
                isinstance(product_id, str)
                and re.fullmatch(r"[1-9]\d*", product_id) is not None
            ):
                # Browser-facing guide IDs are strings to preserve values above
                # JavaScript's 2**53 precision boundary.  Convert only canonical
                # decimal identities back at this server-owned comparison edge.
                trusted_product_id = int(product_id)
            else:
                return []
            if trusted_product_id in product_ids:
                return []
            product_ids.append(trusted_product_id)
        return product_ids

    # A successful comparison overwrites the one-slot Validator receipt while
    # retaining the same active CandidateScope.  On the next user turn the
    # terminal Plan may already be retired, so reconstruct only the ordered IDs
    # from the exact comparison receipt.  Every task/plan/step identity and both
    # finalist lists must agree with the still-current server-owned scope.
    raw_scope = state.domain_state.get("candidateScope")
    raw_guide = state.domain_state.get("shoppingGuide")
    raw_validation = state.domain_state.get("validationResult")
    raw_outputs = state.domain_state.get("stepOutputs")
    if not all(isinstance(item, dict) for item in (
        raw_scope, raw_guide, raw_validation, raw_outputs,
    )):
        return []
    try:
        scope = CandidateScope.model_validate(raw_scope)
        guide = ShoppingGuideState.model_validate(raw_guide)
    except ValueError:
        return []
    if (
        scope.task_id != state.task_id
        or scope.status != "active"
        or scope.category != guide.category
        or list(scope.requirements_snapshot) != list(compiled_shopping_requirements(guide))
        or list(scope.brand_avoidances_snapshot) != list(guide.brand_avoidances)
        or raw_validation.get("outcome") != "passed"
        or raw_validation.get("taskId") != state.task_id
    ):
        return []
    plan_id = raw_validation.get("planId")
    step_results = raw_validation.get("stepResults")
    if not isinstance(plan_id, str) or not isinstance(step_results, list) or len(step_results) != 1:
        return []
    step_result = step_results[0]
    if not isinstance(step_result, dict) or step_result.get("outcome") != "satisfied":
        return []
    expected = step_result.get("expectedOutput")
    summary = step_result.get("evidenceSummary")
    guide_summary = summary.get("requiresGuideDecision") if isinstance(summary, dict) else None
    step_id = step_result.get("stepId")
    output = raw_outputs.get(step_id) if isinstance(step_id, str) else None
    values = output.get("values") if isinstance(output, dict) else None
    product_ids = values.get("productIds") if isinstance(values, dict) else None
    finalists = guide_summary.get("finalistIds") if isinstance(guide_summary, dict) else None
    if (
        expected != {"requiresGuideDecision": True}
        or not isinstance(output, dict)
        or output.get("taskId") != state.task_id
        or output.get("planId") != plan_id
        or output.get("stepId") != step_id
        or not isinstance(product_ids, list)
        or product_ids != finalists
        or product_ids != list(scope.visible_product_ids)
        or len(product_ids) not in {2, 3}
        or any(type(item) is not int or item <= 0 for item in product_ids)
        or len(set(product_ids)) != len(product_ids)
    ):
        return []
    return list(product_ids)


def _used_phone_presentation_only_request(message: str) -> bool:
    """Recognize display-only controls that cannot change shopping meaning."""

    normalized = re.sub(r"\s+", "", message).casefold()
    return any(cue in normalized for cue in (
        "只展示前三个",
        "只展示前三款",
        "展示前三个",
        "展示前三款",
        "不用把20个都详细",
        "不用把二十个都详细",
    ))


def _ordinal_comparison_binding(
    state: TaskState,
    message: str,
    reference_context: ResolvedReferenceContext | None = None,
) -> tuple[str, list[int] | None]:
    """Resolve explicit display ordinals, or request clarification fail-closed."""

    normalized = re.sub(r"\s+", "", message).casefold()
    deictic_visible_choice = any(cue in normalized for cue in (
        "这里面那个", "这里面哪个", "这里面哪一个", "这里面哪款",
        "这些里面那个", "这些里面哪个", "这其中那个", "这其中哪个",
    ))
    previous_batch_surface = any(cue in normalized for cue in (
        "上一批", "上批", "前一批",
    ))
    if (
        not deictic_visible_choice
        and not previous_batch_surface
        and not any(cue in normalized for cue in _COMPARISON_INTENT_CUES)
    ):
        return "none", None
    ordinal_surface = any(cue in normalized for cue in (
        "第一个", "第一款", "第二个", "第二款", "第三个",
        "第三款", "第四个", "第四款", "前两个", "前两款", "这两个",
        "这两款", "前三个", "前三款", "这三个", "这三款", "这3个", "这3款",
        "这个", "这款", "另一个", "另一款", "选中的", "选中这款",
        "上一批", "上批", "前一批",
    ))
    if not ordinal_surface and not deictic_visible_choice:
        return "none", None
    if reference_context is not None:
        if (
            reference_context.task_id != state.task_id
            or reference_context.task_revision != state.revision
        ):
            return "missing_or_out_of_range", None
        trusted_ids = list(reference_context.presentation_ids)
    else:
        trusted_ids = _trusted_validator_presentation_ids(state)
    if previous_batch_surface:
        if reference_context is None or not reference_context.previous_batch_product_ids:
            return "missing_or_out_of_range", None
        trusted_ids = list(reference_context.previous_batch_product_ids)

    ordinal_indices: list[int] = []
    for index, aliases in enumerate((
        ("第一个", "第一款"),
        ("第二个", "第二款"),
        ("第三个", "第三款"),
        ("第四个", "第四款"),
    )):
        if any(alias in normalized for alias in aliases):
            ordinal_indices.append(index)
    if len(ordinal_indices) >= 2:
        if max(ordinal_indices) >= len(trusted_ids):
            return "missing_or_out_of_range", None
        selected = [trusted_ids[index] for index in ordinal_indices]
        if len(selected) not in {2, 3}:
            return "ambiguous", None
        return "bound", selected

    focused_surface = any(cue in normalized for cue in (
        "这个", "这款", "当前这个", "当前这款", "选中的", "选中这款",
    ))
    other_surface = any(cue in normalized for cue in ("另一个", "另一款"))
    focused_id = (
        reference_context.focused_product_id
        if reference_context is not None
        else None
    )
    if focused_surface and focused_id is not None:
        selected = [focused_id]
        if ordinal_indices:
            index = ordinal_indices[0]
            if index >= len(trusted_ids):
                return "missing_or_out_of_range", None
            if trusted_ids[index] not in selected:
                selected.append(trusted_ids[index])
        if other_surface:
            comparison_ids = (
                list(reference_context.compared_product_ids)
                if reference_context is not None
                else []
            )
            source_ids = (
                comparison_ids
                if len(comparison_ids) == 2 and focused_id in comparison_ids
                else trusted_ids
                if len(trusted_ids) == 2
                else []
            )
            selected.extend(item for item in source_ids if item not in selected)
        if len(selected) in {2, 3}:
            return "bound", selected
        return "ambiguous", None
    explicit_first_two = (
        any(cue in normalized for cue in ("第一个", "第一款"))
        and any(cue in normalized for cue in ("第二个", "第二款"))
    ) or any(cue in normalized for cue in ("前两个", "前两款"))
    explicit_first_three = (
        (
            any(cue in normalized for cue in ("第一个", "第一款"))
            and any(cue in normalized for cue in ("第二个", "第二款"))
            and any(cue in normalized for cue in ("第三个", "第三款"))
        )
        or any(cue in normalized for cue in ("前三个", "前三款"))
    )
    if explicit_first_three:
        if len(trusted_ids) < 3:
            return "missing_or_out_of_range", None
        return "bound", trusted_ids[:3]
    if explicit_first_two:
        if len(trusted_ids) < 2:
            return "missing_or_out_of_range", None
        return "bound", trusted_ids[:2]
    if deictic_visible_choice:
        if len(trusted_ids) < 2:
            return "missing_or_out_of_range", None
        # The normal browser surface publishes three Validator-owned decision
        # cards.  A natural “这里面哪个” refers to those visible cards, so let
        # the comparison tool and final LLM judge them instead of launching a
        # new full-catalog search.
        return "bound", trusted_ids[:3]
    if any(cue in normalized for cue in ("这两个", "这两款")):
        if len(trusted_ids) == 2:
            return "bound", trusted_ids
        return "ambiguous", None
    if any(cue in normalized for cue in ("这三个", "这三款", "这3个", "这3款")):
        if len(trusted_ids) == 3:
            return "bound", trusted_ids
        return "ambiguous", None
    return "ambiguous", None


def _requests_compound_first_two_comparison(message: str) -> bool:
    normalized = re.sub(r"\s+", "", message).casefold()
    return bool(re.search(
        r"(?:再|然后|随后|接着|并且?|同时)(?:比较|对比)"
        r"(?:检索|搜索|结果|出来的)?(?:前两个|前两款)",
        normalized,
    ))


def _bound_comparison_guide_patch(
    state: TaskState,
    expected_ids: list[int],
) -> dict[str, Any]:
    """Revalidate ordinal IDs against the current revision before persistence."""

    trusted_ids = _trusted_validator_presentation_ids(state)
    if (
        len(expected_ids) not in {2, 3}
        or any(type(item) is not int or item <= 0 for item in expected_ids)
        or len(set(expected_ids)) != len(expected_ids)
        or any(item not in trusted_ids for item in expected_ids)
    ):
        raise ValueError("ordinal comparison no longer matches Validator presentation")
    raw_guide = state.domain_state.get("shoppingGuide")
    guide = ShoppingGuideState.model_validate(raw_guide)
    return {
        "shoppingGuide": guide.model_copy(update={
            "mode": "compare",
            "candidate_ids": trusted_ids,
            "compared_ids": list(expected_ids),
        }).model_dump(by_alias=True, mode="json")
    }


def _server_owned_use_case_guide_patch(
    state: TaskState,
    use_case: str,
    message: str,
) -> dict[str, Any]:
    """Persist a deterministic retrieval intent without granting model write access."""

    raw_guide = state.domain_state.get("shoppingGuide")
    guide = (
        ShoppingGuideState.model_validate(raw_guide)
        if isinstance(raw_guide, dict)
        else ShoppingGuideState(mode="recommend", category="phone")
    )
    guide = _canonicalize_explicit_used_phone_semantics(state, message, guide) or guide
    guide = _canonicalize_explicit_used_phone_negation(message, guide) or guide
    use_cases = list(guide.use_cases)
    if use_case not in use_cases:
        use_cases.append(use_case)
    return {
        "shoppingGuide": guide.model_copy(update={
            "use_cases": use_cases,
        }).model_dump(by_alias=True, mode="json")
    }


def _nonbinding_used_phone_acceptance_mentions(message: str) -> set[str]:
    result = _nonbinding_used_phone_platform_acceptance_mentions(message)
    for clause in _used_phone_clauses(message):
        if not any(cue in clause for cue in ("可以接受", "能接受", "没关系", "不介意")):
            continue
        if "划痕" in clause:
            result.add("scratch_level")
        if "外壳" in clause or "机壳" in clause:
            result.add("shell_condition")
    return result


def _validate_used_phone_requirement_compatibility(
    guide_state: ShoppingGuideState | None,
) -> None:
    """Reject impossible hard brand/OS conjunctions before persistence."""

    if guide_state is None or guide_state.category != "phone":
        return
    by_key = {
        item.key: item
        for item in guide_state.requirements
        if item.priority == "hard" and item.operator in {"eq", "in"}
    }
    brand_requirement = by_key.get("brand")
    os_requirement = by_key.get("os")
    if brand_requirement is None or os_requirement is None:
        return

    raw_brands = (
        brand_requirement.value
        if isinstance(brand_requirement.value, list)
        else [brand_requirement.value]
    )
    raw_systems = (
        os_requirement.value
        if isinstance(os_requirement.value, list)
        else [os_requirement.value]
    )
    brands = {canonicalize_brand(str(value)) for value in raw_brands}
    systems = {str(value).casefold() for value in raw_systems}
    compatible = any(
        (system == "ios" and brand == "apple")
        or (system == "android" and brand != "apple")
        for brand in brands
        for system in systems
    )
    if not compatible:
        raise TaskStatePayloadValidationError(
            "hard phone brand and operating-system requirements are incompatible",
            code="incompatible_phone_brand_os_requirements",
            field_path="domainStatePatch.shoppingGuide.requirements",
        )


def _nonbinding_used_phone_ambiguous_mentions(message: str) -> set[str]:
    """Account for fields the user explicitly leaves undecided this turn."""

    normalized = re.sub(r"\s+", "", message).casefold()
    undecided_cues = ("还没想好", "没想好", "尚未决定", "未决定")
    if any(cue in normalized for cue in undecided_cues) and any(
        cue in normalized for cue in ("ios", "安卓", "android", "系统")
    ):
        return {"os"}
    return set()


def _canonicalize_explicit_used_phone_semantics(
    state: TaskState,
    message: str,
    guide_state: ShoppingGuideState | None,
) -> ShoppingGuideState | None:
    """Bind exact seven-field query semantics to the current user utterance."""

    if guide_state is None or guide_state.category != "phone":
        return guide_state
    explicit = _explicit_used_phone_requirements(message)
    existing_raw = state.domain_state.get("shoppingGuide")
    try:
        existing = (
            ShoppingGuideState.model_validate(existing_raw)
            if isinstance(existing_raw, dict)
            else None
        )
    except ValueError:
        existing = None
    if guide_state.mode == "compare":
        # A comparison dimension is not a preference. Rebuild this lane only
        # from previously accepted requirements, rather than turning current
        # comparison wording into a new filter or trusting model-added values
        # such as ``inferred:dimension``. ``compare_products`` already returns
        # all seven controlled fields, including unknown/conflict.
        requirements = {
            item.key: item for item in (existing.requirements if existing else [])
        }
        for key in _explicit_used_phone_requirement_removals(message):
            requirements.pop(key, None)
        return guide_state.model_copy(
            update={"requirements": list(requirements.values())}
        )
    first_unanchored_turn = (
        existing is None
        or (
            not existing.requirements
            and not existing.brand_avoidances
            and not existing.candidate_ids
            and not existing.compared_ids
        )
    )
    normalized_message = re.sub(r"\s+", "", message).casefold()
    vague_phone_cues = (
        "成色好", "靠谱", "耐用", "备用机", "日常用", "给家里人",
        "reliable", "durable", "backup phone",
    )
    enforce_user_text_binding = (
        not explicit
        and any(cue in normalized_message for cue in vague_phone_cues)
    )
    requirements = {
        item.key: item
        for item in guide_state.requirements
        if not (
            first_unanchored_turn
            and enforce_user_text_binding
            and item.key in USED_PHONE_ATTRIBUTE_REGISTRY
        )
    }
    requirements.update(explicit)
    for key in _explicit_used_phone_requirement_removals(message):
        requirements.pop(key, None)
    return guide_state.model_copy(update={"requirements": list(requirements.values())})


_SCOPE_RERANK_REFERENCE_CUES = (
    "这其中", "这些里面", "这里面", "这些中", "这一批", "刚才推荐",
    "刚才那批", "上一轮", "上轮", "刚刚那些", "刚刚推荐",
)
_SCOPE_RERANK_ORDER_CUES = (
    "哪一个", "哪一款", "哪一台", "哪一部", "哪个", "哪款", "哪部",
    "哪几个", "哪几款", "谁",
)
_SCOPE_RERANK_SUPERLATIVE_CUES = (
    "最好", "最强", "更好", "更强", "更佳",
)
# The in-scope rerank reorders public title/text claims only.  Anything that
# asks for measured capability (benchmarks, real photos, thermals, real-world
# experience) is explicitly excluded and must fall back to the capability
# boundary — title relevance is never a proxy for real camera/gaming evidence.
_SCOPE_RERANK_EXCLUDED_MEASURED_CUES = (
    "跑分", "帧率", "实拍", "画质", "实际性能", "真实性能", "真实体验",
    "发热", "散热", "续航实测", "电池实测", "实测",
)
_SCOPE_RERANK_CAMERA_CUES = ("拍照", "相机", "摄影", "摄像头", "像素")
_SCOPE_RERANK_GAMING_CUES = ("打游戏", "游戏", "电竞", "高刷")
_VALIDATED_SCOPE_ANSWER_CUES = (
    "根据你确实知道的属性",
    "根据已有属性",
    "根据已知属性",
)

_EVIDENCE_RESEARCH_GAP_CUES_V2: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "gaming_performance",
        ("打游戏", "游戏表现", "游戏性能", "游戏流畅", "帧率", "电竞"),
    ),
    (
        "thermal_performance",
        ("散热", "发热", "温控", "机身温度"),
    ),
    (
        "camera_quality",
        ("相机表现", "相机效果", "拍照表现", "摄影表现", "影像表现", "画质", "实拍"),
    ),
)


def _scope_rerank_reference_present(message: str) -> bool:
    """Detect a deictic reference to the previous candidate batch."""
    normalized = re.sub(r"\s+", "", message).casefold()
    return any(cue in normalized for cue in _SCOPE_RERANK_REFERENCE_CUES)


def _requested_evidence_research_gap_keys_v2(message: str) -> tuple[str, ...]:
    """Map explicit user wording to the fixed child-Agent evidence vocabulary."""

    normalized = re.sub(r"\s+", "", message).casefold()
    return tuple(
        key
        for key, cues in _EVIDENCE_RESEARCH_GAP_CUES_V2
        if any(cue in normalized for cue in cues)
    )


def _explicit_evidence_research_intent_v2(message: str) -> bool:
    """Require an explicit verification/unknown contract, not mere comparison."""

    normalized = re.sub(r"\s+", "", message).casefold()
    return any(cue in normalized for cue in (
        "核验", "验证", "可验证", "证据", "没有证据", "无证据",
        "标为未知", "明确未知", "不知道就说不知道", "无法核实",
    ))


def _active_scope_research_gap_keys_v2(
    state: TaskState,
    message: str,
) -> tuple[str, ...]:
    """Authorize capability research only over an exact active prior scope."""

    keys = _requested_evidence_research_gap_keys_v2(message)
    if (
        not keys
        or not _scope_rerank_reference_present(message)
        or not _explicit_evidence_research_intent_v2(message)
    ):
        return ()
    raw_scope = state.domain_state.get("candidateScope")
    raw_guide = state.domain_state.get("shoppingGuide")
    if not isinstance(raw_scope, dict) or not isinstance(raw_guide, dict):
        return ()
    try:
        scope = CandidateScope.model_validate(raw_scope)
        guide = ShoppingGuideState.model_validate(raw_guide)
    except ValueError:
        return ()
    if (
        scope.task_id != state.task_id
        or scope.status != "active"
        or scope.source_revision > state.revision
        or guide.category != scope.category
        or list(compiled_shopping_requirements(guide))
        != list(scope.requirements_snapshot)
        or list(guide.brand_avoidances) != list(scope.brand_avoidances_snapshot)
    ):
        return ()
    return keys


def _scope_rerank_title_order_intent(message: str) -> str | None:
    """Return a title-claim ranking intent for an in-scope ordering turn.

    Only an ordering question (“哪个/哪款/谁 … 最好/更强”) over a prior-batch
    reference is eligible.  Intent is grounded in the same public title/text
    vocabulary the rerank tool can actually rank; anything that asks for
    measured capability is excluded and falls back to the capability boundary.
    """
    normalized = re.sub(r"\s+", "", message).casefold()
    if not any(cue in normalized for cue in _SCOPE_RERANK_ORDER_CUES):
        return None
    if not any(cue in normalized for cue in _SCOPE_RERANK_SUPERLATIVE_CUES):
        return None
    if any(cue in normalized for cue in _SCOPE_RERANK_EXCLUDED_MEASURED_CUES):
        return None
    if any(cue in normalized for cue in _SCOPE_RERANK_GAMING_CUES):
        return "gaming_title_claim"
    if any(cue in normalized for cue in _SCOPE_RERANK_CAMERA_CUES):
        return "camera_title_claim"
    return None


def _scope_rerank_plan_request(
    state: TaskState,
    message: str,
) -> dict[str, Any] | None:
    """Build a server-owned one-turn rerank request, or None to fail closed.

    Eligibility requires an active CandidateScope bound to this task whose
    requirement/brand-avoidance snapshots still equal the current guide (base
    conditions unchanged), plus a prior-batch ordering intent whose only intent
    is the title-claim rerank.  The absence of a valid scope — missing,
    invalidated, cross-task, expired or snapshot-drifted — returns None so the
    caller either clarifies or falls back to the existing deterministic paths.
    """
    if state.task_type != "ecommerce_guide":
        return None
    if not _scope_rerank_reference_present(message):
        return None
    intent = _scope_rerank_title_order_intent(message)
    if intent is None:
        return None
    raw_scope = state.domain_state.get("candidateScope")
    if not isinstance(raw_scope, dict):
        return None
    try:
        scope = CandidateScope.model_validate(raw_scope)
    except ValueError:
        return None
    if scope.task_id != state.task_id or scope.status != "active":
        return None
    raw_guide = state.domain_state.get("shoppingGuide")
    if not isinstance(raw_guide, dict):
        return None
    try:
        existing = ShoppingGuideState.model_validate(raw_guide)
    except ValueError:
        return None
    if existing.category != scope.category:
        return None
    if list(compiled_shopping_requirements(existing)) != list(
        scope.requirements_snapshot
    ):
        return None
    if list(existing.brand_avoidances) != list(scope.brand_avoidances_snapshot):
        return None
    return {
        "scopeId": scope.scope_id,
        "rankingIntent": intent,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }


def _server_owned_scope_rerank_request(
    state: TaskState,
    request: dict[str, Any] | None,
    message: str,
) -> dict[str, Any] | None:
    """Re-derive the rerank request against the persisted state before commit.

    The decision layer produced ``request`` from an earlier snapshot; between
    that decision and the write the state may have advanced (OCC).  Re-running
    the exact server-owned eligibility on the authoritative ``latest`` state
    guarantees the persisted request always matches the live scope and guide.
    """
    if request is None:
        return None
    fresh = _scope_rerank_plan_request(state, message)
    if fresh is None or fresh.get("scopeId") != request.get("scopeId"):
        return None
    return fresh


_EXPLICIT_UNSUPPORTED_PRODUCT_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("平板", ("平板", "tablet")),
    ("相机", ("相机", "单反", "微单")),
    ("智能手表", ("智能手表", "运动手表")),
    ("显示器", ("显示器", "电脑屏幕")),
    ("电子书阅读器", ("电子书阅读器", "电纸书", "kindle")),
)


def _explicit_unsupported_product_category(message: str) -> str | None:
    normalized = re.sub(r"\s+", "", message).casefold()
    for label, aliases in _EXPLICIT_UNSUPPORTED_PRODUCT_CATEGORIES:
        matched_aliases = [alias for alias in aliases if alias in normalized]
        if not matched_aliases:
            continue
        # “相机”也可以是手机的待核验能力维度。把“相机表现/效果/质量”
        # 当成购买相机品类会在首轮直接短路，导致手机检索和证据核验均不可达。
        # 只有这些明确的能力表达被排除；“想买相机/推荐微单”等品类意图
        # 仍继续走 unsupported-category 的 fail-closed 边界。
        if label == "相机" and (
            any(
                marker in normalized
                for marker in (
                    "相机表现",
                    "相机效果",
                    "相机能力",
                    "相机质量",
                    "相机素质",
                    "拍照表现",
                    "影像表现",
                )
            )
            or (
                any(token in normalized for token in ("手机", "安卓", "android", "ios", "iphone"))
                and re.search(r"相机(?:怎么样|如何|好不好|强不强)", normalized)
            )
        ):
            continue
        if matched_aliases:
            return label
    return None


def _deterministic_used_phone_task_state_decision(
    state: TaskState,
    message: str,
    reference_context: ResolvedReferenceContext | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Compile exact public used-phone language without an LLM round trip.

    Only the frozen seven-field vocabulary and an already-bound comparison pair
    are eligible.  Vague requests, category inference and missing comparison
    identities still go through the strict model extractor (and may clarify).
    The result is submitted through the same validation/persistence boundary as
    model output; this helper never writes TaskState directly.
    """

    if state.task_type != "ecommerce_guide":
        return None, None
    raw_guide = state.domain_state.get("shoppingGuide")
    try:
        parsed_guide = (
            ShoppingGuideState.model_validate(raw_guide)
            if isinstance(raw_guide, dict)
            else None
        )
    except ValueError:
        return None, None
    unsupported_category = _explicit_unsupported_product_category(message)
    if (
        unsupported_category is not None
        and (parsed_guide is None or parsed_guide.category is None)
    ):
        blocker = f"当前导购尚不支持{unsupported_category}品类"
        return {
            "status": "collecting_information",
            "goal": message,
            "addUnknowns": [blocker],
            "resolveUnknowns": [item for item in state.unknowns if item != blocker],
            "pendingQuestions": [
                "当前导购运行时尚不支持该品类；目前可处理手机、笔记本和耳机。"
                "你希望改为其中哪一类？"
            ],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": None,
                "requirements": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_unsupported_category_boundary",
            reason="unsupported_product_category",
        )
    if not isinstance(raw_guide, dict):
        # A first turn may omit the literal word “phone”, so category routing
        # cannot publish the empty guide yet.  A bounded iOS/Android acceptance
        # pair is nevertheless phone-specific enough to bootstrap the same
        # deterministic contract; this avoids a slow model call that used to
        # invent an unsupported memory requirement for “偶尔打游戏”.
        if not _nonbinding_used_phone_platform_acceptance_mentions(message):
            return None, None
        existing = ShoppingGuideState(mode="recommend", category="phone")
    else:
        existing = parsed_guide
        assert existing is not None
    if existing.category != "phone":
        return None, None

    normalized = re.sub(r"\s+", "", message).casefold()
    if _references_invalidated_candidate_scope(state, message):
        blocker = "引用的商品来自已失效的旧候选范围"
        question = (
            "这两个来自旧筛选范围，可能不满足当前硬条件。"
            "请基于当前候选重新指定序号，或明确提供要比较的商品 ID。"
        )
        return {
            "status": "collecting_information",
            "goal": message,
            "addUnknowns": [blocker],
            "resolveUnknowns": [
                item for item in state.unknowns if item != blocker
            ],
            "pendingQuestions": [question],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in existing.requirements
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_stale_scope_clarification",
            reason="stale_candidate_reference",
        )
    if (
        _used_phone_presentation_only_request(message)
        and _trusted_validator_presentation_ids(state)
    ):
        return {
            "status": "ready",
            "goal": message,
            "resolveUnknowns": list(state.unknowns),
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "mode": existing.mode,
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in existing.requirements
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_presentation_control",
            reason="presentation_only",
        )
    text_claim_discovery = _used_phone_text_claim_discovery(message)
    compound_comparison = _requests_compound_first_two_comparison(message)
    ordinal_status, ordinal_ids = _ordinal_comparison_binding(
        state,
        message,
        reference_context,
    )
    if compound_comparison and not (
        list(reference_context.presentation_ids)
        if reference_context is not None
        else _trusted_validator_presentation_ids(state)
    ):
        ordinal_status, ordinal_ids = "none", None
    comparison = (
        ordinal_status == "bound"
        or (
            any(cue in normalized for cue in _COMPARISON_INTENT_CUES)
            and len(existing.compared_ids) in {2, 3}
            and len(set(existing.compared_ids)) == len(existing.compared_ids)
        )
    )
    substitution = (
        len(existing.candidate_ids) == 1
        and any(cue in normalized for cue in (
            "替代", "替代品", "替代款", "换一款", "换掉当前", "当前这台",
            "同系统", "系统和电池", "系统、电池", "条件相同", "条件不降级",
            "不能放宽", "继续找", "还有没有",
        ))
    )
    explicit = _explicit_used_phone_requirements(message)
    brand_negations = parse_brand_negations(message)
    exclusions = _explicit_used_phone_exclusions(message)
    removals = _explicit_used_phone_requirement_removals(message)
    priority_changes = _explicit_used_phone_priority_changes(message)
    retained = _explicit_used_phone_retained_requirements(message)
    covered_keys = (
        set(explicit) | set(exclusions) | set(removals) | set(priority_changes)
        | retained
        | _nonbinding_used_phone_acceptance_mentions(message)
        | _nonbinding_used_phone_ambiguous_mentions(message)
    ) & (set(USED_PHONE_ATTRIBUTE_REGISTRY) | {"price_minor"})
    mentioned_keys = _used_phone_controlled_mentions(message)
    if (
        "brand" in explicit
        or "brand" in removals
        or brand_negations.targets
        or brand_negations.released_brands
    ):
        mentioned_keys.add("brand")
        covered_keys.add("brand")
    unsupported = _unsupported_used_phone_capability(message)
    scope_research_gap_keys = _active_scope_research_gap_keys_v2(state, message)
    raw_scope = state.domain_state.get("candidateScope")
    validated_scope_answer = bool(
        isinstance(raw_scope, dict)
        and raw_scope.get("taskId") == state.task_id
        and raw_scope.get("status") == "active"
        and any(cue in normalized for cue in _VALIDATED_SCOPE_ANSWER_CUES)
    )
    if (
        brand_negations.unresolved_cues
        and not brand_negations.targets
        and not brand_negations.released_brands
        and not explicit
        and not exclusions
    ):
        blocker = "否决表达没有绑定到明确品牌或商品"
        return {
            "status": "collecting_information",
            "goal": message,
            "addUnknowns": [blocker],
            "resolveUnknowns": [item for item in state.unknowns if item != blocker],
            "pendingQuestions": ["请明确说出你不想要的品牌或商品。"],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json")
                    for item in existing.requirements
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_negation_clarification",
            reason="unbound_negative_target",
            mentioned_keys={"brand"},
            covered_keys=set(),
        )
    if scope_research_gap_keys:
        # This is a read-only follow-up over the exact previous display scope.
        # It preserves every shopping constraint and merely authorizes the
        # bounded EvidenceResearchAgent hook to investigate fixed gap keys.
        return {
            "status": "ready",
            "goal": message,
            "resolveUnknowns": list(state.unknowns),
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "mode": existing.mode,
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in existing.requirements
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_scope_evidence_research",
            reason="active_scope_allowlisted_capability_gaps",
            mentioned_keys=set(scope_research_gap_keys),
            covered_keys=set(scope_research_gap_keys),
        )
    early_capability_boundary = bool(
        unsupported is not None
        and unsupported[0] == "unsupported_game_camera_evidence"
        and (
            any(cue in normalized for cue in _SCOPE_RERANK_EXCLUDED_MEASURED_CUES)
            or not _scope_rerank_reference_present(message)
        )
    )
    if early_capability_boundary:
        reason, blocker, question = unsupported
        target_by_key = {item.key: item for item in existing.requirements}
        target_by_key.update(explicit)
        target = list(target_by_key.values())
        return {
            "status": "collecting_information",
            "goal": message,
            "addUnknowns": [blocker],
            "resolveUnknowns": list(state.unknowns),
            "pendingQuestions": [question],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in target
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_capability_boundary",
            reason=reason,
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )
    if ordinal_status in {"ambiguous", "missing_or_out_of_range"}:
        blocker = "商品序号没有唯一绑定到上一轮 Validator 放行的展示结果"
        question = (
            "我找不到至少两件仍可信的上一轮展示商品，请先完成一次检索再明确说“比较前两个”。"
            if ordinal_status == "missing_or_out_of_range"
            else "请明确说“比较第一个和第二个”或“比较这三个”；单个序号或范围不明确时我不会猜商品 ID。"
        )
        return {
            "status": "collecting_information",
            "goal": message,
            "addUnknowns": [blocker],
            "resolveUnknowns": [item for item in state.unknowns if item != blocker],
            "pendingQuestions": [question],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in existing.requirements
                ],
                "removeRequirementKeys": [],
            }},
        }, {
            **_task_state_extraction_observation(
                route="deterministic_ordinal_clarification",
                reason=ordinal_status,
            ),
        }
    scope_rerank = _scope_rerank_plan_request(state, message)
    scope_order_present = (
        _scope_rerank_reference_present(message)
        and _scope_rerank_title_order_intent(message) is not None
    )
    scope_blocking_conditions = bool(
        comparison or substitution or explicit or exclusions or removals
        or priority_changes or retained or brand_negations.targets
        or brand_negations.released_brands or compound_comparison
    )
    if scope_rerank is not None and not scope_blocking_conditions:
        # Server-owned one-turn rerank over the previous candidate batch.  The
        # base conditions are unchanged by construction (the scope snapshots
        # matched), so the guide patch preserves the existing requirements
        # verbatim and the one-turn request is published separately for the
        # Planner/Executor.  No new full-catalog search happens here.
        guide_patch = {
            "mode": "recommend",
            "category": "phone",
            "upsertRequirements": [
                item.model_dump(mode="json") for item in existing.requirements
            ],
            "removeRequirementKeys": [],
        }
        arguments = {
            "status": "ready",
            "goal": message,
            "resolveUnknowns": list(state.unknowns),
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": guide_patch},
        }
        observation = _task_state_extraction_observation(
            route="deterministic_scope_rerank",
            reason=f"scope_{scope_rerank['rankingIntent']}",
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )
        observation["_scopeRerankRequest"] = scope_rerank
        return arguments, observation
    if scope_order_present and not scope_blocking_conditions and scope_rerank is None:
        # A prior-batch reference + ordering intent without a valid server-owned
        # scope can never reorder anything.  Asking the model to guess product
        # identities or IDs would be the exact failure the scope contract
        # forbids, so this fail-closed turn clarifies instead.
        blocker = "当前没有可引用的上一轮候选"
        return {
            "status": "collecting_information",
            "goal": message,
            "addUnknowns": [blocker],
            "resolveUnknowns": [item for item in state.unknowns if item != blocker],
            "pendingQuestions": ["请先完成一次商品检索，我才能在这一轮候选里帮你排序。"],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in existing.requirements
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_scope_rerank_clarification",
            reason="missing_active_scope",
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )
    if (
        _broad_used_phone_discovery(message)
        and text_claim_discovery is None
        and unsupported is None
        and not comparison
        and not substitution
        and not scope_order_present
        and not explicit
        and not exclusions
        and not removals
        and not priority_changes
        and not retained
        and not brand_negations.targets
        and not brand_negations.released_brands
    ):
        target_by_key = {item.key: item for item in existing.requirements}
        target_by_key.update(explicit)
        target = list(target_by_key.values())
        return {
            "status": "ready",
            "goal": _broad_used_phone_retrieval_goal(message),
            "resolveUnknowns": list(state.unknowns),
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in target
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_exploratory_discovery",
            reason="broad_catalog_discovery",
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )
    if unsupported is not None:
        reason, blocker, question = unsupported
        target_by_key = {item.key: item for item in existing.requirements}
        target_by_key.update(explicit)
        target = list(target_by_key.values())
        return {
            "status": "collecting_information",
            "goal": message,
            "addUnknowns": [blocker],
            "resolveUnknowns": list(state.unknowns),
            "pendingQuestions": [question],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in target
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_capability_boundary",
            reason=reason,
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )
    if text_claim_discovery is not None and not comparison:
        target_by_key = {item.key: item for item in existing.requirements}
        target_by_key.update(explicit)
        target = list(target_by_key.values())
        return {
            "status": "ready",
            "goal": message,
            "resolveUnknowns": list(state.unknowns),
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "recommend",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in target
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_text_claim_discovery",
            reason=text_claim_discovery,
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )
    if validated_scope_answer and not comparison and not explicit:
        return {
            "status": "ready",
            "goal": message,
            "resolveUnknowns": list(state.unknowns),
            "pendingQuestions": [],
            "domainStatePatch": {"shoppingGuide": {
                "mode": "compare",
                "category": "phone",
                "upsertRequirements": [
                    item.model_dump(mode="json") for item in existing.requirements
                ],
                "removeRequirementKeys": [],
            }},
        }, _task_state_extraction_observation(
            route="deterministic_validated_scope_answer",
            reason="validated_scope_answer",
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )
    if not comparison and not substitution and mentioned_keys - covered_keys:
        # A partial deterministic parse is more dangerous than no parse: it
        # makes a truncated TaskState look executable.  Fall back to the
        # bounded extractor unless every controlled mention is accounted for.
        return None, _task_state_extraction_observation(
            route="model_fallback",
            reason="partial_controlled_coverage",
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )
    if (
        not comparison and not substitution and not explicit
        and not exclusions and not removals and not priority_changes and not retained
        and not brand_negations.targets and not brand_negations.released_brands
    ):
        return None, _task_state_extraction_observation(
            route="model_fallback",
            reason="no_deterministic_signal",
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
        )

    if comparison:
        target = list(existing.requirements)
        mode = "compare"
    else:
        mode = "recommend"
        target_by_key = {item.key: item for item in existing.requirements}
        target_by_key.update(explicit)
        for key in removals:
            target_by_key.pop(key, None)
        for key, values in exclusions.items():
            target_by_key[key] = ShoppingRequirement(
                key=key,
                operator="not_in",
                value=values,
                unit="enum",
                priority="hard",
                source="user",
            )
        for key, priority in priority_changes.items():
            current = target_by_key.get(key)
            if current is not None:
                target_by_key[key] = current.model_copy(update={"priority": priority})
        if any(key not in target_by_key for key in retained):
            # A retain reference cannot create a missing value.  Let the
            # bounded extractor clarify instead of inventing one.
            return None, _task_state_extraction_observation(
                route="model_fallback",
                reason="retained_value_missing",
                mentioned_keys=mentioned_keys,
                covered_keys=covered_keys,
            )
        target = list(target_by_key.values())

    target_keys = {item.key for item in target}
    if substitution:
        # A bound substitution deliberately carries the trusted reference
        # product requirements forward.  Report those retained keys as
        # server-covered even when the new turn uses a generic phrase such as
        # "conditions must not be relaxed" instead of repeating each value.
        mentioned_keys |= target_keys
        covered_keys |= target_keys
    guide_patch = {
        "mode": mode,
        "category": "phone",
        "upsertRequirements": [
            item.model_dump(mode="json") for item in target
        ],
        "removeRequirementKeys": [
            item.key for item in existing.requirements
            if item.key not in target_keys
        ],
    }
    arguments = {
        "status": "ready",
        "goal": message,
        "resolveUnknowns": list(state.unknowns),
        "pendingQuestions": [],
        "domainStatePatch": {"shoppingGuide": guide_patch},
    }
    reason = (
        "bound_comparison"
        if comparison
        else "bound_substitution"
        if substitution
        else "complete_controlled_coverage"
    )
    observation = _task_state_extraction_observation(
        route="deterministic_complete",
        reason=reason,
        mentioned_keys=mentioned_keys,
        covered_keys=covered_keys,
    )
    if ordinal_ids is not None:
        observation["_boundComparedIds"] = list(ordinal_ids)
    if compound_comparison and ordinal_ids is None:
        observation["_compoundComparison"] = True
    return arguments, observation


def _deterministic_used_phone_task_state_arguments(
    state: TaskState,
    message: str,
) -> dict[str, Any] | None:
    """Compatibility wrapper returning only the deterministic patch."""

    arguments, _observation = _deterministic_used_phone_task_state_decision(
        state,
        message,
    )
    return arguments


def _require_clarification_for_empty_phone_recommendation(
    state: TaskState,
    payload: dict[str, Any],
    guide_state: ShoppingGuideState | None,
    *,
    message: str,
) -> None:
    """Do not execute a catalog-wide search from an ungrounded vague request."""

    if (
        _used_phone_text_claim_discovery(message) is not None
        or _broad_used_phone_discovery(message)
        or parse_brand_negations(message).released_brands
        or
        guide_state is None
        or guide_state.category != "phone"
        or guide_state.mode != "recommend"
        or guide_state.requirements
        or guide_state.brand_avoidances
        or guide_state.candidate_ids
        or state.unknowns
        or payload.get("addUnknowns")
        or payload.get("pendingQuestions")
    ):
        return
    blocker = "缺少可映射到二手手机受控属性的明确筛选条件"
    payload["addUnknowns"] = [blocker]
    payload["resolveUnknowns"] = [
        item for item in state.unknowns if item != blocker
    ]
    payload["pendingQuestions"] = [
        "请补充一个明确条件，例如系统、电池健康、屏幕/电池是否原装、主板维修或外观状态。"
    ]
    payload["status"] = "collecting_information"


_KNOWN_PHONE_CATEGORY_UNKNOWN_CUES = (
    "品类", "目标类别", "哪类二手", "手机/笔记本/耳机", "手机、笔记本、耳机",
    "category",
)


def _resolve_confirmed_phone_category_unknowns(
    state: TaskState,
    payload: dict[str, Any],
    guide_state: ShoppingGuideState | None,
) -> None:
    """Remove blockers contradicted by the validated controlled category."""

    if guide_state is None or guide_state.category != "phone":
        return

    def is_category_unknown(value: object) -> bool:
        return isinstance(value, str) and any(
            cue in variant
            for variant in _unknown_text_variants(value)
            for cue in _KNOWN_PHONE_CATEGORY_UNKNOWN_CUES
        )

    payload["addUnknowns"] = [
        item for item in payload.get("addUnknowns", [])
        if not is_category_unknown(item)
    ]
    resolved = list(payload.get("resolveUnknowns", []))
    for item in state.unknowns:
        if is_category_unknown(item) and item not in resolved:
            resolved.append(item)
    payload["resolveUnknowns"] = resolved
    if not _effective_unknowns(state, payload):
        payload["pendingQuestions"] = []
        if (
            payload.get("status") == "collecting_information"
            or state.status == "collecting_information"
        ):
            payload["status"] = "ready"


def _resolve_executable_shopping_unknowns(
    state: TaskState,
    payload: dict[str, Any],
    guide_state: ShoppingGuideState | None,
    *,
    require_status: bool,
) -> None:
    """Clear model-created blockers that the next tool call itself resolves.

    Once the server has a validated comparison pair, missing product evidence
    is a precondition for ``compare_products`` rather than a reason to ask the
    user for permission.  Likewise, a recommendation with at least one valid
    requirement can run without speculative brand/budget/model preferences.
    Genuine category, comparison-identity and safety blockers remain intact.
    """

    if (
        not require_status
        or payload.get("status", state.status) != "collecting_information"
        or guide_state is None
        or guide_state.category is None
    ):
        return
    executable_compare = (
        guide_state.mode == "compare"
        and len(guide_state.compared_ids) == 2
        and len(set(guide_state.compared_ids)) == 2
    )
    executable_recommend = (
        guide_state.mode == "recommend"
        and bool(guide_state.requirements or guide_state.brand_avoidances)
    )
    if not (executable_compare or executable_recommend):
        return

    additions = payload.get("addUnknowns", [])
    if not isinstance(additions, list):
        return

    def is_nonblocking(value: object) -> bool:
        if not isinstance(value, str):
            return False
        if _is_blocking_shopping_reference_unknown(value):
            return False
        variants = _unknown_text_variants(value)
        if executable_compare:
            compare_evidence_cues = (
                "证据", "尚未检索", "缺少详情", "无法进行比较", "未读取",
                "evidence", "not retrieved", "missing detail",
            )
            if any(cue in variant for variant in variants for cue in compare_evidence_cues):
                return True
        if executable_recommend:
            return any(
                cue in variant
                for variant in variants
                for cues in _NONBLOCKING_SHOPPING_UNKNOWN_CUES.values()
                for cue in cues
            ) or any(
                cue in variant
                for variant in variants
                for cue in ("其他硬性", "其他要求", "其他条件", "额外要求")
            )
        return False

    payload["addUnknowns"] = [item for item in additions if not is_nonblocking(item)]
    resolved = list(payload.get("resolveUnknowns", []))
    for item in state.unknowns:
        if is_nonblocking(item) and item not in resolved:
            resolved.append(item)
    payload["resolveUnknowns"] = resolved
    if not _effective_unknowns(state, payload):
        payload["pendingQuestions"] = []
        payload["status"] = "ready"


def _server_turn_ledger_patch(state: TaskState, message: str) -> dict[str, Any]:
    turn_count = state.domain_state.get("turnCount", 0)
    if not isinstance(turn_count, int) or isinstance(turn_count, bool):
        turn_count = 0
    return {
        "turnCount": turn_count + 1,
        "lastUserMessage": message,
    }


def _task_state_extraction_observation(
    *,
    route: str,
    reason: str,
    mentioned_keys: set[str] | frozenset[str] | tuple[str, ...] = (),
    covered_keys: set[str] | frozenset[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the minimal server-owned parser decision receipt.

    The receipt intentionally excludes the user message and extracted values.
    It is safe to carry in TaskState snapshots and lets production traces answer
    whether a turn used the deterministic fast path or the bounded model
    extractor, including the exact controlled-field coverage gap.
    """

    mentioned = sorted(set(mentioned_keys))
    covered = sorted(set(covered_keys))
    return {
        "schemaVersion": "used-phone-task-state-extraction-decision-v1",
        "route": route,
        "reason": reason,
        "mentionedKeys": mentioned,
        "coveredKeys": covered,
        "uncoveredKeys": sorted(set(mentioned) - set(covered)),
    }


def _with_extraction_metrics(
    observation: dict[str, Any] | None,
    *,
    execution_kind: str,
    model_call_count: int,
    llm_duration_ms: float,
    repair_used: bool,
) -> dict[str, Any]:
    """Attach server-measured execution facts to the parser receipt."""

    result = dict(observation or _task_state_extraction_observation(
        route="model_fallback",
        reason="outside_deterministic_phone_contract",
    ))
    result.update({
        "executionKind": execution_kind,
        "modelCalled": model_call_count > 0,
        "modelCallCount": model_call_count,
        "llmDurationMs": round(max(llm_duration_ms, 0.0), 2),
        "repairUsed": repair_used,
    })
    return result


def _validate_optional_shopping_questions(
    state: TaskState,
    value: object,
    guide_state: ShoppingGuideState | None,
    *,
    recommend_mode_explicit: bool,
    message: str,
    proposed_goal: str,
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("optionalShoppingQuestions must be an array")
    if not value:
        return []
    # Check the user's mode first: a fabricated recommend-shaped patch must
    # not mask an explicit comparison request.
    for intent_text in (
        f" {message.casefold()} ",
        f" {proposed_goal.casefold()} ",
    ):
        if any(cue in intent_text for cue in _COMPARISON_INTENT_CUES):
            raise ValueError(
                "optional shopping questions require explicit recommendation intent"
            )
    if (
        state.task_type != "ecommerce_guide"
        or guide_state is None
        or not recommend_mode_explicit
        or guide_state.mode != "recommend"
        or guide_state.category is None
        or not (guide_state.requirements or guide_state.brand_avoidances)
    ):
        raise ValueError("optional shopping questions require an executable recommendation")
    # Reject an explicit comparison masquerading as recommend, but do not
    # require a positive recommendation verb: executable negative filters such
    # as "不要非原装屏的二手机" are recommendation requests too.
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind"}:
            raise ValueError("invalid optional shopping question")
        kind = item.get("kind")
        if kind not in _OPTIONAL_SHOPPING_KINDS:
            raise ValueError("unsupported optional shopping question kind")
        if kind not in seen:
            seen.add(kind)
            result.append({
                "kind": kind,
                "question": _OPTIONAL_SHOPPING_QUESTION_TEXT[kind],
            })
    return result


def _reject_nonblocking_recommendation_unknowns(
    state: TaskState,
    payload: dict[str, Any],
    guide_state: ShoppingGuideState | None,
    *,
    require_status: bool,
) -> None:
    """Reject optional shopping preferences submitted as blocking unknowns.

    This is a strict unified-extractor boundary.  It does not rewrite the
    model's state or silently drop questions: a rejected payload remains at
    zero persistence and is returned to the model through the existing single
    structured repair.  Comparison identity, category, safety boundaries and
    unmappable evidence requirements are deliberately outside this allowlist.
    """

    if (
        not require_status
        or state.task_type != "ecommerce_guide"
        or guide_state is None
        or guide_state.mode != "recommend"
        or guide_state.category is None
        or not (guide_state.requirements or guide_state.brand_avoidances)
        or payload.get("status", state.status) != "collecting_information"
    ):
        return
    optional_unknowns: list[tuple[int, str, str]] = []
    for index, value in enumerate(payload.get("addUnknowns", [])):
        if not isinstance(value, str):
            continue
        if _is_blocking_shopping_reference_unknown(value):
            continue
        normalized = value.casefold()
        for kind, cues in _NONBLOCKING_SHOPPING_UNKNOWN_CUES.items():
            if any(cue in normalized for cue in cues):
                optional_unknowns.append((index, value, kind))
                break
    if not optional_unknowns:
        return
    indices = ",".join(str(index) for index, _value, _kind in optional_unknowns)
    kinds = ",".join(sorted({kind for _index, _value, kind in optional_unknowns}))
    raise TaskStatePayloadValidationError(
        "executable recommendation cannot block on optional shopping preferences; "
        f"use ready plus optionalShoppingQuestions for: {kinds}",
        code="nonblocking_shopping_preference_marked_unknown",
        field_path=f"addUnknowns[{indices}]",
    )


def _enforce_task_executability_invariants(
    state: TaskState,
    payload: dict[str, Any],
    *,
    allow_auto_ready: bool = False,
    requested_status: str | None = None,
) -> None:
    """Keep pending questions and executable status consistent with unknowns."""

    effective_unknowns = _effective_unknowns(state, payload)
    proposed_questions = payload.get("pendingQuestions", state.pending_questions)
    if (
        state.status == "collecting_information"
        and allow_auto_ready
        and "status" not in payload
        and proposed_questions == []
        and not effective_unknowns
    ):
        payload["status"] = "ready"
    if effective_unknowns and proposed_questions == []:
        raise TaskStatePayloadValidationError(
            "cannot clear pending questions while unknowns remain",
            code="cannot_clear_pending_while_unknowns_remain",
            field_path="pendingQuestions",
        )
    if requested_status == "collecting_information" and (
        not effective_unknowns or not proposed_questions
    ):
        raise TaskStatePayloadValidationError(
            "collecting_information requires both a blocking unknown and a pending question",
            code="collecting_information_requires_blocker",
            field_path="status",
        )
    if payload.get("status") in {"ready", "executing"} and (
        effective_unknowns or proposed_questions
    ):
        raise TaskStatePayloadValidationError(
            "task cannot become executable with unresolved questions",
            code="task_cannot_become_executable_with_unresolved_questions",
            field_path="status",
        )


async def _notify_task_state(
    callback: TaskStateCallback | None,
    state: TaskState,
    phase: str,
) -> None:
    if callback is not None:
        await callback(state, phase)


async def _persist_task_patch(
    state: TaskState,
    payload: dict[str, Any],
    *,
    phase: str,
    on_task_state: TaskStateCallback | None,
    retry_domain_patch_builder: Callable[[TaskState], dict[str, Any]] | None = None,
    retry_payload_builder: Callable[[TaskState], dict[str, Any]] | None = None,
) -> TaskState:
    """Apply a server-owned revision and retry once if another writer won the CAS."""
    patch_payload = dict(payload)
    patch_payload["expectedRevision"] = state.revision
    patch_payload["actor"] = "agent"
    try:
        patch = TaskStatePatchRequest.model_validate(patch_payload)
        updated = await update_task_state(state.task_id, patch)
    except TaskStateRevisionConflictError:
        retry_domain_patch = patch_payload.get("domainStatePatch")
        if (
            isinstance(retry_domain_patch, dict)
            and retry_domain_patch.get("optionalShoppingQuestions")
        ):
            raise ValueError(
                "optional shopping questions are not replayed after a revision conflict"
            )
        latest = await get_task_state(state.task_id)
        if latest is None:
            raise
        if retry_payload_builder is not None:
            # Semantic shopping updates must be rebuilt in full from the CAS
            # winner. Reusing constraints/status derived from the stale base
            # can silently restore an old budget or candidate range.
            patch_payload = dict(retry_payload_builder(latest))
        if "activePlan" in patch_payload and patch_payload["activePlan"] is None:
            # A new user turn may retire only the same class of terminal Plan.
            # Never erase a Plan that another writer has just made active.
            if (
                latest.active_plan is not None
                and latest.active_plan.status not in {"completed", "failed", "stale"}
            ):
                raise ValueError(
                    "cannot retire a concurrently active Plan"
                )
        if retry_payload_builder is None and retry_domain_patch_builder is not None:
            patch_payload["domainStatePatch"] = retry_domain_patch_builder(latest)
        patch_payload["expectedRevision"] = latest.revision
        patch_payload["status"] = _normalized_task_status(
            latest,
            patch_payload.get("status"),
        )
        if patch_payload["status"] is None:
            patch_payload.pop("status")
        _enforce_task_executability_invariants(
            latest,
            patch_payload,
            allow_auto_ready=False,
        )
        patch = TaskStatePatchRequest.model_validate(patch_payload)
        updated = await update_task_state(latest.task_id, patch)
    await _notify_task_state(on_task_state, updated, phase)
    return updated


def _build_validated_task_state_payload(
    state: TaskState,
    arguments: dict[str, Any],
    *,
    message: str,
    require_status: bool = False,
    allow_auto_ready: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Pure-validate a model ``update_task_state`` payload before any persistence.

    Returns ``(payload, model_domain_patch)``: the server patch with bookkeeping
    fields filled, plus the raw model domain patch used to rebuild after a
    revision conflict. Every rejection here happens before persistence and is
    wrapped into ``TaskStatePayloadValidationError`` so the bounded repair can
    echo a structured reason to the model; this function never writes.
    """
    try:
        payload = _validated_model_task_patch(arguments)
        if require_status and "status" not in payload:
            raise TaskStatePayloadValidationError(
                "update_task_state payload is missing the required status field",
                code="missing_required_status",
                field_path="status",
            )
        _prevalidate_strict_task_state_payload(
            state,
            payload,
            require_status=require_status,
        )
        _validate_model_task_patch_items(payload)
        model_domain_patch = _validated_model_domain_patch(
            state,
            payload.pop("domainStatePatch", _MODEL_DOMAIN_STATE_PATCH_MISSING),
        )
        optional_shopping_value = payload.pop("optionalShoppingQuestions", None)
        requested_status = payload.get("status")
        status = _normalized_task_status(state, requested_status)
        if status is None:
            payload.pop("status", None)
        else:
            payload["status"] = status

        materialized_domain_patch, guide_state = _materialize_model_domain_patch(
            state,
            model_domain_patch,
        )
        constraints_changed = False
        if state.task_type == "ecommerce_guide":
            effective_guide_state = guide_state
            if effective_guide_state is None:
                existing_guide = state.domain_state.get("shoppingGuide")
                if isinstance(existing_guide, dict):
                    try:
                        effective_guide_state = ShoppingGuideState.model_validate(
                            existing_guide
                        )
                    except ValueError:
                        effective_guide_state = None
            guide_state = _canonicalize_explicit_used_phone_semantics(
                state, message, effective_guide_state
            )
            guide_state = _canonicalize_explicit_used_phone_negation(
                message, guide_state
            )
            _validate_used_phone_requirement_compatibility(guide_state)
            if guide_state is not None:
                guide_state, constraints_changed = clear_stale_guide_references(
                    state,
                    guide_state,
                )
            existing_guide_dump = (
                effective_guide_state.model_dump(by_alias=True, mode="json")
                if effective_guide_state is not None else None
            )
            canonical_guide_dump = (
                guide_state.model_dump(by_alias=True, mode="json")
                if guide_state is not None else None
            )
            if guide_state is not None and (
                materialized_domain_patch
                or canonical_guide_dump != existing_guide_dump
            ):
                materialized_domain_patch["shoppingGuide"] = canonical_guide_dump
            _resolve_confirmed_phone_category_unknowns(
                state, payload, guide_state
            )
            _resolve_executable_shopping_unknowns(
                state, payload, guide_state, require_status=require_status
            )
            _require_clarification_for_empty_phone_recommendation(
                state, payload, guide_state, message=message
            )
            _synchronize_ecommerce_constraint_projection(
                state,
                payload,
                guide_state,
            )
        effective_requested_status = payload.get("status", requested_status)
        _enforce_task_executability_invariants(
            state,
            payload,
            allow_auto_ready=allow_auto_ready,
            requested_status=(
                effective_requested_status if require_status else None
            ),
        )
        _reject_nonblocking_recommendation_unknowns(
            state,
            payload,
            guide_state,
            require_status=require_status,
        )
        recommend_mode_explicit = False
        if state.task_type == "ecommerce_guide" and "shoppingGuide" in model_domain_patch:
            raw_guide = model_domain_patch["shoppingGuide"]
            recommend_mode_explicit = (
                isinstance(raw_guide, dict) and raw_guide.get("mode") == "recommend"
            )
        elif state.task_type == "ecommerce_guide" and optional_shopping_value is not None:
            existing_guide = state.domain_state.get("shoppingGuide")
            if guide_state is not None:
                recommend_mode_explicit = guide_state.mode == "recommend"
            elif isinstance(existing_guide, dict):
                recommend_mode_explicit = existing_guide.get("mode") == "recommend"
                try:
                    guide_state = ShoppingGuideState.model_validate(existing_guide)
                except ValueError:
                    guide_state = None
        optional_shopping_questions = _validate_optional_shopping_questions(
            state,
            optional_shopping_value,
            guide_state,
            recommend_mode_explicit=recommend_mode_explicit,
            message=message,
            proposed_goal=str(payload.get("goal", state.goal)),
        )
        shopping_transition_patch = (
            build_shopping_state_transition_patch(
                state,
                guide_state,
                payload,
                constraints_changed=constraints_changed,
            )
            if state.task_type == "ecommerce_guide" and guide_state is not None
            else {}
        )
        payload["domainStatePatch"] = {
            **materialized_domain_patch,
            **shopping_transition_patch,
            **_server_turn_ledger_patch(state, message),
        }
        if optional_shopping_questions:
            payload["domainStatePatch"]["optionalShoppingQuestions"] = (
                optional_shopping_questions
            )
        if (
            state.task_type == "ecommerce_guide"
            and state.active_plan is not None
            and state.active_plan.status in {"completed", "failed", "stale"}
        ):
            # A terminal Plan is the receipt for one completed action, not a
            # lock on the durable shopping task.  Retire it only after this new
            # user-turn patch has passed all extraction and domain validation.
            payload["activePlan"] = None
            payload["planningFailure"] = None
        return payload, model_domain_patch
    except TaskStatePayloadValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskStatePayloadValidationError(str(exc)) from exc


async def _apply_task_state_update(
    state: TaskState,
    arguments: dict[str, Any],
    *,
    message: str,
    on_task_state: TaskStateCallback | None,
    require_status: bool = False,
    allow_auto_ready: bool = False,
    server_domain_patch: dict[str, Any] | None = None,
    server_domain_patch_builder: Callable[[TaskState], dict[str, Any]] | None = None,
) -> TaskState:
    """Validate a model patch while keeping bookkeeping fields server-owned.

    Pure validation runs in ``_build_validated_task_state_payload`` and may raise
    ``TaskStatePayloadValidationError`` before any persistence, giving the
    bounded repair in ``_update_task_state_for_unified_harness`` a clean chance
    to resubmit. Unified persist-stage failures never become partial writes;
    only the legacy caller keeps the turn-ledger fallback behavior.

    ``require_status`` forces an explicit model status; ``allow_auto_ready`` is
    False by default so nothing silently auto-readies a model payload. The
    unified extractor always passes ``require_status=True, allow_auto_ready=False``;
    the legacy runtime opts back into its codified normalization with explicit
    ``require_status=False, allow_auto_ready=True``.
    """
    payload, model_domain_patch = _build_validated_task_state_payload(
        state,
        arguments,
        message=message,
        require_status=require_status,
        allow_auto_ready=allow_auto_ready,
    )
    def apply_server_patch(current: TaskState, proposed: dict[str, Any]) -> None:
        # Builders must see this turn's validated guide, not the previous
        # revision's guide (which would restore old requirements/candidate IDs).
        future_domain = dict(current.domain_state)
        for key, value in proposed["domainStatePatch"].items():
            if value is None:
                future_domain.pop(key, None)
            else:
                future_domain[key] = value
        future = current.model_copy(update={"domain_state": future_domain})
        patch = (
            server_domain_patch_builder(future)
            if server_domain_patch_builder is not None else server_domain_patch
        )
        if not patch:
            return
        proposed["domainStatePatch"].update(patch)
        if current.task_type == "ecommerce_guide" and "shoppingGuide" in patch:
            # Finalize the lifecycle and scope from the final server-owned
            # guide, atomically with the same write, including OCC rebuilds.
            guide = ShoppingGuideState.model_validate(patch["shoppingGuide"])
            guide, changed = clear_stale_guide_references(current, guide)
            proposed["domainStatePatch"]["shoppingGuide"] = guide.model_dump(
                by_alias=True, mode="json",
            )
            proposed["domainStatePatch"].update(build_shopping_state_transition_patch(
                current, guide, proposed, constraints_changed=changed,
            ))
            # The first validation already materialized a constraint table.
            # Rebuild that derived table, but still reject conflicts with any
            # constraints explicitly supplied by the model.
            projection = {
                "upsertConstraints": list(arguments.get("upsertConstraints", [])),
                "removeConstraintKeys": list(arguments.get("removeConstraintKeys", [])),
            }
            _synchronize_ecommerce_constraint_projection(current, projection, guide)
            proposed.update(projection)

    apply_server_patch(state, payload)

    def rebuild_domain_patch(latest: TaskState) -> dict[str, Any]:
        latest_model_patch, _ = _materialize_model_domain_patch(
            latest,
            model_domain_patch,
        )
        return {
            **latest_model_patch,
            **_server_turn_ledger_patch(latest, message),
            **(
                server_domain_patch_builder(latest)
                if server_domain_patch_builder is not None
                else (server_domain_patch or {})
            ),
        }

    def rebuild_payload(latest: TaskState) -> dict[str, Any]:
        rebuilt, _ = _build_validated_task_state_payload(
            latest,
            arguments,
            message=message,
            require_status=require_status,
            allow_auto_ready=allow_auto_ready,
        )
        apply_server_patch(latest, rebuilt)
        return rebuilt

    try:
        return await _persist_task_patch(
            state,
            payload,
            phase="user_state_updated",
            on_task_state=on_task_state,
            retry_domain_patch_builder=rebuild_domain_patch,
            retry_payload_builder=rebuild_payload,
        )
    except (ValueError, TaskStateTransitionError):
        if require_status:
            # Unified extraction must not turn a rejected full write into a
            # partial domain-only write. Only pre-persist validation may repair.
            raise
        # State maintenance must not make the user-facing Agent unavailable when
        # the model proposes a malformed field. Preserve at least the turn ledger.
        fallback_domain_patch = dict(payload["domainStatePatch"])
        fallback_domain_patch.pop("optionalShoppingQuestions", None)
        return await _persist_task_patch(
            state,
            {"domainStatePatch": fallback_domain_patch},
            phase="user_state_fallback",
            on_task_state=on_task_state,
            retry_domain_patch_builder=rebuild_domain_patch,
        )


async def _record_tool_progress(
    state: TaskState,
    *,
    tool_name: str,
    phase: str,
    on_task_state: TaskStateCallback | None,
    trace: ToolTrace | None = None,
) -> TaskState:
    domain_patch: dict[str, Any]
    status: str | None = None
    if phase == "tool_started":
        if state.status == "ready":
            status = "executing"
        domain_patch = {"activeTool": tool_name}
    else:
        if state.status == "executing":
            status = "ready"
        call_count = state.domain_state.get("toolCallCount", 0)
        if not isinstance(call_count, int) or isinstance(call_count, bool):
            call_count = 0
        domain_patch = {
            "activeTool": None,
            "toolCallCount": call_count + 1,
            "lastToolResult": {
                "tool": tool_name,
                "ok": bool(trace and trace.ok),
                "durationMs": trace.duration_ms if trace else None,
            },
        }
    payload: dict[str, Any] = {"domainStatePatch": domain_patch}
    if status is not None:
        payload["status"] = status
    return await _persist_task_patch(
        state,
        payload,
        phase=phase,
        on_task_state=on_task_state,
    )


def _explicit_harness_tool_schemas(
    message: str,
    state: TaskState,
) -> list[dict[str, Any]]:
    """Build a stable tool menu for an initial or resumed explicit Plan."""

    routing_message = _task_routing_message(message, state)
    decision = route_tool_schemas(routing_message)
    selected = list(decision.schemas)
    selected_names = {_tool_schema_name(schema) for schema in selected}
    # A server-owned pending one-turn in-scope rerank request makes the rerank
    # tool visible to the deterministic Planner for exactly this turn.  The
    # Planner only creates the step when shoppingGuideSources publish
    # scopeId + scopeRankedItemIds + rankingIntent (an active CandidateScope
    # bound to this request); the bare presence of the schema never grants the
    # model an extra action outside a plan step.  This gate MUST precede the
    # activePlan early-return below: a rerank turn retires the completed
    # search Plan at task-state update time, so `state.active_plan` is already
    # None when the deterministic rerank plan is being built.
    if state.domain_state.get("scopeRerankRequest") and (
        "rerank_products_in_scope" not in selected_names
    ):
        for schema in TOOL_SCHEMAS:
            if _tool_schema_name(schema) == "rerank_products_in_scope":
                selected.append(schema)
                selected_names.add("rerank_products_in_scope")
                break
    if state.active_plan is None:
        return selected

    required_names = {step.tool_name for step in state.active_plan.steps}
    for schema in TOOL_SCHEMAS:
        name = _tool_schema_name(schema)
        if name in required_names and name not in selected_names:
            selected.append(schema)
            selected_names.add(name)
    return selected


def _is_validated_presentation_control_turn(
    message: str,
    state: TaskState,
) -> bool:
    """Recognize a current display projection without reusing a stale receipt."""

    if state.active_plan is not None:
        return False
    observation = state.domain_state.get("taskStateExtraction")
    return bool(
        isinstance(observation, dict)
        and observation.get("route") == "deterministic_presentation_control"
        and observation.get("reason") == "presentation_only"
        and _used_phone_presentation_only_request(message)
        and _trusted_validator_presentation_ids(state)
    )


def _should_use_explicit_harness(message: str, state: TaskState) -> bool:
    """Gradually route only contract-covered production tasks to the new loop."""

    if state.pending_questions or state.status not in {"ready", "executing"}:
        return False
    if _is_validated_presentation_control_turn(message, state):
        return False
    if state.active_plan is not None:
        supported = harness_contract_tool_names()
        return all(step.tool_name in supported for step in state.active_plan.steps)

    decision = route_tool_schemas(_task_routing_message(message, state))
    names = {_tool_schema_name(schema) for schema in decision.schemas}
    return bool(names) and names.issubset(harness_contract_tool_names())


HARNESS_CONTRACT_TOOL_NAMES = harness_contract_tool_names()


def _is_harness_contract_covered(message: str, state: TaskState) -> bool:
    """Return True only for tools with Planner/Executor/Validator contracts."""

    if state.active_plan is not None:
        names = {step.tool_name for step in state.active_plan.steps}
        return bool(names) and names.issubset(HARNESS_CONTRACT_TOOL_NAMES)
    else:
        decision = route_tool_schemas(_task_routing_message(message, state))
        if decision.kind == "context_only":
            return True
        if decision.kind == "unknown":
            return False
        names = {_tool_schema_name(schema) for schema in decision.schemas}
        return bool(names) and names.issubset(HARNESS_CONTRACT_TOOL_NAMES)


def _is_context_only_turn(message: str, state: TaskState) -> bool:
    """Identify a no-tool turn without treating it as a legacy fallback.

    ``select_tool_schemas`` returns the complete menu when no evidence-bearing
    intent is recognized. Such a turn may be answered from the bounded
    ContextPack, but it may not call a business tool or make external claims.
    """

    if _is_validated_presentation_control_turn(message, state):
        return True
    if state.active_plan is not None:
        return False
    decision = route_tool_schemas(_task_routing_message(message, state))
    return decision.kind == "context_only"


def _validated_presentation_control_answer(state: TaskState) -> str | None:
    """Render the already-validated first three cards without another model call."""

    published = build_validated_guide_result(state)
    products = published.get("products") if isinstance(published, dict) else None
    if not isinstance(products, list) or not products:
        return None
    lines = ["好的，按当前 Validator 已放行的顺序，只展示前三个商品："]
    for index, item in enumerate(products[:3], start=1):
        product = item.get("product") if isinstance(item, dict) else None
        if not isinstance(product, dict):
            return None
        product_id = product.get("id")
        title = product.get("title")
        brand = product.get("brand")
        if not isinstance(product_id, str) or not isinstance(title, str):
            return None
        lines.append(f"\n{index}. **{title}**（ID：{product_id}）")
        if isinstance(brand, str) and brand:
            lines.append(f"   - 品牌：{brand}")
        price_minor = product.get("snapshotPriceMinor")
        price_label = "快照价"
        if type(price_minor) is not int:
            price_minor = product.get("syntheticReferencePriceMinor")
            price_label = "模拟参考价（AI 合成，非真实报价）"
        if type(price_minor) is int and price_minor >= 0:
            lines.append(f"   - {price_label}：¥{price_minor / 100:.2f}")
    lines.append("\n以上仅调整展示数量，没有进行新的检索或比较。")
    return "\n".join(lines)


async def _run_context_only_answer(
    message: str,
    *,
    history: list[dict] | None,
    client: AsyncOpenAI,
    state: TaskState,
    on_answer_delta: AnswerDeltaCallback | None,
    deadline_at: float | None = None,
    react_shadow_observation: Any | None = None,
    evaluation_context_arm: Any | None = None,
) -> tuple[str, list[ToolTrace], list[dict], str | None, TraceSummary | None]:
    """Answer a no-tool turn through ContextPack with an observable trace."""

    import uuid

    run_id = (
        evaluation_context_arm.identity.run_id
        if evaluation_context_arm is not None
        else f"run-{uuid.uuid4().hex[:12]}"
    )
    trace_builder = TraceBuilder(run_id, mode="context_pack")
    from .graph.runtime import CONTROL_POLICY_REVISIONS
    entered_runtime = (
        "react_v0_shadow"
        if react_shadow_observation is not None
        else settings.agent_control_runtime
    )
    if entered_runtime not in CONTROL_POLICY_REVISIONS:
        entered_runtime = "fixed_v1"
    trace_builder.set_entered_runtime(entered_runtime)
    policy_revision = CONTROL_POLICY_REVISIONS.get(entered_runtime)
    if policy_revision is not None:
        trace_builder.set_control_policy(entered_runtime, policy_revision)
    if react_shadow_observation is not None:
        _record_react_shadow_observation(trace_builder, react_shadow_observation)
    trace_builder.set_context(
        state.task_id,
        evaluation_context_arm.identity.session_id
        if evaluation_context_arm is not None else None,
    )
    trace_builder.set_revision_before(state.revision)
    trace_builder.set_base_context_revision(state.revision)
    turn_messages = [{"role": "user", "content": message}]
    try:
        pack = await build_context_pack(
            state,
            allowed_tools=[],
            history=history,
            run_id=run_id,
        )
        pack = await _authoritative_evaluation_pack(
            pack,
            evaluation_context_arm=evaluation_context_arm,
            task_state=state,
            message=message,
            tool_schemas=[],
            phase="SHOPPING_FINAL_ANSWER",
        )
        pack_hash = context_pack_hash(pack)
        trace_builder.set_context_pack(pack_hash, context_pack_token_count(pack))
        trace_builder.start_phase("context_only_answer")
        if _is_validated_presentation_control_turn(message, state):
            answer = _validated_presentation_control_answer(state)
            if answer is None:
                raise ValueError("validated presentation cards are unavailable")
            if on_answer_delta is not None:
                await on_answer_delta(answer)
            trace_builder.end_phase("answered_deterministically")
            trace_builder.set_final("context_only_answer")
            trace_builder.set_revision_after(state.revision)
            turn_messages.append({"role": "assistant", "content": answer})
            return answer, [], turn_messages, run_id, trace_builder.summary()
        loop = asyncio.get_running_loop()
        timeout = (
            max(deadline_at - loop.time(), 0.001)
            if deadline_at is not None
            else max(float(settings.agent_request_deadline_seconds), 0.1)
        )
        answer_policy = (
            "本轮没有获准调用业务工具。只回答寒暄、能力说明或当前任务上下文；"
            "不得声称查询了商户、商品、价格、库存、评论或其他外部事实。"
        )
        answer = await asyncio.wait_for(
            _generate_final_answer(
                client,
                messages=[
                    context_pack_system_message(pack),
                    {
                        "role": "system",
                        "content": answer_policy,
                    },
                    {"role": "user", "content": message},
                ],
                tool_traces=[],
                on_answer_delta=on_answer_delta,
                fallback="你好，我可以帮助你进行本地生活查询或商品导购。",
            ),
            timeout=timeout,
        )
        trace_builder.end_phase("answered")
        trace_builder.set_final("context_only_answer")
        trace_builder.set_revision_after(state.revision)
        turn_messages.append({"role": "assistant", "content": answer})
        return answer, [], turn_messages, run_id, trace_builder.summary()
    except Exception:
        logger.exception("Context-only run %s failed", run_id)
        trace_builder.mark_degraded("context_only_answer_failed")
        trace_builder.set_final("context_only_answer_failed")
        trace_builder.set_revision_after(state.revision)
        answer = "抱歉，本轮上下文处理失败，系统已安全停止。"
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        turn_messages.append({"role": "assistant", "content": answer})
        return answer, [], turn_messages, run_id, trace_builder.summary()
    finally:
        try:
            await _persist_trace_safely(trace_builder.finish())
        except Exception:
            logger.exception("Failed to finish context-only trace %s", run_id)


def _harness_tool_trace(result: HarnessStepResult) -> ToolTrace | None:
    executor = result.executor_result
    if executor is None or executor.execution_result is None:
        return None
    return executor.execution_result.tool_trace


def _harness_stop_answer(
    result: HarnessStepResult,
    tool_traces: list[ToolTrace] | None = None,
) -> str:
    if result.replanner_result is not None:
        return "抱歉，现有证据仍不足以可靠回答，我已经停止继续尝试，避免重复无效查询。"
    if result.validator_result is not None:
        if result.validator_result.error_code == "product_candidates_missing":
            return (
                "检索已经完成，但当前没有商品同时满足全部硬条件。"
                "请放宽一个硬条件后再试；系统不会把未知或不满足的商品冒充为合格候选。"
            )
        return "抱歉，本轮执行结果没有通过可靠性校验，因此暂时不能据此回答。"
    trace = _harness_tool_trace(result)
    if trace is None and tool_traces:
        trace = next(
            (
                item
                for item in reversed(tool_traces)
                if item.tool == "search_products"
            ),
            None,
        )
    if result.executor_result is not None or trace is not None:
        detail = trace.detail if trace is not None and isinstance(trace.detail, dict) else {}
        if detail.get("code") == "product_recall_unavailable":
            category = detail.get("requestedCategory")
            label = {
                "phone": "手机",
                "laptop": "笔记本",
                "headphones": "耳机",
            }.get(category, "该品类")
            return (
                f"当前冻结数据快照中没有可用于{label}导购的商品记录，"
                "因此不能给出候选；系统没有用其他品类商品冒充结果。"
            )
        return "抱歉，本轮工具执行没有成功完成，请稍后重试。"
    if result.planner_result is not None:
        return "抱歉，我暂时无法为这个任务生成可靠的执行计划。"
    return "抱歉，当前任务无法继续执行，请补充信息或稍后重试。"


def _react_boundary_answer(answer_context_ref: object) -> str | None:
    """Render server-authored boundary answers without stale Validator reuse."""

    if not isinstance(answer_context_ref, str) or not answer_context_ref.startswith(
        "evidence-boundary:"
    ):
        return None
    if answer_context_ref.startswith("evidence-boundary:camera:"):
        return (
            "当前证据不支持比较相机、夜景等实际拍照表现；"
            "我不会把商品标题宣传当成已验证事实。"
            "你可以改为比较当前已核验字段，或补充可信的外部证据。"
        )
    if answer_context_ref.startswith("evidence-boundary:gaming:"):
        return (
            "当前证据不支持比较大型游戏帧率、发热等实测表现；"
            "我不会把商品标题宣传当成已验证事实。"
            "你可以改为比较当前已核验字段，或补充可信的外部证据。"
        )
    return (
        "当前证据不支持这项实际性能结论；我不会把商品标题宣传当成已验证事实。"
        "你可以改为比较当前已核验字段，或补充可信的外部证据。"
    )


def _should_use_comparison_judge(state: TaskState) -> bool:
    """Keep durable and non-durable final-answer routing semantically identical."""

    guide_raw = state.domain_state.get("shoppingGuide")
    extraction_raw = state.domain_state.get("taskStateExtraction")
    deterministic_scope_answer = bool(
        isinstance(extraction_raw, dict)
        and extraction_raw.get("route") == "deterministic_validated_scope_answer"
    )
    return bool(
        isinstance(guide_raw, dict)
        and guide_raw.get("mode") == "compare"
        and not deterministic_scope_answer
    )


def _comparison_reference_claim_is_invalid(
    answer: str,
    final_answer_view: FinalAnswerContextView,
) -> bool:
    """Reject count claims inferred from a selected comparison pair.

    ``rankedFinalists`` contains only the products selected for comparison; it
    is not the complete CandidateScope.  When the server has bound an original
    display ordinal such as 3, a model must not reinterpret the two returned
    finalists as proof that only two matches exist or that the third item is
    missing.
    """

    selection = final_answer_view.answer_format.get("comparisonSelection")
    if not isinstance(selection, dict):
        return False
    ordinals = selection.get("sourceDisplayOrdinals")
    if (
        not isinstance(ordinals, list)
        or not ordinals
        or any(type(item) is not int or item <= 0 for item in ordinals)
        or max(ordinals) < 3
    ):
        return False
    normalized = re.sub(r"\s+", "", answer)
    return bool(re.search(
        r"(?:没有|不存在)(?:第)?(?:3|三)(?:个|款)"
        r"|(?:只找到|只有)(?:2|两)(?:个|款)"
        r"|无法凑满(?:3|三)(?:个|款)",
        normalized,
    ))


def _validated_results_for_answer_context(
    state: TaskState,
    tool_traces: list[ToolTrace],
    answer_context_ref: object,
) -> list[dict[str, Any]]:
    """Resolve current-turn validation or an exactly bound persisted scope.

    Presentation-only and evidence-follow-up turns can legitimately advance the
    TaskState revision without executing a new plan.  In that case the prior
    ValidatorResult is stale for the current revision, but the active
    CandidateScope remains publishable only through its server-owned
    ``validated-scope:<scopeId>`` reference and the strict source-identity
    checks in ``_build_validated_scope_results``.
    """

    try:
        return _build_validated_results(state, tool_traces)
    except ValueError:
        persisted_comparison = _build_persisted_comparison_results(
            state,
            answer_context_ref if isinstance(answer_context_ref, str) else None,
        )
        if persisted_comparison is not None:
            return persisted_comparison
        return _build_validated_scope_results(
            state,
            answer_context_ref if isinstance(answer_context_ref, str) else None,
        )


def _displayed_candidate_ids_v2(
    validated_results: list[dict[str, Any]],
    *,
    candidate_scope: CandidateScope,
    limit: int = 3,
) -> tuple[int, ...]:
    """Read the actual displayed order from the Validator-owned projection."""

    allowed = set(candidate_scope.ranked_item_ids)
    for result in reversed(validated_results):
        summary = result.get("validationSummary")
        if not isinstance(summary, dict):
            continue
        for summary_key in (
            "requiresGuideDecision",
            "requiresScopeRerank",
            "requiresProductCandidates",
        ):
            section = summary.get(summary_key)
            if not isinstance(section, dict):
                continue
            rows = section.get("rankedFinalists")
            if not isinstance(rows, list):
                rows = section.get("productPresentations")
            if not isinstance(rows, list) or not rows:
                continue
            ids: list[int] = []
            for row in rows[:limit]:
                raw_id = row.get("productId") if isinstance(row, dict) else None
                if type(raw_id) is not int or raw_id not in allowed or raw_id in ids:
                    raise ValueError("displayed candidates are not bound to CandidateScope")
                ids.append(raw_id)
            if ids:
                return tuple(ids)
    return ()


async def _maybe_run_multi_agent_research_v2(
    *,
    state: TaskState,
    validated_results: list[dict[str, Any]],
    final_answer_view: FinalAnswerContextView,
    run_id: str,
    user_message: str,
    client: AsyncOpenAI,
    tool_transport: Callable[..., Awaitable[ToolTrace]],
    trace_builder: TraceBuilder,
    remaining_budget_seconds: float,
) -> dict[str, Any] | None:
    """Run the bounded read-only child Agent when Validator evidence has gaps.

    Any routing, provider, tool, contract, deadline or Redis-merge failure is
    recorded and falls back to the already validated single-Agent answer.  Raw
    child tool observations never enter the parent answer context.
    """

    if not settings.multi_agent_v2_enabled:
        return None
    raw_scope = state.domain_state.get("candidateScope")
    if not isinstance(raw_scope, dict):
        return None
    try:
        scope = CandidateScope.model_validate(raw_scope)
        if (
            scope.status != "active"
            or scope.task_id != state.task_id
            or scope.source_revision > state.revision
        ):
            raise ValueError("active CandidateScope is not bound to current TaskState")

        from .evidence_research_v1 import (
            NoInvestigationNeeded,
            ResearchMergeGuardV1,
        )
        from .multi_agent_runtime_v2 import (
            deepseek_research_decision_v2,
            extract_investigation_maps_v2,
            run_multi_agent_research_v2,
            sha256_json,
        )

        hard, conflicts, unknowns = extract_investigation_maps_v2(
            validated_results,
            candidate_scope=scope,
        )
        requested_gap_keys = _active_scope_research_gap_keys_v2(
            state,
            user_message,
        )
        if requested_gap_keys:
            displayed_ids = _displayed_candidate_ids_v2(
                validated_results,
                candidate_scope=scope,
            )
            if not displayed_ids:
                raise ValueError("capability research has no Validator-bound display")
            for candidate_id in displayed_ids:
                unknowns[candidate_id] = tuple(sorted(
                    set(unknowns.get(candidate_id, ())) | set(requested_gap_keys)
                ))
        if not (hard or conflicts or unknowns):
            return None
        allotted = min(
            max(float(settings.multi_agent_v2_deadline_seconds), 0.1),
            max(float(remaining_budget_seconds) - 0.5, 0.1),
        )
        deadline_at = datetime.now(timezone.utc) + timedelta(seconds=allotted)

        async def child_tool_caller(name: str, arguments: dict[str, Any]) -> ToolTrace:
            trace = await tool_transport(name, arguments)
            error_code = None
            if not trace.ok and isinstance(trace.detail, dict):
                value = trace.detail.get("code")
                error_code = value if isinstance(value, str) else None
            trace_builder.record_tool_call(
                tool_name=f"evidence_research_agent:{name}",
                ok=trace.ok,
                duration_ms=trace.duration_ms,
                error_code=error_code,
                arguments_summary="candidate_scope_bound_product_ids",
            )
            return trace

        async def child_decide(view: dict[str, Any]):
            return await deepseek_research_decision_v2(
                client,
                model=settings.deepseek_model,
                child_view=view,
            )

        trace_builder.start_phase("evidence_research_agent")
        serialized_parent_view = (
            final_answer_view.model_dump(by_alias=True, mode="json")
            if hasattr(final_answer_view, "model_dump")
            else {"contextHash": str(final_answer_view.context_hash)}
        )
        parent_context_binding_hash = sha256_json({
            "taskId": state.task_id,
            "taskRevision": state.revision,
            "finalAnswerContextView": serialized_parent_view,
        })
        try:
            result = await asyncio.wait_for(
                run_multi_agent_research_v2(
                    candidate_scope=scope,
                    task_revision=state.revision,
                    parent_run_id=run_id,
                    parent_context_binding_hash=parent_context_binding_hash,
                    research_goal=(
                        "核验当前 CandidateScope 中影响用户目标的缺失或冲突证据："
                        + user_message
                    ),
                    hard_unknowns_by_product=hard,
                    conflicts_by_product=conflicts,
                    unknowns_by_product=unknowns,
                    tool_caller=child_tool_caller,
                    decide=child_decide,
                    merge_guard=ResearchMergeGuardV1(),
                    deadline_at=deadline_at,
                    on_model_call=_observe_llm_call,
                ),
                timeout=allotted,
            )
        except NoInvestigationNeeded:
            trace_builder.end_phase("direct_answer")
            return None
        except Exception:
            trace_builder.end_phase("failed_closed_to_single_agent")
            raise
        trace_builder.end_phase("validated_and_merged")
        return result.parent_projection()
    except Exception:
        logger.exception(
            "Multi-Agent V2 degraded to single-Agent answer for task=%s revision=%s",
            state.task_id,
            state.revision,
        )
        trace_builder.mark_degraded("multi_agent_v2_failed_closed")
        return None


def _durable_resume_rejected_message(result: Any) -> str:
    """Human-facing message for a fail-closed resume rejection (#5)."""
    reason = result.rejected_reason if result is not None else None
    if reason == "empty_answer":
        return "回答为空，请补充完成当前任务所需的关键信息后再继续。"
    if reason == "answer_too_long":
        return "回答过长，请用一句简洁的关键信息补充。"
    if reason in ("cross_task", "thread_malformed"):
        return "回复与当前任务不匹配，系统已安全停止；请重新开始当前任务。"
    if reason == "revision_mismatch":
        return "当前任务已经更新，请查看最新问题后重新回答。"
    if reason == "no_pending_interrupt":
        return "当前没有待回答的问题；请发送一条新消息继续。"
    if reason == "resolved_interrupt_different_payload":
        return "该问题已经回答过，且本次回答与之前不同；如需修改请重新开始当前任务。"
    if reason in ("proposal_hash_mismatch", "invalid_answer"):
        return "回复无效，请用一句简洁的关键信息补充后重试。"
    return "回复无效，系统已安全停止；请重试。"


def _durable_message_basis(current: TaskState, fallback: str) -> str:
    """Return the latest server-owned message that may drive durable planning."""
    domain_state = current.domain_state or {}
    basis = domain_state.get("v2UserMessage") or fallback
    pending = domain_state.get("v2PendingClarification")
    latest_applied = domain_state.get("lastUserMessage")
    if (
        isinstance(pending, dict)
        and pending.get("status") == "resolved"
        and isinstance(latest_applied, str)
        and latest_applied
    ):
        basis = latest_applied
    return str(basis)


async def _authoritative_evaluation_pack(
    pack: ContextPack,
    *,
    evaluation_context_arm: Any | None,
    task_state: TaskState,
    message: str,
    tool_schemas: list[dict[str, Any]],
    phase: str,
) -> ContextPack:
    if evaluation_context_arm is None:
        return pack
    from .evaluation_context_arm import authoritative_context_pack

    return await authoritative_context_pack(
        pack,
        capability=evaluation_context_arm,
        task_revision=task_state.revision,
        phase=phase,
        tool_schemas=tool_schemas,
        query=message,
    )


def _durable_projector_factory(
    message: str,
    history: list[dict] | None,
    run_id: str,
    memory_run_binding: MemoryRunBinding | None,
    evaluation_context_arm: Any | None = None,
) -> Callable[[TaskState], Awaitable[ContextProjector]]:
    """Build the durable projector factory for ``run_graph_v2_durable``.

    Each durable node rebuilds the ContextProjector from the CURRENT live
    TaskState revision (via ``_resolve_projector``), so a resumed/restarted
    graph projects the post-answer / post-plan revision instead of the stale
    pre-run pack built by the request layer (requirement #4 / #6).
    """

    async def factory(current: TaskState) -> ContextProjector:
        # After a resume, the semantically-applied answer is the latest turn;
        # before that boundary, the original server-owned request remains the
        # basis. Both values come from TaskState, never client-side history.
        basis = _durable_message_basis(current, message)
        pack = await build_context_pack(
            current,
            allowed_tools=[
                s["function"]["name"]
                for s in _explicit_harness_tool_schemas(basis, current)
            ],
            history=history,
            run_id=run_id,
        )
        pack = await _authoritative_evaluation_pack(
            pack,
            evaluation_context_arm=evaluation_context_arm,
            task_state=current,
            message=basis,
            tool_schemas=_explicit_harness_tool_schemas(basis, current),
            phase="SHOPPING_PLANNER",
        )
        return ContextProjector(
            pack,
            long_term_memory_context=memory_run_binding,
            long_term_memory_enabled=memory_run_binding is not None,
        )

    return factory


async def _persist_trace_safely(trace: AgentRunTrace) -> None:
    """Persist trace to store, logging but never raising on failure."""
    try:
        from .agent_trace import get_trace_store
        await get_trace_store().save(trace)
    except Exception:
        logger.exception("Failed to persist trace %s", trace.run_id)


def _record_react_shadow_observation(
    trace_builder: TraceBuilder,
    observation: Any,
) -> None:
    """Attach one redacted shadow decision to an authoritative run trace."""

    if observation.decision_source == "model":
        _observe_llm_call(
            "react_decision",
            observation.duration_ms,
            failed=observation.status != "accepted",
        )
    view = observation.view
    action = observation.action
    view_hash = view.decision_view_hash if view is not None else None
    view_tokens = None
    if view is not None:
        from .control.react_context import decision_view_token_count

        view_tokens = decision_view_token_count(view)
        trace_builder.record_context_view(
            "react_decision",
            view_hash,
            view_tokens,
        )
    trace_builder.record_react_decision(
        status=observation.status,
        decision_source=observation.decision_source,
        task_revision=observation.task_revision,
        view_hash=view_hash,
        view_token_count=view_tokens,
        adaptive_trigger=(
            view.observation_summary.adaptive_trigger
            if view is not None
            else None
        ),
        action_id=action.action_id if action is not None else None,
        action_kind=action.kind if action is not None else None,
        option_id=observation.selected_option_id,
        published_option_ids=(
            [item.option_id for item in view.allowed_action_options]
            if view is not None else []
        ),
        reason_code=action.reason_code if action is not None else None,
        tool_name=action.tool_name if action is not None else None,
        error_code=observation.error_code,
        duration_ms=observation.duration_ms,
    )


def _record_react_action_outcome(
    trace_builder: TraceBuilder,
    outcome: Any,
) -> None:
    """Attach one redacted executable action result to the run trace."""

    trace_builder.record_react_outcome(
        action_id=outcome.action_id,
        status=outcome.status,
        validator_outcome=outcome.validator_outcome,
        state_revision_after=outcome.state_revision_after,
        retryable=outcome.retryable,
        error_code=outcome.error_code,
        observation_ref=outcome.observation_ref,
    )


async def _finish_react_shadow_terminal_trace(
    *,
    state: TaskState,
    observation: Any,
    final_action: str,
    session_id: str | None,
    pack: ContextPack | None = None,
) -> tuple[str, TraceSummary]:
    """Persist shadow evidence for an authoritative pre-Harness terminal."""

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    trace_builder = TraceBuilder(run_id, mode="context_pack")
    trace_builder.set_context(state.task_id, session_id)
    trace_builder.set_revision_before(state.revision)
    trace_builder.set_revision_after(state.revision)
    trace_builder.set_base_context_revision(state.revision)
    trace_builder.set_entered_runtime("react_v0_shadow")
    if pack is not None:
        trace_builder.set_context_pack(
            context_pack_hash(pack),
            context_pack_token_count(pack),
        )
    _record_react_shadow_observation(trace_builder, observation)
    trace_builder.set_final(final_action)
    finished = trace_builder.finish()
    await _persist_trace_safely(finished)
    return run_id, trace_builder.summary()


async def _run_react_v0_live_agent(
    message: str,
    *,
    history: list[dict] | None,
    client: AsyncOpenAI,
    task_state: TaskState,
    on_answer_delta: AnswerDeltaCallback | None,
    on_task_state: TaskStateCallback | None,
    deadline_at: float,
    session_id: str | None,
    memory_run_binding: MemoryRunBinding | None,
) -> tuple[str, list[ToolTrace], list[dict], str | None, TraceSummary | None]:
    """Run the isolated, read-only executable ReAct V0 vertical slice."""

    from .control.react_runtime import run_react_v0_loop

    run_id = f"run-{uuid.uuid4().hex[:12]}"
    trace_builder = TraceBuilder(run_id, mode="context_pack")
    trace_builder.set_entered_runtime("react_v0")
    trace_builder.set_context(task_state.task_id, session_id)
    trace_builder.set_revision_before(task_state.revision)
    trace_builder.set_base_context_revision(task_state.revision)
    trace_builder.start_phase("react_loop")
    pending_action: Any | None = None
    observed_tool_traces: list[ToolTrace] = []

    def record_decision(observation: Any) -> None:
        nonlocal pending_action
        _record_react_shadow_observation(trace_builder, observation)
        if observation.status == "accepted" and observation.action is not None:
            pending_action = observation.action

    def record_outcome(outcome: Any) -> None:
        nonlocal pending_action
        _record_react_action_outcome(trace_builder, outcome)
        if (
            pending_action is not None
            and outcome.action_id == pending_action.action_id
        ):
            pending_action = None

    def close_pending_action(*, current: TaskState, error_code: str) -> None:
        """Close a selected action when outer cancellation preempts the loop."""

        nonlocal pending_action
        if pending_action is None:
            return
        trace_builder.record_react_outcome(
            action_id=pending_action.action_id,
            status="FAILED",
            validator_outcome=(
                "PASSED" if pending_action.kind == "ANSWER" else "FAILED"
            ),
            state_revision_after=current.revision,
            retryable=False,
            error_code=error_code,
            observation_ref=(
                pending_action.answer_context_ref
                if pending_action.kind == "ANSWER"
                else None
            ),
        )
        pending_action = None

    def record_tool_trace(tool_trace: ToolTrace) -> None:
        observed_tool_traces.append(tool_trace)
        error_code = None
        if not tool_trace.ok and isinstance(tool_trace.detail, dict):
            raw_code = tool_trace.detail.get("code")
            error_code = raw_code if isinstance(raw_code, str) else None
        trace_builder.record_tool_call(
            tool_name=tool_trace.tool,
            ok=tool_trace.ok,
            duration_ms=tool_trace.duration_ms,
            error_code=error_code,
            arguments_summary="server_owned_argument_refs",
        )

    async def answer_handler(
        action: Any,
        current: TaskState,
        tool_traces: list[ToolTrace],
    ) -> str:
        pack = await build_context_pack(
            current,
            allowed_tools=[],
            history=history,
            run_id=run_id,
        )
        trace_builder.set_context_pack(
            context_pack_hash(pack),
            context_pack_token_count(pack),
        )
        projector = ContextProjector(
            pack,
            long_term_memory_context=memory_run_binding,
            long_term_memory_enabled=memory_run_binding is not None,
        )
        validated_results = _validated_results_for_answer_context(
            current,
            tool_traces,
            action.answer_context_ref,
        )
        validated_results = apply_memory_rerank_to_validated_results(
            current,
            validated_results,
            memory_run_binding=memory_run_binding,
            memory_rerank_weight=settings.memory_rerank_lambda,
        )
        evidence_refs = _build_validated_evidence_refs(validated_results)
        final_answer_view = projector.final_answer_view(
            validated_results=validated_results,
            evidence_refs=evidence_refs,
            phase_task_revision=current.revision,
        )
        from .harness import _validate_view_and_record

        _validate_view_and_record(
            final_answer_view,
            current,
            trace_builder,
            "final_answer",
            projector=projector,
        )
        multi_agent_projection = await _maybe_run_multi_agent_research_v2(
            state=current,
            validated_results=validated_results,
            final_answer_view=final_answer_view,
            run_id=run_id,
            user_message=message,
            client=client,
            tool_transport=tool_transport,
            trace_builder=trace_builder,
            remaining_budget_seconds=max(
                deadline_at - asyncio.get_running_loop().time(),
                0.1,
            ),
        )
        trace_builder.start_phase("final_answer")
        _record_view(trace_builder, "final_answer", final_answer_view)
        trace_builder.record_phase_task_revision(
            "final_answer", current.revision
        )
        trace_builder.set_evidence_refs(evidence_refs)
        try:
            use_comparison_judge = _should_use_comparison_judge(current)
            if multi_agent_projection is not None:
                answer = _render_multi_agent_parent_answer_v2(
                    final_answer_view,
                    multi_agent_projection,
                )
            elif action.reason_code == "answer_evidence_boundary":
                # The missing evidence is already a server-proven fact.  A
                # generative answer adds no value and can silently promote a
                # title claim into a capability conclusion.
                answer = _render_used_phone_evidence_boundary_answer()
            else:
                answer = (
                    None
                    if use_comparison_judge
                    else _render_validated_used_phone_answer(
                        current,
                        final_answer_view,
                    )
                )
            prebuilt_answer = answer is not None
            if answer is None:
                # Buffer comparison prose until it passes the deterministic
                # capability-claim guard; never stream an unvalidated claim.
                answer_delta = None if use_comparison_judge else on_answer_delta
                remaining_for_answer = deadline_at - asyncio.get_running_loop().time()
                model_timeout = min(
                    settings.agent_react_final_answer_timeout_seconds,
                    max(remaining_for_answer - 1.0, 0.1),
                )
                try:
                    answer = await asyncio.wait_for(
                        _generate_final_answer(
                            client,
                            messages=[],
                            tool_traces=tool_traces,
                            on_answer_delta=answer_delta,
                            fallback="任务已经执行完成，但我暂时无法整理出可靠回答。",
                            final_answer_view=final_answer_view,
                        ),
                        timeout=model_timeout,
                    )
                except (
                    asyncio.TimeoutError,
                    APIConnectionError,
                    APITimeoutError,
                    RateLimitError,
                    InternalServerError,
                ):
                    # A streamed non-comparison answer may already have emitted
                    # partial text. Buffered comparisons can always degrade to
                    # the server-owned renderer without corrupting the response.
                    if answer_delta is not None:
                        raise
                    answer = _render_react_final_answer_fallback(
                        current,
                        final_answer_view,
                        action.reason_code,
                    )
                if (
                    use_comparison_judge
                    and (
                        _has_unsupported_used_phone_capability_claim(answer)
                        or _comparison_reference_claim_is_invalid(
                            answer, final_answer_view
                        )
                    )
                ):
                    answer = _render_validated_used_phone_answer(
                        current,
                        final_answer_view,
                    )
                    if answer is None:
                        answer = _render_used_phone_evidence_boundary_answer()
                if use_comparison_judge and on_answer_delta is not None:
                    await on_answer_delta(answer)
            if multi_agent_projection is not None:
                if _has_unsupported_used_phone_capability_claim(answer):
                    answer = _render_validated_used_phone_answer(
                        current,
                        final_answer_view,
                    ) or _render_used_phone_evidence_boundary_answer()
                if on_answer_delta is not None:
                    await on_answer_delta(answer)
            elif prebuilt_answer and on_answer_delta is not None:
                await on_answer_delta(answer)
        except asyncio.CancelledError:
            trace_builder.end_phase("cancelled")
            raise
        except Exception:
            trace_builder.end_phase("failed")
            raise
        trace_builder.end_phase("generated")
        return answer

    def schema_provider(current: TaskState) -> list[dict[str, Any]]:
        return _explicit_harness_tool_schemas(message, current)

    try:
        loop = asyncio.get_running_loop()
        remaining = deadline_at - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        result = await asyncio.wait_for(
            run_react_v0_loop(
                state=task_state,
                user_message=message,
                client=client,
                model=settings.deepseek_model,
                decision_timeout_seconds=(
                    settings.agent_react_decision_timeout_seconds
                ),
                max_iterations=settings.agent_react_max_iterations,
                tool_schema_provider=schema_provider,
                answer_handler=answer_handler,
                on_task_state=on_task_state,
                on_decision=record_decision,
                on_outcome=record_outcome,
                on_tool_trace=record_tool_trace,
            ),
            timeout=remaining,
        )
    except asyncio.TimeoutError:
        latest = await get_task_state(task_state.task_id)
        current = latest or task_state
        close_pending_action(
            current=current,
            error_code="react_v0_deadline_exceeded",
        )
        trace_builder.end_phase("deadline_exceeded")
        trace_builder.mark_degraded("react_v0_deadline_exceeded")
        trace_builder.set_final("needs_review")
        trace_builder.set_revision_after(current.revision)
        answer = "本轮超过总执行时间限制，系统已安全停止。"
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        finished = trace_builder.finish()
        await _persist_trace_safely(finished)
        turns = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ]
        return (
            answer,
            list(observed_tool_traces),
            turns,
            run_id,
            trace_builder.summary(),
        )
    except Exception:
        logger.exception("Executable ReAct V0 run %s failed", run_id)
        latest = await get_task_state(task_state.task_id)
        current = latest or task_state
        close_pending_action(
            current=current,
            error_code="react_v0_runtime_failed",
        )
        trace_builder.end_phase("failed")
        trace_builder.mark_degraded("react_v0_runtime_failed")
        trace_builder.set_final("needs_review")
        trace_builder.set_revision_after(current.revision)
        answer = "系统执行发生异常，本轮已安全停止。"
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        finished = trace_builder.finish()
        await _persist_trace_safely(finished)
        turns = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ]
        return (
            answer,
            list(observed_tool_traces),
            turns,
            run_id,
            trace_builder.summary(),
        )

    trace_builder.end_phase(result.terminal_action)
    trace_builder.set_revision_after(result.state.revision)
    trace_builder.set_final(result.terminal_action)
    if result.failure_code is not None:
        trace_builder.mark_degraded(result.failure_code)
    if on_answer_delta is not None and result.terminal_action != "answer":
        await on_answer_delta(result.answer)
    finished = trace_builder.finish()
    await _persist_trace_safely(finished)
    turns = [
        {"role": "user", "content": message},
        {"role": "assistant", "content": result.answer},
    ]
    return (
        result.answer,
        list(result.tool_traces),
        turns,
        run_id,
        trace_builder.summary(),
    )


def _durable_enabled_for_run(
    memory_run_binding: MemoryRunBinding | None,
) -> bool:
    """Memory V13 snapshots are process-local and cannot cross checkpoints."""
    memory_bound = bool(
        memory_run_binding is not None
        and memory_run_binding.payload_for_phase("planner") is not None
    )
    return bool(settings.agent_graph_v2_durable_enabled and not memory_bound)


async def _run_explicit_harness_agent(
    message: str,
    *,
    history: list[dict] | None,
    client: AsyncOpenAI,
    task_state: TaskState,
    on_answer_delta: AnswerDeltaCallback | None,
    on_task_state: TaskStateCallback | None,
    deadline_at: float | None = None,
    resume: dict[str, Any] | None = None,
    restart: bool = False,
    pause_resume: dict[str, Any] | None = None,
    session_id: str | None = None,
    react_shadow_observation: Any | None = None,
    memory_run_binding: MemoryRunBinding | None = None,
    reference_context: ResolvedReferenceContext | None = None,
    evaluation_context_arm: Any | None = None,
) -> tuple[str, list[ToolTrace], list[dict], str | None, TraceSummary | None]:
    """Drive the persisted Harness until it reaches one user-facing boundary.

    Day-2 durable mode (``settings.agent_graph_v2_durable_enabled``) runs the
    durable graph instead: ``resume`` carries a validated clarification answer
    payload; ``restart=True`` continues a process-interrupted thread.  Server
    identity always comes from the persisted thread cursor / resume payload.
    """

    state = task_state
    tool_traces: list[ToolTrace] = []
    emitted_executor_trace_ids: set[tuple[str, str, str, str, str]] = set()
    turn_messages: list[dict] = [{"role": "user", "content": message}]
    durable_result: Any | None = None

    # ── ContextPack + TraceBuilder integration ────────────────────────────
    import uuid
    # V13 memory bindings are immutable only for this process-local run.
    # Until their sealed snapshot can be checkpointed and re-issued after a
    # restart, a run that actually retains memory must never create a durable
    # interrupt/checkpoint that it cannot safely resume.
    durable = _durable_enabled_for_run(memory_run_binding)
    resolved_thread: str | None = None
    run_id = (
        evaluation_context_arm.identity.run_id
        if evaluation_context_arm is not None
        else f"run-{uuid.uuid4().hex[:12]}"
    )
    if durable and (resume is not None or restart):
        from .graph import resolve_durable_identity
        resolved_run, resolved_thread = await resolve_durable_identity(
            task_state.task_id, resume=resume, restart=restart, session_id=session_id
        )
        if resolved_run:
            run_id = resolved_run
    bind_agent_llm_call_span(run_id=run_id)
    use_context_pack = settings.agent_context_mode == "context_pack"
    mode = "context_pack" if use_context_pack else "legacy"
    trace_builder = TraceBuilder(run_id, mode=mode)
    # Durable HTTP execution is server-owned by the authenticated session.
    # Preserve that owner on the run trace so later evidence readback can bind
    # task/session/run as one identity instead of producing an orphaned trace.
    trace_builder.set_context(state.task_id, session_id)
    trace_builder.set_revision_before(state.revision)

    # ── V2 control-plane mode (default off; V1 remains production) ────────
    # Invalid combo (live AND shadow) is fail-closed to V1 with a degraded
    # marker.  Shadow mode records V1's tool calls + LLM replies and replays
    # them through agent/app/graph with zero side effects; the official answer
    # always stays the V1 result.
    v2_live = bool(settings.agent_graph_v2_enabled)
    v2_shadow = bool(settings.agent_graph_v2_shadow_enabled)
    if v2_live and v2_shadow:
        v2_live = False
        v2_shadow = False
        trace_builder.mark_degraded("graph_v2_invalid_combo")
    if settings.agent_control_runtime == "react_v0":
        trace_builder.set_entered_runtime("react_v0")
    elif settings.agent_control_runtime == "react_v1":
        trace_builder.set_entered_runtime("react_v1")
    elif settings.agent_control_runtime == "react_v0_shadow":
        trace_builder.set_entered_runtime("react_v0_shadow")
    elif durable:
        trace_builder.set_entered_runtime("graph_v2_durable")
    elif v2_live:
        trace_builder.set_entered_runtime("graph_v2")
    else:
        trace_builder.set_entered_runtime("fixed_v1")
    from .graph.runtime import CONTROL_POLICY_REVISIONS
    policy_revision = CONTROL_POLICY_REVISIONS.get(settings.agent_control_runtime)
    if policy_revision is not None:
        trace_builder.set_control_policy(
            settings.agent_control_runtime,
            policy_revision,
        )
    if react_shadow_observation is not None:
        _record_react_shadow_observation(
            trace_builder,
            react_shadow_observation,
        )
    graph_observations: dict[str, Any] = {
        "ok": False,
        "initial_state": state,
        "final_state": None,
        "result": None,
        "limit_reached": False,
        "compound_second_round": False,
    }
    shadow_capture: Any = None
    shadow_recording_path: Path | None = None
    shadow_evidence_dir: Path | None = None

    projector: ContextProjector | None = None
    pack: ContextPack | None = None
    pack_hash: str | None = None
    trace_finished = False
    critic_job: tuple[dict[str, Any], list[dict[str, Any]]] | None = None
    loop = asyncio.get_running_loop()
    deadline_at = deadline_at or (
        loop.time() + max(float(settings.agent_request_deadline_seconds), 0.1)
    )

    if settings.agent_control_runtime == "react_v0":
        trace_builder.mark_degraded("react_v0_not_accepted")
        trace_builder.set_final("safe_stop", error="react_v0_not_accepted")
        trace_builder.set_revision_after(state.revision)
        answer = (
            "当前实验运行时尚未通过验收，本轮已安全停止，"
            "没有调用业务工具。"
        )
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        turn_messages.append({"role": "assistant", "content": answer})
        finished = trace_builder.finish()
        await _persist_trace_safely(finished)
        return answer, tool_traces, turn_messages, run_id, trace_builder.summary()

    def remaining_budget() -> float:
        remaining = deadline_at - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    context_shadow_ordinal = 0

    async def observe_context_compiler_shadow(
        current_pack: ContextPack,
        current: TaskState,
        tool_schemas: list[dict[str, Any]],
        phase: str,
    ) -> None:
        """Record-only CTX1a compilation; never changes the authoritative pack."""

        nonlocal context_shadow_ordinal
        if not (
            settings.context_compiler_shadow_enabled
            or settings.context_receipt_evaluation_mode
        ):
            return
        try:
            from .context_compiler_v1 import compile_context_pack_shadow_v1

            compiled = await compile_context_pack_shadow_v1(
                current_pack,
                tenant_id="local",
                owner_id=(session_id or current.session_id or f"task:{current.task_id}"),
                session_id=(session_id or current.session_id or f"task:{current.task_id}"),
                task_id=current.task_id,
                task_revision=current.revision,
                phase=phase,
                model_call_ordinal=context_shadow_ordinal,
                tool_schemas=tool_schemas,
                model_config={
                    "provider": "deepseek",
                    "model": settings.deepseek_model,
                },
                deadline_at=datetime.now(timezone.utc)
                + timedelta(seconds=max(remaining_budget(), 0.01)),
                budget_tokens=DEFAULT_TOKEN_BUDGET,
                persist=(
                    settings.context_receipts_enabled
                    or settings.context_receipt_evaluation_mode
                ),
                evaluation_mode=settings.context_receipt_evaluation_mode,
            )
            context_shadow_ordinal += 1
            bind_agent_llm_call_span(
                context_receipt_id=compiled.receipt.receipt_id,
                context_binding_hash=compiled.receipt.binding_hash,
            )
            trace_builder.record_context_view(
                f"context_compiler_shadow:{phase.lower()}",
                compiled.receipt.semantic_hash,
                compiled.receipt.estimated_tokens,
            )
        except Exception:
            if settings.context_receipt_evaluation_mode:
                raise
            logger.exception("ContextCompiler V1 shadow failed")
            trace_builder.mark_degraded("context_compiler_shadow_failed")

    async def apply_durable_clarification_answer(
        current: TaskState,
        answer_text: str,
    ) -> TaskState:
        """Apply a validated resume answer through the normal TaskState gate."""
        applied_reference = reference_context
        if applied_reference is not None:
            # The clarification node first persists its server-owned resolution
            # receipt, which advances TaskState by exactly one revision.  The
            # browser receipt was already resolved against the pre-resume live
            # revision at the HTTP boundary, so rebind it only across this one
            # verified graph transition.  Any other drift fails closed instead
            # of falling back to stale Validator order or guessing a focus.
            receipt = (current.domain_state or {}).get("v2PendingClarification")
            expected_answer_hash = hashlib.sha256(
                answer_text.encode("utf-8")
            ).hexdigest()[:16]
            if (
                applied_reference.task_id != current.task_id
                or current.revision != applied_reference.task_revision + 1
                or not isinstance(receipt, dict)
                or receipt.get("status") != "resolved"
                or receipt.get("taskId") != current.task_id
                or receipt.get("appliedRevision") != current.revision
                or receipt.get("answerHash") != expected_answer_hash
            ):
                raise RuntimeError(
                    "durable_reference_context_transition_invalid"
                )
            applied_reference = replace(
                applied_reference,
                task_revision=current.revision,
            )
        return await _update_task_state_for_unified_harness(
            answer_text,
            history=history,
            client=client,
            task_state=current,
            on_task_state=None,
            reference_context=applied_reference,
            evaluation_context_arm=evaluation_context_arm,
        )

    if use_context_pack:
        try:
            active_tool_schemas = _explicit_harness_tool_schemas(message, state)
            pack = await build_context_pack(
                state,
                allowed_tools=[
                    s["function"]["name"] for s in active_tool_schemas
                ],
                history=history,
                run_id=run_id,
            )
            pack = await _authoritative_evaluation_pack(
                pack,
                evaluation_context_arm=evaluation_context_arm,
                task_state=state,
                message=message,
                tool_schemas=active_tool_schemas,
                phase="SHOPPING_PLANNER",
            )
            if evaluation_context_arm is None:
                await observe_context_compiler_shadow(
                    pack, state, active_tool_schemas, "SHOPPING_PLANNER"
                )
            pack_hash = context_pack_hash(pack)
            token_count = context_pack_token_count(pack)
            trace_builder.set_context_pack(pack_hash, token_count)
            trace_builder.set_base_context_revision(state.revision)  # freeze revision
            projector = ContextProjector(
                pack,
                long_term_memory_context=memory_run_binding,
                long_term_memory_enabled=memory_run_binding is not None,
            )
        except Exception:
            logger.exception("Failed to build ContextPack; stopping unified Harness")
            trace_builder.mark_degraded("context_pack_build_failed")
            trace_builder.set_final("context_pack_build_failed")
            trace_builder.set_revision_after(state.revision)
            answer = "抱歉，本轮上下文构建失败，系统已安全停止，没有使用未受约束的旧上下文继续执行。"
            if on_answer_delta is not None:
                await on_answer_delta(answer)
            turn_messages.append({"role": "assistant", "content": answer})
            finished = trace_builder.finish()
            await _persist_trace_safely(finished)
            trace_finished = True
            return answer, tool_traces, turn_messages, run_id, trace_builder.summary()

    if v2_shadow and not durable:
        from pathlib import Path as _Path

        from .graph import CaptureLLMClient
        from .transport_resolver import get_record_transport

        shadow_evidence_dir = _Path(".runtime") / f"e2e-agent-graph-v2-day1-{run_id}"
        shadow_recording_path = shadow_evidence_dir / "recording.jsonl"
        shadow_capture = CaptureLLMClient(client)
        tool_transport = get_record_transport(
            recording_path=shadow_recording_path,
            run_id=run_id,
            context_pack_hash=pack_hash,
        )
        graph_client = shadow_capture
    else:
        tool_transport = (
            evaluation_context_arm.tool_transport
            if evaluation_context_arm is not None
            else get_tool_transport(
                run_id=run_id,
                context_pack_hash=pack_hash,
            )
        )
        graph_client = client

    async def persist_durable_terminal_response(answer: str) -> bool:
        """Store every user-visible terminal outcome of a resolved resume.

        A failed tool is still a terminal outcome. Exact replay must return the
        identical already-published failure text rather than silently replacing
        it with a different generic message or a fresh model answer.
        """
        nonlocal state

        async def notify_published_state() -> None:
            """Expose the post-publication revision without weakening commit."""

            try:
                await _notify_task_state(
                    on_task_state,
                    state,
                    "durable_terminal_response_published",
                )
            except Exception:
                # The immutable response and TaskState receipt are already
                # committed.  A transport/UI callback must not turn that
                # durable success into an apparent failed request.
                logger.exception(
                    "Failed to publish finalized TaskState for task=%s revision=%s",
                    state.task_id,
                    state.revision,
                )
        if not durable:
            return True
        if not (durable_result is not None and session_id):
            return False
        from .graph import (
            read_terminal_response_receipt,
            write_terminal_response_receipt,
        )
        from .graph.resume import (
            prepare_terminal_response_receipt,
            session_owner_hash,
        )
        from .graph.runtime import CONTROL_POLICY_REVISIONS

        policy = settings.agent_control_runtime
        policy_revision = CONTROL_POLICY_REVISIONS.get(policy)
        if policy_revision is None:
            return False
        answer_sha256 = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        owner_hash = session_owner_hash(session_id)
        existing = (state.domain_state or {}).get("v2FinalAnswerReceipt")
        exact_existing = bool(
            isinstance(existing, dict)
            and existing.get("runId") == durable_result.run_id
            and existing.get("threadId") == durable_result.thread_id
            and existing.get("sessionOwnerHash") == owner_hash
            and existing.get("controlPolicy") == policy
            and existing.get("policyRevision") == policy_revision
            and existing.get("answerSha256") == answer_sha256
            and isinstance(existing.get("publicationId"), str)
            and existing.get("finalizationRevision") == state.revision
            and type(existing.get("baseTaskRevision")) is int
        )
        same_terminal_identity = bool(
            isinstance(existing, dict)
            and existing.get("runId") == durable_result.run_id
            and existing.get("threadId") == durable_result.thread_id
            and existing.get("sessionOwnerHash") == owner_hash
            and existing.get("controlPolicy") == policy
            and existing.get("policyRevision") == policy_revision
        )
        if same_terminal_identity and not exact_existing:
            trace_builder.mark_degraded("final_answer_already_committed_conflict")
            return False
        if exact_existing:
            base_revision = existing["baseTaskRevision"]
        else:
            base_revision = state.revision
        publication_id = durable_result.proposal_hash or (
            f"final-{policy}-r{base_revision}"
        )
        if exact_existing:
            stored = await write_terminal_response_receipt(
                task_id=state.task_id,
                run_id=durable_result.run_id,
                thread_id=durable_result.thread_id,
                proposal_hash=publication_id,
                session_id=session_id,
                answer=answer,
                state_revision=state.revision,
                base_task_revision=base_revision,
                control_policy=policy,
            )
            if not stored:
                trace_builder.mark_degraded(
                    "terminal_response_receipt_write_failed"
                )
            else:
                await notify_published_state()
            return stored
        else:
            finalization_receipt = {
                "version": 1,
                "taskId": state.task_id,
                "runId": durable_result.run_id,
                "threadId": durable_result.thread_id,
                "sessionOwnerHash": owner_hash,
                "controlPolicy": policy,
                "policyRevision": policy_revision,
                "baseTaskRevision": base_revision,
                "finalizationRevision": base_revision + 1,
                "answerSha256": answer_sha256,
                "publicationId": publication_id,
            }
            prepared = prepare_terminal_response_receipt(
                task_id=state.task_id,
                run_id=durable_result.run_id,
                thread_id=durable_result.thread_id,
                proposal_hash=publication_id,
                session_id=session_id,
                answer=answer,
                state_revision=base_revision + 1,
                base_task_revision=base_revision,
                control_policy=policy,
            )
            if prepared is None:
                trace_builder.mark_degraded(
                    "terminal_response_receipt_write_failed"
                )
                return False
            side_key, side_payload = prepared
            try:
                state = await update_task_state(
                    state.task_id,
                    TaskStatePatchRequest(
                        expectedRevision=base_revision,
                        actor="agent",
                        domainStatePatch={
                            "v2FinalAnswerReceipt": finalization_receipt
                        },
                    ),
                    immutable_side_record=(
                        side_key,
                        json.dumps(
                            side_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                    ),
                )
            except Exception:
                recovered = await get_task_state(state.task_id)
                replay = None
                if recovered is not None:
                    replay = await read_terminal_response_receipt(
                        task_id=recovered.task_id,
                        run_id=durable_result.run_id,
                        thread_id=durable_result.thread_id,
                        proposal_hash=publication_id,
                        session_id=session_id,
                        task_state=recovered,
                    )
                if (
                    recovered is not None
                    and replay is not None
                    and replay.get("answer") == answer
                ):
                    state = recovered
                    await notify_published_state()
                    return True
                trace_builder.mark_degraded("final_answer_finalization_conflict")
                return False
        await notify_published_state()
        return True

    async def on_graph_transition(result: HarnessStepResult) -> None:
        nonlocal state
        state = result.task_state
        trace = _harness_tool_trace(result)
        if trace is not None:
            execution = result.executor_result.execution_result
            assert execution is not None
            identity = (
                execution.task_id,
                execution.plan_id,
                execution.step_id,
                execution.tool_name,
                execution.started_at.isoformat(),
            )
            if identity in emitted_executor_trace_ids:
                return
            emitted_executor_trace_ids.add(identity)
            tool_traces.append(trace)
        await _notify_task_state(
            on_task_state,
            state,
            f"harness_{result.action}",
        )

    try:
        # LangGraph owns the bounded loop. The surrounding block handles only
        # the single terminal user-facing boundary returned by the graph.
        for graph_round in range(2):
            if durable:
                # ── Day-2 durable control plane ─────────────────────────
                # Real interrupt()/resume(), durable plain-Redis checkpoints,
                # TaskState revision reconciliation and process-restart
                # recovery.  The durable runner is the single authority on
                # thread identity and boundaries.
                from .graph import run_graph_v2_durable

                async def durable_tool_transport_v2(
                    tool_name: str, arguments: dict[str, Any], context: Any,
                ) -> ToolTrace:
                    # Explicit adapter: the durable Inbox boundary supplies
                    # context to this transport invocation; legacy callers are
                    # never inferred by arity inside the durable runner.
                    from .tool_execution_v2 import ToolExecutionContext

                    if type(context) is not ToolExecutionContext:
                        raise TypeError("durable_tool_execution_context_invalid")
                    return await tool_transport(
                        tool_name,
                        arguments,
                        execution_context=context,
                    )

                durable_result = await asyncio.wait_for(
                    run_graph_v2_durable(
                        task_id=state.task_id,
                        session_id=session_id,
                        resume=resume,
                        restart=restart,
                        pause_resume=pause_resume,
                        run_id=run_id,
                        thread_id=resolved_thread,
                        user_message=message,
                        client=client,
                        model=settings.deepseek_model,
                        resolve_tool_schemas=lambda current: (
                            _explicit_harness_tool_schemas(
                                _durable_message_basis(current, message), current
                            )
                        ),
                        tool_caller=tool_transport,
                        tool_caller_v2=durable_tool_transport_v2,
                        trace_builder=trace_builder,
                        projector=projector,
                        projector_factory=_durable_projector_factory(
                            message, history, run_id, memory_run_binding,
                            evaluation_context_arm,
                        ),
                        clarification_answer_applier=(
                            apply_durable_clarification_answer
                        ),
                        max_transitions=MAX_HARNESS_TRANSITIONS,
                        system_policies=None,
                        on_transition=on_graph_transition,
                        control_policy=settings.agent_control_runtime,
                        react_max_model_decisions=(
                            settings.agent_react_v1_max_model_decisions
                        ),
                        react_decision_timeout_seconds=(
                            settings.agent_react_decision_timeout_seconds
                        ),
                        on_model_call=(
                            lambda stage, duration_ms, **kwargs: _observe_llm_call(
                                stage,
                                duration_ms,
                                failed=bool(kwargs.get("failed")),
                                record_receipt=False,
                            )
                        ),
                        on_model_call_receipt=(
                            lambda stage, duration_ms, **kwargs: _observe_llm_call(
                                stage,
                                duration_ms,
                                record_aggregate=False,
                                **kwargs,
                            )
                        ),
                    ),
                    timeout=remaining_budget(),
                )
                if durable_result.task_state is not None:
                    state = durable_result.task_state
                    # Interrupt nodes do not complete and therefore emit no
                    # HarnessStepResult transition.  Publish the rehydrated
                    # live TaskState explicitly so the HTTP layer does not
                    # return the pre-graph revision used to start the run.
                    await _notify_task_state(
                        on_task_state,
                        state,
                        f"durable_{durable_result.boundary}",
                    )
                graph_observations["final_state"] = state
                boundary = durable_result.boundary
                if durable_result.mode == "idempotent_replay":
                    from .graph import read_terminal_response_receipt

                    receipt = await read_terminal_response_receipt(
                        task_id=state.task_id,
                        run_id=durable_result.run_id,
                        thread_id=durable_result.thread_id,
                        proposal_hash=durable_result.proposal_hash or "",
                        session_id=session_id or "",
                        task_state=state,
                    )
                    if receipt is None:
                        # Never regenerate from an already-resolved client
                        # payload. A missing record fails closed without LLM.
                        answer = "当前续答结果已过期或无法校验，请重新开始当前任务。"
                        trace_builder.set_final("idempotent_replay_receipt_missing")
                        trace_builder.mark_degraded("idempotent_replay_receipt_missing")
                    else:
                        answer = receipt["answer"]
                        trace_builder.set_final("idempotent_resume_replay")
                    trace_builder.set_revision_after(state.revision)
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                    graph_observations["ok"] = True
                    # Main skips history persistence for the corresponding
                    # preflight-matched replay, so this remains a no-op turn.
                    return answer, [], [], run_id, trace_builder.summary()
                terminal_finalization = (state.domain_state or {}).get(
                    "v2FinalAnswerReceipt"
                )
                if (
                    durable_result.mode == "restart"
                    and boundary != "clarification"
                    and isinstance(terminal_finalization, dict)
                    and terminal_finalization.get("runId")
                    == durable_result.run_id
                    and terminal_finalization.get("threadId")
                    == durable_result.thread_id
                    and isinstance(
                        terminal_finalization.get("publicationId"), str
                    )
                ):
                    from .graph import read_terminal_response_receipt

                    receipt = await read_terminal_response_receipt(
                        task_id=state.task_id,
                        run_id=durable_result.run_id,
                        thread_id=durable_result.thread_id,
                        proposal_hash=terminal_finalization["publicationId"],
                        session_id=session_id or "",
                        task_state=state,
                    )
                    if receipt is None:
                        answer = (
                            "当前终态回答已提交但无法校验，系统已安全停止；"
                            "请重新开始当前任务。"
                        )
                        trace_builder.set_final(
                            "terminal_restart_receipt_missing"
                        )
                        trace_builder.mark_degraded(
                            "terminal_restart_receipt_missing"
                        )
                    else:
                        answer = receipt["answer"]
                        trace_builder.set_final("terminal_restart_replay")
                    trace_builder.set_revision_after(state.revision)
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                    graph_observations["ok"] = True
                    return answer, [], [], run_id, trace_builder.summary()
                if boundary == "clarification":
                    if not durable_result.proposal_hash:
                        trace_builder.mark_degraded(
                            "durable_clarification_identity_missing"
                        )
                        answer = (
                            "当前澄清任务缺少可验证的续作身份，系统已安全停止；"
                            "请重新开始当前任务。"
                        )
                        trace_builder.set_final(
                            "durable_clarification_identity_missing"
                        )
                        trace_builder.set_revision_after(state.revision)
                        if on_answer_delta is not None:
                            await on_answer_delta(answer)
                        turn_messages.append({"role": "assistant", "content": answer})
                        graph_observations["ok"] = True
                        return answer, tool_traces, turn_messages, run_id, trace_builder.summary()
                    trace_builder.set_durable_resume(
                        task_id=state.task_id,
                        run_id=durable_result.run_id,
                        thread_id=durable_result.thread_id,
                        revision=state.revision,
                        proposal_hash=durable_result.proposal_hash,
                    )
                    answer = (
                        durable_result.question
                        or "请补充完成当前任务所需的关键信息。"
                    )
                    trace_builder.set_final("ask_user")
                    trace_builder.set_revision_after(state.revision)
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                    turn_messages.append({"role": "assistant", "content": answer})
                    graph_observations["ok"] = True
                    return answer, tool_traces, turn_messages, run_id, trace_builder.summary()
                if boundary == "operator_paused":
                    from .graph.pause_control import public_pause_receipt

                    receipt = public_pause_receipt(durable_result.pause_receipt)
                    if receipt is None or not receipt.get("checkpointHash"):
                        answer = (
                            "暂停请求未能确认持久化检查点，系统已安全停止；"
                            "请稍后重新开始当前任务。"
                        )
                        trace_builder.mark_degraded(
                            "checkpoint_pause_receipt_missing"
                        )
                        trace_builder.set_final(
                            "checkpoint_pause_receipt_missing"
                        )
                    else:
                        trace_builder.set_checkpoint_pause(receipt)
                        answer = "已在安全检查点暂停。你可以稍后继续当前任务。"
                        trace_builder.set_final("operator_paused")
                    trace_builder.set_revision_after(state.revision)
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                    turn_messages.append({"role": "assistant", "content": answer})
                    graph_observations["ok"] = True
                    return answer, tool_traces, turn_messages, run_id, trace_builder.summary()
                if boundary == "state_diverged":
                    answer = "检测到当前任务状态与历史执行不一致，系统已安全停止；请重新开始当前任务。"
                    trace_builder.set_final("state_diverged")
                    trace_builder.set_revision_after(state.revision)
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                    turn_messages.append({"role": "assistant", "content": answer})
                    graph_observations["ok"] = True
                    return answer, tool_traces, turn_messages, run_id, trace_builder.summary()
                if boundary == "resume_rejected":
                    answer = _durable_resume_rejected_message(durable_result)
                    trace_builder.set_final("resume_rejected")
                    trace_builder.set_revision_after(state.revision)
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                    turn_messages.append({"role": "assistant", "content": answer})
                    graph_observations["ok"] = True
                    return answer, tool_traces, turn_messages, run_id, trace_builder.summary()
                if boundary == "fault_injected":
                    answer = "任务执行在安全故障点被终止，系统已安全停止；你可以稍后继续当前任务。"
                    trace_builder.set_final("fault_injected")
                    trace_builder.mark_degraded("executor_fault_injected")
                    trace_builder.set_revision_after(state.revision)
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                    turn_messages.append({"role": "assistant", "content": answer})
                    graph_observations["ok"] = True
                    return answer, tool_traces, turn_messages, run_id, trace_builder.summary()
                if boundary == "max_transitions_exceeded":
                    result = HarnessStepResult(
                        action="continue_to_executor", task_state=state
                    )
                    graph_state = {
                        "transitions": [result],
                        "transition_limit_reached": True,
                    }
                    graph_observations["result"] = result
                    graph_observations["limit_reached"] = True
                    break
                # task_completed / stop_turn flow through the shared boundary
                # handling below exactly like V1.
                result = HarnessStepResult(
                    action=(
                        "task_completed"
                        if boundary == "task_completed"
                        else "stop_turn"
                    ),
                    task_state=state,
                )
                graph_state = {
                    "transitions": [result],
                    "transition_limit_reached": False,
                }
                graph_observations["result"] = result
                graph_observations["limit_reached"] = False
            elif v2_live:
                from .graph import GraphV2Runtime, run_graph_v2

                graph_state = await asyncio.wait_for(
                    run_graph_v2(
                        state,
                        GraphV2Runtime(
                            user_message=message,
                            client=client,
                            model=settings.deepseek_model,
                            resolve_tool_schemas=lambda current: (
                                _explicit_harness_tool_schemas(message, current)
                            ),
                            tool_caller=tool_transport,
                            trace_builder=trace_builder,
                            projector=projector,
                            max_transitions=MAX_HARNESS_TRANSITIONS,
                            system_policies=None,
                            on_transition=on_graph_transition,
                        ),
                    ),
                    timeout=remaining_budget(),
                )
                trace_builder.record_graph_v2_events(graph_state["node_events"])
            else:
                graph_state = await asyncio.wait_for(
                    run_controlled_react_graph(
                        state,
                        ReActGraphRuntime(
                            user_message=message,
                            client=graph_client,
                            model=settings.deepseek_model,
                            resolve_tool_schemas=lambda current: (
                                _explicit_harness_tool_schemas(message, current)
                            ),
                            step_runner=run_harness_step,
                            tool_caller=tool_transport,
                            trace_builder=trace_builder,
                            projector=projector,
                            max_transitions=MAX_HARNESS_TRANSITIONS,
                            on_transition=on_graph_transition,
                        ),
                    ),
                    timeout=remaining_budget(),
                )
            result = graph_state["transitions"][-1]
            graph_observations["result"] = result
            graph_observations["final_state"] = state
            graph_observations["limit_reached"] = bool(
                graph_state["transition_limit_reached"]
            )

            if result.action == "continue_to_executor":
                if graph_state["transition_limit_reached"]:
                    break
                raise RuntimeError(
                    "LangGraph returned a non-terminal ReAct action unexpectedly"
                )
            if result.action == "ask_user":
                trace_builder.set_final("ask_user")
                trace_builder.set_revision_after(state.revision)
                if state.pending_questions:
                    question_messages = (
                        [context_pack_system_message(pack), {"role": "user", "content": message}]
                        if pack is not None
                        else [
                            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                            {"role": "user", "content": message},
                            _task_state_context(state),
                        ]
                    )
                    answer = await asyncio.wait_for(
                        _generate_pending_task_question(
                            client,
                            messages=question_messages,
                            state=state,
                            on_answer_delta=on_answer_delta,
                        ),
                        timeout=remaining_budget(),
                    )
                else:
                    answer = "请补充完成当前任务所需的信息。"
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                turn_messages.append({"role": "assistant", "content": answer})
                graph_observations["ok"] = True
                return answer, tool_traces, turn_messages, run_id, trace_builder.summary()
            if result.action == "task_completed":
                trace_builder.set_revision_after(state.revision)

                answer_context_ref = (
                    getattr(durable_result, "graph_state", {}).get(
                        "react_answer_context_ref"
                    )
                    if durable
                    and durable_result is not None
                    and isinstance(getattr(durable_result, "graph_state", {}), dict)
                    else None
                )
                boundary_answer = _react_boundary_answer(answer_context_ref)
                if boundary_answer is not None:
                    trace_builder.set_final("evidence_boundary_answer")
                    if not await persist_durable_terminal_response(boundary_answer):
                        boundary_answer = (
                            "终态回答未能写入持久化发布凭证，"
                            "为避免不可重放的输出，本轮已安全停止。"
                        )
                        trace_builder.set_final(
                            "safe_stop",
                            error="terminal_response_receipt_write_failed",
                        )
                    if on_answer_delta is not None:
                        await on_answer_delta(boundary_answer)
                    turn_messages.append(
                        {"role": "assistant", "content": boundary_answer}
                    )
                    graph_observations["ok"] = True
                    return (
                        boundary_answer,
                        tool_traces,
                        turn_messages,
                        run_id,
                        trace_builder.summary(),
                    )

                # Durable runs are single-authority: the graph already bounded
                # every action turn, so the V1 compound second round must not
                # re-enter the loop as a fresh (new-thread) run.
                compound = (
                    None
                    if durable
                    else state.domain_state.get("compoundComparison")
                )
                if (
                    isinstance(compound, dict)
                    and compound.get("status") == "ready"
                    and compound.get("taskId") == state.task_id
                ):
                    if graph_round != 0:
                        raise RuntimeError(
                            "compound comparison exceeded the bounded two-action turn"
                        )
                    # The shadow replays ONE graph boundary; a compound second
                    # round is driven by this outer block, not by the graph, so
                    # it is not covered by the Day-1 shadow.
                    graph_observations["compound_second_round"] = True
                    if use_context_pack:
                        active_tool_schemas = _explicit_harness_tool_schemas(
                            message, state
                        )
                        pack = await build_context_pack(
                            state,
                            allowed_tools=[
                                schema["function"]["name"]
                                for schema in active_tool_schemas
                            ],
                            history=history,
                            run_id=run_id,
                        )
                        pack = await _authoritative_evaluation_pack(
                            pack,
                            evaluation_context_arm=evaluation_context_arm,
                            task_state=state,
                            message=message,
                            tool_schemas=active_tool_schemas,
                            phase="SHOPPING_PLANNER",
                        )
                        if evaluation_context_arm is None:
                            await observe_context_compiler_shadow(
                                pack, state, active_tool_schemas, "SHOPPING_PLANNER"
                            )
                        pack_hash = context_pack_hash(pack)
                        trace_builder.set_context_pack(
                            pack_hash,
                            context_pack_token_count(pack),
                        )
                        trace_builder.set_base_context_revision(state.revision)
                        projector = ContextProjector(
                            pack,
                            long_term_memory_context=memory_run_binding,
                            long_term_memory_enabled=memory_run_binding is not None,
                        )
                    continue

                # ── Project FinalAnswerView in context_pack mode ─────────
                final_answer_view = None
                final_answer_phase_started = False
                multi_agent_projection: dict[str, Any] | None = None
                if durable and use_context_pack:
                    # The durable graph may advance TaskState through resume,
                    # planning, execution, and validation. Rebuild from that
                    # live revision; never stamp a frozen pre-graph pack with a
                    # newer phaseTaskRevision.
                    active_tool_schemas = _explicit_harness_tool_schemas(
                        _durable_message_basis(state, message), state
                    )
                    pack = await build_context_pack(
                        state,
                        allowed_tools=[
                            schema["function"]["name"]
                            for schema in active_tool_schemas
                        ],
                        history=history,
                        run_id=run_id,
                    )
                    pack = await _authoritative_evaluation_pack(
                        pack,
                        evaluation_context_arm=evaluation_context_arm,
                        task_state=state,
                        message=_durable_message_basis(state, message),
                        tool_schemas=active_tool_schemas,
                        phase="SHOPPING_FINAL_ANSWER",
                    )
                    if evaluation_context_arm is None:
                        await observe_context_compiler_shadow(
                            pack, state, active_tool_schemas, "SHOPPING_FINAL_ANSWER"
                        )
                    pack_hash = context_pack_hash(pack)
                    trace_builder.set_context_pack(
                        pack_hash, context_pack_token_count(pack)
                    )
                    trace_builder.set_base_context_revision(state.revision)
                    projector = ContextProjector(
                        pack,
                        long_term_memory_context=memory_run_binding,
                        long_term_memory_enabled=memory_run_binding is not None,
                    )
                if projector is not None:
                    validated_results = _validated_results_for_answer_context(
                        state,
                        tool_traces,
                        answer_context_ref,
                    )
                    validated_results = apply_memory_rerank_to_validated_results(
                        state,
                        validated_results,
                        memory_run_binding=memory_run_binding,
                        memory_rerank_weight=settings.memory_rerank_lambda,
                    )
                    validated_evidence_refs = _build_validated_evidence_refs(
                        validated_results
                    )
                    final_answer_view = projector.final_answer_view(
                        validated_results=validated_results,
                        evidence_refs=validated_evidence_refs,
                        phase_task_revision=state.revision,
                    )
                    # Pre-validate: the view's phaseTaskRevision must match
                    # the current state.revision before generating final answer.
                    # Uses _validate_view_and_record so mismatches are recorded
                    # in Trace as context_boundary_mismatch (fail-closed).
                    from .harness import _validate_view_and_record
                    _validate_view_and_record(
                        final_answer_view,
                        state,
                        trace_builder,
                        "final_answer",
                        projector=projector,
                    )
                    multi_agent_projection = await _maybe_run_multi_agent_research_v2(
                        state=state,
                        validated_results=validated_results,
                        final_answer_view=final_answer_view,
                        run_id=run_id,
                        user_message=message,
                        client=client,
                        tool_transport=tool_transport,
                        trace_builder=trace_builder,
                        remaining_budget_seconds=remaining_budget(),
                    )
                    if trace_builder is not None:
                        trace_builder.start_phase("final_answer")
                        final_answer_phase_started = True
                        _record_view(trace_builder, "final_answer", final_answer_view)
                        trace_builder.record_phase_task_revision(
                            "final_answer", state.revision
                        )

                    if settings.evidence_critic_enabled:
                        from .recommendation_draft import build_recommendation_draft

                        candidates: list[dict[str, Any]] = []
                        evidence_items: list[dict[str, Any]] = []
                        for validated_result in validated_results:
                            projected = validated_result.get("evidence")
                            if not isinstance(projected, dict):
                                continue
                            products = projected.get("products")
                            if isinstance(products, list):
                                candidates.extend(
                                    item for item in products if isinstance(item, dict)
                                )
                            evidence = projected.get("evidence")
                            if isinstance(evidence, list):
                                evidence_items.extend(
                                    item for item in evidence if isinstance(item, dict)
                                )
                        guide = state.domain_state.get("shoppingGuide")
                        requirements = (
                            guide.get("requirements", [])
                            if isinstance(guide, dict)
                            and isinstance(guide.get("requirements", []), list)
                            else []
                        )
                        if candidates:
                            draft = build_recommendation_draft(
                                candidates,
                                requirements,
                                evidence_items,
                            )
                            trace_builder.set_recommendation(
                                RecommendationDraftTrace(
                                    selected_product_ids=draft.selected_product_ids,
                                    claims_count=len(draft.claims),
                                    unknown_count=len(draft.unknowns),
                                    evidence_refs_count=len(validated_evidence_refs),
                                )
                            )
                            critic_job = (
                                draft.model_dump(by_alias=True, mode="json"),
                                candidates,
                            )

                evidence = [
                    trace.model_dump(by_alias=True, mode="json")
                    for trace in tool_traces
                ]
                use_comparison_judge = _should_use_comparison_judge(state)
                answer = (
                    _render_multi_agent_parent_answer_v2(
                        final_answer_view,
                        multi_agent_projection,
                    )
                    if multi_agent_projection is not None
                    and final_answer_view is not None
                    else (
                        None
                        if use_comparison_judge
                        else _render_validated_used_phone_answer(
                            state,
                            final_answer_view,
                        )
                    )
                )
                generated_by_model = answer is None
                if answer is None:
                    answer = await asyncio.wait_for(
                        _generate_final_answer(
                        client,
                        messages=[
                            {"role": "system", "content": AGENT_SYSTEM_PROMPT},
                            *(history or []),
                            {"role": "user", "content": message},
                            _task_state_context(state),
                            {
                                "role": "system",
                                "content": (
                                    "显式Harness已经完成执行并通过Validator。"
                                    "只能依据下面的本轮工具证据回答，不得编造：\n"
                                    + json.dumps(evidence, ensure_ascii=False)
                                ),
                            },
                        ],
                        tool_traces=tool_traces,
                        # Durable answers are buffered until the TaskState
                        # revision is rechecked after generation. Streaming a
                        # stale partial answer cannot be recalled.
                        on_answer_delta=(
                            None
                            if durable
                            or use_comparison_judge
                            or multi_agent_projection is not None
                            else on_answer_delta
                        ),
                        fallback="任务已经执行完成，但我暂时无法整理出可靠回答。",
                        final_answer_view=final_answer_view,
                        multi_agent_projection=multi_agent_projection,
                        ),
                        timeout=remaining_budget(),
                    )
                if (
                    use_comparison_judge
                    and _comparison_reference_claim_is_invalid(
                        answer, final_answer_view
                    )
                ):
                    answer = _render_validated_used_phone_answer(
                        state, final_answer_view
                    ) or _render_used_phone_evidence_boundary_answer()
                if (
                    multi_agent_projection is not None
                    and _has_unsupported_used_phone_capability_claim(answer)
                ):
                    answer = _render_validated_used_phone_answer(
                        state, final_answer_view
                    ) or _render_used_phone_evidence_boundary_answer()
                if durable:
                    live_after_answer = await get_task_state(state.task_id)
                    if (
                        live_after_answer is None
                        or live_after_answer.revision != state.revision
                    ):
                        if live_after_answer is not None:
                            state = live_after_answer
                        answer = (
                            "检测到回答生成期间任务状态已变化，"
                            "本轮旧答案已丢弃；请继续当前任务。"
                        )
                        trace_builder.mark_degraded("final_answer_state_diverged")
                        trace_builder.set_final(
                            "state_diverged", error="final_answer_state_diverged"
                        )
                        trace_builder.set_revision_after(state.revision)
                        if on_answer_delta is not None:
                            await on_answer_delta(answer)
                        turn_messages.append(
                            {"role": "assistant", "content": answer}
                        )
                        graph_observations["ok"] = True
                        return (
                            answer,
                            tool_traces,
                            turn_messages,
                            run_id,
                            trace_builder.summary(),
                        )
                    # Commit the immutable publication receipt after the live
                    # revision recheck and before emitting the first byte.
                    if not await persist_durable_terminal_response(answer):
                        answer = (
                            "最终回答未能写入持久化发布凭证，"
                            "为避免发布不可重放的内容，本轮已安全停止。"
                        )
                        trace_builder.set_final(
                            "safe_stop", error="terminal_response_receipt_write_failed"
                        )
                        trace_builder.set_revision_after(state.revision)
                        if on_answer_delta is not None:
                            await on_answer_delta(answer)
                        turn_messages.append({"role": "assistant", "content": answer})
                        graph_observations["ok"] = True
                        return (
                            answer,
                            tool_traces,
                            turn_messages,
                            run_id,
                            trace_builder.summary(),
                        )
                    if on_answer_delta is not None:
                        await on_answer_delta(answer)
                elif use_comparison_judge and on_answer_delta is not None:
                    await on_answer_delta(answer)
                elif multi_agent_projection is not None and on_answer_delta is not None:
                    await on_answer_delta(answer)
                elif not generated_by_model and on_answer_delta is not None:
                    await on_answer_delta(answer)
                if trace_builder is not None and final_answer_phase_started:
                    trace_builder.end_phase("generated")
                # A validated task is not a completed response until answer
                # generation itself succeeds.
                trace_builder.set_final("task_completed")
                turn_messages.append({"role": "assistant", "content": answer})
                graph_observations["ok"] = True
                return answer, tool_traces, turn_messages, run_id, trace_builder.summary()

            trace_builder.set_final(result.action)
            trace_builder.set_revision_after(state.revision)
            answer = _harness_stop_answer(result, tool_traces)
            if not await persist_durable_terminal_response(answer):
                answer = (
                    "终态回答未能写入持久化发布凭证，"
                    "为避免不可重放的输出，本轮已安全停止。"
                )
                trace_builder.set_final(
                    "safe_stop", error="terminal_response_receipt_write_failed"
                )
            if on_answer_delta is not None:
                await on_answer_delta(answer)
            turn_messages.append({"role": "assistant", "content": answer})
            graph_observations["ok"] = True
            return answer, tool_traces, turn_messages, run_id, trace_builder.summary()

        trace_builder.set_final("max_transitions_exceeded")
        trace_builder.set_revision_after(state.revision)
        answer = "抱歉，本轮任务超过了安全执行步数，已停止继续运行。"
        if not await persist_durable_terminal_response(answer):
            answer = (
                "终态回答未能写入持久化发布凭证，"
                "为避免不可重放的输出，本轮已安全停止。"
            )
            trace_builder.set_final(
                "safe_stop", error="terminal_response_receipt_write_failed"
            )
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        turn_messages.append({"role": "assistant", "content": answer})
        graph_observations["ok"] = True
        return answer, tool_traces, turn_messages, run_id, trace_builder.summary()

    except asyncio.TimeoutError:
        logger.warning("Harness run %s exceeded request deadline", run_id)
        try:
            latest = await asyncio.wait_for(
                get_task_state(state.task_id),
                timeout=1.0,
            )
            if latest is not None:
                state = latest
                await _notify_task_state(
                    on_task_state,
                    state,
                    "request_deadline_cleanup",
                )
        except Exception:
            logger.exception(
                "Failed to refresh TaskState after request deadline for task=%s",
                state.task_id,
            )
        trace_builder.mark_degraded("request_deadline_exceeded")
        trace_builder.set_final("request_deadline_exceeded")
        trace_builder.set_revision_after(state.revision)
        answer = "抱歉，本轮任务超过了总执行时间限制，已安全停止；你可以稍后继续当前任务。"
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        turn_messages.append({"role": "assistant", "content": answer})
        return answer, tool_traces, turn_messages, run_id, trace_builder.summary()
    except Exception:
        logger.exception("Harness run %s terminated by exception", run_id)
        trace_builder.mark_degraded("harness_exception")
        raise
    finally:
        if not trace_finished:
            try:
                finished = trace_builder.finish()
                finalize_transport = getattr(tool_transport, "finalize", None)
                if finalize_transport is not None:
                    await finalize_transport(finished)
                # Entry-only durable boundaries (for example a scope-negative
                # stop) have graph events but no business phase.  They are
                # still real, auditable Agent runs and must remain available
                # through the debug trace endpoint.
                if finished.phases or finished.graph_v2_events:
                    await _persist_trace_safely(finished)
                    if critic_job is not None:
                        from .critic_queue import get_critic_queue

                        await get_critic_queue().enqueue(
                            run_id,
                            critic_job[0],
                            critic_job[1],
                        )
            except Exception:
                logger.exception("Failed to persist trace during harness cleanup for %s", run_id)
        # ── V2 Record→Replay shadow (default off) ─────────────────────────
        # Replays V1's recorded tool calls + LLM replies through graph/ under
        # an isolated TaskState store.  Never affects the official V1 answer;
        # every divergence is written to evidence and logged.
        if (
            v2_shadow
            and not durable
            and graph_observations["ok"]
            and not graph_observations["compound_second_round"]
            and graph_observations["result"] is not None
            and graph_observations["final_state"] is not None
            and shadow_capture is not None
            and shadow_recording_path is not None
            and shadow_evidence_dir is not None
        ):
            try:
                await _run_v2_shadow_after_v1(
                    run_id=run_id,
                    message=message,
                    client=client,
                    result=graph_observations["result"],
                    initial_state=graph_observations["initial_state"],
                    final_state=graph_observations["final_state"],
                    limit_reached=graph_observations["limit_reached"],
                    v1_trace_builder=trace_builder,
                    recording_path=shadow_recording_path,
                    evidence_dir=shadow_evidence_dir,
                    projector=projector,
                    graph_client_capture=shadow_capture,
                )
            except Exception:
                logger.exception(
                    "graph_v2 shadow failed for %s", run_id
                )


async def _run_v2_shadow_after_v1(
    *,
    run_id: str,
    message: str,
    client: AsyncOpenAI,
    result: HarnessStepResult,
    initial_state: TaskState,
    final_state: TaskState,
    limit_reached: bool,
    v1_trace_builder: TraceBuilder,
    recording_path: Path,
    evidence_dir: Path,
    projector: ContextProjector | None,
    graph_client_capture: Any,
) -> None:
    """Run the no-side-effect V2 Record→Replay shadow after the V1 run."""
    from .graph import (
        ReplayLLMClient,
        ReplayTransport,
        build_isolated_task_state_client,
        build_v1_reference,
        read_recorded_tool_names,
        run_graph_v2_shadow,
    )

    phases = v1_trace_builder.summary().phases
    business_phases = [
        str(item.phase)
        for item in phases
        if str(item.phase) in {"planner", "executor", "validator", "replanner"}
    ]
    v1_reference = build_v1_reference(
        final_action=result.action,
        transition_limit_reached=limit_reached,
        final_task_state=final_state,
        guide_result=build_validated_guide_result(final_state),
        tool_names=read_recorded_tool_names(recording_path),
        phase_order=business_phases,
        request_id=run_id,
    )
    replay = ReplayTransport(replay_path=recording_path, strict=True)
    isolated = build_isolated_task_state_client()
    shadow_trace = TraceBuilder(f"{run_id}-shadow", mode="graph_v2_shadow")
    shadow_trace.set_context(final_state.task_id, None)
    shadow_trace.set_revision_before(initial_state.revision)
    outcome = await run_graph_v2_shadow(
        initial_task_state=initial_state,
        v1_reference=v1_reference,
        v2_client=ReplayLLMClient(graph_client_capture.captured),
        replay_transport=replay,
        isolated_client=isolated,
        shadow_trace_builder=shadow_trace,
        user_message=message,
        model=settings.deepseek_model,
        resolve_tool_schemas=lambda current: (
            _explicit_harness_tool_schemas(message, current)
        ),
        system_policies=None,
        max_transitions=MAX_HARNESS_TRANSITIONS,
        max_replans=3,
        evidence_dir=evidence_dir,
        projector=projector,
        recording_path=recording_path,
        request_id=run_id,
    )
    if outcome.get("matched"):
        logger.info(
            "graph_v2 shadow MATCHED for %s (evidence=%s)",
            run_id,
            outcome.get("evidenceDir"),
        )
    else:
        logger.warning(
            "graph_v2 shadow DIVERGED for %s (matched=%s error=%s evidence=%s)",
            run_id,
            outcome.get("matched"),
            outcome.get("shadowError"),
            outcome.get("evidenceDir"),
        )


async def _run_legacy_agent(
    message: str,
    history: list[dict] | None = None,
    on_answer_delta: AnswerDeltaCallback | None = None,
    task_state: TaskState | None = None,
    on_task_state: TaskStateCallback | None = None,
    domain_hint: str = "auto",
) -> tuple[str, list[ToolTrace], list[dict], str | None, TraceSummary | None]:
    """让 DeepSeek 自己决定要不要调工具：开单 -> 执行 -> 喂回结果 -> 总结。"""
    if task_state is None and _is_landmark_shop_question(message):
        trace = await call_tool(
            "search_shops",
            _forced_landmark_shop_arguments(message),
        )
        answer = _render_landmark_shop_answer(trace)
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        return (
            answer,
            [trace],
            [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ],
            None,
            None,
        )

    client = get_client()
    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        *([_task_state_context(task_state)] if task_state is not None else []),
        *(history or []),
        {"role": "user", "content": message},
    ]
    turn_messages: list[dict] = [{"role": "user", "content": message}]
    tool_traces: list[ToolTrace] = []
    task_state_updated = task_state is None

    # 工具规划轮始终使用完整响应；只有最后面向用户的成稿才允许流式输出。
    # 有TaskState时额外增加一轮，专门做内部状态Patch，不占用业务工具安全阀额度。
    for _ in range(MAX_TOOL_ROUNDS + int(task_state is not None)):
        routing_message = _task_routing_message(message, task_state)
        if domain_hint == "ecommerce" and "ecommerce_guide" not in routing_message:
            routing_message = f"[ecommerce_guide]\n{routing_message}"
        if not task_state_updated:
            selected_tool_schemas = [TASK_STATE_TOOL_SCHEMA]
            allowed_tool_names = {TASK_STATE_TOOL_NAME}
            required_name = None
        else:
            selected_tool_schemas = select_tool_schemas(routing_message)
            allowed_tool_names = {
                _tool_schema_name(schema) for schema in selected_tool_schemas
            }
            required_name = required_tool_name(routing_message)
            if (
                task_state is not None
                and task_state.status == "collecting_information"
                and task_state.pending_questions
            ):
                # A clarification turn may finish without external evidence.
                required_name = None

        planning_messages = messages
        if not task_state_updated:
            planning_messages = [
                *messages,
                {"role": "system", "content": TASK_STATE_PLANNING_PROMPT},
            ]
        elif on_answer_delta is not None:
            planning_messages = [
                *messages,
                {"role": "system", "content": TOOL_PLANNING_PROMPT},
            ]
        create_kwargs = {
            "model": settings.deepseek_model,
            "messages": planning_messages,
            "tools": selected_tool_schemas,
        }
        requested_tool_choice: Any | None = None
        if not task_state_updated:
            requested_tool_choice = {
                "type": "function",
                "function": {"name": TASK_STATE_TOOL_NAME},
            }
        elif _is_landmark_shop_question(routing_message) and not tool_traces:
            requested_tool_choice = {
                "type": "function",
                "function": {"name": "search_shops"},
            }
        elif required_name and not tool_traces:
            requested_tool_choice = "required"
        if requested_tool_choice is not None:
            create_kwargs.update(
                tool_choice_kwargs(settings.deepseek_model, requested_tool_choice)
            )
        planning_started = time.perf_counter()
        planning_call_failed = True
        response = None
        try:
            response = await client.chat.completions.create(**create_kwargs)
            planning_call_failed = False
        finally:
            _observe_llm_call(
                "task_state" if not task_state_updated else "shopping_policy_decision",
                (time.perf_counter() - planning_started) * 1000.0,
                failed=planning_call_failed,
                response=response,
            )
        reply = response.choices[0].message

        # 规划阶段没有继续开单：证据已准备完毕，单独生成用户可见的最终回答。
        if not reply.tool_calls:
            if not task_state_updated and task_state is not None:
                task_state = await _apply_task_state_update(
                    task_state,
                    {},
                    message=message,
                    on_task_state=on_task_state,
                    # Legacy runtime isolation: this is an internal no-state-call
                    # turn ledger, not a model payload. It keeps the pre-existing
                    # empty-patch normalization (no required status, auto-ready
                    # permitted) that the legacy loop and its tests rely on.
                    require_status=False,
                    allow_auto_ready=True,
                )
                messages.append(_task_state_context(task_state))
                task_state_updated = True
                if (
                    task_state.status == "collecting_information"
                    and task_state.pending_questions
                ):
                    answer = await _generate_pending_task_question(
                        client,
                        messages=messages,
                        state=task_state,
                        on_answer_delta=on_answer_delta,
                    )
                    turn_messages.append({"role": "assistant", "content": answer})
                    return answer, tool_traces, turn_messages, None, None
                if _should_use_explicit_harness(message, task_state):
                    return await _run_explicit_harness_agent(
                        message,
                        history=history,
                        client=client,
                        task_state=task_state,
                        on_answer_delta=on_answer_delta,
                        on_task_state=on_task_state,
                    )
                continue
            if required_name and not tool_traces:
                if task_state is not None:
                    task_state = await _record_tool_progress(
                        task_state,
                        tool_name=required_name,
                        phase="tool_started",
                        on_task_state=on_task_state,
                    )
                trace = await call_tool(
                    required_name,
                    _forced_tool_arguments(required_name, routing_message),
                )
                if task_state is not None:
                    task_state = await _record_tool_progress(
                        task_state,
                        tool_name=required_name,
                        phase="tool_finished",
                        on_task_state=on_task_state,
                        trace=trace,
                    )
                tool_traces.append(trace)
                final_messages = [
                    *messages,
                    *(
                        [_task_state_context(task_state)]
                        if task_state is not None
                        else []
                    ),
                    {
                        "role": "user",
                        "content": (
                            "本轮问题必须依据工具结果回答。"
                            f"原问题：{message}\n"
                            f"工具 {required_name} 返回："
                            f"{json.dumps(trace.detail, ensure_ascii=False)}\n"
                            "请只依据这些工具结果回答；如果证据不足，请说明暂时查不到，"
                            "不要使用旧对话记忆补答案。"
                        ),
                    },
                ]
                answer = await _generate_final_answer(
                    client,
                    messages=final_messages,
                    tool_traces=tool_traces,
                    on_answer_delta=on_answer_delta,
                    fallback="抱歉，我暂时没能根据工具结果整理出回答。",
                )
                final_message = {"role": "assistant", "content": answer}
                turn_messages.append(final_message)
                return answer, tool_traces, turn_messages, None, None
            if on_answer_delta is not None:
                answer = await _generate_final_answer(
                    client,
                    messages=messages,
                    tool_traces=tool_traces,
                    on_answer_delta=on_answer_delta,
                    fallback="抱歉，我暂时没能整理出回答，请换种说法再试一次。",
                )
                final_message = {"role": "assistant", "content": answer}
                turn_messages.append(final_message)
                return answer, tool_traces, turn_messages, None, None
            answer = _enforce_tool_answer_constraints(reply.content or "", tool_traces)
            final_message = {"role": "assistant", "content": answer}
            turn_messages.append(final_message)
            return answer, tool_traces, turn_messages, None, None

        if not task_state_updated and task_state is not None:
            # This planning response is internal runtime state, so it is visible
            # to the current model call but is not persisted as chat history.
            task_call = next(
                (
                    call
                    for call in reply.tool_calls
                    if call.function.name == TASK_STATE_TOOL_NAME
                ),
                None,
            )
            if task_call is None:
                # Legacy runtime: no explicit state call this turn, record only
                # the turn ledger. Never forge an update_task_state call.
                arguments: dict[str, Any] = {}
            else:
                # Shared strict parse boundary: malformed model arguments raise
                # instead of being silently rewritten to an empty patch.
                arguments = _parse_task_state_arguments(task_call)
            task_state = await _apply_task_state_update(
                task_state,
                arguments,
                message=message,
                on_task_state=on_task_state,
                # Legacy runtime isolation: the pre-existing legacy path
                # tolerates model payloads without an explicit status and may
                # normalize a cleared collecting_information to ready. This is
                # explicitly isolated from the strict unified extractor, which
                # always passes require_status=True and allow_auto_ready=False.
                require_status=False,
                allow_auto_ready=True,
            )
            if task_call is not None:
                internal_assistant = _assistant_message(reply)
                internal_assistant["tool_calls"] = [
                    call
                    for call in internal_assistant.get("tool_calls", [])
                    if call.get("id") == task_call.id
                ]
                messages.append(internal_assistant)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": task_call.id,
                        "content": json.dumps(
                            {
                                "ok": True,
                                "taskState": task_state.model_dump(
                                    by_alias=True,
                                    mode="json",
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
            messages.append(_task_state_context(task_state))
            task_state_updated = True
            if (
                task_state.status == "collecting_information"
                and task_state.pending_questions
            ):
                answer = await _generate_pending_task_question(
                    client,
                    messages=messages,
                    state=task_state,
                    on_answer_delta=on_answer_delta,
                )
                turn_messages.append({"role": "assistant", "content": answer})
                return answer, tool_traces, turn_messages, None, None
            if _should_use_explicit_harness(message, task_state):
                return await _run_explicit_harness_agent(
                    message,
                    history=history,
                    client=client,
                    task_state=task_state,
                    on_answer_delta=on_answer_delta,
                    on_task_state=on_task_state,
                )
            continue

        # 模型开单了 -> 先把“开单”这条 assistant 消息放回剧本（模型要看到自己开过单）
        assistant_message = _assistant_message(reply)
        messages.append(assistant_message)
        turn_messages.append(assistant_message)
        round_tool_failed = False
        for tool_index, tool_call in enumerate(reply.tool_calls):
            name = tool_call.function.name
            # 模型给的参数是文字，先试着翻译成字典；翻坏了不崩，喂回错误让它下一轮改对重发
            try:
                arguments = json.loads(tool_call.function.arguments or "{}")
            except json.JSONDecodeError:
                error_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": f"参数不是合法 JSON，无法解析：{tool_call.function.arguments}。请重新调用并给出正确参数。",
                }
                messages.append(error_message)
                turn_messages.append(error_message)
                continue
            if name not in allowed_tool_names:
                error_message = {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": f"本轮问题不允许调用 {name}，请改用可用工具：{', '.join(sorted(allowed_tool_names))}。",
                }
                messages.append(error_message)
                turn_messages.append(error_message)
                continue
            if name == "search_knowledge":
                if _is_policy_knowledge_question(routing_message):
                    arguments["sources"] = ["policy_docs"]
                elif _is_merchant_profile_question(routing_message):
                    arguments["sources"] = ["merchant_docs"]
            if name == "search_places" and _is_place_question(routing_message):
                forced_place_arguments = _forced_place_arguments(routing_message)
                if arguments.get("query"):
                    forced_place_arguments.pop("query", None)
                arguments = {**arguments, **forced_place_arguments}
            if name == "search_shops" and _is_landmark_shop_question(routing_message):
                arguments = {
                    **arguments,
                    **_forced_landmark_shop_arguments(routing_message),
                }
            if task_state is not None:
                task_state = await _record_tool_progress(
                    task_state,
                    tool_name=name,
                    phase="tool_started",
                    on_task_state=on_task_state,
                )
            trace = await call_tool(name, arguments)   # 派单台真正执行
            if task_state is not None:
                task_state = await _record_tool_progress(
                    task_state,
                    tool_name=name,
                    phase="tool_finished",
                    on_task_state=on_task_state,
                    trace=trace,
                )
            tool_traces.append(trace)
            round_tool_failed = round_tool_failed or not trace.ok
            # 把工具结果作为 role=tool 的消息喂回模型
            tool_message = {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(trace.detail, ensure_ascii=False),
            }
            messages.append(tool_message)
            turn_messages.append(tool_message)
            if task_state is not None:
                messages.append(_task_state_context(task_state))
            if (
                name == "search_knowledge"
                and isinstance(trace.detail, dict)
                and trace.detail.get("sources") == ["merchant_docs"]
            ):
                messages.append({"role": "system", "content": MERCHANT_DOC_ANSWER_GUARD})
            elif (
                name == "search_knowledge"
                and isinstance(trace.detail, dict)
                and trace.detail.get("sources") == ["policy_docs"]
            ):
                messages.append({"role": "system", "content": POLICY_DOC_ANSWER_GUARD})
            if _has_ambiguous_place_search([trace]):
                for skipped_call in reply.tool_calls[tool_index + 1 :]:
                    skipped_message = {
                        "role": "tool",
                        "tool_call_id": skipped_call.id,
                        "content": (
                            "地点搜索返回多个同名候选，本轮已停止后续详情调用。"
                            "请列出候选并让用户确认行政区。"
                        ),
                    }
                    messages.append(skipped_message)
                    turn_messages.append(skipped_message)
                break

        # 下游不可用时让模型基于失败事实直接收尾，避免同一轮反复改写参数轰炸服务。
        ambiguous_place = _has_ambiguous_place_search(tool_traces)
        if ambiguous_place:
            messages.append({
                "role": "system",
                "content": (
                    "地点搜索返回多个同名候选。本轮只能列出候选的名称、行政区和地址并请用户确认，"
                    "不得选择候选或继续查询任何一个候选的详情。"
                ),
            })
        if round_tool_failed or _is_landmark_shop_question(routing_message) or ambiguous_place:
            answer = await _generate_final_answer(
                client,
                messages=messages,
                tool_traces=tool_traces,
                on_answer_delta=on_answer_delta,
                fallback="抱歉，查询服务暂时不可用，请稍后再试。",
            )
            turn_messages.append({"role": "assistant", "content": answer})
            return answer, tool_traces, turn_messages, None, None

    # 转满 MAX_TOOL_ROUNDS 轮还在开单 -> 强制收尾：不带 tools 再请求一次，逼模型用已有数据作答
    answer = await _generate_final_answer(
        client,
        messages=messages,
        tool_traces=tool_traces,
        on_answer_delta=on_answer_delta,
        fallback="抱歉，我暂时没能整理出结果，换种说法再问我一次哈～",
    )
    turn_messages.append({"role": "assistant", "content": answer})
    return answer, tool_traces, turn_messages, None, None


async def _run_deterministic_preflight(
    message: str,
    *,
    task_state: TaskState | None,
    domain_hint: str,
    on_answer_delta: AnswerDeltaCallback | None,
    on_task_state: TaskStateCallback | None,
    deadline_at: float | None = None,
) -> tuple[str, list[ToolTrace], list[dict], str | None, TraceSummary | None] | None:
    """Run safety and confirmed-transaction gates before runtime routing.

    These are server-owned deterministic policies, not a legacy Agent loop.
    """

    smalltalk_answer = _deterministic_smalltalk_answer(message)
    if smalltalk_answer is not None:
        if on_answer_delta is not None:
            await on_answer_delta(smalltalk_answer)
        return smalltalk_answer, [], [
            {"role": "user", "content": message},
            {"role": "assistant", "content": smalltalk_answer},
        ], None, None

    if is_order_status_query(message):
        order_reference = extract_order_reference(message)
        if order_reference is None:
            answer = "请提供完整的订单 ID 或订单号，我才能查询订单状态。"
            trace = ToolTrace(
                tool="query_order_status",
                ok=False,
                detail={
                    "code": "invalid_order_reference",
                    "message": answer,
                },
            )
        else:
            trace = await dispatch_order_status(order_reference)
            answer = render_order_status_result(trace)
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        return answer, [trace], [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ], None, None

    ecommerce_turn = (
        domain_hint == "ecommerce"
        or (task_state is not None and task_state.task_type == "ecommerce_guide")
        or is_ecommerce_message(message)
    )
    confirmation_action = explicit_confirmation_action(message)
    if ecommerce_turn and confirmation_action is not None:
        handoff = TransactionHandoff(confirmation_action)
        transaction_trace = await dispatch_confirmed_handoff(confirmation_action)
        transaction_detail = (
            transaction_trace.detail
            if isinstance(transaction_trace.detail, dict)
            else {}
        )
        if transaction_trace.ok:
            answer = render_transaction_result(transaction_trace)
            if on_answer_delta is not None:
                await on_answer_delta(answer)
            return answer, [
                ToolTrace(
                    tool="transaction_handoff",
                    ok=True,
                    detail={
                        "status": "completed_by_transaction_agent",
                        "action": confirmation_action,
                    },
                ),
                transaction_trace,
            ], [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ], None, None
        failure_code = transaction_detail.get("code")
        if failure_code not in {
            "transaction_agent_disabled",
            "transaction_auth_provider_unavailable",
        }:
            answer = str(transaction_detail.get("message", "交易交接未完成。"))
            if on_answer_delta is not None:
                await on_answer_delta(answer)
            return answer, [
                ToolTrace(
                    tool="transaction_handoff",
                    ok=False,
                    detail={
                        "status": "rejected_by_transaction_agent",
                        "action": confirmation_action,
                        "code": failure_code or "transaction_handoff_failed",
                    },
                )
            ], [
                {"role": "user", "content": message},
                {"role": "assistant", "content": answer},
            ], None, None
        answer = (
            "我已识别到你的交易确认，但当前导购不会直接下单、取消订单或发起支付。"
            "这项操作需要交由独立的受信交易服务接管。"
        )
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        return answer, [
            ToolTrace(tool="transaction_handoff", ok=False, detail=handoff.detail())
        ], [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ], None, None

    if ecommerce_turn and is_unsafe_shopping_use(message):
        answer = (
            "我不能帮助选择用于窃听、破解、偷拍或绕过监护的设备。"
            "如果你的目标是合法的隐私保护、儿童安全或网络安全学习，我可以改为推荐"
            "具有家长控制、隐私指示或适合授权安全实验的普通设备。"
        )
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        return answer, [], [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ], None, None
    return None


def _parse_task_state_arguments(
    task_call: Any, *, response: Any = None, strict: bool = False,
) -> dict[str, Any]:
    """Parse an ``update_task_state`` tool call, fail-closed on any malformed input.

    Returns the parsed arguments dict. A ``None`` call, invalid JSON, or a JSON
    root that is not an object raise ``TaskStatePayloadValidationError`` with a
    stable code/field_path so the bounded repair can echo the exact rejection
    back to the model; nothing is silently rewritten to an empty patch.
    """
    if task_call is None:
        raise TaskStatePayloadValidationError(
            "update_task_state tool call is missing",
            code="missing_task_state_tool_call",
            field_path="arguments",
        )
    if response is not None and getattr(response.choices[0], "finish_reason", None) == "length":
        raise TaskStatePayloadValidationError(
            "update_task_state arguments were truncated; submit only the minimal changed fields",
            code="truncated_task_state_arguments",
            field_path="arguments",
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("non-finite JSON number")

    def finite_float(value: str) -> float:
        import math

        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite JSON number")
        return number

    try:
        parsed = json.loads(
            task_call.function.arguments or "{}",
            object_pairs_hook=unique_object, parse_constant=reject_constant,
            parse_float=finite_float,
        )
    except (ValueError, TypeError) as exc:
        raise TaskStatePayloadValidationError(
            "update_task_state arguments are not valid JSON",
            code="invalid_task_state_arguments_json",
            field_path="arguments",
        ) from exc
    if not isinstance(parsed, dict):
        raise TaskStatePayloadValidationError(
            "update_task_state arguments root must be a JSON object",
            code="task_state_arguments_not_an_object",
            field_path="arguments",
        )
    if strict:
        from .task_state_output_contract import decode_strict_task_patch

        try:
            return decode_strict_task_patch(parsed, TASK_STATE_TOOL_SCHEMA)
        except ValueError as exc:
            raise TaskStatePayloadValidationError(
                "strict tool arguments do not match the declared wire schema",
                code="strict_task_state_wire_schema_mismatch", field_path="arguments",
            ) from exc
    return parsed


def _assistant_tool_call_message(task_call: Any) -> dict[str, str]:
    """Render the original assistant tool call back to the model for repair."""
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "id": task_call.id,
                "type": "function",
                "function": {
                    "name": task_call.function.name,
                    "arguments": task_call.function.arguments or "{}",
                },
            }
        ],
    }


def _task_state_validation_error_message(
    error: TaskStatePayloadValidationError,
) -> dict[str, Any]:
    """Structured, non-leaking summary of a pure-validation rejection."""
    return {
        "valid": False,
        "code": error.code,
        "field_path": error.field_path,
        "message": str(error),
        "persist": 0,
    }


async def _submit_task_state_extraction(
    client: AsyncOpenAI,
    messages: list[dict],
) -> tuple[Any | None, Any]:
    """Request one ``update_task_state`` call and return call plus response.

    Models that support named tool selection receive a forced choice. DeepSeek
    V4 thinking models must omit ``tool_choice``; the single-tool menu plus the
    strict name check below preserves the same fail-closed runtime boundary.
    """
    tool_schema = TASK_STATE_TOOL_SCHEMA
    if settings.task_state_extraction_strict_enabled:
        from .task_state_output_contract import require_strict_endpoint, strict_task_state_tool

        require_strict_endpoint(settings.deepseek_base_url, settings.deepseek_model)
        tool_schema = strict_task_state_tool(TASK_STATE_TOOL_SCHEMA)
        messages = [*messages, {"role": "system", "content": (
            "本次使用 strict 工具 schema：所有属性必须提供；原可选字段用 null 表示不修改，"
            "空数组 [] 表示显式清空。不得用空字符串代替 null，不得遗漏 status。"
            "只提交本轮变化；未变字段填 null。value 仅支持标量、null 或字符串列表。"
        )}]
    response = await client.chat.completions.create(
        model=settings.deepseek_model,
        messages=messages,
        tools=[tool_schema],
        max_tokens=settings.task_state_extraction_max_tokens,
        **({"extra_body": {"thinking": {"type": "disabled"}}}
           if settings.deepseek_model.strip().lower().startswith("deepseek-v4-") else {}),
        **tool_choice_kwargs(
            settings.deepseek_model,
            {
                "type": "function",
                "function": {"name": TASK_STATE_TOOL_NAME},
            },
        ),
    )
    reply = response.choices[0].message
    return (
        next((
            call
            for call in (reply.tool_calls or [])
            if call.function.name == TASK_STATE_TOOL_NAME
        ), None),
        response,
    )


async def _repair_task_state_payload(
    client: AsyncOpenAI,
    *,
    planning_messages: list[dict],
    original_call: Any,
    validation_error: TaskStatePayloadValidationError,
) -> tuple[dict[str, Any], Any]:
    """One bounded structured repair call after a pure-validation rejection.

    Returns the repaired ``arguments`` dict. Fail-closed: if the repair call is
    missing, invalid JSON, or non-dict, the original validation error is re-raised
    so the caller never persists a partial model payload and never calls a third
    time.
    """
    repair_messages = [
        *planning_messages,
        _assistant_tool_call_message(original_call),
        {
            "role": "tool",
            "tool_call_id": original_call.id,
            "content": json.dumps(
                _task_state_validation_error_message(validation_error),
                ensure_ascii=False,
            ),
        },
        {"role": "system", "content": TASK_STATE_REPAIR_PROMPT},
    ]
    repair_call, repair_response = await _submit_task_state_extraction(
        client, repair_messages
    )
    if repair_call is None:
        raise validation_error
    try:
        return _parse_task_state_arguments(
            repair_call, response=repair_response,
            strict=settings.task_state_extraction_strict_enabled,
        ), repair_response
    except TaskStatePayloadValidationError:
        # The repair itself was malformed (invalid JSON / non-object): fail
        # closed by re-raising the original rejection so nothing persists.
        raise validation_error from None


async def _update_task_state_for_unified_harness(
    message: str,
    *,
    history: list[dict] | None,
    client: AsyncOpenAI,
    task_state: TaskState,
    on_task_state: TaskStateCallback | None,
    reference_context: ResolvedReferenceContext | None = None,
    evaluation_context_arm: Any | None = None,
) -> TaskState:
    """Run bounded structured state extraction before the Harness.

    Raw conversation history must not bypass ContextPack budgeting merely
    because this stage runs before Planner. Build a pre-update pack from the
    persisted state and summarized history, then send only that bounded pack
    plus the current user message to the extractor.

    If the extractor's payload fails pure validation (nothing persisted), at most
    one structured repair call resubmits a corrected payload. A second failure
    fail-closes so the turn ends safely with zero business persists.
    """

    deterministic_arguments, extraction_observation = (
        _deterministic_used_phone_task_state_decision(
            task_state,
            message,
            reference_context,
        )
    )
    if deterministic_arguments is not None:
        bound_ids = None
        compound_comparison = False
        text_claim_use_case = None
        scope_rerank_request = None
        if extraction_observation is not None:
            bound_ids = extraction_observation.pop("_boundComparedIds", None)
            compound_comparison = bool(
                extraction_observation.pop("_compoundComparison", False)
            )
            scope_rerank_request = extraction_observation.pop(
                "_scopeRerankRequest", None
            )
            if extraction_observation.get("route") == "deterministic_text_claim_discovery":
                reason = extraction_observation.get("reason")
                if isinstance(reason, str):
                    text_claim_use_case = reason
        existing_guide_raw = task_state.domain_state.get("shoppingGuide")
        try:
            existing_guide = ShoppingGuideState.model_validate(existing_guide_raw)
        except ValueError:
            existing_guide = None
        if (
            text_claim_use_case is None
            and existing_guide is not None
            and existing_guide.mode == "recommend"
            and (existing_guide.use_cases or isinstance(bound_ids, list))
            and isinstance(deterministic_arguments.get("goal"), str)
            and deterministic_arguments["goal"] == message
        ):
            relation_label = "基于上述候选回答" if isinstance(bound_ids, list) else "追加条件"
            combined_goal = f"{task_state.goal}；{relation_label}：{message}"
            if len(combined_goal) <= 2000:
                deterministic_arguments["goal"] = combined_goal
        extraction_observation = _with_extraction_metrics(
            extraction_observation,
            execution_kind="deterministic",
            model_call_count=0,
            llm_duration_ms=0.0,
            repair_used=False,
        )
        def deterministic_server_patch(latest: TaskState) -> dict[str, Any]:
            patch: dict[str, Any] = {
                "taskStateExtraction": extraction_observation,
            }
            if isinstance(bound_ids, list):
                patch.update(_bound_comparison_guide_patch(latest, bound_ids))
            if isinstance(text_claim_use_case, str):
                patch.update(_server_owned_use_case_guide_patch(
                    latest,
                    text_claim_use_case,
                    message,
                ))
            if compound_comparison:
                patch["compoundComparison"] = {
                    "status": "awaiting_search_validation",
                    "kind": "compare_first_two",
                    "taskId": latest.task_id,
                }
            # The one-turn rerank request is server-owned and cleared on every
            # write; it is re-published only when the decision layer produced
            # one and the OCC-safe re-check against ``latest`` still holds.
            patch["scopeRerankRequest"] = _server_owned_scope_rerank_request(
                latest,
                scope_rerank_request,
                message,
            )
            return patch

        return await _apply_task_state_update(
            task_state,
            deterministic_arguments,
            message=message,
            on_task_state=on_task_state,
            require_status=True,
            allow_auto_ready=False,
            server_domain_patch_builder=deterministic_server_patch,
        )

    if extraction_observation is None and task_state.task_type == "ecommerce_guide":
        extraction_observation = _task_state_extraction_observation(
            route="model_fallback",
            reason="outside_deterministic_phone_contract",
        )

    active_tool_schemas = _explicit_harness_tool_schemas(message, task_state)
    pre_update_pack = await build_context_pack(
        task_state,
        allowed_tools=[
            schema["function"]["name"]
            for schema in active_tool_schemas
        ],
        history=history,
    )
    pre_update_pack = await _authoritative_evaluation_pack(
        pre_update_pack,
        evaluation_context_arm=evaluation_context_arm,
        task_state=task_state,
        message=message,
        tool_schemas=active_tool_schemas,
        phase="SHOPPING_PLANNER",
    )

    planning_messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        context_pack_system_message(pre_update_pack),
        {"role": "user", "content": message},
        {"role": "system", "content": TASK_STATE_PLANNING_PROMPT},
    ]
    loop = asyncio.get_running_loop()
    llm_started = loop.time()
    task_call_failed = True
    task_response = None
    try:
        task_call, task_response = await _submit_task_state_extraction(
            client, planning_messages
        )
        task_call_failed = False
    finally:
        llm_duration_ms = (loop.time() - llm_started) * 1000.0
        _observe_llm_call(
            "task_state",
            llm_duration_ms,
            failed=task_call_failed,
            response=task_response,
        )
    if task_call is None:
        # Missing update_task_state tool call: there is no original call to echo
        # back for a bounded repair. Return the untouched state so the caller
        # detects the no-op (revision unchanged) and safe-stops with zero
        # business persistence, no forged tool call and no auto-ready.
        return task_state
    try:
        arguments = _parse_task_state_arguments(
            task_call, response=task_response,
            strict=settings.task_state_extraction_strict_enabled,
        )
        extraction_observation = _with_extraction_metrics(
            extraction_observation,
            execution_kind="model",
            model_call_count=1,
            llm_duration_ms=llm_duration_ms,
            repair_used=False,
        )
        return await _apply_task_state_update(
            task_state,
            arguments,
            message=message,
            on_task_state=on_task_state,
            require_status=True,
            allow_auto_ready=False,
            server_domain_patch={
                "taskStateExtraction": extraction_observation,
                # A model-path turn never decides an in-scope rerank; any
                # leftover one-turn request is cleared so it cannot leak into a
                # later deterministic turn.
                "scopeRerankRequest": None,
            } if extraction_observation is not None else None,
        )
    except TaskStatePayloadValidationError as exc:
        # Pure-validation rejection: nothing was persisted, so a single bounded
        # repair is safe. Post-write failures (OCC/persist/backend/timeout) are
        # not TaskStatePayloadValidationError and never reach this branch.
        repair_started = loop.time()
        repair_call_failed = True
        repair_response = None
        try:
            repaired_arguments, repair_response = await _repair_task_state_payload(
                client,
                planning_messages=planning_messages,
                original_call=task_call,
                validation_error=exc,
            )
            repair_call_failed = False
        finally:
            repair_duration_ms = (loop.time() - repair_started) * 1000.0
            _observe_llm_call(
                "task_state",
                repair_duration_ms,
                failed=repair_call_failed,
                response=repair_response,
            )
        llm_duration_ms += repair_duration_ms
        extraction_observation = _with_extraction_metrics(
            extraction_observation,
            execution_kind="model",
            model_call_count=2,
            llm_duration_ms=llm_duration_ms,
            repair_used=True,
        )
        return await _apply_task_state_update(
            task_state,
            repaired_arguments,
            message=message,
            on_task_state=on_task_state,
            require_status=True,
            allow_auto_ready=False,
            server_domain_patch={
                "taskStateExtraction": extraction_observation,
                # A model-path turn never decides an in-scope rerank; any
                # leftover one-turn request is cleared so it cannot leak into a
                # later deterministic turn.
                "scopeRerankRequest": None,
            } if extraction_observation is not None else None,
        )


async def _unified_pre_harness_safe_stop(
    message: str,
    *,
    state: TaskState,
    answer: str,
    failure_code: str,
    on_answer_delta: AnswerDeltaCallback | None,
    evaluation_context_arm: Any | None = None,
) -> tuple[str, list[ToolTrace], list[dict], str, TraceSummary]:
    """Return a structured Agent failure before Planner/tool execution begins."""

    if evaluation_context_arm is not None:
        from .evaluation_context_arm import validate_evaluation_capability

        validate_evaluation_capability(
            evaluation_context_arm, task_id=state.task_id, session_id=state.session_id,
        )
    run_id = (
        evaluation_context_arm.identity.run_id
        if evaluation_context_arm is not None else f"run-{uuid.uuid4().hex[:12]}"
    )
    trace_builder = TraceBuilder(run_id, mode="context_pack")
    if failure_code == "react_v0_not_accepted":
        trace_builder.set_entered_runtime("react_v0")
    trace_builder.set_context(state.task_id, state.session_id)
    trace_builder.set_revision_before(state.revision)
    trace_builder.set_base_context_revision(state.revision)
    trace_builder.start_phase("pre_harness")
    trace_builder.end_phase(
        "safe_stopped",
        {"failureCode": failure_code},
        task_revision=state.revision,
    )
    trace_builder.mark_degraded(failure_code)
    trace_builder.set_final("safe_stop", error=failure_code)
    trace_builder.set_revision_after(state.revision)
    if on_answer_delta is not None:
        await on_answer_delta(answer)
    finished = trace_builder.finish()
    await _persist_trace_safely(finished)
    return answer, [], [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ], run_id, trace_builder.summary()


async def _run_unified_harness_agent(
    message: str,
    *,
    history: list[dict] | None,
    task_state: TaskState,
    on_answer_delta: AnswerDeltaCallback | None,
    on_task_state: TaskStateCallback | None,
    deadline_at: float | None = None,
    resume: dict[str, Any] | None = None,
    restart: bool = False,
    pause_resume: dict[str, Any] | None = None,
    session_id: str | None = None,
    memory_run_binding: MemoryRunBinding | None = None,
    reference_context: ResolvedReferenceContext | None = None,
    evaluation_context_arm: Any | None = None,
) -> tuple[str, list[ToolTrace], list[dict], str | None, TraceSummary | None]:
    """Run one TaskState -> ContextPack -> persisted Harness path."""

    async def safe_stop(message: str, **kwargs: Any):
        return await _unified_pre_harness_safe_stop(
            message, evaluation_context_arm=evaluation_context_arm, **kwargs,
        )

    if (
        settings.agent_control_runtime == "react_v0"
        and not settings.agent_react_live_enabled
    ):
        return await safe_stop(
            message,
            state=task_state,
            answer=(
                "当前实验运行时尚未通过验收，本轮已安全停止，"
                "没有调用业务工具。"
            ),
            failure_code="react_v0_not_accepted",
            on_answer_delta=on_answer_delta,
        )

    if settings.agent_control_runtime == "react_v1" and not (
        settings.agent_react_live_enabled
        and settings.agent_graph_v2_durable_enabled
    ):
        return await safe_stop(
            message,
            state=task_state,
            answer=(
                "当前 ReAct V1 尚未同时启用执行与持久化安全门，"
                "本轮已安全停止，没有调用业务工具。"
            ),
            failure_code="react_v1_not_accepted",
            on_answer_delta=on_answer_delta,
        )

    if settings.agent_control_runtime == "adaptive_hybrid_v1":
        return await safe_stop(
            message,
            state=task_state,
            answer="当前混合回退策略尚未进入验收，本轮已安全停止。",
            failure_code="adaptive_hybrid_v1_not_accepted",
            on_answer_delta=on_answer_delta,
        )

    client = (
        evaluation_context_arm.model_client
        if evaluation_context_arm is not None
        else get_client()
    )
    loop = asyncio.get_running_loop()
    deadline_at = deadline_at or (
        loop.time() + max(float(settings.agent_request_deadline_seconds), 0.1)
    )

    def remaining_budget() -> float:
        remaining = deadline_at - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    if resume is not None or restart:
        # Day-2 durable resume/restart: the live TaskState (server-loaded by
        # taskId) is already authoritative.  Re-running the extractor here
        # would advance the revision, replace the server-owned run marker and
        # destroy the parked clarification/checkpoint being recovered.
        state = task_state
    else:
        try:
            state = await asyncio.wait_for(
                _update_task_state_for_unified_harness(
                    message,
                    history=history,
                    client=client,
                    task_state=task_state,
                    on_task_state=on_task_state,
                    reference_context=reference_context,
                    evaluation_context_arm=evaluation_context_arm,
                ),
                timeout=remaining_budget(),
            )
        except asyncio.TimeoutError:
            answer = "抱歉，本轮请求超过了总执行时间限制，已安全停止；你可以稍后继续当前任务。"
            return await safe_stop(
                message, state=task_state, answer=answer,
                failure_code="task_state_update_timeout",
                on_answer_delta=on_answer_delta,
            )
        except Exception:
            logger.exception("Bounded TaskState extraction failed")
            answer = "抱歉，本轮任务状态构建失败，系统已安全停止；请稍后重试。"
            return await safe_stop(
                message, state=task_state, answer=answer,
                failure_code="task_state_update_failed",
                on_answer_delta=on_answer_delta,
            )
        if state.revision == task_state.revision:
            # The extractor returned the untouched state: no update_task_state tool
            # call was produced, so there is no original call to echo back for a
            # bounded repair. Safe-stop with zero business persistence, no forged
            # tool call and no auto-ready (the P1 boundary from review 001).
            answer = "抱歉，本轮任务状态构建失败，系统已安全停止；请稍后重试。"
            return await safe_stop(
                message, state=state, answer=answer,
                failure_code="task_state_update_missing",
                on_answer_delta=on_answer_delta,
            )
    if settings.agent_control_runtime == "react_v0":
        if resume is not None or restart:
            return await safe_stop(
                message,
                state=state,
                answer=(
                    "当前实验运行时暂未接入恢复执行，本轮已安全停止，"
                    "没有继续调用业务工具。"
                ),
                failure_code="react_v0_resume_not_supported",
                on_answer_delta=on_answer_delta,
            )
        return await _run_react_v0_live_agent(
            message,
            history=history,
            client=client,
            task_state=state,
            on_answer_delta=on_answer_delta,
            on_task_state=on_task_state,
            deadline_at=deadline_at,
            session_id=session_id,
            memory_run_binding=memory_run_binding,
        )
    react_shadow_observation = None
    if (
        settings.agent_control_runtime == "react_v0_shadow"
        and resume is None
        and not restart
    ):
        from .control.react_decision import observe_react_v0_shadow

        shadow_started = loop.time()
        react_shadow_observation = await observe_react_v0_shadow(
            state=state,
            user_message=message,
            allowed_tool_names=[
                schema["function"]["name"]
                for schema in _explicit_harness_tool_schemas(message, state)
            ],
            client=client,
            model=settings.deepseek_model,
            timeout_seconds=settings.agent_react_decision_timeout_seconds,
        )
        # Shadow may add latency, but it must not consume the authoritative
        # fixed_v1 execution budget or change its terminal result.
        deadline_at += loop.time() - shadow_started
    if (
        resume is None
        and state.status == "collecting_information"
        and state.pending_questions
        and settings.agent_graph_v2_durable_enabled
    ):
        # A durable clarification must be owned by LangGraph's real
        # clarification node + interrupt(), so the HTTP response carries the
        # server-owned run/thread identity and can later accept Command(resume).
        # The old direct question generator returned runId=None and never
        # created a durable interrupt, making Day-2 replay/restart impossible.
        return await _run_explicit_harness_agent(
            message,
            history=history,
            client=client,
            task_state=state,
            on_answer_delta=on_answer_delta,
            on_task_state=on_task_state,
            deadline_at=deadline_at,
            resume=resume,
            restart=restart,
            pause_resume=pause_resume,
            session_id=session_id,
            react_shadow_observation=react_shadow_observation,
            memory_run_binding=memory_run_binding,
            reference_context=reference_context,
            evaluation_context_arm=evaluation_context_arm,
        )
    if resume is None and state.status == "collecting_information" and state.pending_questions:
        try:
            pack = await asyncio.wait_for(
                build_context_pack(
                    state,
                    allowed_tools=[
                        schema["function"]["name"]
                        for schema in _explicit_harness_tool_schemas(message, state)
                    ],
                    history=history,
                ),
                timeout=remaining_budget(),
            )
            pack = await _authoritative_evaluation_pack(
                pack,
                evaluation_context_arm=evaluation_context_arm,
                task_state=state,
                message=message,
                tool_schemas=_explicit_harness_tool_schemas(message, state),
                phase="SHOPPING_FINAL_ANSWER",
            )
            question_messages = [
                context_pack_system_message(pack),
                {"role": "user", "content": message},
            ]
        except Exception:
            logger.exception("ContextPack build failed while asking for clarification")
            answer = "抱歉，本轮上下文构建失败，系统已安全停止，请稍后重试。"
            return await safe_stop(
                message, state=state, answer=answer,
                failure_code="clarification_context_build_failed",
                on_answer_delta=on_answer_delta,
            )
        try:
            answer = await asyncio.wait_for(
                _generate_pending_task_question(
                    client,
                    messages=question_messages,
                    state=state,
                    on_answer_delta=on_answer_delta,
                ),
                timeout=remaining_budget(),
            )
        except asyncio.TimeoutError:
            answer = "抱歉，本轮请求超过了总执行时间限制，已安全停止；请稍后继续。"
            return await safe_stop(
                message, state=state, answer=answer,
                failure_code="clarification_generation_timeout",
                on_answer_delta=on_answer_delta,
            )
        turn_messages = [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ]
        if react_shadow_observation is not None:
            run_id, summary = await _finish_react_shadow_terminal_trace(
                state=state,
                observation=react_shadow_observation,
                final_action="ask_user",
                session_id=session_id,
                pack=pack,
            )
            return answer, [], turn_messages, run_id, summary
        return answer, [], turn_messages, None, None

    if resume is None and not _should_use_explicit_harness(message, state):
        if _is_context_only_turn(message, state):
            return await _run_context_only_answer(
                message,
                history=history,
                client=client,
                state=state,
                on_answer_delta=on_answer_delta,
                deadline_at=deadline_at,
                react_shadow_observation=react_shadow_observation,
                evaluation_context_arm=evaluation_context_arm,
            )
        # This should normally be prevented by the pre-route contract check.
        # Fail closed instead of silently entering the legacy loop mid-turn.
        answer = "当前任务尚未具备可执行条件，请补充最关键的缺失信息后再试。"
        return await safe_stop(
            message, state=state, answer=answer,
            failure_code="harness_contract_not_executable",
            on_answer_delta=on_answer_delta,
        )

    return await _run_explicit_harness_agent(
        message,
        history=history,
        client=client,
        task_state=state,
        on_answer_delta=on_answer_delta,
        on_task_state=on_task_state,
        deadline_at=deadline_at,
        resume=resume,
        restart=restart,
        pause_resume=pause_resume,
        session_id=session_id,
        react_shadow_observation=react_shadow_observation,
        memory_run_binding=memory_run_binding,
        reference_context=reference_context,
        evaluation_context_arm=evaluation_context_arm,
    )


async def run_agent(
    message: str,
    history: list[dict] | None = None,
    on_answer_delta: AnswerDeltaCallback | None = None,
    task_state: TaskState | None = None,
    on_task_state: TaskStateCallback | None = None,
    domain_hint: str = "auto",
    resume: dict[str, Any] | None = None,
    restart: bool = False,
    pause_resume: dict[str, Any] | None = None,
    session_id: str | None = None,
    memory_run_binding: MemoryRunBinding | None = None,
    reference_context: ResolvedReferenceContext | None = None,
    evaluation_context_arm: Any | None = None,
) -> tuple[str, list[ToolTrace], list[dict], str | None, TraceSummary | None]:
    """Public Agent entry point with one observable routing decision."""

    if evaluation_context_arm is not None:
        if task_state is None:
            raise RuntimeError("evaluation_context_arm_requires_task_state")
        from .evaluation_context_arm import (
            apply_history_policy,
            validate_evaluation_capability,
        )

        evaluation_context_arm = validate_evaluation_capability(
            evaluation_context_arm,
            task_id=task_state.task_id,
            session_id=session_id,
        )
        history = apply_history_policy(history, evaluation_context_arm)

    loop = asyncio.get_running_loop()
    deadline_at = loop.time() + max(
        float(settings.agent_request_deadline_seconds), 0.1
    )

    # A resume/restart is a continuation of an existing durable run, not a new
    # user turn — the deterministic preflight must not short-circuit it with a
    # fresh-request answer (e.g. a missing-info question).
    preflight = None if (resume is not None or restart) else await _run_deterministic_preflight(
        message,
        task_state=task_state,
        domain_hint=domain_hint,
        on_answer_delta=on_answer_delta,
        on_task_state=on_task_state,
        deadline_at=deadline_at,
    )
    if preflight is not None:
        return preflight

    covered = bool(
        task_state is not None
        and _is_harness_contract_covered(message, task_state)
    )
    if resume is not None and task_state is None:
        # Fail closed: a resume without a live TaskState cannot be validated.
        answer = "当前任务状态不存在或已过期，无法继续；请重新开始当前任务。"
        if on_answer_delta is not None:
            await on_answer_delta(answer)
        return answer, [], [
            {"role": "user", "content": message},
            {"role": "assistant", "content": answer},
        ], None, None
    orchestrator = AgentOrchestrator(
        mode=(
            "legacy"
            if settings.agent_context_mode == "legacy"
            else settings.agent_orchestrator_mode
        ),
        legacy_fallback_enabled=settings.agent_legacy_fallback_enabled,
    )
    decision = orchestrator.decide(
        has_task_state=task_state is not None,
        harness_contract_covered=covered or resume is not None,
    )
    logger.info(
        "agent_orchestrator route=%s reason=%s task_id=%s",
        decision.route,
        decision.reason,
        task_state.task_id if task_state is not None else None,
    )

    if decision.route == "unified_harness":
        assert task_state is not None
        return await _run_unified_harness_agent(
            message,
            history=history,
            task_state=task_state,
            on_answer_delta=on_answer_delta,
            on_task_state=on_task_state,
            deadline_at=deadline_at,
            resume=resume,
            restart=restart,
            pause_resume=pause_resume,
            session_id=session_id,
            memory_run_binding=memory_run_binding,
            reference_context=reference_context,
            evaluation_context_arm=evaluation_context_arm,
        )
    if decision.route in {"legacy_rollback", "compatibility_fallback"}:
        if evaluation_context_arm is not None:
            raise RuntimeError("evaluation_context_arm_legacy_route_forbidden")
        return await _run_legacy_agent(
            message,
            history=history,
            on_answer_delta=on_answer_delta,
            task_state=task_state,
            on_task_state=on_task_state,
            domain_hint=domain_hint,
        )

    if decision.reason == "task_state_missing":
        answer = (
            "当前请求缺少统一执行链路必需的 TaskState，"
            "系统已安全停止，没有调用旧执行循环。"
        )
    else:
        answer = (
            "当前请求所需的 Agent 工具契约尚未迁移到统一执行链路，"
            "系统已安全停止，没有调用旧执行循环。"
        )
    if on_answer_delta is not None:
        await on_answer_delta(answer)
    return answer, [], [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ], None, None
