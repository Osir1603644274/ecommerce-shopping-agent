from pathlib import Path

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


AGENT_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = AGENT_ROOT.parent


class Settings(BaseSettings):
    backend_base_url: str = "http://localhost:8080"
    memory_projection_client_enabled: bool = False
    memory_projection_client_timeout_seconds: float = 2.0
    # Same-origin browser session for the explicit-memory canary. Java JWTs
    # remain server-side in Redis; the browser receives only an HttpOnly id.
    memory_bff_enabled: bool = False
    memory_bff_session_ttl_seconds: int = 1800
    memory_bff_cookie_secure: bool = False
    memory_bff_canary_usernames: str = "memory-v13-e2e"
    memory_bff_epoch: str = "v13-canary-1"
    # Same-origin browser session used only by the opt-in commerce demo.  Java
    # JWTs stay in Redis and are never exposed to browser JavaScript.
    commerce_demo_enabled: bool = False
    commerce_demo_session_ttl_seconds: int = 1800
    commerce_demo_cookie_secure: bool = False
    # This opens only the Java simulator bridge used by the local demo.  It is
    # independent from PAYMENT_SIMULATOR_ENABLED and remains false by default.
    commerce_demo_payment_simulation_enabled: bool = False
    memory_candidate_ttl_seconds: int = 900
    # Server-owned receipt for the exact product-card order published to one
    # browser session.  It is deliberately short-lived and is validated again
    # against the current TaskState/CandidateScope on every reference turn.
    reference_context_ttl_seconds: int = 3600
    memory_candidate_stream_key: str = "memory:candidate:v13:stream"
    memory_rerank_lambda: float = 0.01
    memory_active_catalog_revision: str = "used-phone-439-09807c773ce6"
    memory_catalog_values_path: str = (
        "evaluation/assets/used_phone_memory_catalog_v13_20260830/catalog-values.jsonl"
    )
    memory_catalog_values_sha256: str = (
        "2ce140b3670521078ba1821eaaf55029d73efc4715f6c0f6e16c117180756c36"
    )
    qdrant_url: str = "http://localhost:6333"
    ecommerce_guide_enabled: bool = True
    agent_transaction_enabled: bool = False
    transaction_confirmation_ttl_seconds: int = 300
    # Once an explicit confirmation has been durably recorded, retain the
    # immutable command long enough to reconcile a process crash without
    # asking the model to reconstruct write arguments.
    transaction_execution_ttl_seconds: int = 7 * 24 * 3600
    product_collection_name: str = "product_catalog"
    # Production normal path: Java -> Elasticsearch lexical Top-50.  The local
    # BM25 and dense/RRF paths remain explicit rollback/experiment modes only.
    product_retrieval_mode: str = "elasticsearch"
    shopping_state_authority: str = "v2"
    product_vector_backend: str = "qdrant"
    product_vector_timeout_seconds: float = 3.0
    # Optional semantic title reranking is deliberately off until the frozen
    # real-query qrel shows a quality gain worth its latency and model cost.
    product_title_reranker_enabled: bool = False
    product_title_reranker_candidate_limit: int = 20
    product_title_reranker_timeout_seconds: float = 6.0
    # Synthetic reference prices are a derived sidecar, never verified Java
    # snapshot prices.  Budget use requires the explicit audit-visible policy.
    used_phone_synthetic_price_policy: str = "disabled"
    used_phone_synthetic_price_dir: str = (
        "data/derived/ecommerce/used_phone_synthetic_reference_price_v1"
    )
    # Agent runtime tracing
    agent_trace_ttl_seconds: int = 86400  # 24h
    agent_trace_debug_enabled: bool = False
    agent_trace_debug_key: str = ""
    # V1 per-provider-call receipts are redacted and opt-in until the shadow
    # and evaluation gates complete.  Evaluation mode fails closed if neither
    # Redis nor the append-only spool can persist a receipt.
    model_call_receipts_enabled: bool = False
    model_call_receipt_evaluation_mode: bool = False
    model_call_receipt_ttl_seconds: int = 86400
    model_call_receipt_spool_path: str = (
        ".runtime/context-multiagent-v1/model-call-receipts.jsonl"
    )
    context_compiler_shadow_enabled: bool = False
    context_receipts_enabled: bool = False
    context_receipt_evaluation_mode: bool = False
    context_receipt_ttl_seconds: int = 86400
    context_receipt_spool_path: str = (
        ".runtime/context-multiagent-v1/context-receipts.jsonl"
    )
    # Multi-Agent V2 is a real coordinator -> read-only EvidenceResearchAgent
    # -> coordinator path.  Its bounded synthetic holdout passed on 2026-09-02;
    # only evidence-gap turns route here, and every failure degrades to the
    # already-validated single-Agent answer. Transaction authority is absent.
    multi_agent_v2_enabled: bool = True
    multi_agent_v2_deadline_seconds: float = 20.0
    # V1 name remains for compatibility with older evaluation packages.
    evidence_research_agent_v1_enabled: bool = False
    research_merge_claim_ttl_seconds: int = 86400
    # Durable local capture of web-submitted queries.  The store redacts
    # explicit credentials, is independent from chat memory deletion, and is
    # never consumed by online retrieval or prompts.
    web_query_intake_enabled: bool = False
    web_query_intake_path: str = ".runtime/web-query-intake/queries.sqlite3"
    web_query_intake_retention_days: int = 90
    web_query_intake_write_timeout_seconds: float = 0.25
    # Fast-preview "先快后完整" path for used-phone web queries.  Off by default
    # so JSON flows and existing tests keep their exact behavior; the live demo
    # enables it explicitly.  The catalog revision token is bumped whenever the
    # frozen used-phone product dataset changes so previews never reuse stale
    # candidates after a data refresh.
    used_phone_fast_preview_enabled: bool = False
    used_phone_fast_preview_cache_max_entries: int = 256
    used_phone_fast_preview_catalog_revision: str = "used-phone-catalog-v1-2026-08"
    # ContextPack is the production default. Legacy remains an explicit rollback
    # alias and must never run inside the unified Harness.
    agent_context_mode: str = "context_pack"
    # Unified Harness is the production path. Legacy is an explicit rollback
    # mode only; automatic per-request fallback is fail-closed by default.
    agent_orchestrator_mode: str = "unified"
    agent_legacy_fallback_enabled: bool = False
    # Decision-control runtime. ``react_v1`` is the web production default;
    # ``fixed_v1`` remains the explicit rollback and paired-control path.
    # Historical control modes remain available only to versioned evaluation
    # runners that deliberately open the independent experiment gate.
    agent_experimental_control_runtimes_enabled: bool = False
    agent_control_runtime: str = "react_v1"
    # ``react_v0`` remains fail-closed unless an isolated pilot explicitly
    # enables executable read-only actions.  This second gate prevents an
    # accidental runtime-name change from replacing fixed_v1 in normal use.
    agent_react_live_enabled: bool = True
    agent_react_max_iterations: int = 4
    agent_react_v1_max_model_decisions: int = 2
    # Shadow has an independent bounded model call. Its elapsed time is not
    # charged against the authoritative fixed_v1 execution budget.
    agent_react_decision_timeout_seconds: float = 15.0
    # A live ReAct answer-model call must leave enough request budget for a
    # Validator-only server fallback when the provider stalls.
    agent_react_final_answer_timeout_seconds: float = 30.0
    agent_executor_lease_seconds: int = 60
    # DeepSeek V4 thinking-mode extraction can legitimately consume ~20s
    # before the bounded Harness begins. Keep enough budget for one such
    # extraction plus a read-only retrieval step; individual vector/tool calls
    # retain their own much smaller timeouts.
    agent_request_deadline_seconds: float = 45.0
    # EvidenceCritic shadow mode
    evidence_critic_enabled: bool = False
    evidence_critic_timeout_seconds: float = 12.0
    evidence_critic_shutdown_timeout_seconds: float = 5.0
    # Tool transport mode: live | record | replay | mcp_in_process_readonly.
    # MCP remains opt-in through the separate strict MCP flags below.
    agent_tool_transport_mode: str = "live"
    agent_tool_transport_record_path: str = "./recordings/tool_calls.jsonl"
    agent_tool_transport_replay_path: str = "./recordings/tool_calls.jsonl"
    agent_tool_transport_replay_strict: bool = True
    # Day4 MCP is an explicitly opt-in, in-process read-only gateway.  The
    # default keeps every existing Harness/Executor path on its current
    # transport and does not expose an MCP server.
    agent_mcp_readonly_enabled: bool = False
    agent_mcp_transport_mode: str = "disabled"
    # V2 control-plane graph (graph/). Default-off; V1 (react_graph) remains
    # the production path. Invalid combo (enabled AND shadow_enabled) is
    # fail-closed to V1 at request time.
    agent_graph_v2_enabled: bool = False
    agent_graph_v2_shadow_enabled: bool = False
    # DAY2 durable checkpoint + real interrupt()/resume() control plane.  This
    # flag turns on the durable plain-Redis saver, clarification/operator pause
    # boundaries and process-restart resume path.  It is required by the
    # production react_v1 control policy; fixed_v1 remains the rollback path.
    agent_graph_v2_durable_enabled: bool = True
    # Record-only server-owned Strategy routing shadow.  It never runs tools,
    # models, BOUNDED_REACT, or durable receipt/replay logic; default off keeps
    # V1/V2 request behavior unchanged.
    agent_strategy_shadow_enabled: bool = False
    # Durable Strategy receipt ledger is an opt-in seam only; Batch 2a does
    # not wire it into graph dispatch.
    agent_strategy_receipt_ledger_enabled: bool = False
    agent_strategy_receipt_ledger_ttl_seconds: int = 86400
    agent_strategy_receipt_ledger_lease_seconds: int = 30
    agent_strategy_receipt_ledger_recovery_max: int = 1
    # DAY2 test-only deterministic fault point (requirement #8): after a live
    # search step's output AND TaskState receipt are persisted but before the
    # next graph checkpoint is confirmed, the durable executor terminates the
    # process at that exact window.  Values: "" (off) | "after_executor_receipt".
    # Default off — production V1/V2 runs never fire the fault point.
    agent_graph_v2_fault_point: str = ""
    rag_collection_name: str = "merchant_reviews"
    knowledge_collection_name: str = "knowledge_chunks"
    knowledge_source_aware_enabled: bool = False
    knowledge_review_hybrid_enabled: bool = False
    knowledge_review_hybrid_candidate_limit: int = 30
    knowledge_review_hybrid_vector_weight: float = 0.20
    knowledge_review_hybrid_bm25_weight: float = 0.80
    knowledge_review_excluded_sources: tuple[str, ...] = ("seed",)
    knowledge_advanced_reranker_timeout_seconds: float = 18.0
    rag_embedding_model: str = "BAAI/bge-small-zh-v1.5"
    rag_model_cache_dir: str = ".cache/fastembed"
    rag_data_dir: str = "rag"

    # Redis 地址：用来存多轮对话的会话历史，替代原来的进程内存字典。
    # 格式 redis://主机:端口/库编号；本机默认连 localhost，docker 里由 REDIS_URL 环境变量覆盖成 redis://redis:6379/0。
    redis_url: str = "redis://localhost:6379/0"

    # DeepSeek 大模型配置。DeepSeek 接口与 OpenAI 兼容，所以用 openai 库 + base_url 指向 DeepSeek。
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    # Dedicated extraction budget; a truncated tool call must never be applied.
    task_state_extraction_max_tokens: int = Field(default=4096, ge=128, le=16384)
    # Opt-in only: requires the official /beta endpoint, never silently reroutes.
    task_state_extraction_strict_enabled: bool = False

    @field_validator("rag_model_cache_dir", "rag_data_dir", "memory_catalog_values_path")
    @classmethod
    def resolve_agent_path(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = AGENT_ROOT / path
        return str(path.resolve())

    @field_validator("web_query_intake_path")
    @classmethod
    def resolve_web_query_intake_path(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = REPOSITORY_ROOT / path
        return str(path.resolve())

    @field_validator("used_phone_synthetic_price_dir")
    @classmethod
    def resolve_repository_path(cls, value: str) -> str:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = REPOSITORY_ROOT / path
        return str(path.resolve())

    @field_validator("product_retrieval_mode")
    @classmethod
    def validate_product_retrieval_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        aliases = {
            "es": "elasticsearch",
            "elasticsearch": "elasticsearch",
            "rrf": "hybrid",
            "hybrid": "hybrid",
            "bm25": "bm25",
        }
        if normalized not in aliases:
            raise ValueError(
                "product_retrieval_mode must be elasticsearch, bm25, or hybrid"
            )
        return aliases[normalized]

    @field_validator("memory_rerank_lambda")
    @classmethod
    def validate_memory_rerank_lambda(cls, value: float) -> float:
        normalized = float(value)
        if normalized not in {0.01, 0.03, 0.05, 0.08}:
            raise ValueError("memory_rerank_lambda must belong to the frozen grid")
        return normalized

    @field_validator("shopping_state_authority")
    @classmethod
    def validate_shopping_state_authority(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"v2", "legacy"}:
            raise ValueError("shopping_state_authority must be v2 or legacy")
        return normalized

    @field_validator("product_vector_backend")
    @classmethod
    def validate_product_vector_backend(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"qdrant", "local"}:
            raise ValueError("product_vector_backend must be qdrant or local")
        return normalized

    @field_validator("used_phone_synthetic_price_policy")
    @classmethod
    def validate_synthetic_price_policy(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"disabled", "display_only", "budget_and_ranking"}:
            raise ValueError(
                "used_phone_synthetic_price_policy must be disabled, display_only, "
                "or budget_and_ranking"
            )
        return normalized

    @field_validator("agent_context_mode")
    @classmethod
    def validate_agent_context_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"legacy", "context_pack"}:
            raise ValueError("agent_context_mode must be legacy or context_pack")
        return normalized

    @field_validator("agent_orchestrator_mode")
    @classmethod
    def validate_agent_orchestrator_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"unified", "legacy"}:
            raise ValueError("agent_orchestrator_mode must be unified or legacy")
        return normalized

    @field_validator("agent_control_runtime")
    @classmethod
    def validate_agent_control_runtime(
        cls,
        value: str,
        info: ValidationInfo,
    ) -> str:
        normalized = value.strip().lower()
        production_runtimes = {"fixed_v1", "react_v1"}
        experimental_runtimes = {
            "react_v0_shadow",
            "react_v0",
            "adaptive_hybrid_v1",
        }
        if normalized in production_runtimes:
            return normalized
        if normalized in experimental_runtimes:
            if bool(info.data.get("agent_experimental_control_runtimes_enabled")):
                return normalized
            raise ValueError(
                "historical agent_control_runtime requires "
                "agent_experimental_control_runtimes_enabled=true"
            )
        raise ValueError(
            "agent_control_runtime must be fixed_v1 or react_v1; historical "
            "evaluation modes additionally require the experimental runtime gate"
        )

    @field_validator("agent_react_v1_max_model_decisions")
    @classmethod
    def validate_agent_react_v1_max_model_decisions(cls, value: int) -> int:
        if value != 2:
            raise ValueError("agent_react_v1_max_model_decisions must equal 2")
        return value

    @field_validator("agent_react_decision_timeout_seconds")
    @classmethod
    def validate_agent_react_decision_timeout_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("agent_react_decision_timeout_seconds must be positive")
        return value

    @field_validator("agent_react_final_answer_timeout_seconds")
    @classmethod
    def validate_agent_react_final_answer_timeout_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("agent_react_final_answer_timeout_seconds must be positive")
        return value

    @field_validator("agent_react_max_iterations")
    @classmethod
    def validate_agent_react_max_iterations(cls, value: int) -> int:
        if not 1 <= value <= 8:
            raise ValueError("agent_react_max_iterations must be between 1 and 8")
        return value

    @field_validator("agent_mcp_transport_mode")
    @classmethod
    def validate_agent_mcp_transport_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"disabled", "in_process_readonly"}:
            raise ValueError(
                "agent_mcp_transport_mode must be disabled or in_process_readonly"
            )
        return normalized

    @field_validator("agent_tool_transport_mode")
    @classmethod
    def validate_agent_tool_transport_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {
            "live",
            "record",
            "replay",
            "mcp_in_process_readonly",
        }:
            raise ValueError(
                "agent_tool_transport_mode must be live, record, replay, "
                "or mcp_in_process_readonly"
            )
        return normalized

    # 配置文件位置跟随项目本身，不受启动命令所在目录影响。
    # agent/.env 可用于单独部署；仓库根目录的 .env 用于本地整套项目。
    model_config = SettingsConfigDict(
        env_file=(AGENT_ROOT / ".env", REPOSITORY_ROOT / ".env"),
        extra="ignore",
    )


settings = Settings()
