from __future__ import annotations

from pathlib import Path

from scripts.verify_used_phone_benchmark_java_api import (
    ATTRIBUTE_FIELD_MAP,
    PRODUCT_FIELD_MAP,
)


def test_java_projection_contract_covers_every_seed_column_except_db_timestamp():
    assert set(PRODUCT_FIELD_MAP) == {
        "attribute_text", "brand", "category_l1", "category_l2", "category_l3",
        "currency", "data_nature", "dataset_revision", "id", "price_status",
        "provenance_url", "seller", "snapshot_price_minor", "source",
        "source_item_id", "source_license", "title",
    }
    assert set(ATTRIBUTE_FIELD_MAP) == {
        "attribute_key", "confidence", "evidence_field", "extraction_method",
        "normalized_boolean", "normalized_number", "normalized_text", "raw_value",
        "unit", "value_type",
    }


def test_verifier_source_does_not_contain_hidden_benchmark_inputs():
    source = Path(__file__).parents[1] / "scripts" / "verify_used_phone_benchmark_java_api.py"
    text = source.read_text(encoding="utf-8")
    assert "qrel" not in text.lower()
    assert "hidden" not in text.lower()
    assert "sealed" not in text.lower()
