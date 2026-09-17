from ..base import DomainSpec, register_domain
from ..context_skill import (
    CONTEXT_CONTRACT_VERSION,
    ECOMMERCE_CONTEXT_SKILL_ID,
)
from ...control.validation_contracts import (
    RUNTIME_TOOL_CONTRACTS,
    RuntimeToolContract,
)
from .models import (
    BrandAvoidance,
    CATEGORY_ALIASES,
    CATEGORY_LABELS,
    DOMESTIC_PHONE_BRANDS,
    SPEC_REGISTRY,
    CandidateScope,
    ProductCategory,
    RequirementOperator,
    RequirementPriority,
    ScopeRerankRequest,
    ShoppingGuideState,
    ShoppingRequirement,
    bm25_rank,
    canonicalize_brand,
    compare_product_details,
    compiled_shopping_requirements,
    detect_product_brands,
    detect_product_category,
    expand_product_query,
    product_brand_aliases,
    is_ecommerce_message,
    is_unsafe_shopping_use,
    reciprocal_rank_fusion,
    rule_rerank_candidates,
    structured_requirement_rank,
    tokenize_product_text,
    validate_requirements,
)
from .brand_negation import (
    BrandNegationParseResult,
    BrandNegationTarget,
    NegationStrength,
    parse_brand_negations,
)
from .ranking_contract import (
    SCOPE_RERANK_OUTPUT_FIELDS,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
)
from .tools import (
    compare_products_tool,
    get_product_details_tool,
    rerank_products_in_scope_tool,
    search_products_tool,
)
from .transactions import (
    ConfirmationStoreUnavailable,
    bind_transaction_context,
    clear_transaction_confirmations,
    extract_order_reference,
    explicit_confirmation_action,
    is_order_status_query,
    render_order_status_result,
    render_transaction_result,
)

ECOMMERCE_GUIDE_TOOL_NAMES = (
    "search_products",
    "get_product_details",
    "compare_products",
    "search_product_evidence",
    "rerank_products_in_scope",
)
ECOMMERCE_TOOL_NAMES = (
    *ECOMMERCE_GUIDE_TOOL_NAMES,
)

ECOMMERCE_DOMAIN = register_domain(
    DomainSpec(
        domain_id="ecommerce",
        task_type="ecommerce_guide",
        label="商品导购",
        tool_names=ECOMMERCE_TOOL_NAMES,
        matches=is_ecommerce_message,
        context_skill_id=ECOMMERCE_CONTEXT_SKILL_ID,
        context_skill_version=CONTEXT_CONTRACT_VERSION,
    )
)

# Server-owned in-scope rerank contract.  Registered at import time so Planner /
# Executor / Validator / Harness all see the tool surface before any module-level
# registry check.  ``rerank_products_in_scope`` deliberately stays OUT of
# ECOMMERCE_GUIDE_TOOL_NAMES: it is a state-gated follow-up action, never part of
# the always-available recommendation menu, and the plan step's required_names
# mechanism adds its schema only when the deterministic planner created the step.
RUNTIME_TOOL_CONTRACTS["rerank_products_in_scope"] = RuntimeToolContract(
    expected_outputs=frozenset({"requiresScopeRerank"}),
    normalized_output_fields=SCOPE_RERANK_OUTPUT_FIELDS,
    validated_evidence_fields=(
        "contractVersion", "scopeId", "inputProductIds", "rankedItemIds",
        "productIds", "rankingSignal", "degraded", "noFullSearch", "candidates",
        "citationTrace", "rankingTrace", "evidence", "evidenceRefs",
        "eliminated",
    ),
    contract_version=TWO_STAGE_RANKING_CONTRACT_VERSION,
)

__all__ = [
    "BrandAvoidance",
    "BrandNegationParseResult",
    "BrandNegationTarget",
    "CATEGORY_ALIASES",
    "CATEGORY_LABELS",
    "DOMESTIC_PHONE_BRANDS",
    "CandidateScope",
    "ConfirmationStoreUnavailable",
    "ECOMMERCE_DOMAIN",
    "ECOMMERCE_GUIDE_TOOL_NAMES",
    "ECOMMERCE_TOOL_NAMES",
    "ProductCategory",
    "NegationStrength",
    "RequirementOperator",
    "RequirementPriority",
    "SPEC_REGISTRY",
    "ScopeRerankRequest",
    "ShoppingGuideState",
    "ShoppingRequirement",
    "bm25_rank",
    "canonicalize_brand",
    "compare_product_details",
    "compiled_shopping_requirements",
    "compare_products_tool",
    "bind_transaction_context",
    "clear_transaction_confirmations",
    "detect_product_category",
    "detect_product_brands",
    "expand_product_query",
    "is_ecommerce_message",
    "is_unsafe_shopping_use",
    "get_product_details_tool",
    "explicit_confirmation_action",
    "extract_order_reference",
    "is_order_status_query",
    "product_brand_aliases",
    "parse_brand_negations",
    "reciprocal_rank_fusion",
    "rerank_products_in_scope_tool",
    "render_order_status_result",
    "rule_rerank_candidates",
    "search_products_tool",
    "structured_requirement_rank",
    "render_transaction_result",
    "tokenize_product_text",
    "validate_requirements",
]
