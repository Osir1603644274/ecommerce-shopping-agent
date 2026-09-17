"""Backward-compatible exports for the ecommerce domain contract.

New code should import from :mod:`app.domains.ecommerce`.
"""

from .domains.ecommerce.models import (
    CATEGORY_ALIASES,
    CATEGORY_LABELS,
    SPEC_REGISTRY,
    ProductCategory,
    RequirementOperator,
    RequirementPriority,
    ShoppingGuideState,
    ShoppingRequirement,
    bm25_rank,
    compare_product_details,
    detect_product_category,
    is_ecommerce_message,
    is_unsafe_shopping_use,
    reciprocal_rank_fusion,
    rule_rerank_candidates,
    tokenize_product_text,
    validate_requirements,
)

__all__ = [
    "CATEGORY_ALIASES",
    "CATEGORY_LABELS",
    "SPEC_REGISTRY",
    "ProductCategory",
    "RequirementOperator",
    "RequirementPriority",
    "ShoppingGuideState",
    "ShoppingRequirement",
    "bm25_rank",
    "compare_product_details",
    "detect_product_category",
    "is_ecommerce_message",
    "is_unsafe_shopping_use",
    "reciprocal_rank_fusion",
    "rule_rerank_candidates",
    "tokenize_product_text",
    "validate_requirements",
]
