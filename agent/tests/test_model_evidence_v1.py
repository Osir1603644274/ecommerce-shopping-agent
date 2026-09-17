from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.domains.ecommerce.model_evidence_v1 import (
    FrozenModelEvidenceRegistryV1,
    normalize_title_model_claim_v1,
    resolve_model_evidence_v1,
)


ROOT = Path(__file__).resolve().parents[2]
V2 = ROOT / "evaluation" / "context-multiagent-v2-feasibility-v2"
REGISTRY_PATH = V2 / "source-coverage-pilot-v2.jsonl"
REGISTRY_SHA256 = "5ed1af61f82e3ee40d2f9e72e6663b3c3870a89df932bec66109b198c629be65"


@pytest.fixture(scope="module")
def registry() -> FrozenModelEvidenceRegistryV1:
    return FrozenModelEvidenceRegistryV1.from_jsonl(
        REGISTRY_PATH,
        expected_sha256=REGISTRY_SHA256,
    )


def test_runtime_title_normalizer_is_byte_input_equivalent_to_frozen_v2_audit() -> None:
    rows = [
        json.loads(line)
        for line in (V2 / "title-model-claims-v2.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(rows) == 439
    for row in rows:
        actual = normalize_title_model_claim_v1(
            item_id=row["itemId"],
            title=row["title"],
            catalog_brand=row["catalogBrand"],
        )
        assert actual.status == row["status"]
        assert [item.canonical_model_claim for item in actual.claims] == [
            item["canonicalModelClaim"] for item in row["claims"]
        ]
        assert actual.authority == "SELLER_TITLE_CLAIM_ONLY"
        assert actual.listing_identity_verified is False


def test_registry_is_url_only_and_bound_to_one_24_cluster_pilot(
    registry: FrozenModelEvidenceRegistryV1,
) -> None:
    assert registry.record_count == 24
    assert registry.source_sha256 == REGISTRY_SHA256
    assert registry.pilot_binding_sha256 == (
        "17c5a2625ff6cbb1f231b9d916a498f24d5a807df2829cee32911664c3dcf0ad"
    )


def test_exact_model_routes_references_but_physical_condition_stays_unknown(
    registry: FrozenModelEvidenceRegistryV1,
) -> None:
    result = resolve_model_evidence_v1(
        item_id="demo-13",
        title="Apple 苹果 iPhone 13 二手手机",
        catalog_brand="苹果/Apple",
        evidence_gap_keys=(
            "official_model_reference",
            "independent_performance_reference",
            "physical_device_condition",
        ),
        registry=registry,
    )

    assert result.observation.status == "TITLE_CLAIM_SINGLE"
    assert result.observation.claims[0].canonical_model_claim == "apple:13"
    assert [item.status for item in result.findings] == [
        "REFERENCE_AVAILABLE",
        "REFERENCE_AVAILABLE",
        "UNKNOWN",
    ]
    assert result.findings[0].authority == "OFFICIAL_MODEL_REFERENCE"
    assert result.findings[1].authority == "INDEPENDENT_MODEL_TEST_REFERENCE"
    assert result.findings[2].reason_code == "TRUSTED_DEVICE_JOIN_KEY_ABSENT"
    assert result.network_calls == 0
    assert result.model_calls == 0
    assert result.stop_reason == "COMPLETE_REFERENCE_ROUTING"

    schema = json.loads(
        (ROOT / "schemas/used-phone-model-evidence-v1/model-evidence-result.schema.json")
        .read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(
        result.model_dump(by_alias=True, mode="json")
    )


def test_ambiguous_title_never_selects_one_model(
    registry: FrozenModelEvidenceRegistryV1,
) -> None:
    result = resolve_model_evidence_v1(
        item_id="ambiguous",
        title="苹果6sp二手备用机，也可选苹果8",
        catalog_brand="苹果/Apple",
        evidence_gap_keys=(
            "official_model_reference",
            "physical_device_condition",
        ),
        registry=registry,
    )

    assert result.observation.status == "TITLE_CLAIM_AMBIGUOUS"
    assert len(result.observation.claims) == 2
    assert result.findings[0].status == "CONFLICT"
    assert result.findings[0].source_locators == ()
    assert result.findings[1].status == "UNKNOWN"
    assert result.stop_reason == "TITLE_CONFLICT"


def test_unresolved_title_fails_closed_without_source_lookup(
    registry: FrozenModelEvidenceRegistryV1,
) -> None:
    result = resolve_model_evidence_v1(
        item_id="unresolved",
        title="二手智能手机备用机，具体型号以实物为准",
        catalog_brand="其他",
        evidence_gap_keys=("official_model_reference",),
        registry=registry,
    )

    assert result.observation.status == "TITLE_CLAIM_UNRESOLVED"
    assert result.findings[0].status == "UNKNOWN"
    assert result.findings[0].reason_code == "TITLE_MODEL_UNRESOLVED"
    assert result.stop_reason == "FAIL_CLOSED_UNKNOWN"


def test_library_or_news_locator_is_not_promoted_to_controlled_test(
    registry: FrozenModelEvidenceRegistryV1,
) -> None:
    result = resolve_model_evidence_v1(
        item_id="mate30",
        title="华为 Mate30 二手手机",
        catalog_brand="华为/Huawei",
        evidence_gap_keys=(
            "official_model_reference",
            "independent_performance_reference",
        ),
        registry=registry,
    )

    assert result.observation.claims[0].canonical_model_claim == "huawei:mate30"
    assert result.findings[0].status == "REFERENCE_AVAILABLE"
    assert result.findings[1].status == "UNKNOWN"
    assert result.findings[1].reason_code == "CONTROLLED_MODEL_TEST_LOCATOR_ABSENT"


def test_registry_rejects_hash_drift() -> None:
    with pytest.raises(ValueError, match="hash mismatch"):
        FrozenModelEvidenceRegistryV1.from_jsonl(
            REGISTRY_PATH,
            expected_sha256="0" * 64,
        )


def test_registry_rejects_identity_elevation_and_copied_page_content(tmp_path: Path) -> None:
    source = json.loads(REGISTRY_PATH.read_text(encoding="utf-8").splitlines()[0])
    source["listingIdentityVerified"] = True
    path = tmp_path / "identity.jsonl"
    path.write_text(json.dumps(source, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must not assert listing identity"):
        FrozenModelEvidenceRegistryV1.from_jsonl(path)

    source["listingIdentityVerified"] = False
    source["official"]["pageContent"] = "copied body"
    path = tmp_path / "content.jsonl"
    path.write_text(json.dumps(source, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="copied third-party content"):
        FrozenModelEvidenceRegistryV1.from_jsonl(path)


def test_registry_digest_matches_raw_bytes() -> None:
    assert hashlib.sha256(REGISTRY_PATH.read_bytes()).hexdigest() == REGISTRY_SHA256
