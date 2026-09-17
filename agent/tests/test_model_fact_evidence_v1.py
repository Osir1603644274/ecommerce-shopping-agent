from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.domains.ecommerce.model_fact_evidence_v1 import (
    FrozenModelFactRegistryV1,
    compare_wifi_runtime_diagnostic_v1,
    resolve_model_facts_v1,
)


ROOT = Path(__file__).resolve().parents[2]
FACTS = ROOT / "evaluation/used-phone-model-facts-dev-v1/facts.jsonl"


@pytest.fixture(scope="module")
def registry() -> FrozenModelFactRegistryV1:
    return FrozenModelFactRegistryV1.from_jsonl(FACTS)


def test_five_normalized_facts_validate_against_schema(
    registry: FrozenModelFactRegistryV1,
) -> None:
    schema = json.loads(
        (ROOT / "schemas/used-phone-model-evidence-v1/model-fact.schema.json")
        .read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)
    assert len(registry.facts) == 5
    for fact in registry.facts:
        validator.validate(fact.model_dump(by_alias=True, mode="json"))
        assert fact.listing_identity_verified is False
        assert fact.production_ranking_authorized is False


def test_exact_single_title_resolves_model_level_fact_only(
    registry: FrozenModelFactRegistryV1,
) -> None:
    result = resolve_model_facts_v1(
        item_id="iphone13",
        title="苹果 iPhone 13 二手手机",
        catalog_brand="Apple/苹果",
        registry=registry,
    )
    assert result.status == "FACTS_AVAILABLE"
    assert result.facts[0].value == 1019
    assert result.listing_identity_verified is False
    assert result.production_ranking_authorized is False
    assert result.network_calls == 0
    assert result.model_calls == 0


def test_ambiguous_or_unresolved_title_never_receives_fact(
    registry: FrozenModelFactRegistryV1,
) -> None:
    ambiguous = resolve_model_facts_v1(
        item_id="ambiguous",
        title="苹果 iPhone 13 或 iPhone 15 二手手机",
        catalog_brand="Apple/苹果",
        registry=registry,
    )
    unresolved = resolve_model_facts_v1(
        item_id="unresolved",
        title="二手手机，具体型号见实物",
        catalog_brand="其他",
        registry=registry,
    )
    assert (ambiguous.status, ambiguous.facts) == ("CONFLICT", ())
    assert (unresolved.status, unresolved.facts) == ("UNKNOWN", ())


def test_cross_review_order_is_diagnostic_and_never_authorizes_ranking(
    registry: FrozenModelFactRegistryV1,
) -> None:
    comparison = compare_wifi_runtime_diagnostic_v1(registry.facts)
    assert comparison.ordered_model_claims == (
        "apple:13",
        "redmi:k70pro",
        "honor:90",
        "samsung:zflip5",
        "huawei:mate40pro",
    )
    assert comparison.production_ranking_authorized is False
    assert "Browser version" in comparison.caveat


def test_registry_rejects_digest_drift() -> None:
    with pytest.raises(ValueError, match="hash mismatch"):
        FrozenModelFactRegistryV1.from_jsonl(FACTS, expected_sha256="0" * 64)
