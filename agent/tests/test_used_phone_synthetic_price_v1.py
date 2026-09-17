import copy
import json
import os
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent.evaluation.used_phone_synthetic_price_v1 import (
    DEFAULT_SEED,
    EXPECTED_ITEM_COUNT,
    SyntheticPriceContractError,
    build_rows,
    derive_price,
    load_catalog,
    validate_bundle,
)
from agent.scripts.freeze_used_phone_synthetic_prices_v1 import freeze
from agent.app.domains.ecommerce.models import ShoppingRequirement, rule_rerank_candidates
from agent.app.domains.ecommerce.synthetic_prices import (
    SyntheticPriceRuntimeError,
    apply_synthetic_prices,
    synthetic_price_value,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FROZEN_BUNDLE = REPOSITORY_ROOT / "data/derived/ecommerce/used_phone_synthetic_reference_price_v1"
EXPANDED_BUNDLE = REPOSITORY_ROOT / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3"
SCHEMA = REPOSITORY_ROOT / "agent/evaluation/schemas/used_phone_synthetic_reference_price_v1.schema.json"


def _configured_source_catalog() -> Path | None:
    raw = os.getenv("USED_PHONE_SOURCE_CATALOG", "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        return path if path.is_file() else None
    except OSError:
        return None


CATALOG = _configured_source_catalog()


def _row(**values):
    attributes = {
        key: {"status": "known", "value": value}
        for key, value in {
            "battery_health": "90_95",
            "screen_originality": "original",
            "motherboard_repair": "not_repaired",
            "battery_originality": "original",
            "scratch_level": "none",
            "shell_condition": "normal",
            "os": "ios",
        }.items()
    }
    return {
        "itemId": values.get("itemId", "1001"),
        "brand": values.get("brand", "苹果/Apple"),
        "title": values.get("title", "Apple iPhone 15 Pro 二手手机"),
        "attributes": values.get("attributes", attributes),
    }


def test_price_is_deterministic_and_explicitly_synthetic():
    first = derive_price(_row(), seed=DEFAULT_SEED)
    second = derive_price(_row(), seed=DEFAULT_SEED)
    assert first == second
    assert first["dataNature"] == first["priceStatus"] == "synthetic"
    assert first["disclosureZh"] == "AI 合成，非真实报价"
    assert "snapshotPriceMinor" not in first
    assert abs(first["derivation"]["perturbationFraction"]) <= 0.04


def test_seven_attributes_only_reduce_and_critical_defect_cannot_get_positive_noise():
    clean = _row()
    damaged = copy.deepcopy(clean)
    damaged["attributes"]["motherboard_repair"]["value"] = "repaired"
    clean_price = derive_price(clean)
    damaged_price = derive_price(damaged)
    assert damaged_price["referencePriceMinor"] < clean_price["referencePriceMinor"]
    assert damaged_price["derivation"]["perturbationFraction"] <= 0
    assert damaged_price["derivation"]["criticalDefectPositivePerturbationCapped"] is True


def test_title_inference_is_recorded_as_synthetic_not_source_fact():
    price = derive_price(_row(title="Apple iPhone 16 Pro Max 美版二手"))
    derivation = price["derivation"]
    assert derivation["titleTier"] == "current_flagship"
    assert derivation["titleInferenceStatus"] == "synthetic_inference_not_source_fact"
    assert derivation["titleInferencePattern"]


@pytest.mark.skipif(CATALOG is None, reason="set USED_PHONE_SOURCE_CATALOG to the SHA-pinned source catalog")
def test_full_catalog_freeze_is_complete_reproducible_and_no_overwrite(tmp_path):
    assert CATALOG is not None
    rows = build_rows(load_catalog(CATALOG))
    assert len(rows) == EXPECTED_ITEM_COUNT
    first = freeze(catalog=CATALOG, output=tmp_path)
    second = freeze(catalog=CATALOG, output=tmp_path)
    assert first == second
    manifest, by_id = validate_bundle(tmp_path)
    assert len(by_id) == EXPECTED_ITEM_COUNT
    assert manifest["policyBoundary"]["mayPopulateVerifiedSnapshot"] is False
    prices_path = tmp_path / "prices.jsonl"
    prices_path.write_text(prices_path.read_text(encoding="utf-8") + "{}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        freeze(catalog=CATALOG, output=tmp_path)


def test_committed_frozen_bundle_is_complete_and_valid_without_external_worktree():
    manifest, by_id = validate_bundle(FROZEN_BUNDLE)

    assert len(by_id) == EXPECTED_ITEM_COUNT
    assert manifest["sourceCatalog"]["sha256"] == (
        "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50"
    )
    assert manifest["output"]["rowCount"] == EXPECTED_ITEM_COUNT
    assert manifest["policyBoundary"]["mayPopulateVerifiedSnapshot"] is False


def test_catalog_sha_mismatch_fails_closed(tmp_path):
    fake = tmp_path / "catalog.jsonl"
    fake.write_text(json.dumps(_row(), ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(SyntheticPriceContractError, match="SHA-256"):
        load_catalog(fake)


def test_runtime_overlay_never_populates_verified_snapshot_fields():
    product = {"id": 1029734, "priceStatus": "unverified", "snapshotPriceMinor": None}
    overlaid = apply_synthetic_prices(
        [product], directory=str(FROZEN_BUNDLE), policy="display_only"
    )[0]
    assert overlaid["priceStatus"] == "unverified"
    assert overlaid["snapshotPriceMinor"] is None
    assert overlaid["syntheticReferencePrice"]["priceStatus"] == "synthetic"
    assert synthetic_price_value(overlaid, allow_budget=True) == (None, None)
    assert synthetic_price_value(overlaid, allow_budget=False)[0] > 0


def test_runtime_accepts_audited_439_expansion_bundle_without_weakening_252_identity():
    prices = [
        json.loads(line)
        for line in (EXPANDED_BUNDLE / "prices.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(prices) == 439
    expanded_row = next(
        row
        for row in prices
        if int(row["itemId"]) > 2**31
    )
    product = {
        "id": int(expanded_row["itemId"]),
        "priceStatus": "unverified",
        "snapshotPriceMinor": None,
    }
    overlaid = apply_synthetic_prices(
        [product], directory=str(EXPANDED_BUNDLE), policy="budget_and_ranking"
    )[0]
    projected = overlaid["syntheticReferencePrice"]
    assert projected["referencePriceMinor"] == expanded_row["referencePriceMinor"]
    assert projected["sourceCatalogSha256"] == (
        "725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75"
    )
    assert synthetic_price_value(overlaid, allow_budget=True)[0] == expanded_row["referencePriceMinor"]


def test_budget_policy_is_explicit_and_auditable_in_reranker():
    product = {
        "id": 1029734,
        "title": "小米8",
        "brand": "小米/MI",
        "source": "KuaiSearch",
        "provenanceUrl": "https://example.invalid/frozen",
        "priceStatus": "unverified",
        "snapshotPriceMinor": None,
        "currency": "CNY",
        "categoryL1": "二手",
        "categoryL2": "二手手机通讯",
        "categoryL3": "二手手机",
        "attributeText": "",
        "attributes": [],
    }
    budget = ShoppingRequirement(
        key="price_minor", operator="lte", value=500_000, unit="CNY_MINOR",
        priority="hard", source="user",
    )
    display = apply_synthetic_prices(
        [product], directory=str(FROZEN_BUNDLE), policy="display_only"
    )
    budgeted = apply_synthetic_prices(
        [product], directory=str(FROZEN_BUNDLE), policy="budget_and_ranking"
    )
    assert rule_rerank_candidates("phone", display, [budget])["products"][0]["hardUnknowns"] == 1
    result = rule_rerank_candidates("phone", budgeted, [budget])
    assert result["products"][0]["checks"][0]["status"] == "pass"
    price_evidence = next(
        row for row in result["evidence"]
        if row["field"] == "syntheticReferencePriceMinor"
    )
    assert price_evidence["priceStatus"] == "synthetic"
    assert price_evidence["pricePolicy"] == "budget_and_ranking"


def test_runtime_tampering_fails_closed():
    product = {"id": 1029734, "priceStatus": "unverified", "snapshotPriceMinor": None}
    overlaid = apply_synthetic_prices(
        [product], directory=str(FROZEN_BUNDLE), policy="budget_and_ranking"
    )[0]
    overlaid["syntheticReferencePrice"]["sourceCatalogSha256"] = "0" * 64
    with pytest.raises(SyntheticPriceRuntimeError, match="invalid synthetic"):
        synthetic_price_value(overlaid, allow_budget=True)


def test_all_frozen_rows_match_published_json_schema():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    rows = [json.loads(line) for line in (FROZEN_BUNDLE / "prices.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == EXPECTED_ITEM_COUNT
    for row in rows:
        assert list(validator.iter_errors(row)) == []
