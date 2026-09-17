"""Independent gates for the materialized controlled CommerceWorld-CN-V1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.evaluation import commerce_controlled_world_v1 as controlled
from agent.evaluation import commerce_world_v1 as world
from agent.evaluation import scenario_lab_v1 as lab


def _build(tmp_path: Path):
    return controlled.materialize_controlled_world(tmp_path / "controlled")


def _rewrite_manifest_after_artifact_change(output_dir: Path, rows: list[dict], artifact_name: str) -> None:
    artifact_path = output_dir / artifact_name
    artifact_path.write_text("".join(world.canonical_bytes(row).decode("utf-8") for row in rows), encoding="utf-8")
    manifest_path = output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for artifact in manifest["artifacts"]:
        if artifact["path"] == artifact_name:
            artifact["sha256"] = world.sha256_file(artifact_path)
    manifest["provenanceSha256"] = world.sha256_bytes(world.canonical_bytes({"worldKind": manifest["worldKind"], "generationSeed": manifest["generationSeed"], "generatorIdentity": manifest["generatorIdentity"], "catalogRevision": manifest["catalogRevision"], "ledgerSha256": manifest["licenseLedgerSha256"], "artifacts": manifest["artifacts"], "environmentRevisions": world.ENVIRONMENTS, "diversity": manifest.get("diversity")}))
    manifest_path.write_bytes(world.canonical_bytes(manifest))


def test_controlled_world_exact_shape_attributes_and_synthetic_tier(tmp_path: Path):
    result = _build(tmp_path)
    manifest = world.load_world_manifest(result.output_dir / "manifest.json")
    assert manifest["status"] == "READY"
    assert manifest["worldKind"] == controlled.CONTROLLED_WORLD_KIND
    assert manifest["actuals"] == world.TARGET_COUNTS
    assert {row["category"]: row["actual"] for row in manifest["categoryCoverage"]} == {category: 1000 for category in world.CATEGORIES}
    assert all(artifact["factTier"] == "synthetic_fixture" for artifact in manifest["artifacts"])
    assert all(row["factTier"] == "synthetic_fixture" for row in result.products)
    for category, metrics in manifest["diversity"]["perCategory"].items():
        assert metrics["uniqueTitles"] >= 750
        assert metrics["uniqueFactVectors"] >= 750
        assert all(coverage >= 2 for coverage in metrics["fieldCoverage"].values())
    for category, spec in controlled.CATEGORY_SPECS.items():
        rows = [row for row in result.products if row["category"] == category]
        assert len(rows) == 1000
        assert all(len(spec["fields"]) >= 6 and all(row["facts"][field]["known"] for field in spec["fields"]) for row in rows)
        assert all(row["facts"]["optionalSellerNote"] == {"known": False, "value": None, "sourceRef": f"{controlled.CONTROLLED_SOURCE_URI}/products#product={row['productId']}&field=optionalSellerNote", "factTier": "synthetic_fixture"} for row in rows[:5])
    merchant_categories = {f"merchant-{index:03d}": set() for index in range(1, 49)}
    for row in result.products:
        merchant_categories[row["merchantId"]].add(row["category"])
    assert all(categories == set(world.CATEGORIES) for categories in merchant_categories.values())


def test_controlled_world_policy_coupon_knowledge_semantics_and_dynamic_witness(tmp_path: Path):
    result = _build(tmp_path)
    root = result.output_dir
    policies = [json.loads(line) for line in (root / "policies.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(policies) == 192
    assert {row["topic"] for row in policies} == {"returns", "shipping", "warranty", "service"}
    assert all(row["merchantId"] in {f"merchant-{index:03d}" for index in range(1, 49)} and len(row["keyFields"]) >= 2 and row["version"] for row in policies)
    coupons = [json.loads(line) for line in (root / "coupons.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(coupons) == 144
    assert {row["kind"] for row in coupons} == {"fixed", "percent"}
    assert {row["scope"] for row in coupons} == {"merchant", "platform"}
    assert {row["stackable"] for row in coupons} == {True, False}
    knowledge = [json.loads(line) for line in (root / "knowledge.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(knowledge) == 120
    for row in knowledge:
        assert row["selectionRule"]["field"] == row["attributeField"]
        assert row["selectionRule"]["value"] == row["attributeValue"]
        assert any(product["category"] == row["category"] and product["facts"][row["attributeField"]]["value"] == row["attributeValue"] for product in result.products)
    environments = {revision: [json.loads(line) for line in (root / "environments" / f"{revision}.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()] for revision in world.ENVIRONMENTS}
    assert all(len(rows) == 12_000 for rows in environments.values())
    assert all(row["previousRevision"] is None for row in environments["E0"])
    assert all(row["previousRevision"] == "E0" for row in environments["E1"])
    e1_by_id = {row["productId"]: row for row in environments["E1"]}
    e2_by_id = {row["productId"]: row for row in environments["E2"]}
    assert all(row["previousPrice"] == e1_by_id[row["productId"]]["price"] for row in environments["E2"])
    assert any(row["stock"] == 0 for row in environments["E2"])
    assert any(row["promotionEligible"] != e2_by_id[row["productId"]]["promotionEligible"] for row in environments["E3"])


def test_controlled_world_coupon_has_solvable_and_unsolvable_threshold_examples(tmp_path: Path):
    result = _build(tmp_path)
    products = list(result.products)
    offers = [json.loads(line) for line in (result.output_dir / "environments" / "E0.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    offer_by_id = {row["productId"]: row for row in offers}
    coupons = [json.loads(line) for line in (result.output_dir / "coupons.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    merchant_one_product = next(product for product in products if product["merchantId"] == "merchant-001" and product["price"] >= 125)
    merchant_coupon = next(coupon for coupon in coupons if coupon["couponId"] == "coupon-merchant-001-01")
    subtotal, discount, total = lab.calculate_cart_total([{"productId": merchant_one_product["productId"], "quantity": 1}], products, [merchant_coupon], environment_rows=[offer_by_id[merchant_one_product["productId"]]], environment_revision="E0")
    assert subtotal == offer_by_id[merchant_one_product["productId"]]["price"]
    assert discount == merchant_coupon["amount"]
    assert total == round(subtotal - discount, 2)
    high_threshold_coupon = next(coupon for coupon in coupons if coupon["couponId"] == "coupon-merchant-001-03")
    assert lab.calculate_cart_total([{"productId": merchant_one_product["productId"], "quantity": 1}], products, [high_threshold_coupon], environment_rows=[offer_by_id[merchant_one_product["productId"]]], environment_revision="E0")[1] == 0.0


def test_controlled_world_is_path_independent_and_manifest_mutation_fails(tmp_path: Path):
    left = _build(tmp_path / "left")
    right = controlled.materialize_controlled_world(tmp_path / "right" / "controlled")
    for relative in ("manifest.json", "license-ledger.jsonl", "products.jsonl", "merchants.jsonl", "policies.jsonl", "coupons.jsonl", "knowledge.jsonl", "environments/E0.jsonl", "environments/E1.jsonl", "environments/E2.jsonl", "environments/E3.jsonl"):
        assert (left.output_dir / relative).read_bytes() == (right.output_dir / relative).read_bytes()
    product_path = left.output_dir / "products.jsonl"
    product_path.write_bytes(product_path.read_bytes() + b"\n")
    with pytest.raises(world.CommerceWorldError, match="SHA mismatch"):
        world.load_world_manifest(left.output_dir / "manifest.json")


def test_controlled_world_rejects_fake_source_claim_and_unknown_value(tmp_path: Path):
    result = _build(tmp_path)
    product_path = result.output_dir / "products.jsonl"
    rows = [json.loads(line) for line in product_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["facts"]["optionalSellerNote"]["value"] = "attacker-value"
    _rewrite_manifest_after_artifact_change(result.output_dir, rows, "products.jsonl")
    with pytest.raises(world.CommerceWorldError, match="unknown fact carries a value"):
        world.load_world_manifest(result.output_dir / "manifest.json")
    second = _build(tmp_path / "second")
    product_path = second.output_dir / "products.jsonl"
    rows = [json.loads(line) for line in product_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["facts"]["brand"]["sourceRef"] = "fake://attacker-controlled"
    rows[0]["facts"]["brand"]["factTier"] = "source_claim"
    _rewrite_manifest_after_artifact_change(second.output_dir, rows, "products.jsonl")
    with pytest.raises(world.CommerceWorldError, match="provenance/tier drift"):
        world.load_world_manifest(second.output_dir / "manifest.json")


def test_controlled_world_policy_terms_keyfields_and_text_are_audited(tmp_path: Path):
    result = _build(tmp_path)
    policy_path = result.output_dir / "policies.jsonl"
    rows = [json.loads(line) for line in policy_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["terms"]["returnDays"] = 9999
    _rewrite_manifest_after_artifact_change(result.output_dir, rows, "policies.jsonl")
    with pytest.raises(world.CommerceWorldError, match="above range"):
        world.load_world_manifest(result.output_dir / "manifest.json")
    second = _build(tmp_path / "second-policy")
    policy_path = second.output_dir / "policies.jsonl"
    rows = [json.loads(line) for line in policy_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["keyFields"] = ["returnDays"]
    _rewrite_manifest_after_artifact_change(second.output_dir, rows, "policies.jsonl")
    with pytest.raises(world.CommerceWorldError, match="terms/keyFields"):
        world.load_world_manifest(second.output_dir / "manifest.json")


def test_controlled_world_rejects_collapsed_twelve_combination_catalog_even_with_rehashed_files(tmp_path: Path):
    result = _build(tmp_path)
    product_path = result.output_dir / "products.jsonl"
    rows = [json.loads(line) for line in product_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for category, spec in controlled.CATEGORY_SPECS.items():
        category_rows = [row for row in rows if row["category"] == category]
        seeds = category_rows[:12]
        for index, target in enumerate(category_rows):
            source = seeds[index % len(seeds)]
            target["title"] = source["title"]
            for field in spec["fields"]:
                target["facts"][field] = dict(source["facts"][field])
                target["facts"][field]["sourceRef"] = f"{controlled.CONTROLLED_SOURCE_URI}/products#product={target['productId']}&field={field}"
            target["facts"]["category"] = dict(target["facts"]["category"])
            target["facts"]["category"]["value"] = category
            target["facts"]["category"]["sourceRef"] = f"{controlled.CONTROLLED_SOURCE_URI}/products#product={target['productId']}&field=category"
            target["facts"]["title"] = dict(target["facts"]["title"])
            target["facts"]["title"]["value"] = target["title"]
            target["facts"]["title"]["sourceRef"] = f"{controlled.CONTROLLED_SOURCE_URI}/products#product={target['productId']}&field=title"
    manifest_path = result.output_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["diversity"] = controlled.diversity_metrics(rows)
    manifest_path.write_bytes(world.canonical_bytes(manifest))
    _rewrite_manifest_after_artifact_change(result.output_dir, rows, "products.jsonl")
    with pytest.raises(world.CommerceWorldError, match="diversity"):
        world.load_world_manifest(manifest_path)


def test_controlled_world_rejects_rehashed_false_changed_fields(tmp_path: Path):
    result = _build(tmp_path)
    environment_path = result.output_dir / "environments" / "E2.jsonl"
    rows = [json.loads(line) for line in environment_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    changed = next(row for row in rows if row["mutation"]["changedFields"] == ["stock"])
    changed["mutation"] = {"kind": "noop", "changedFields": []}
    _rewrite_manifest_after_artifact_change(result.output_dir, rows, "environments/E2.jsonl")
    with pytest.raises(world.CommerceWorldError, match="changedFields"):
        world.load_world_manifest(result.output_dir / "manifest.json")
