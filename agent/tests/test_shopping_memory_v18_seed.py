from evaluation.shopping_memory_v18_real_full_chain_20260830 import (
    seed_used_phone_catalog as seed,
)


def test_v18_seed_binds_every_attribute_to_current_production_contract() -> None:
    _products, attributes, _observed = seed.build_rows()

    assert len(attributes) == 2814
    expected_field = seed.sql_text(seed.ATTRIBUTE_EVIDENCE_FIELD)
    expected_ruleset = seed.sql_text(seed.ATTRIBUTE_RULESET_VERSION)
    assert all(expected_field in row and expected_ruleset in row for row in attributes)


def test_v18_seed_preserves_comma_delimited_raw_attribute_grammar() -> None:
    _products, attributes, _observed = seed.build_rows()

    comma_hex = ",".encode("utf-8").hex()
    assert any(comma_hex in row for row in attributes)
