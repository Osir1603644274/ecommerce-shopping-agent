"""Tests for RecommendationDraft and EvidenceCritic."""

import json

import pytest

from app.recommendation_draft import (
    RecommendationDraft,
    DraftClaim,
    build_recommendation_draft,
)
from app.evidence_critic import (
    IssueCode,
    CriticIssue,
    CriticOutput,
    structural_critic,
)


def _make_candidate(product_id, checks=None, **overrides):
    base = {
        "id": product_id,
        "title": f"Test Product {product_id}",
        "brand": "TestBrand",
        "categoryL1": "手机/数码/电脑办公",
        "categoryL2": "手机通讯",
        "categoryL3": "智能手机",
    }
    base.update(overrides)
    return {
        "product": base,
        "facts": {
            "productId": product_id,
            "title": base["title"],
            "brand": base["brand"],
        },
        "checks": checks or [],
        "fullyMatched": True,
        "hardFailures": 0,
        "hardUnknowns": 0,
        "scoreBreakdown": {"final": 0.95},
        "evidenceRefs": ["ref-1"],
        "evidence": [{"ref": "ref-1", "field": "title", "rawValue": base["title"]}],
    }


class TestRecommendationDraft:
    def test_build_from_candidates(self):
        candidates = [
            _make_candidate(1, checks=[
                {
                    "key": "memory_gb", "operator": "gte", "expected": 12,
                    "unit": "GB", "priority": "hard", "actual": 16,
                    "displayValue": "16GB", "status": "pass",
                    "evidenceRef": "product:1:attribute:memory_gb",
                },
                {
                    "key": "price_minor", "operator": "lte", "expected": 300000,
                    "unit": "CNY_MINOR", "priority": "hard", "actual": 299900,
                    "displayValue": "299900", "status": "pass",
                    "evidenceRef": "product:1:snapshotPriceMinor",
                },
            ]),
            _make_candidate(2, checks=[
                {
                    "key": "memory_gb", "operator": "gte", "expected": 12,
                    "unit": "GB", "priority": "hard", "actual": None,
                    "displayValue": "未知", "status": "unknown",
                    "evidenceRef": None,
                },
            ]),
        ]
        draft = build_recommendation_draft(
            candidates,
            requirements=[
                {"key": "memory_gb", "operator": "gte", "value": 12, "unit": "GB", "priority": "hard", "source": "user"},
                {"key": "price_minor", "operator": "lte", "value": 300000, "unit": "CNY_MINOR", "priority": "hard", "source": "user"},
            ],
            evidence_refs=[{"ref": "ref-1", "field": "title"}],
        )
        assert 1 in draft.selected_product_ids
        assert 2 in draft.selected_product_ids
        assert len(draft.claims) >= 2  # one claim per pass check
        assert len(draft.unknowns) >= 1  # memory_gb unknown for product 2

    def test_all_claims_have_evidence_refs(self):
        candidates = [
            _make_candidate(1, checks=[
                {
                    "key": "brand", "operator": "eq", "expected": "Sony",
                    "unit": "text", "priority": "hard", "actual": "Sony",
                    "displayValue": "Sony", "status": "pass",
                    "evidenceRef": "product:1:brand",
                },
            ]),
        ]
        draft = build_recommendation_draft(
            candidates,
            requirements=[
                {"key": "brand", "operator": "eq", "value": "Sony", "unit": "text", "priority": "hard", "source": "user"},
            ],
            evidence_refs=[],
        )
        for claim in draft.claims:
            assert len(claim.evidence_ref_ids) > 0, f"Claim {claim.claim_id} has no evidence refs"

    def test_snapshot_notice_propagation(self):
        draft = build_recommendation_draft(
            [],
            [],
            [],
            snapshot_notice="历史公开数据快照",
        )
        assert draft.snapshot_notice == "历史公开数据快照"


class TestEvidenceCritic:
    def test_detects_candidate_outside_set(self):
        draft_dict = {
            "selectedProductIds": [99],
            "claims": [{"claimId": "c-1", "productId": 99, "statement": "ok", "evidenceRefIds": ["ref-x"]}],
        }
        issues = structural_critic(draft_dict, candidates=[_make_candidate(1)])
        assert any(i.code == IssueCode.CANDIDATE_OUTSIDE_SET for i in issues)

    def test_detects_unsupported_claim(self):
        draft_dict = {
            "selectedProductIds": [1],
            "claims": [{"claimId": "c-1", "productId": 1, "statement": "no ref", "evidenceRefIds": []}],
        }
        issues = structural_critic(draft_dict, candidates=[_make_candidate(1)])
        assert any(i.code == IssueCode.UNSUPPORTED_CLAIM for i in issues)

    def test_detects_unknown_claimed_satisfied(self):
        draft_dict = {
            "selectedProductIds": [1],
            "claims": [
                {
                    "claimId": "c-1", "productId": 1,
                    "statement": "未知 memory",
                    "evidenceRefIds": ["ref-1"],
                },
            ],
        }
        issues = structural_critic(draft_dict, candidates=[_make_candidate(1)])
        assert any(i.code == IssueCode.UNKNOWN_CLAIMED_SATISFIED for i in issues)

    def test_detects_hard_constraint_violation(self):
        draft_dict = {"selectedProductIds": [], "claims": []}
        candidates = [
            _make_candidate(1, checks=[
                {
                    "key": "price_minor", "operator": "lte", "expected": 100000,
                    "unit": "CNY_MINOR", "priority": "hard", "actual": 300000,
                    "displayValue": "300000", "status": "fail",
                    "evidenceRef": None,
                },
            ]),
        ]
        issues = structural_critic(draft_dict, candidates)
        assert any(i.code == IssueCode.HARD_CONSTRAINT_VIOLATION for i in issues)

    def test_clean_case_produces_zero_issues(self):
        draft_dict = {
            "selectedProductIds": [1],
            "claims": [
                {
                    "claimId": "c-1", "productId": 1,
                    "statement": "Product 1 satisfies memory_gb: actual=16",
                    "evidenceRefIds": ["product:1:attribute:memory_gb"],
                },
            ],
        }
        candidates = [
            _make_candidate(1, checks=[
                {
                    "key": "memory_gb", "operator": "gte", "expected": 12,
                    "unit": "GB", "priority": "hard", "actual": 16,
                    "displayValue": "16GB", "status": "pass",
                    "evidenceRef": "product:1:attribute:memory_gb",
                },
            ]),
        ]
        issues = structural_critic(draft_dict, candidates)
        assert len(issues) == 0

    def test_combined_issues(self):
        """Intentionally broken draft with multiple issues."""
        draft_dict = {
            "selectedProductIds": [1, 99],
            "claims": [
                {"claimId": "c-1", "productId": 99, "statement": "ok", "evidenceRefIds": ["ref"]},
                {"claimId": "c-2", "productId": 1, "statement": "未知 field", "evidenceRefIds": ["ref"]},
                {"claimId": "c-3", "productId": 1, "statement": "no ref", "evidenceRefIds": []},
            ],
        }
        candidates = [
            _make_candidate(1, checks=[
                {
                    "key": "price_minor", "operator": "lte", "expected": 100000,
                    "unit": "CNY_MINOR", "priority": "hard", "actual": 300000,
                    "displayValue": "300000", "status": "fail",
                    "evidenceRef": None,
                },
            ]),
        ]
        issues = structural_critic(draft_dict, candidates)
        codes = {i.code for i in issues}
        assert IssueCode.CANDIDATE_OUTSIDE_SET in codes
        assert IssueCode.UNKNOWN_CLAIMED_SATISFIED in codes
        assert IssueCode.UNSUPPORTED_CLAIM in codes
        assert IssueCode.HARD_CONSTRAINT_VIOLATION in codes

    def test_approved_field_reflects_issues(self):
        output = CriticOutput(approved=True, issues=[])
        assert output.approved is True

        output2 = CriticOutput(approved=False, issues=[
            CriticIssue(code=IssueCode.UNSUPPORTED_CLAIM, description="test"),
        ])
        assert output2.approved is False
