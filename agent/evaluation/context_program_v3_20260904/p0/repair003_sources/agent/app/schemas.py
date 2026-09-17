import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthResponse(BaseModel):
    model_config = ConfigDict(title="健康检查响应", populate_by_name=True)

    service: str = Field(description="服务名称", examples=["电商导购 Agent 服务"])
    status: str = Field(description="服务状态", examples=["运行中"])
    # REPAIR-004: live Agent health must expose the process's ACTUALLY loaded
    # non-secret runtime retrieval config (fixed field names), so the real
    # runner can reconcile observable config instead of echoing expectations.
    # Empty defaults keep any legacy construction valid; a read-back that sees
    # the empty value treats it as missing and fails BLOCKED.
    backend_base_url: str = Field(
        default="",
        alias="backendBaseUrl",
        description="本进程实际加载的检索后端 base URL（非秘密运行配置，用于对外可核验的 active-backend 断言）",
        examples=["http://127.0.0.1:18082"],
    )
    product_retrieval_mode: str = Field(
        default="",
        alias="productRetrievalMode",
        description="本进程实际加载的检索模式（非秘密运行配置，用于对外可核验的 active-backend 断言）",
        examples=["elasticsearch"],
    )


class ChatRequest(BaseModel):
    model_config = ConfigDict(title="智能体对话请求", populate_by_name=True)

    message: str = Field(
        min_length=1,
        max_length=2000,
        description="用户输入的问题",
        examples=["帮我找附近适合两个人吃的火锅店，预算 150 元以内。"],
    )
    session_id: str | None = Field(
        default=None,
        alias="sessionId",
        min_length=1,
        max_length=64,
        description="可选的会话编号。相同 sessionId 会共享最近的对话历史。",
        examples=["browser-550e8400-e29b-41d4-a716-446655440000"],
    )
    domain_hint: Literal["auto", "local_life", "ecommerce"] = Field(
        default="auto",
        alias="domainHint",
        description="领域提示；历史专用接口仍接受 local_life。",
    )


class ReferenceContextHint(BaseModel):
    """Opaque server receipt plus untrusted browser presentation hints."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    handle: str = Field(min_length=40, max_length=64, strict=True)
    presentation_mode: Literal["compact", "expanded"] = Field(
        default="compact",
        alias="presentationMode",
    )
    focused_product_id: str | None = Field(
        default=None,
        alias="focusedProductId",
        min_length=1,
        max_length=40,
        strict=True,
    )

    @field_validator("handle")
    @classmethod
    def validate_handle(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_-]{40,64}", value) is None:
            raise ValueError("reference handle is invalid")
        return value

    @field_validator("focused_product_id")
    @classmethod
    def validate_focused_product_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if re.fullmatch(r"[1-9]\d*", value) is None:
            raise ValueError("focusedProductId must be a canonical positive decimal string")
        return value


class EcommerceChatRequest(ChatRequest):
    model_config = ConfigDict(title="电商导购对话请求", populate_by_name=True)

    domain_hint: Literal["auto", "ecommerce"] = Field(
        default="auto",
        alias="domainHint",
        description="正式 Agent 入口只支持电商导购；auto 等价于 ecommerce。",
    )
    recipient_scope: Literal["self", "other", "unknown"] = Field(
        default="unknown",
        alias="recipientScope",
        description="用户显式选择的本轮购买对象；长期记忆仅在 self 时可应用。",
    )
    reference_context: ReferenceContextHint | None = Field(
        default=None,
        alias="referenceContext",
        description="上一次商品卡片展示的短期引用句柄与当前UI焦点；服务端会重新校验。",
    )


class RagChatRequest(ChatRequest):
    mode: str = Field(
        default="auto",
        pattern="^(auto|standard|advanced)$",
        description=(
            "auto按问题选择快速Hybrid或高级内容判断；standard固定使用快速Hybrid；"
            "advanced固定启用内容重排和引用审计。"
        ),
        examples=["auto"],
    )


class ToolTrace(BaseModel):
    model_config = ConfigDict(title="工具调用记录", populate_by_name=True)

    tool: str = Field(description="工具名称", examples=["backend_health"])
    ok: bool = Field(description="工具调用是否成功", examples=[True])
    duration_ms: float | None = Field(
        default=None,
        alias="durationMs",
        ge=0,
        description="本次工具调用耗时，单位毫秒",
        examples=[42.18],
    )
    detail: str | dict[str, Any] | None = Field(
        default=None,
        description="工具调用的补充信息",
        examples=["后端服务可访问"],
    )
    knowledge_result: dict[str, Any] | None = Field(
        default=None,
        alias="knowledgeResult",
        description="可选的统一知识检索结果；不会作为工具detail重复发送给大模型",
    )


class MemoryLoadTrace(BaseModel):
    """Public-safe aggregate for one governed memory resolution attempt."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    reason: Literal[
        "disabled", "no_browser_session", "category_unavailable",
        "recipient_suppressed", "session_rejected", "authority_exception",
        "durable_guard_unavailable",
        "durable_task_suppressed",
        "projection_disabled", "projection_no_credential",
        "projection_unavailable", "projection_invalid",
        "available_empty", "available_retained",
    ]
    duration_ms: float = Field(alias="durationMs", ge=0)
    eligible: bool
    attempted: bool
    projection_outcome: Literal[
        "not_attempted", "available", "disabled", "no_credential",
        "unavailable", "invalid", "session_rejected", "authority_exception",
    ] = Field(alias="projectionOutcome")
    retained_count: int = Field(alias="retainedCount", ge=0)
    truncated: bool


class RequestTrace(BaseModel):
    model_config = ConfigDict(title="请求追踪信息", populate_by_name=True)

    request_id: str = Field(
        alias="requestId",
        description="本次请求的唯一追踪编号，用于把一次前端请求、后端日志和工具调用串起来",
        examples=["req-a1b2c3d4"],
    )
    route: str = Field(description="本次请求进入的接口路径", examples=["/agent/chat-llm"])
    status: str = Field(
        description="兼容字段：请求处理结果，不代表工具或业务成功",
        examples=["ok"],
    )
    transport_status: Literal["response_generated", "response_pending"] = Field(
        default="response_generated",
        alias="transportStatus",
        description="服务端响应封装状态；不表示客户端一定已完整接收响应",
    )
    tool_status: Literal["not_called", "ok", "failed", "mixed"] = Field(
        default="not_called",
        alias="toolStatus",
        description="本次请求内全部工具调用的聚合结果",
    )
    failure_class: Literal[
        "none", "request_failure", "agent_failure", "tool_failure",
        "request_and_agent_failure", "request_and_tool_failure",
        "agent_and_tool_failure", "request_agent_and_tool_failure",
    ] = Field(
        default="none",
        alias="failureClass",
        description="将请求处理失败与工具业务失败分开的稳定分类",
    )
    candidate_count: int = Field(
        default=0,
        alias="candidateCount",
        ge=0,
        description="最后一次成功 search_products 的严格合法候选ID数量",
    )
    run_id: str | None = Field(
        default=None,
        alias="runId",
        description="进入统一Harness时关联的Agent运行身份",
    )
    agent_status: Literal["not_run", "ok", "failed"] = Field(
        default="not_run",
        alias="agentStatus",
        description="统一Agent结构化终态；与HTTP和工具状态正交",
    )
    agent_final_action: str | None = Field(
        default=None,
        alias="agentFinalAction",
        description="AgentRunTrace的结构化finalAction",
    )
    agent_failure_code: str | None = Field(
        default=None,
        alias="agentFailureCode",
        description="Agent安全停止或降级的稳定错误码",
    )
    total_duration_ms: float = Field(
        alias="totalDurationMs",
        ge=0,
        description="接口从接收到请求到组织完响应的总耗时，单位毫秒",
        examples=[238.42],
    )
    slow: bool = Field(
        default=False,
        description="本次请求是否超过慢请求阈值",
        examples=[False],
    )
    slow_threshold_ms: float = Field(
        alias="slowThresholdMs",
        ge=0,
        description="慢请求判断阈值，单位毫秒",
        examples=[3000.0],
    )
    bottleneck: str = Field(
        default="none",
        description="慢请求的初步瓶颈分类：none、agent、retrieval、llm、mixed 或 unknown",
        examples=["agent"],
    )
    agent_duration_ms: float | None = Field(
        default=None,
        alias="agentDurationMs",
        ge=0,
        description="统一 Agent 主循环耗时，包含模型调用、工具调用和工具结果回填，单位毫秒",
        examples=[221.18],
    )
    llm_duration_ms: float | None = Field(
        default=None,
        alias="llmDurationMs",
        ge=0,
        description="直接调用大模型生成回答的耗时，单位毫秒",
        examples=[180.25],
    )
    model_call_counts: dict[str, int] = Field(
        default_factory=dict,
        alias="modelCallCounts",
        description=(
            "按 task_manager、task_state、react_decision、final_answer 分层的模型调用次数"
        ),
    )
    model_call_failures: dict[str, int] = Field(
        default_factory=dict,
        alias="modelCallFailures",
        description="按模型阶段记录已发起但失败的调用次数",
    )
    llm_duration_by_stage_ms: dict[str, float] = Field(
        default_factory=dict,
        alias="llmDurationByStageMs",
        description="各模型阶段的累计耗时，单位毫秒",
    )
    retrieval_duration_ms: float | None = Field(
        default=None,
        alias="retrievalDurationMs",
        ge=0,
        description="RAG 评论检索耗时，单位毫秒",
        examples=[42.91],
    )
    tool_duration_ms: float | None = Field(
        default=None,
        alias="toolDurationMs",
        ge=0,
        description="本次请求所有工具调用耗时之和，单位毫秒",
        examples=[128.64],
    )
    tool_count: int = Field(
        default=0,
        alias="toolCount",
        ge=0,
        description="本次请求触发的工具调用次数",
        examples=[1],
    )
    tool_names: list[str] = Field(
        default_factory=list,
        alias="toolNames",
        description="本次请求触发的工具名称列表",
        examples=[["search_reviews"]],
    )
    review_ids: list[str] = Field(
        default_factory=list,
        alias="reviewIds",
        description="本次请求检索到并暴露给回答链路的评论编号",
        examples=[["review-005", "review-006"]],
    )
    task_id: str | None = Field(
        default=None,
        alias="taskId",
        description="本轮对话绑定的TaskState编号；未提供sessionId时为空。",
    )
    task_revision: int | None = Field(
        default=None,
        alias="taskRevision",
        ge=1,
        description="本轮完成时的TaskState revision。",
    )
    memory_load: MemoryLoadTrace | None = Field(
        default=None,
        alias="memoryLoad",
        description="不含身份、凭据、记忆内容或原始ID的长期记忆加载摘要。",
    )


class ChatResponse(BaseModel):
    model_config = ConfigDict(title="智能体对话响应", populate_by_name=True)

    answer: str = Field(description="智能体回复")
    tool_trace: list[ToolTrace] = Field(default_factory=list, description="本轮对话触发的工具调用记录")
    trace: RequestTrace | None = Field(default=None, description="本轮请求的工程追踪信息")
    run_id: str | None = Field(default=None, alias="runId", description="Agent运行时追踪ID")
    trace_summary: dict[str, Any] | None = Field(default=None, alias="traceSummary", description="TraceSummary精简版")
    task_state: dict[str, Any] | None = Field(
        default=None,
        alias="taskState",
        description="相同sessionId复用的最新TaskState快照。",
    )
    task_relation: dict[str, Any] | None = Field(
        default=None,
        alias="taskRelation",
        description="本轮消息与当前/历史任务之间的TaskManager判断。",
    )

    reference_context: dict[str, Any] | None = Field(
        default=None,
        alias="referenceContext",
        description=(
            "本轮完成后重新绑定到最新TaskState revision的短期商品展示收据；"
            "不包含服务端候选或会话身份。"
        ),
    )


    guide_result: dict[str, Any] | None = Field(
        default=None,
        alias="guideResult",
        description="商品导购的证据化决选结果；非电商请求为 null。",
    )


class RagSource(BaseModel):
    model_config = ConfigDict(title="RAG 评论来源", populate_by_name=True)

    review_id: str = Field(alias="reviewId", description="评论在原始语料中的唯一编号")
    shop_id: int = Field(alias="shopId", description="评论所属商户编号")
    shop_name: str = Field(alias="shopName", description="评论所属商户名称")
    text: str = Field(description="本次交给大模型的评论原文")
    score: float = Field(description="问题向量与评论向量的相似度分数")


class RagChatResponse(BaseModel):
    model_config = ConfigDict(title="RAG 问答响应")

    answer: str = Field(description="依据检索评论生成的回答")
    sources: list[RagSource] = Field(
        default_factory=list,
        description="本次实际提供给大模型的评论证据，按检索相关度排序",
    )
    mode: str = Field(
        default="auto",
        description="本次实际使用的RAG模式。",
    )
    pipeline: dict[str, Any] | None = Field(
        default=None,
        description="advanced模式的阶段、检索trace和确定性引用审计。",
    )
    trace: RequestTrace | None = Field(default=None, description="本轮请求的工程追踪信息")


class RecommendedShop(BaseModel):
    model_config = ConfigDict(title="推荐商户卡片", populate_by_name=True)

    shop_id: int = Field(alias="shopId", description="推荐商户编号")
    shop_name: str = Field(alias="shopName", description="推荐商户名称")
    type_id: int = Field(alias="typeId", description="商户分类编号")
    avg_price: int = Field(alias="avgPrice", description="人均价格，单位元")
    address: str = Field(description="商户地址")
    reason: str = Field(description="推荐原因")
    trigger_shop_ids: list[int] = Field(
        default_factory=list,
        alias="triggerShopIds",
        description="触发本次推荐的历史商户编号",
    )
    trigger_shop_names: list[str] = Field(
        default_factory=list,
        alias="triggerShopNames",
        description="触发本次推荐的历史商户名称，用于向用户解释推荐原因",
    )
    score: float | None = Field(default=None, description="推荐分数")
    distance_meters: float | None = Field(
        default=None,
        alias="distanceMeters",
        description="用户当前位置到商户的距离，单位米；未提供位置时为空",
    )


class ShopRecommendationsResponse(BaseModel):
    model_config = ConfigDict(title="猜你喜欢推荐响应", populate_by_name=True)

    user_id: str = Field(alias="userId", description="本次推荐所使用的用户编号")
    shops: list[RecommendedShop] = Field(default_factory=list, description="推荐商户列表")
    tool_trace: ToolTrace | None = Field(
        default=None,
        alias="toolTrace",
        description="推荐接口内部调用 Spring Boot 的工具记录",
    )
    trace: RequestTrace | None = Field(default=None, description="本轮请求的工程追踪信息")


class SessionClearResponse(BaseModel):
    model_config = ConfigDict(title="清除会话响应")

    session_id: str = Field(alias="sessionId", description="被清除的会话编号")
    cleared: bool = Field(description="是否已完成清除")


class ReviewVectorSyncRequest(BaseModel):
    model_config = ConfigDict(title="评论向量同步请求", populate_by_name=True)

    shop_id: int = Field(alias="shopId", gt=0, description="评论所属商户编号")
    shop_name: str = Field(alias="shopName", min_length=1, max_length=128)
    content: str = Field(min_length=1, max_length=2000, description="评论正文")
    source: str = Field(default="user", max_length=32)
    language: str = Field(default="zh", max_length=16)
    content_zh: str | None = Field(default=None, alias="contentZh", max_length=2000)
    translation_status: str = Field(default="not_required", alias="translationStatus", max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=10)


class ReviewVectorSyncResponse(BaseModel):
    model_config = ConfigDict(title="评论向量同步响应", populate_by_name=True)

    review_id: str = Field(alias="reviewId")
    operation: str = Field(description="本次执行的同步操作")
    synced: bool = Field(description="Qdrant 是否已完成同步")
