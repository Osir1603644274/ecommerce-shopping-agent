from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from evaluation.kuaisearch_multicategory_breadth_audit_v1 import (
    PORTFOLIO_KEYS,
    _document_signature,
    _evidence_signature,
    _gate,
)


ROOT = Path(__file__).resolve().parents[2]


def test_exact_signature_requires_all_public_fields() -> None:
    evidence = {
        "title": "商品A", "brand": "品牌", "seller": "商家",
        "evidenceRefs": [{"rawValue": "红色,大号"}],
    }
    document = {
        "title": "商品A", "brand": "品牌", "seller_name": "商家",
        "attr_value": "红色,大号",
    }
    assert _evidence_signature(evidence) == _document_signature(document)
    changed = dict(document, seller_name="另一商家")
    assert _evidence_signature(evidence) != _document_signature(changed)


def test_gate_distinguishes_core_and_conditional_phone_case() -> None:
    metric = {
        "categoryKey": "1/39/0", "matchedDocumentCount": 20,
        "positiveQueryCount": 20, "evidenceProductCount": 30,
        "distinctAttributeSignatures": 25, "positiveQrelShare": 0.8,
        "uniqueTitleRate": 0.95,
    }
    assert _gate(metric)[0] == "CORE_BREADTH_CANDIDATE"
    metric.update(categoryKey="30/59/57", positiveQueryCount=9)
    assert _gate(metric)[0] == "CONDITIONAL_LOW_QUERY_SUPPORT"
    metric.update(categoryKey="30/77/84", positiveQueryCount=1)
    assert _gate(metric)[0] == "INSUFFICIENT_FOR_PORTFOLIO"


def test_portfolio_is_diverse_and_fixed() -> None:
    assert len(PORTFOLIO_KEYS) == 12
    assert len(set(PORTFOLIO_KEYS)) == 12
    assert "46/133/185" not in PORTFOLIO_KEYS


def test_candidate_schema_is_valid() -> None:
    schema = json.loads((ROOT / "agent/evaluation/schemas/kuaisearch_multicategory_breadth_candidate_v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)

