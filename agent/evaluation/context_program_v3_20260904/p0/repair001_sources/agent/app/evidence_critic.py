"""EvidenceCritic — shadow-only evidence auditor for product recommendations.

This is a CONTROLLED agent with strict guardrails:
  - Read-only: ContextPack, RecommendationDraft, candidate products, facts, evidence refs
  - No search tools, no business tools, no write operations
  - Runs AT MOST once per request
  - Returns fixed structured JSON
  - Does NOT modify the user-facing answer (shadow-only in initial phase)

Issue codes:
  - hard_constraint_violation
  - unknown_claimed_satisfied
  - unsupported_claim
  - evidence_mismatch
  - candidate_outside_set
  - contradictory_comparison

Control:
  - Feature flag: EVIDENCE_CRITIC_ENABLED (env var)
  - Online: bounded background queue (default 100), non-blocking
  - Eval mode: synchronous execution via explicit flag
"""

from __future__ import annotations

import json
import logging
import time
from enum import Enum
from typing import Any, Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from .settings import settings

logger = logging.getLogger(__name__)

# ── Issue codes ──────────────────────────────────────────────────────────────


class IssueCode(str, Enum):
    HARD_CONSTRAINT_VIOLATION = "hard_constraint_violation"
    UNKNOWN_CLAIMED_SATISFIED = "unknown_claimed_satisfied"
    UNSUPPORTED_CLAIM = "unsupported_claim"
    EVIDENCE_MISMATCH = "evidence_mismatch"
    CANDIDATE_OUTSIDE_SET = "candidate_outside_set"
    CONTRADICTORY_COMPARISON = "contradictory_comparison"


# ── Structured output schema ─────────────────────────────────────────────────


class CriticIssue(BaseModel):
    """One issue found by the critic."""

    code: IssueCode
    claim_id: str | None = None
    product_id: int | None = None
    description: str
    evidence_ref_ids: list[str] = Field(default_factory=list)


class CriticOutput(BaseModel):
    """Fixed structured JSON output from EvidenceCritic."""

    model_config = ConfigDict(populate_by_name=True)

    approved: bool
    issues: list[CriticIssue] = Field(default_factory=list)
    required_corrections: list[str] = Field(
        default_factory=list, alias="requiredCorrections"
    )
    checked_evidence_refs: list[str] = Field(
        default_factory=list, alias="checkedEvidenceRefs"
    )
    critic_version: str = Field(default="1.0", alias="criticVersion")


# ── Deterministic structural checks (no model required) ──────────────────────


def structural_critic(
    draft: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[CriticIssue]:
    """Fast, deterministic checks that require zero model calls.

    These catch hard_constraint_violation, candidate_outside_set,
    unknown_claimed_satisfied, and unsupported_claim without any LLM.
    """
    issues: list[CriticIssue] = []

    candidate_ids = set()
    for candidate in candidates:
        pid = candidate.get("product", candidate).get("id") if isinstance(candidate, dict) else None
        if pid is not None:
            candidate_ids.add(int(pid))

    selected = set(draft.get("selectedProductIds", []))
    for pid in selected - candidate_ids:
        issues.append(
            CriticIssue(
                code=IssueCode.CANDIDATE_OUTSIDE_SET,
                product_id=pid,
                description=f"Product {pid} is in selectedProductIds but not in any candidate set",
            )
        )

    for claim in draft.get("claims", []):
        pid = claim.get("productId")
        if pid is not None and pid not in candidate_ids:
            issues.append(
                CriticIssue(
                    code=IssueCode.CANDIDATE_OUTSIDE_SET,
                    product_id=pid,
                    claim_id=claim.get("claimId"),
                    description=f"Claim {claim.get('claimId')} references product {pid} outside candidate set",
                )
            )

        if not claim.get("evidenceRefIds"):
            issues.append(
                CriticIssue(
                    code=IssueCode.UNSUPPORTED_CLAIM,
                    product_id=pid,
                    claim_id=claim.get("claimId"),
                    description=f"Claim {claim.get('claimId')} has no evidenceRefIds",
                )
            )

        statement = claim.get("statement", "")
        if "未知" in statement or "unknown" in statement.lower():
            if claim.get("evidenceRefIds"):
                issues.append(
                    CriticIssue(
                        code=IssueCode.UNKNOWN_CLAIMED_SATISFIED,
                        product_id=pid,
                        claim_id=claim.get("claimId"),
                        description=f"Claim {claim.get('claimId')} states unknown but has evidence refs",
                    )
                )

    for candidate in candidates:
        raw = candidate.get("product", candidate) if isinstance(candidate, dict) else {}
        if not isinstance(raw, dict):
            continue
        checks = candidate.get("checks", [])
        for check in checks or []:
            if not isinstance(check, dict):
                continue
            if check.get("priority") == "hard" and check.get("status") == "fail":
                issues.append(
                    CriticIssue(
                        code=IssueCode.HARD_CONSTRAINT_VIOLATION,
                        product_id=int(raw.get("id", 0)),
                        description=(
                            f"Hard constraint {check.get('key')} failed: "
                            f"expected {check.get('expected')}, actual {check.get('actual')}"
                        ),
                    )
                )

    return issues


# ── Semantic critic (model call, shadow-only) ────────────────────────────────


_CRITIC_SYSTEM_PROMPT = """You are EvidenceCritic — a read-only evidence auditor.

Your job is to check a RecommendationDraft against candidate products, facts, and
evidence references. You have NO search tools, NO business tools, and CANNOT modify
any data. You return fixed JSON only.

Rules:
1. Every claim must have at least one evidenceRefId.
2. No claim may present "unknown" as satisfied.
3. Claims must not contradict each other (e.g. claiming both "best battery" and
   "best portability" for different products without evidence).
4. Evidence refs must point to actual fields in the candidate products.
5. Selected product IDs must all come from the candidate set.

Return your findings as a JSON object with fields:
- approved: true only if zero issues found
- issues: array of {code, claimId, productId, description, evidenceRefIds}
- requiredCorrections: plain-language list of what must be fixed
- checkedEvidenceRefs: list of all evidence refs you checked
- criticVersion: "1.0"
"""


async def semantic_critic(
    draft: dict[str, Any],
    candidates_summary: list[dict[str, Any]],
    *,
    client: AsyncOpenAI | None = None,
    model: str | None = None,
) -> CriticOutput:
    """Run the LLM-based semantic check (one call, structured output)."""
    import uuid

    if client is None:
        from .llm import get_client as get_llm_client
        client = get_llm_client()
    if model is None:
        model = settings.deepseek_model

    payload = json.dumps(
        {
            "draft": draft,
            "candidatesSummary": candidates_summary,
        },
        ensure_ascii=False,
        default=str,
    )

    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _CRITIC_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
            response_format={"type": "json_object"},
            temperature=0.0,
            max_tokens=1024,
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        return CriticOutput.model_validate(parsed)
    except Exception as exc:
        logger.warning("Semantic critic failed: %s", exc)
        return CriticOutput(
            approved=False,
            issues=[
                CriticIssue(
                    code=IssueCode.UNSUPPORTED_CLAIM,
                    description=f"Semantic critic error: {exc}",
                )
            ],
            required_corrections=["critic execution failed — manual review required"],
            checked_evidence_refs=[],
        )


# ── Combined critic ──────────────────────────────────────────────────────────


async def run_evidence_critic(
    draft: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    semantic: bool = False,
    client: AsyncOpenAI | None = None,
) -> CriticOutput:
    """Run both structural and (optionally) semantic checks.

    In production shadow mode, semantic=False — only deterministic checks run.
    In eval mode, semantic=True — both checks run synchronously.
    """
    issues = structural_critic(draft, candidates)

    if semantic:
        # Build a safe summary for the model (no raw user queries, no auth data)
        safe_summary = []
        for candidate in candidates:
            raw = candidate.get("product", candidate) if isinstance(candidate, dict) else {}
            if not isinstance(raw, dict):
                continue
            safe_summary.append({
                "id": raw.get("id"),
                "title": raw.get("title"),
                "brand": raw.get("brand"),
                "categoryL3": raw.get("categoryL3"),
                "checks": [
                    {
                        "key": c.get("key"),
                        "status": c.get("status"),
                        "evidenceRef": c.get("evidenceRef"),
                    }
                    for c in (candidate.get("checks", []) or [])
                    if isinstance(c, dict)
                ],
            })

        semantic_output = await semantic_critic(draft, safe_summary, client=client)
        issues.extend(semantic_output.issues)

    all_refs: list[str] = []
    for claim in draft.get("claims", []):
        for ref in claim.get("evidenceRefIds", []):
            if isinstance(ref, str) and ref not in all_refs:
                all_refs.append(ref)

    return CriticOutput(
        approved=len(issues) == 0,
        issues=issues,
        required_corrections=[
            f"{issue.code}: {issue.description}" for issue in issues
        ],
        checked_evidence_refs=all_refs,
    )


# ── Feature flag ─────────────────────────────────────────────────────────────


def critic_enabled() -> bool:
    return settings.evidence_critic_enabled
