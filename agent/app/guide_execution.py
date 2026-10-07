"""One read-only shopping retrieval and grounded-answer implementation.

The browser workflow only adds durable phase checkpoints and presentation.
"""
from __future__ import annotations

from typing import Any

from .catalog_conversation import answer_turn, apply_subject_review
from .catalog_service import fingerprint, get_catalog_service, verify_scope


def select_provider_query(current: dict[str, Any], decision: dict[str, Any] | None = None) -> tuple[str, str]:
    """One outbound query choice for direct chat and the checkpointed workspace."""
    rewrite = (decision or {}).get("query")
    if isinstance(rewrite, str) and rewrite.strip():
        return rewrite.strip(), "bounded_rewrite"
    state_query = (current.get("retrievalQuery") or "").strip()
    if state_query:
        return state_query, "shopping_state"
    query = (current.get("query") or "").strip()
    if not query:
        raise ValueError("empty_provider_query")
    return query, "shopping_query_fallback"


async def retrieve(query: str, retrieval_query: str, requirements: list[dict[str, Any]]) -> dict[str, Any]:
    kwargs = {"requirements": requirements, "retrieval_query": retrieval_query or query}
    scope = await get_catalog_service().search(query, **kwargs)
    verify_scope(scope)
    return scope


async def answer(message: str, plan: dict[str, Any], current: dict[str, Any]) -> tuple[str, dict | None, dict | None]:
    """Reject a review over evidence other than the scope supplied to the model."""
    scope = current.get("scope")
    before_hash = fingerprint(scope)
    text, receipt = await answer_turn(message, plan, current)
    review = (receipt or {}).get("scopeReview")
    if review:
        if not scope or review["baseScopeId"] != scope["scopeId"] or fingerprint(scope) != before_hash:
            raise ValueError("catalog_review_evidence_changed")
        scope = apply_subject_review(scope, review["reviews"])
    return text, receipt, scope
