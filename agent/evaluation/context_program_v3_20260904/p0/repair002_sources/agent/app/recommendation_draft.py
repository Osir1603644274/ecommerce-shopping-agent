"""RecommendationDraft — structured recommendation before NL generation.

All fact-type claims MUST trace back to authoritative resolved facts
(Java /resolve) or evidenceRefs.  Missing facts stay as "unknown" —
the model is never allowed to guess.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DraftClaim(BaseModel):
    """One claim about a product, backed by an evidenceRef."""

    model_config = ConfigDict(populate_by_name=True)

    claim_id: str = Field(alias="claimId")
    product_id: int = Field(alias="productId")
    requirement_key: str | None = Field(default=None, alias="requirementKey")
    statement: str
    evidence_ref_ids: list[str] = Field(
        default_factory=list, alias="evidenceRefIds"
    )


class RecommendationDraft(BaseModel):
    """Structured draft produced before the final NL answer.

    The draft is deterministic and verifiable.  It is consumed by:
      - The final-answer generator (to produce user-facing text)
      - EvidenceCritic (to check claims against evidence)
      - AgentRunTrace (for replay and A/B comparison)
    """

    model_config = ConfigDict(populate_by_name=True)

    selected_product_ids: list[int] = Field(
        default_factory=list, alias="selectedProductIds"
    )
    final_order: list[int] = Field(
        default_factory=list,
        alias="finalOrder",
        description="productIds in the order they should be presented",
    )
    claims: list[DraftClaim] = Field(default_factory=list)
    unknowns: list[str] = Field(
        default_factory=list,
        description="Requirements that could not be evaluated",
    )
    snapshot_notice: str | None = Field(
        default=None,
        alias="snapshotNotice",
        description="Data-boundary notice copied from the search tool result",
    )


def build_recommendation_draft(
    candidates: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    evidence_refs: list[dict[str, Any]],
    snapshot_notice: str | None = None,
) -> RecommendationDraft:
    """Deterministically build a draft from tool-output candidates.

    This does NOT call the model.  It extracts structured claims and unknowns
    from the already-ranked, already-validated candidate list that the
    reranker produced.
    """
    from .domains.ecommerce.models import ShoppingRequirement

    selected_ids: list[int] = []
    claims: list[DraftClaim] = []
    unknowns: list[str] = []
    claim_index = 0

    parsed_reqs: list[ShoppingRequirement] = []
    for req in requirements:
        try:
            parsed_reqs.append(ShoppingRequirement.model_validate(req))
        except ValueError:
            continue

    for candidate in candidates[:5]:  # only top 5 can appear in the draft
        product_id = int(candidate.get("product", candidate).get("id", 0))
        if not product_id:
            continue

        checks = candidate.get("checks", [])
        existing_ids = set()

        for check in checks:
            if not isinstance(check, dict):
                continue
            requirement_key = check.get("key", "")
            status = check.get("status", "unknown")
            evidence_ref = check.get("evidenceRef")

            if status == "unknown":
                unknowns.append(f"{requirement_key}:product={product_id}:unknown")
                continue

            if status == "fail":
                continue  # already eliminated by reranker

            statement_map = {
                "pass": f"Product {product_id} satisfies {requirement_key}: "
                        f"actual={check.get('displayValue', check.get('actual'))}",
            }
            statement = statement_map.get(
                status,
                f"Product {product_id} {requirement_key}: {status}",
            )

            claim_id = f"claim-{product_id}-{requirement_key}"
            if claim_id in existing_ids:
                continue
            existing_ids.add(claim_id)
            claim_index += 1

            ref_ids = [evidence_ref] if evidence_ref else []
            claims.append(
                DraftClaim(
                    claim_id=claim_id,
                    product_id=product_id,
                    requirement_key=requirement_key,
                    statement=statement,
                    evidence_ref_ids=ref_ids,
                )
            )

        if product_id not in selected_ids:
            selected_ids.append(product_id)

    return RecommendationDraft(
        selected_product_ids=selected_ids,
        final_order=list(selected_ids),
        claims=claims,
        unknowns=unknowns,
        snapshot_notice=snapshot_notice,
    )
