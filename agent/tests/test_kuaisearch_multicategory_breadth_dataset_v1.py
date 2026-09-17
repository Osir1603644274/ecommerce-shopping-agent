from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from evaluation.kuaisearch_multicategory_breadth_dataset_v1 import (
    build_review_items,
    document_signature,
    evidence_signature,
)


ROOT = Path(__file__).resolve().parents[2]


def test_signatures_require_all_four_public_fields() -> None:
    evidence = {
        "title": "A", "brand": "B", "seller": "S",
        "evidenceRefs": [{"rawValue": "x,y"}],
    }
    document = {"title": "A", "brand": "B", "seller_name": "S", "attr_value": "x,y"}
    assert evidence_signature(evidence) == document_signature(document)
    assert evidence_signature(evidence) != document_signature({**document, "attr_value": "x,z"})


def test_bulk_review_identity_and_boundary() -> None:
    candidates = [
        {"role": "BREADTH", "categoryKey": f"c{i}", "categoryPath": ["a", "b", "c"], "status": "CORE"}
        for i in range(12)
    ]
    query_samples = [
        {"categoryKey": "c0", "categoryPath": ["a", "b", "c"], "sampleType": "positive_query",
         "sample": {"queryId": f"q{i}", "docId": f"d{i}", "sourceRelevance": 2}}
        for i in range(54)
    ]
    product_samples = [
        {"categoryKey": "c0", "categoryPath": ["a", "b", "c"], "sampleType": "product",
         "sample": {"docId": f"p{i}"}}
        for i in range(36)
    ]
    rows = build_review_items(candidates, [*query_samples, *product_samples])
    assert len(rows) == 102
    assert len({row["reviewItemId"] for row in rows}) == 102
    assert all(row["decisionMode"] == "BULK_CHAT_APPROVAL" for row in rows)
    assert all(row["itemByItemFormCompleted"] is False for row in rows)


def test_schemas_are_valid() -> None:
    for name in (
        "kuaisearch_multicategory_human_review_item_v1.schema.json",
        "kuaisearch_multicategory_breadth_record_v1.schema.json",
    ):
        schema = json.loads((ROOT / "agent/evaluation/schemas" / name).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
