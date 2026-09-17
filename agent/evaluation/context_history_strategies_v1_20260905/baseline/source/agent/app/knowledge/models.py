import hashlib
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class KnowledgeChunk(BaseModel):
    """A normalized searchable unit for Agent RAG.

    A chunk can come from a review, shop profile, markdown policy document,
    FAQ entry, or any future knowledge source. Retrieval code should operate
    on this model instead of depending on one specific raw data shape.
    """

    model_config = ConfigDict(title="Agent 知识检索单元", populate_by_name=True)

    chunk_id: str = Field(
        alias="chunkId",
        min_length=1,
        description="检索单元的唯一编号，例如 review:review-005 或 policy:refund:001",
        examples=["review:review-005"],
    )
    source_type: str = Field(
        alias="sourceType",
        min_length=1,
        max_length=64,
        description="知识来源类型，例如 review、shop、policy、faq",
        examples=["review"],
    )
    source_id: str = Field(
        alias="sourceId",
        min_length=1,
        description="原始数据编号，例如 review-005、shop-3、refund-policy",
        examples=["review-005"],
    )
    chunk_index: int = Field(
        default=1,
        alias="chunkIndex",
        ge=1,
        description="该知识块在同一来源中的位置序号，从1开始",
    )
    source_version: str = Field(
        default="1",
        alias="sourceVersion",
        min_length=1,
        max_length=128,
        description="原始知识来源的版本；更新内容时chunkId保持不变，版本递增或变化",
    )
    content: str = Field(
        min_length=1,
        max_length=12000,
        description="真正参与检索和交给大模型引用的文本",
        examples=["店里有靠窗的单人位和插座，下午写作业或办公很舒服。"],
    )
    title: str | None = Field(
        default=None,
        max_length=256,
        description="可选标题，用于前端展示或引用说明",
        examples=["清晨手冲咖啡评论"],
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="用于过滤、排序、调试的补充字段，例如 shopId、typeId、city",
    )
    visibility: str = Field(
        default="public",
        pattern="^(public|user_private|admin_only)$",
        description="权限可见性：public、user_private、admin_only",
        examples=["public"],
    )
    owner_user_id: str | None = Field(
        default=None,
        alias="ownerUserId",
        max_length=128,
        description="当 visibility=user_private 时，用于标识知识所属用户",
    )
    language: str = Field(
        default="zh",
        max_length=16,
        description="内容语言，例如 zh、en",
        examples=["zh"],
    )
    tags: list[str] = Field(
        default_factory=list,
        max_length=20,
        description="可选标签，例如 quiet、office、refund",
    )
    content_hash: str | None = Field(
        default=None,
        alias="contentHash",
        pattern="^[0-9a-f]{64}$",
        description="content的SHA-256，用于判断是否需要重新生成向量",
    )
    updated_at: datetime | None = Field(
        default=None,
        alias="updatedAt",
        description="原始知识最后更新时间，用于同步与审计",
    )

    @model_validator(mode="after")
    def validate_publish_metadata(self) -> "KnowledgeChunk":
        expected_hash = hashlib.sha256(self.content.encode("utf-8")).hexdigest()
        if self.content_hash is None:
            self.content_hash = expected_hash
        elif self.content_hash != expected_hash:
            raise ValueError("contentHash must match content SHA-256")
        if self.visibility == "user_private" and not self.owner_user_id:
            raise ValueError("user_private chunks require ownerUserId")
        return self

    def to_citation(self, *, score: float | None = None, quote: str | None = None) -> "Citation":
        """Create a structured citation from this chunk."""

        return Citation(
            chunk_id=self.chunk_id,
            source_type=self.source_type,
            source_id=self.source_id,
            title=self.title,
            quote=quote or self.content,
            score=score,
            metadata=self.metadata,
        )


class Citation(BaseModel):
    """A structured reference used by Agent answers and evaluations."""

    model_config = ConfigDict(title="Agent 回答引用来源", populate_by_name=True)

    chunk_id: str = Field(alias="chunkId", min_length=1, description="被引用的 chunk 编号")
    source_type: str = Field(alias="sourceType", min_length=1, description="被引用的知识来源类型")
    source_id: str = Field(alias="sourceId", min_length=1, description="被引用的原始数据编号")
    title: str | None = Field(default=None, description="引用标题")
    quote: str | None = Field(default=None, description="被模型使用的证据原文或片段")
    score: float | None = Field(default=None, description="检索或重排分数")
    metadata: dict[str, Any] = Field(default_factory=dict, description="引用对应的补充信息")


class RetrievalStep(BaseModel):
    """One observable step inside a retrieval pipeline."""

    model_config = ConfigDict(title="Agent 检索步骤", populate_by_name=True)

    name: str = Field(min_length=1, description="步骤名称，例如 router、vector、bm25、rerank")
    duration_ms: float | None = Field(
        default=None,
        alias="durationMs",
        ge=0,
        description="该步骤耗时，单位毫秒",
    )
    detail: dict[str, Any] = Field(default_factory=dict, description="该步骤的补充信息")


class RetrievalTrace(BaseModel):
    """Trace data for a full Agent RAG retrieval request."""

    model_config = ConfigDict(title="Agent RAG 检索追踪", populate_by_name=True)

    query: str = Field(min_length=1, description="用户原始检索问题")
    rewritten_query: str | None = Field(
        default=None,
        alias="rewrittenQuery",
        description="可选的改写后问题",
    )
    selected_sources: list[str] = Field(
        default_factory=list,
        alias="selectedSources",
        description="Router 选中的知识源，例如 reviews、policy_docs",
    )
    filters: dict[str, Any] = Field(
        default_factory=dict,
        description="检索使用的 metadata 过滤条件",
    )
    candidate_count: int = Field(
        default=0,
        alias="candidateCount",
        ge=0,
        description="召回到的候选 chunk 数量",
    )
    returned_count: int = Field(
        default=0,
        alias="returnedCount",
        ge=0,
        description="最终返回给 Agent/LLM 的 chunk 数量",
    )
    duration_ms: float | None = Field(
        default=None,
        alias="durationMs",
        ge=0,
        description="完整检索链路耗时，单位毫秒",
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description="最终可用于回答引用的证据列表",
    )
    steps: list[RetrievalStep] = Field(
        default_factory=list,
        description="检索链路中的可观察步骤",
    )


class SearchKnowledgeResult(BaseModel):
    """Unified result returned by any Agent knowledge retrieval flow."""

    model_config = ConfigDict(title="Agent 统一知识检索结果", populate_by_name=True)

    chunks: list[KnowledgeChunk] = Field(
        default_factory=list,
        description="本次检索返回的统一知识片段",
    )
    citations: list[Citation] = Field(
        default_factory=list,
        description="由 chunks 生成的结构化引用",
    )
    trace: RetrievalTrace = Field(description="本次检索过程记录")
    legacy_reviews: list[dict[str, Any]] = Field(
        default_factory=list,
        exclude=True,
        description="仅供旧评论工具兼容返回使用，不进入统一结果序列化",
    )
