"""Contract tests for the independent Commerce Benchmark V1 foundation."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent.evaluation import commerce_benchmark_v1_pilot as pilot
from agent.evaluation import commerce_world_v1 as world
from agent.evaluation import scenario_lab_v1 as lab


FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "fixtures" / "commerce_world_v1_minimal.jsonl"


def _manifest(tmp_path: Path):
    result = world.build_world(FIXTURE, tmp_path / "world")
    return dict(result.manifest), result


def _prediction(scenario_id: str, world_id: str, catalog: str, env: str, **over):
    value = {
        "predictionId": "pred-1",
        "runId": f"run-{scenario_id}",
        "scenarioId": scenario_id,
        "schemaVersion": lab.SCHEMA_VERSIONS["prediction"],
        "worldId": world_id,
        "catalogRevision": catalog,
        "environmentRevision": env,
        "runStatus": "COMPLETED",
        "terminalClass": "SUCCESS",
        "selectedProductIds": [],
        "answerText": "已核验商品事实",
        "claims": [],
        "claimExtraction": {"provenance": "commerce-claim-extractor", "version": "v1", "claimsComplete": True},
        "evidenceCitations": [],
        "documentCitations": [],
        "toolTrace": [],
        "resourceUsage": {"modelCalls": 0, "toolCalls": 0, "latencyMs": 1, "inputTokens": 0, "outputTokens": 0},
    }
    value.update(over)
    if "claims" not in over:
        value["claims"] = [
            {"claimId": f"claim-{index + 1}", "subject": str(citation.get("productId")), "field": str(citation.get("field")), "value": citation.get("value"), "evidenceRefs": [str(citation.get("evidenceId"))]}
                for index, citation in enumerate(value.get("evidenceCitations", []))
        ]
    return value


def _receipt(prediction, oracle, fault):
    return lab.make_runner_receipt(prediction, oracle, fault, world_manifest_sha256="a" * 64, environment_artifact_sha256="b" * 64)


def _ready_oracle(scenario_id: str, world_id: str, catalog: str, env: str, solution_type: str = "single_product", **over):
    value = {
        "scenarioId": scenario_id,
        "schemaVersion": lab.SCHEMA_VERSIONS["oracle"],
        "worldId": world_id,
        "catalogRevision": catalog,
        "environmentRevision": env,
        "split": "development",
        "intentFamily": "product_finder",
        "complexityStratum": "S0_SIMPLE",
        "oracleStatus": "READY",
        "successConditions": {
            "terminalClasses": ["SUCCESS"],
            "solutionType": solution_type,
            "constraintAst": {"kind": "atom", "atom": {"field": "category", "operator": "EQ", "value": "used_phone", "unknownPolicy": "fail"}},
            "evidence": {"minCitations": 1, "forbidUnsupportedClaims": True},
        },
    }
    value.update(over)
    return value


def _input(scenario_id: str, world_id: str, catalog: str, env: str, **over):
    value = {"scenarioId": scenario_id, "schemaVersion": lab.SCHEMA_VERSIONS["input"], "worldId": world_id, "catalogRevision": catalog, "environmentRevision": env, "language": "zh-CN", "turns": [{"turnId": "t1", "role": "user", "text": "我想找一台适合日常使用的二手手机。"}]}
    value.update(over)
    return value


def _fault(scenario_id: str, world_id: str, catalog: str, env: str):
    return {"scenarioId": scenario_id, "schemaVersion": lab.SCHEMA_VERSIONS["fault"], "worldId": world_id, "catalogRevision": catalog, "environmentRevision": env, "faultStatus": "NONE", "injections": []}


def _document_world():
    product = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {
        "category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"},
        "brand": {"known": True, "value": "Apple", "sourceRef": "fixture", "factTier": "source_claim"},
    }}
    knowledge = [{"knowledgeId": "knowledge-used-phone-brand", "category": "used_phone", "topic": "selection_rule", "attributeField": "brand", "attributeValue": "Apple", "selectionRule": {"field": "brand", "operator": "EQ", "value": "Apple", "unknownPolicy": "fail"}, "sourceRef": "synthetic://knowledge/used-phone-brand", "factTier": "synthetic_fixture", "text": "brand=Apple"}]
    policies = [{"policyId": "policy-m1-returns", "merchantId": "m1", "topic": "returns", "version": "v1", "effectiveFrom": "2026-01-01", "effectiveTo": "2027-01-01", "keyFields": ["returnDays", "restockingFeePct"], "terms": {"returnDays": 14, "restockingFeePct": 0.05}, "sourceRef": "synthetic://policy/m1/returns", "factTier": "synthetic_fixture", "text": "returns returnDays=14; restockingFeePct=0.05"}]
    return product, knowledge, policies


def _document_oracle(document_type: str = "knowledge", ast_matches: bool = True):
    oracle = _ready_oracle("ACB-V1-DEV-knowledge_to_product-S1_FIXED_MULTISTEP-01", "w", "c", "E0")
    oracle["intentFamily"] = "knowledge_to_product"
    oracle["complexityStratum"] = "S1_FIXED_MULTISTEP"
    oracle["successConditions"]["acceptableProductIds"] = ["p1"]
    oracle["successConditions"]["constraintAst"] = {"kind": "atom", "atom": {"field": "brand" if ast_matches else "category", "operator": "EQ", "value": "Apple" if ast_matches else "used_phone", "unknownPolicy": "fail"}}
    oracle["successConditions"]["documentDependencies"] = [{"documentType": document_type, "documentId": "knowledge-used-phone-brand" if document_type == "knowledge" else "policy-m1-returns", "field": "brand" if document_type == "knowledge" else "returnDays", "operator": "EQ", "value": "Apple" if document_type == "knowledge" else 14, "binding": "product_constraint" if document_type == "knowledge" else "selected_product_merchant"}]
    return oracle


def test_seven_v1_schemas_pass_draft202012_meta_schema():
    for filename in lab.SCHEMA_FILENAMES.values():
        schema = json.loads((lab.SCHEMAS_DIR / filename).read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)


def test_world_builder_is_deterministic_and_truthfully_pending(tmp_path: Path):
    left = world.build_world(FIXTURE, tmp_path / "left")
    right = world.build_world(FIXTURE, tmp_path / "right")
    assert left.manifest == right.manifest
    assert (left.output_dir / "products.jsonl").read_bytes() == (right.output_dir / "products.jsonl").read_bytes()
    assert left.manifest["status"] == "PENDING_DATA_SCALE"
    assert left.manifest["actuals"]["products"] == 12
    assert left.manifest["targets"]["products"] == 12000
    assert set(left.manifest["environmentRevisions"]) == {"E0", "E1", "E2", "E3"}


def test_world_rejects_unknown_category_and_missing_price_in_12k_shape(tmp_path: Path):
    unknown_path = tmp_path / "unknown-12k.jsonl"
    unknown_path.write_text("\n".join(json.dumps({"productId": f"bad-{index:05d}", "category": "totally_unknown_vertical", "title": "bad"}, ensure_ascii=False) for index in range(12_000)) + "\n", encoding="utf-8")
    with pytest.raises(world.CommerceWorldError, match="unknown or missing product category"):
        world.build_world(unknown_path, tmp_path / "unknown-world")
    missing_price = tmp_path / "missing-price.jsonl"
    missing_price.write_text(json.dumps({"productId": "p1", "category": "used_phone", "title": "phone"}) + "\n", encoding="utf-8")
    with pytest.raises(world.CommerceWorldError, match="price is missing"):
        world.build_world(missing_price, tmp_path / "missing-price-world")


def test_manifest_rejects_missing_required_price_provenance(tmp_path: Path):
    manifest, result = _manifest(tmp_path)
    product_path = result.output_dir / "products.jsonl"
    rows = [json.loads(line) for line in product_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows[0]["facts"]["price"]["factTier"] = "synthetic_fixture"
    product_path.write_text("".join(world.canonical_bytes(row).decode("utf-8") for row in rows), encoding="utf-8")
    manifest_value = json.loads((result.output_dir / "manifest.json").read_text(encoding="utf-8"))
    for artifact in manifest_value["artifacts"]:
        if artifact["path"] == "products.jsonl":
            artifact["sha256"] = world.sha256_file(product_path)
    manifest_value["provenanceSha256"] = world.sha256_bytes(world.canonical_bytes({"catalogRevision": manifest_value["catalogRevision"], "ledgerSha256": manifest_value["licenseLedgerSha256"], "artifacts": manifest_value["artifacts"], "environmentRevisions": world.ENVIRONMENTS}))
    (result.output_dir / "manifest.json").write_bytes(world.canonical_bytes(manifest_value))
    with pytest.raises(world.CommerceWorldError, match="price fact provenance/value audit failed"):
        world.load_world_manifest(result.output_dir / "manifest.json")


def test_all_raw_fact_provenance_is_canonicalized_to_fixture_ledger(tmp_path: Path):
    fixture = tmp_path / "attacker-facts.jsonl"
    fixture.write_text(json.dumps({"productId": "p1", "category": "used_phone", "title": "phone", "price": 100, "facts": {"condition": {"known": True, "value": "excellent", "sourceRef": "fake://attacker-controlled", "factTier": "source_claim"}, "battery": {"known": False, "value": "secret", "sourceRef": "fake://attacker-controlled", "factTier": "source_claim"}}}) + "\n", encoding="utf-8")
    result = world.build_world(fixture, tmp_path / "world")
    product = result.products[0]
    assert product["facts"]["condition"]["sourceRef"] == f"{fixture}#product=p1&field=condition"
    assert product["facts"]["condition"]["factTier"] == "source_claim"
    assert product["facts"]["battery"] == {"known": False, "value": None, "sourceRef": f"{fixture}#product=p1&field=battery", "factTier": "source_claim"}
    world.load_world_manifest(result.output_dir / "manifest.json")


def test_pilot_has_60_dev_30_validation_6_red_and_is_byte_deterministic(tmp_path: Path):
    manifest, result = _manifest(tmp_path)
    first = pilot.generate_pilot_designs(manifest, result.products)
    second = pilot.generate_pilot_designs(manifest, result.products)
    assert {kind: len(rows) for kind, rows in first.items()} == {"input": 96, "oracle": 96, "fault": 96, "prediction": 96}
    assert [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in first["input"]] == [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in second["input"]]
    assert sum(row["oracleStatus"] == "PENDING_SCENARIO_AUTHORING" for row in first["oracle"]) == 96
    assert sum(row["split"] == "development" for row in first["oracle"]) == 60
    assert sum(row["split"] == "validation" for row in first["oracle"]) == 30
    assert sum(row["split"] == "contract_red_team" for row in first["oracle"]) == 6
    for record in first["input"]:
        lab.audit_public_input_leaks(record)
        assert "intentFamily" not in json.dumps(record)


def test_public_private_loader_and_nested_base64_leak_rejected(tmp_path: Path):
    manifest, _ = _manifest(tmp_path)
    sid = "ACB-V1-DEV-product_finder-S0_SIMPLE-01"
    public = _input(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    oracle = _ready_oracle(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    oracle["successConditions"]["acceptableProductIds"] = ["fixture-001"]
    fault = _fault(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    oracle["oracleStatus"] = "PENDING_SCENARIO_AUTHORING"
    fault["faultStatus"] = "PENDING_SCENARIO_AUTHORING"
    prediction = _prediction(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    receipt = _receipt(prediction, oracle, fault)
    paths = {kind: tmp_path / f"{kind}.jsonl" for kind in ("input", "oracle", "fault", "prediction", "receipt")}
    for kind, record in (("input", public), ("oracle", oracle), ("fault", fault), ("prediction", prediction), ("receipt", receipt)):
        lab.write_jsonl(paths[kind], [record], kind)
    aligned = lab.ScenarioV1Set.load(paths["input"], paths["oracle"], paths["fault"], paths["prediction"], paths["receipt"], manifest_path=tmp_path / "world" / "manifest.json")
    assert len(aligned.scenarios) == 1
    view = lab.load_runner_input(paths["input"])[0]
    assert not hasattr(view, "oracle")
    leaked = dict(public)
    leaked["nested"] = {"renamedExpectedProductIds": ["fixture-001"]}
    with pytest.raises(lab.CommerceScenarioError, match="public input leak"):
        lab.audit_public_input_leaks(leaked)
    encoded = base64.b64encode(b'acceptableProductIds:["fixture-001"]').decode("ascii")
    leaked = dict(public)
    leaked["opaque"] = encoded
    with pytest.raises(lab.CommerceScenarioError, match="base64 leak"):
        lab.audit_public_input_leaks(leaked)


def test_loader_rejects_private_product_value_direct_nested_and_base64(tmp_path: Path):
    manifest, _ = _manifest(tmp_path)
    sid = "ACB-V1-DEV-product_finder-S0_SIMPLE-01"
    oracle = _ready_oracle(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    oracle["successConditions"]["acceptableProductIds"] = ["fixture-001"]
    fault = _fault(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    prediction = _prediction(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    receipt = _receipt(prediction, oracle, fault)
    paths = {kind: tmp_path / f"leak-{kind}.jsonl" for kind in ("input", "oracle", "fault", "prediction", "receipt")}
    for kind, record in (("oracle", oracle), ("fault", fault), ("prediction", prediction), ("receipt", receipt)):
        lab.write_jsonl(paths[kind], [record], kind)
    public = _input(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    for leaked_text in ("请只返回 fixture-001", base64.b64encode(b"fixture-001").decode("ascii")):
        public["turns"][0]["text"] = leaked_text
        lab.write_jsonl(paths["input"], [public], "input")
        with pytest.raises(lab.CommerceScenarioError, match="private value"):
            lab.ScenarioV1Set.load(paths["input"], paths["oracle"], paths["fault"], paths["prediction"], paths["receipt"])


def test_environment_revision_drift_is_fail_closed(tmp_path: Path):
    manifest, _ = _manifest(tmp_path)
    sid = "ACB-V1-DEV-product_finder-S0_SIMPLE-01"
    paths = {kind: tmp_path / f"{kind}.jsonl" for kind in ("input", "oracle", "fault", "prediction", "receipt")}
    records = {
        "input": _input(sid, manifest["worldId"], manifest["catalogRevision"], "E0"),
        "oracle": _ready_oracle(sid, manifest["worldId"], manifest["catalogRevision"], "E1"),
        "fault": _fault(sid, manifest["worldId"], manifest["catalogRevision"], "E1"),
        "prediction": _prediction(sid, manifest["worldId"], manifest["catalogRevision"], "E1"),
    }
    records["receipt"] = _receipt(records["prediction"], records["oracle"], records["fault"])
    for kind, record in records.items():
        lab.write_jsonl(paths[kind], [record], kind)
    with pytest.raises(lab.CommerceScenarioError, match="drift"):
        lab.ScenarioV1Set.load(paths["input"], paths["oracle"], paths["fault"], paths["prediction"], paths["receipt"])


def test_unknown_fact_never_passes_and_bundle_verifier_is_deterministic():
    product_known = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"condition": {"known": True, "value": "good"}, "category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    product_unknown = {"productId": "p2", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"condition": {"known": False, "value": None}, "category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    oracle = _ready_oracle("ACB-V1-DEV-product_finder-S0_SIMPLE-01", "w", "c", "E0", sourceProvenance=[])
    oracle["successConditions"]["acceptableProductIds"] = ["p1"]
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p2"], evidenceCitations=[{"evidenceId": "e2", "productId": "p2", "field": "condition", "value": None, "sourceRef": "fixture", "revision": "E0", "factTier": "source_claim"}])
    result = lab.verify_solution(oracle, prediction, [product_known, product_unknown], fault=fault, receipt=_receipt(prediction, oracle, fault), strict_world_binding=False)
    assert result["passed"] is False
    bundle_oracle = _ready_oracle(oracle["scenarioId"], "w", "c", "E0", solution_type="bundle", sourceProvenance=[])
    bundle_oracle["successConditions"]["acceptableBundles"] = [["p1", "p2"]]
    bundle_prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1", "p2"], evidenceCitations=[{"evidenceId": "e1", "productId": "p1", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}, {"evidenceId": "e2", "productId": "p2", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}])
    assert lab.verify_solution(bundle_oracle, bundle_prediction, [product_known, product_unknown], fault=fault, receipt=_receipt(bundle_prediction, bundle_oracle, fault), strict_world_binding=False)["passed"] is True


def test_required_evidence_fields_are_bound_per_selected_product():
    products = [
        {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}, "price": {"known": True, "value": 100, "sourceRef": "fixture", "factTier": "source_claim"}}},
        {"productId": "p2", "category": "used_phone", "merchantId": "m1", "price": 200, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}, "price": {"known": True, "value": 200, "sourceRef": "fixture", "factTier": "source_claim"}}},
        {"productId": "p3", "category": "used_phone", "merchantId": "m1", "price": 300, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}, "price": {"known": True, "value": 300, "sourceRef": "fixture", "factTier": "source_claim"}}},
    ]
    oracle = _ready_oracle("ACB-V1-DEV-multi_select-S1_FIXED_MULTISTEP-required-fields", "w", "c", "E0", solution_type="bundle")
    oracle["successConditions"].update({"acceptableBundles": [["p1", "p2"]], "evidence": {"minCitations": 2, "forbidUnsupportedClaims": True, "requiredFields": ["price"]}})
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")

    def citation(evidence_id: str, product_id: str, field: str, value: object):
        return {"evidenceId": evidence_id, "productId": product_id, "field": field, "value": value, "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}

    missing_p2 = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1", "p2"], evidenceCitations=[citation("p1-price", "p1", "price", 100), citation("p2-category", "p2", "category", "used_phone")])
    assert lab.verify_solution(oracle, missing_p2, products, fault=fault, receipt=_receipt(missing_p2, oracle, fault), strict_world_binding=False)["passed"] is False

    exact = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1", "p2"], evidenceCitations=[citation("p1-price", "p1", "price", 100), citation("p2-price", "p2", "price", 200)])
    assert lab.verify_solution(oracle, exact, products, fault=fault, receipt=_receipt(exact, oracle, fault), strict_world_binding=False)["passed"] is True

    unselected_p3 = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1", "p2"], evidenceCitations=[citation("p1-price", "p1", "price", 100), citation("p2-category", "p2", "category", "used_phone"), citation("p3-price", "p3", "price", 300)])
    assert lab.verify_solution(oracle, unselected_p3, products, fault=fault, receipt=_receipt(unselected_p3, oracle, fault), strict_world_binding=False)["passed"] is False

    no_answer_oracle = _ready_oracle("ACB-V1-DEV-multi_select-S1_FIXED_MULTISTEP-no-answer", "w", "c", "E0")
    no_answer_oracle["successConditions"].update({"terminalClasses": ["CLARIFICATION_REQUIRED"], "allowNoAnswer": True, "evidence": {"minCitations": 0, "forbidUnsupportedClaims": True, "requiredFields": ["price"]}})
    no_answer = _prediction(no_answer_oracle["scenarioId"], "w", "c", "E0", terminalClass="CLARIFICATION_REQUIRED", selectedProductIds=[], answerText="无法确认，请补充预算", claims=[], evidenceCitations=[])
    no_answer_fault = _fault(no_answer_oracle["scenarioId"], "w", "c", "E0")
    assert lab.verify_solution(no_answer_oracle, no_answer, products, fault=no_answer_fault, receipt=_receipt(no_answer, no_answer_oracle, no_answer_fault), strict_world_binding=False)["passed"] is True


def test_cart_coupon_verifier_checks_arithmetic():
    products = [{"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}, "price": {"known": True, "value": 100, "sourceRef": "fixture", "factTier": "source_claim"}}}]
    coupons = [{"couponId": "c1", "merchantId": "m1", "kind": "fixed", "threshold": 50, "amount": 10, "cap": 10, "environmentRevision": "E0", "stackable": False}]
    subtotal, discount, total = lab.calculate_cart_total([{"productId": "p1", "quantity": 1}], products, coupons)
    assert (subtotal, discount, total) == (100.0, 10.0, 90.0)
    oracle = _ready_oracle("ACB-V1-DEV-coupon_budget-S0_SIMPLE-01", "w", "c", "E0", solution_type="cart", sourceProvenance=[])
    oracle["successConditions"]["cartRules"] = {"minItems": 1, "maxItems": 1, "currency": "CNY", "maxTotal": 95}
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "e1", "productId": "p1", "field": "price", "value": 100, "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}], cart={"items": [{"productId": "p1", "quantity": 1}], "subtotal": 100, "discountTotal": 10, "finalTotal": 90, "currency": "CNY", "couponIds": ["c1"]})
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    assert lab.verify_solution(oracle, prediction, products, coupons, fault=fault, receipt=_receipt(prediction, oracle, fault), strict_world_binding=False)["passed"] is True
    prediction["cart"]["finalTotal"] = 99
    assert lab.verify_solution(oracle, prediction, products, coupons, fault=fault, receipt=_receipt(prediction, oracle, fault), strict_world_binding=False)["passed"] is False


def test_constraint_ast_empty_all_not_unknown_and_non_array_in_are_fail_closed():
    assert lab.evaluate_constraint_ast({"kind": "all", "children": []}, {}) is False
    unknown = {"kind": "atom", "atom": {"field": "battery", "operator": "EQ", "value": "90%+", "unknownPolicy": "fail"}}
    assert lab.evaluate_constraint_ast({"kind": "not", "child": unknown}, {}) is False
    assert lab.evaluate_constraint_ast({"kind": "atom", "atom": {"field": "brand", "operator": "IN", "value": "apple", "unknownPolicy": "fail"}}, {"brand": {"known": True, "value": "apple"}}) is False


def test_fabricated_citation_empty_claim_refs_and_incomplete_extractor_fail():
    product = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    oracle = _ready_oracle("ACB-V1-DEV-product_finder-S0_SIMPLE-01", "w", "c", "E0")
    oracle["successConditions"]["acceptableProductIds"] = ["p1"]
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "fake", "productId": "p1", "field": "category", "value": "excellent", "sourceRef": "fabricated", "revision": "c", "factTier": "source_claim"}], claims=[{"claimId": "claim-1", "subject": "p1", "field": "category", "value": "excellent", "evidenceRefs": []}])
    result = lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=_receipt(prediction, oracle, fault), strict_world_binding=False)
    assert result["passed"] is False
    prediction["evidenceCitations"][0].update({"value": "used_phone", "sourceRef": "fixture"})
    prediction["claimExtraction"]["claimsComplete"] = False
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=_receipt(prediction, oracle, fault), strict_world_binding=False)["passed"] is False


def test_factual_answer_with_empty_claims_fails_even_with_attested_receipt():
    product = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    oracle = _ready_oracle("ACB-V1-DEV-product_finder-S0_SIMPLE-01", "w", "c", "E0")
    oracle["successConditions"]["acceptableProductIds"] = ["p1"]
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], claims=[], evidenceCitations=[{"evidenceId": "e", "productId": "p1", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}])
    prediction["answerText"] = "p1 是最适合的商品"
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=_receipt(prediction, oracle, fault), strict_world_binding=False)["passed"] is False


def test_ready_environment_requires_complete_revision_bound_offers_and_exact_receipt():
    product = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    oracle = _ready_oracle("ACB-V1-DEV-product_finder-S0_SIMPLE-01", "w", "c", "E2")
    oracle["successConditions"]["acceptableProductIds"] = ["p1"]
    fault = _fault(oracle["scenarioId"], "w", "c", "E2")
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E2", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "e", "productId": "p1", "field": "price", "value": 120, "sourceRef": "synthetic://offer/E2/p1", "revision": "E2", "factTier": "synthetic_fixture"}])
    receipt = _receipt(prediction, oracle, fault)
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=receipt, world_manifest_sha256="a" * 64, environment_artifact_sha256="b" * 64)["passed"] is False
    mismatched = [{"productId": "p1", "merchantId": "m1", "price": 120, "stock": 2, "environmentRevision": "E1", "sourceRef": "synthetic://offer/E1/p1", "factTier": "synthetic_fixture"}]
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=receipt, environment_rows=mismatched, world_manifest_sha256="a" * 64, environment_artifact_sha256="b" * 64)["passed"] is False
    exact = [{"productId": "p1", "merchantId": "m1", "price": 120, "stock": 2, "promotionEligible": True, "environmentRevision": "E2", "sourceRef": "synthetic://offer/E2/p1", "factTier": "synthetic_fixture"}]
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=receipt, environment_rows=exact, world_manifest_sha256="a" * 64, environment_artifact_sha256="b" * 64)["passed"] is True
    prediction["answerText"] = "answer drift"
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=receipt, environment_rows=exact, world_manifest_sha256="a" * 64, environment_artifact_sha256="b" * 64)["passed"] is False
    prediction["answerText"] = "已核验商品事实"
    fault["injections"].append({"injectionId": "unexpected", "point": "empty_result", "trigger": "after_first_observation", "mutation": {}, "expectedInvariant": "requery_or_abstain", "repeatCount": 1})
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=receipt, environment_rows=exact, world_manifest_sha256="a" * 64, environment_artifact_sha256="b" * 64)["passed"] is False
    assert lab.verify_solution(oracle, prediction, [product], fault=_fault(oracle["scenarioId"], "w", "c", "E2"), receipt=receipt, environment_rows=exact, world_manifest_sha256="c" * 64, environment_artifact_sha256="b" * 64)["passed"] is False


def test_prediction_schema_rejects_embedded_receipts_and_bare_receipt_fails():
    prediction = _prediction("ACB-V1-DEV-product_finder-S0_SIMPLE-01", "w", "c", "E0")
    prediction["runnerAttestation"] = {"attestationStatus": "ATTESTED"}
    with pytest.raises(lab.CommerceScenarioError, match="Additional properties"):
        lab.validate_record(prediction, "prediction")
    prediction.pop("runnerAttestation")
    prediction["answerReceipt"] = {"answerStatus": "ATTESTED"}
    with pytest.raises(lab.CommerceScenarioError, match="Additional properties"):
        lab.validate_record(prediction, "prediction")


def test_scenario_set_requires_physical_receipt_file():
    with pytest.raises(lab.CommerceScenarioError, match="separate runner receipt"):
        lab.ScenarioV1Set.load("input.jsonl", "oracle.jsonl", "fault.jsonl", "prediction.jsonl")


def test_score_set_rejects_untrusted_inputs_and_pending_ready_oracle(tmp_path: Path):
    manifest, result = _manifest(tmp_path)
    sid = "ACB-V1-DEV-product_finder-S0_SIMPLE-01"
    public = _input(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    oracle = _ready_oracle(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    oracle["successConditions"]["acceptableProductIds"] = ["forged-p1"]
    fault = _fault(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    prediction = _prediction(sid, manifest["worldId"], manifest["catalogRevision"], "E0", selectedProductIds=["forged-p1"])
    receipt = _receipt(prediction, oracle, fault)
    paths = {kind: tmp_path / f"ready-{kind}.jsonl" for kind in ("input", "oracle", "fault", "prediction", "receipt")}
    for kind, record in (("input", public), ("oracle", oracle), ("fault", fault), ("prediction", prediction), ("receipt", receipt)):
        lab.write_jsonl(paths[kind], [record], kind)
    with pytest.raises(lab.CommerceScenarioError, match="READY oracle"):
        lab.ScenarioV1Set.load(paths["input"], paths["oracle"], paths["fault"], paths["prediction"], paths["receipt"], manifest_path=result.output_dir / "manifest.json")
    pilot_paths = pilot.write_pilot_designs(result.output_dir / "manifest.json", tmp_path / "pilot", result.output_dir / "products.jsonl")
    aligned = lab.ScenarioV1Set.load(pilot_paths["input"], pilot_paths["oracle"], pilot_paths["fault"], pilot_paths["prediction"], pilot_paths["receipt"], manifest_path=result.output_dir / "manifest.json")
    run = {"runId": "run-test", "strategy": "BOUNDED_REACT", "modelRevision": "model-test", "promptRevision": "prompt-test", "worldRevision": manifest["catalogRevision"], "sealed": False}
    with pytest.raises(lab.CommerceScenarioError, match="unaudited manifest Mapping"):
        lab.score_set(aligned, run_manifest=run, world_manifest=manifest)
    with pytest.raises(lab.CommerceScenarioError, match="caller-supplied"):
        lab.score_set(aligned, [{"productId": "forged-p1"}], run_manifest=run, world_manifest=result.output_dir / "manifest.json")


def test_cart_empty_unknown_cross_merchant_wrong_revision_and_percent_caps_fail_closed():
    products = [{"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}, "price": {"known": True, "value": 100, "sourceRef": "fixture", "factTier": "source_claim"}}}]
    coupons = [{"couponId": "c1", "merchantId": "m1", "kind": "fixed", "threshold": 50, "amount": 10, "cap": 10, "environmentRevision": "E0", "stackable": False}, {"couponId": "c2", "merchantId": "m2", "kind": "fixed", "threshold": 50, "amount": 10, "cap": 10, "environmentRevision": "E0", "stackable": False}]
    oracle = _ready_oracle("ACB-V1-DEV-coupon_budget-S0_SIMPLE-01", "w", "c", "E0", solution_type="cart")
    oracle["successConditions"]["cartRules"] = {"minItems": 1, "maxItems": 1, "currency": "CNY", "maxTotal": 100}
    base = {"items": [{"productId": "p1", "quantity": 1}], "subtotal": 100, "discountTotal": 0, "finalTotal": 100, "currency": "CNY", "couponIds": []}
    pred = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "e", "productId": "p1", "field": "price", "value": 100, "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}], cart=base)
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    assert lab.verify_solution(oracle, pred, products, coupons, fault=fault, receipt=_receipt(pred, oracle, fault), strict_world_binding=False)["passed"] is True
    for bad_coupon in (["unknown"], ["c2"]):
        pred["cart"]["couponIds"] = bad_coupon
        assert lab.verify_solution(oracle, pred, products, coupons, fault=fault, receipt=_receipt(pred, oracle, fault), strict_world_binding=False)["passed"] is False
    pred["cart"]["couponIds"] = ["c1"]
    pred["cart"]["discountTotal"] = 10
    pred["cart"]["finalTotal"] = 90
    coupons[0]["environmentRevision"] = "E1"
    assert lab.verify_solution(oracle, pred, products, coupons, fault=fault, receipt=_receipt(pred, oracle, fault), strict_world_binding=False)["passed"] is False
    percent = [{"couponId": "p1", "merchantId": "m1", "kind": "percent", "threshold": 0, "amount": 0.5, "cap": 10, "environmentRevision": "E0", "stackable": True}, {"couponId": "p2", "merchantId": "m1", "kind": "percent", "threshold": 0, "amount": 0.5, "cap": 10, "environmentRevision": "E0", "stackable": True}]
    assert lab.calculate_cart_total([{"productId": "p1", "quantity": 1}], products, percent, environment_revision="E0")[1] == 20.0
    duplicate_items = list(base["items"]) + list(base["items"])
    with pytest.raises(lab.CommerceScenarioError):
        lab.calculate_cart_total(duplicate_items, products)


def test_active_environment_offer_overrides_catalog_constraints_fail_closed():
    product = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    oracle = _ready_oracle("ACB-V1-DEV-product_finder-S0_SIMPLE-dynamic-scope", "w", "c", "E2")
    oracle["successConditions"]["acceptableProductIds"] = ["p1"]
    fault = _fault(oracle["scenarioId"], "w", "c", "E2")
    offer = {"productId": "p1", "merchantId": "m1", "price": 120, "stock": 0, "promotionEligible": False, "environmentRevision": "E2", "sourceRef": "synthetic://offer/E2/p1", "factTier": "synthetic_fixture"}

    def check(field: str, operator: str, expected: object, citation_value: object, expected_pass: bool):
        oracle["successConditions"]["constraintAst"] = {"kind": "atom", "atom": {"field": field, "operator": operator, "value": expected, "unknownPolicy": "fail"}}
        prediction = _prediction(oracle["scenarioId"], "w", "c", "E2", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": f"e-{field}", "productId": "p1", "field": field, "value": citation_value, "sourceRef": offer["sourceRef"], "revision": "E2", "factTier": "synthetic_fixture"}])
        result = lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=_receipt(prediction, oracle, fault), environment_rows=[offer], world_manifest_sha256="a" * 64, environment_artifact_sha256="b" * 64)
        assert result["passed"] is expected_pass

    # The catalog price is 100, but the active E2 offer is 120.
    check("price", "EQ", 100, 120, False)
    check("price", "EQ", 120, 120, True)
    check("stock", "GTE", 1, 0, False)
    check("promotionEligible", "EQ", True, False, False)
    check("promotionEligible", "EQ", False, False, True)
    oracle["successConditions"]["constraintAst"] = {"kind": "atom", "atom": {"field": "price", "operator": "EQ", "value": 120, "unknownPolicy": "fail"}}
    missing_offer_prediction = _prediction(oracle["scenarioId"], "w", "c", "E2", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "e-missing", "productId": "p1", "field": "price", "value": 120, "sourceRef": offer["sourceRef"], "revision": "E2", "factTier": "synthetic_fixture"}])
    assert lab.verify_solution(oracle, missing_offer_prediction, [product], fault=fault, receipt=_receipt(missing_offer_prediction, oracle, fault), strict_world_binding=False)["passed"] is False


def test_dynamic_environment_sequence_uses_terminal_revision_for_product_and_receipt():
    products = [
        {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}},
        {"productId": "p2", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}},
    ]
    oracle = _ready_oracle("ACB-V1-DEV-dynamic_replanning-S1_FIXED_MULTISTEP-terminal", "w", "c", "E0")
    oracle["successConditions"].update({
        "acceptableProductIds": ["p1", "p2"],
        "environmentSequence": ["E0", "E2"],
        "constraintAst": {"kind": "atom", "atom": {"field": "stock", "operator": "GTE", "value": 1, "unknownPolicy": "fail"}},
    })
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    environment_rows = {
        "E0": [
            {"productId": "p1", "merchantId": "m1", "price": 100, "stock": 1, "promotionEligible": True, "environmentRevision": "E0", "sourceRef": "synthetic://offer/E0/p1", "factTier": "synthetic_fixture"},
            {"productId": "p2", "merchantId": "m1", "price": 100, "stock": 0, "promotionEligible": True, "environmentRevision": "E0", "sourceRef": "synthetic://offer/E0/p2", "factTier": "synthetic_fixture"},
        ],
        "E2": [
            {"productId": "p1", "merchantId": "m1", "price": 100, "stock": 0, "promotionEligible": True, "environmentRevision": "E2", "sourceRef": "synthetic://offer/E2/p1", "factTier": "synthetic_fixture"},
            {"productId": "p2", "merchantId": "m1", "price": 100, "stock": 1, "promotionEligible": True, "environmentRevision": "E2", "sourceRef": "synthetic://offer/E2/p2", "factTier": "synthetic_fixture"},
        ],
    }

    def candidate(product_id: str, citation_revision: str = "E2", receipt_environment_sha: str = "e" * 64, verifier_environment_sha: str | None = None):
        row = next(item for item in environment_rows[citation_revision] if item["productId"] == product_id)
        prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=[product_id], evidenceCitations=[
            {"evidenceId": f"e-category-{product_id}", "productId": product_id, "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"},
            {"evidenceId": f"e-stock-{product_id}", "productId": product_id, "field": "stock", "value": row["stock"], "sourceRef": row["sourceRef"], "revision": citation_revision, "factTier": "synthetic_fixture"},
        ])
        receipt = lab.make_runner_receipt(prediction, oracle, fault, world_manifest_sha256="a" * 64, environment_artifact_sha256=receipt_environment_sha)
        return lab.verify_solution(oracle, prediction, products, fault=fault, receipt=receipt, environment_rows=environment_rows["E0"], environment_rows_by_revision=environment_rows, world_manifest_sha256="a" * 64, environment_artifact_sha256=verifier_environment_sha or receipt_environment_sha)

    # p1 was valid at E0 but is sold out at terminal E2; p2 is the valid replacement.
    assert candidate("p1")["passed"] is False
    assert candidate("p2")["passed"] is True
    # A stale E0 citation cannot support a terminal E2 observation.
    assert candidate("p2", citation_revision="E0")["passed"] is False
    # The receipt must bind the terminal artifact, not the starting E0 artifact.
    assert candidate("p2", receipt_environment_sha="b" * 64, verifier_environment_sha="e" * 64)["passed"] is False


def test_dynamic_environment_sequence_cart_uses_terminal_prices_and_coupon_revision():
    product = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    oracle = _ready_oracle("ACB-V1-DEV-coupon_budget-S1_FIXED_MULTISTEP-terminal", "w", "c", "E0", solution_type="cart")
    oracle["successConditions"].update({
        "environmentSequence": ["E0", "E2"],
        "cartRules": {"minItems": 1, "maxItems": 1, "currency": "CNY", "maxTotal": 200},
    })
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    environment_rows = {
        "E0": [{"productId": "p1", "merchantId": "m1", "price": 100, "stock": 1, "promotionEligible": True, "environmentRevision": "E0", "sourceRef": "synthetic://offer/E0/p1", "factTier": "synthetic_fixture"}],
        "E2": [{"productId": "p1", "merchantId": "m1", "price": 150, "stock": 1, "promotionEligible": True, "environmentRevision": "E2", "sourceRef": "synthetic://offer/E2/p1", "factTier": "synthetic_fixture"}],
    }
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[
        {"evidenceId": "e-category", "productId": "p1", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"},
        {"evidenceId": "e-price", "productId": "p1", "field": "price", "value": 150, "sourceRef": "synthetic://offer/E2/p1", "revision": "E2", "factTier": "synthetic_fixture"},
    ], cart={"items": [{"productId": "p1", "quantity": 1}], "subtotal": 150, "discountTotal": 0, "finalTotal": 150, "currency": "CNY", "couponIds": []})
    receipt = lab.make_runner_receipt(prediction, oracle, fault, world_manifest_sha256="a" * 64, environment_artifact_sha256="e" * 64)
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=receipt, environment_rows=environment_rows["E0"], environment_rows_by_revision=environment_rows, world_manifest_sha256="a" * 64, environment_artifact_sha256="e" * 64)["passed"] is True
    prediction["cart"]["subtotal"] = 100
    prediction["cart"]["finalTotal"] = 100
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=lab.make_runner_receipt(prediction, oracle, fault, world_manifest_sha256="a" * 64, environment_artifact_sha256="e" * 64), environment_rows=environment_rows["E0"], environment_rows_by_revision=environment_rows, world_manifest_sha256="a" * 64, environment_artifact_sha256="e" * 64)["passed"] is False


@pytest.mark.parametrize("sequence", (["E2"], ["E0", "E2", "E2"], [], ["E0", "E9"]))
def test_dynamic_environment_sequence_invalid_or_unavailable_fails_closed(sequence):
    product = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 100, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    oracle = _ready_oracle("ACB-V1-DEV-dynamic_replanning-S1_FIXED_MULTISTEP-invalid-sequence", "w", "c", "E0")
    oracle["successConditions"].update({"acceptableProductIds": ["p1"], "environmentSequence": sequence})
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "e", "productId": "p1", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}])
    receipt = _receipt(prediction, oracle, fault)
    result = lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=receipt, environment_rows=[{"productId": "p1", "merchantId": "m1", "price": 100, "stock": 1, "promotionEligible": True, "environmentRevision": "E0", "sourceRef": "synthetic://offer/E0/p1", "factTier": "synthetic_fixture"}], environment_rows_by_revision={"E0": [{"productId": "p1", "merchantId": "m1", "price": 100, "stock": 1, "promotionEligible": True, "environmentRevision": "E0", "sourceRef": "synthetic://offer/E0/p1", "factTier": "synthetic_fixture"}]}, world_manifest_sha256="a" * 64, environment_artifact_sha256="b" * 64)
    assert result["passed"] is False


def test_coupon_threshold_and_discount_use_merchant_scope_not_whole_cart():
    products = [
        {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 60, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}, "price": {"known": True, "value": 60, "sourceRef": "fixture", "factTier": "source_claim"}}},
        {"productId": "p2", "category": "used_phone", "merchantId": "m2", "price": 1000, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}, "price": {"known": True, "value": 1000, "sourceRef": "fixture", "factTier": "source_claim"}}},
    ]
    items = [{"productId": "p1", "quantity": 1}, {"productId": "p2", "quantity": 1}]
    threshold_coupon = {"couponId": "m1-threshold", "merchantId": "m1", "scope": "merchant", "kind": "fixed", "threshold": 100, "amount": 50, "cap": 50, "environmentRevision": "E0", "stackable": False}
    assert lab.calculate_cart_total(items, products, [threshold_coupon], environment_revision="E0")[1] == 0.0
    percent_coupon = {"couponId": "m1-percent", "merchantId": "m1", "scope": "merchant", "kind": "percent", "threshold": 0, "amount": 0.5, "cap": 100, "environmentRevision": "E0", "stackable": False}
    platform_coupon = {"couponId": "platform-percent", "merchantId": "platform", "scope": "platform", "kind": "percent", "threshold": 0, "amount": 0.1, "cap": 2000, "environmentRevision": "E0", "stackable": False}
    assert lab.calculate_cart_total(items, products, [percent_coupon], environment_revision="E0") == (1060.0, 30.0, 1030.0)
    assert lab.calculate_cart_total(items, products, [platform_coupon], environment_revision="E0") == (1060.0, 106.0, 954.0)
    fixed_coupon = {"couponId": "m1-fixed", "merchantId": "m1", "scope": "merchant", "kind": "fixed", "threshold": 0, "amount": 100, "cap": 100, "environmentRevision": "E0", "stackable": False}
    assert lab.calculate_cart_total(items, products, [fixed_coupon], environment_revision="E0")[1] == 60.0
    with pytest.raises(lab.CommerceScenarioError, match="applicable cart items"):
        lab.calculate_cart_total([{"productId": "p2", "quantity": 1}], products, [percent_coupon], environment_revision="E0")

    oracle = _ready_oracle("ACB-V1-DEV-coupon_budget-S0_SIMPLE-scope", "w", "c", "E0", solution_type="cart")
    oracle["successConditions"]["cartRules"] = {"minItems": 2, "maxItems": 2, "currency": "CNY", "maxTotal": 1060, "allowedCouponIds": ["m1-percent"]}
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1", "p2"], evidenceCitations=[
        {"evidenceId": "e-p1", "productId": "p1", "field": "price", "value": 60, "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"},
        {"evidenceId": "e-p2", "productId": "p2", "field": "price", "value": 1000, "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"},
    ], cart={"items": items, "subtotal": 1060, "discountTotal": 30, "finalTotal": 1030, "currency": "CNY", "couponIds": ["m1-percent"]})
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    assert lab.verify_solution(oracle, prediction, products, [percent_coupon], fault=fault, receipt=_receipt(prediction, oracle, fault), strict_world_binding=False)["passed"] is True


def test_stackable_coupon_consumption_is_scoped_canonical_and_order_invariant():
    products = [
        {"productId": "p1", "category": "used_phone", "merchantId": "m1", "price": 60, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}},
        {"productId": "p2", "category": "used_phone", "merchantId": "m2", "price": 1000, "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}},
    ]
    items = [{"productId": "p1", "quantity": 1}, {"productId": "p2", "quantity": 1}]
    m1_a = {"couponId": "m1-a", "merchantId": "m1", "scope": "merchant", "kind": "fixed", "threshold": 0, "amount": 50, "cap": 50, "environmentRevision": "E0", "stackable": True}
    m1_b = {"couponId": "m1-b", "merchantId": "m1", "scope": "merchant", "kind": "fixed", "threshold": 0, "amount": 50, "cap": 50, "environmentRevision": "E0", "stackable": True}
    assert lab.calculate_cart_total(items, products, [m1_a, m1_b], environment_revision="E0") == (1060.0, 60.0, 1000.0)
    assert lab.calculate_cart_total(items, products, [m1_b, m1_a], environment_revision="E0") == (1060.0, 60.0, 1000.0)
    m2_one = {"couponId": "m2-one", "merchantId": "m2", "scope": "merchant", "kind": "fixed", "threshold": 0, "amount": 50, "cap": 50, "environmentRevision": "E0", "stackable": True}
    assert lab.calculate_cart_total(items, products, [m1_a, m2_one], environment_revision="E0")[1] == 100.0
    platform = {"couponId": "platform", "merchantId": "platform", "scope": "platform", "kind": "fixed", "threshold": 0, "amount": 2000, "cap": 2000, "environmentRevision": "E0", "stackable": True}
    assert lab.calculate_cart_total(items, products, [m1_a, platform], environment_revision="E0") == (1060.0, 1060.0, 0.0)

    oracle = _ready_oracle("ACB-V1-DEV-coupon_budget-S0_SIMPLE-canonical-order", "w", "c", "E0", solution_type="cart")
    oracle["successConditions"]["cartRules"] = {"minItems": 2, "maxItems": 2, "currency": "CNY", "maxTotal": 1060, "allowedCouponIds": ["m1-a", "m1-b"]}
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1", "p2"], evidenceCitations=[
        {"evidenceId": "e-p1-order", "productId": "p1", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"},
        {"evidenceId": "e-p2-order", "productId": "p2", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"},
    ], cart={"items": items, "subtotal": 1060, "discountTotal": 60, "finalTotal": 1000, "currency": "CNY", "couponIds": ["m1-b", "m1-a"]})
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    assert lab.verify_solution(oracle, prediction, products, [m1_a, m1_b], fault=fault, receipt=_receipt(prediction, oracle, fault), strict_world_binding=False)["passed"] is True


def test_manifest_is_exactly_mapped_and_artifact_mutation_is_rejected(tmp_path: Path):
    manifest, result = _manifest(tmp_path)
    assert manifest["actuals"] == {"products": 12, "merchants": 48, "policies": 192, "coupons": 144, "knowledge": 120}
    assert len((result.output_dir / "merchants.jsonl").read_text(encoding="utf-8").splitlines()) == 48
    assert manifest["licenseGate"] == "pending_review"
    world.load_world_manifest(result.output_dir / "manifest.json")
    product_path = result.output_dir / "products.jsonl"
    product_path.write_bytes(product_path.read_bytes() + b"\n")
    with pytest.raises(world.CommerceWorldError, match="SHA mismatch"):
        world.load_world_manifest(result.output_dir / "manifest.json")


def test_pilot_requires_audited_exact_ready_manifest_and_enumerates_real_bundles(tmp_path: Path):
    manifest, result = _manifest(tmp_path)
    records = pilot.generate_pilot_designs(manifest, result.products)
    assert all(row["oracleStatus"] == "PENDING_SCENARIO_AUTHORING" for row in records["oracle"])
    products = [{"productId": "a", "category": "used_phone", "merchantId": "m1", "price": 1}, {"productId": "b", "category": "used_phone", "merchantId": "m1", "price": 1}, {"productId": "c", "category": "used_phone", "merchantId": "m2", "price": 1}]
    assert pilot.enumerate_acceptable_products(products, "used_phone") == ("a", "b", "c")
    assert pilot.enumerate_acceptable_bundles(products, "used_phone") == (("a", "b"),)
    assert pilot.enumerate_acceptable_carts(products, "used_phone", max_total=1) == (("a",), ("b",), ("c",))


def test_pilot_cosmetic_skeleton_stays_pending_even_when_counts_look_ready():
    manifest = {"worldId": "w", "catalogRevision": "c", "status": "READY", "manifestAudit": "verified", "licenseGate": "synthetic_fixture", "actuals": dict(world.TARGET_COUNTS), "categoryCoverage": [{"category": category, "target": 1000, "actual": 1000} for category in world.CATEGORIES]}
    products = [{"productId": f"p-{index:05d}", "category": world.CATEGORIES[index % len(world.CATEGORIES)], "merchantId": f"merchant-{index % 48 + 1:03d}", "price": 1} for index in range(12_000)]
    records = pilot.generate_pilot_designs(manifest, products, [{"couponId": f"c-{index:03d}"} for index in range(144)])
    assert len(records["oracle"]) == 96
    assert {row["oracleStatus"] for row in records["oracle"]} == {"PENDING_SCENARIO_AUTHORING"}
    assert all("Phase 2" in row["designReason"] for row in records["oracle"])


def test_pilot_enumerators_are_bounded_for_12k_shape():
    products = [{"productId": f"p-{index:05d}", "category": "used_phone", "merchantId": f"merchant-{index % 48 + 1:03d}", "price": 1} for index in range(12_000)]
    started = time.perf_counter()
    bundles = pilot.enumerate_acceptable_bundles(products, "used_phone", max_results=64)
    carts = pilot.enumerate_acceptable_carts(products, "used_phone", max_total=10, max_results=64)
    elapsed = time.perf_counter() - started
    assert len(bundles) == 64
    assert len(carts) == 64
    assert elapsed < 5.0


def test_s2_requires_exact_fault_observation_and_single_product_completed_once():
    sid = "ACB-V1-DEV-dynamic_replanning-S2_OBSERVATION_DEPENDENT-01"
    product = {"productId": "p1", "category": "used_phone", "merchantId": "m1", "sourceRef": "fixture", "facts": {"category": {"known": True, "value": "used_phone", "sourceRef": "fixture", "factTier": "source_claim"}}}
    oracle = _ready_oracle(sid, "w", "c", "E0")
    oracle["complexityStratum"] = "S2_OBSERVATION_DEPENDENT"
    oracle["successConditions"]["requiredObservationPoints"] = ["stock_change"]
    oracle["successConditions"]["acceptableProductIds"] = ["p1"]
    fault = {"scenarioId": sid, "worldId": "w", "catalogRevision": "c", "environmentRevision": "E0", "faultStatus": "READY", "injections": [{"injectionId": "i1", "point": "stock_change", "trigger": "after_first_observation", "mutation": {}, "expectedInvariant": "do_not_sell_out", "repeatCount": 1}]}
    pred = _prediction(sid, "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "e", "productId": "p1", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}], toolTrace=[{"index": 0, "tool": "search", "outcome": "ok", "observationDependent": True, "observationPoint": "wrong_point", "injectionId": "i1", "beforeObservationDigest": "1" * 64, "afterObservationDigest": "2" * 64, "outputDigest": "3" * 64}], resourceUsage={"modelCalls": 1, "toolCalls": 1, "latencyMs": 1, "inputTokens": 1, "outputTokens": 1})
    assert lab.verify_solution(oracle, pred, [product], fault=fault, receipt=_receipt(pred, oracle, fault), strict_world_binding=False)["passed"] is False
    pred["toolTrace"][0]["observationPoint"] = "stock_change"
    bare = dict(pred)
    assert lab.verify_solution(oracle, bare, [product], fault=fault, receipt=None, strict_world_binding=False)["passed"] is False
    receipt = _receipt(pred, oracle, fault)
    assert lab.verify_solution(oracle, pred, [product], fault=fault, receipt=receipt, strict_world_binding=False)["passed"] is True
    receipt["outputDigestSha256"] = "f" * 64
    assert lab.verify_solution(oracle, pred, [product], fault=fault, receipt=receipt, strict_world_binding=False)["passed"] is False
    pred["runStatus"] = "NOT_RUN"
    assert lab.verify_solution(oracle, pred, [product], fault=fault, receipt=_receipt(pred, oracle, fault), strict_world_binding=False)["eligible"] is False


def test_score_set_requires_explicit_run_and_real_world_sha(tmp_path: Path):
    manifest, result = _manifest(tmp_path)
    pilot_paths = pilot.write_pilot_designs(result.output_dir / "manifest.json", tmp_path / "pilot", result.output_dir / "products.jsonl")
    aligned = lab.ScenarioV1Set.load(pilot_paths["input"], pilot_paths["oracle"], pilot_paths["fault"], pilot_paths["prediction"], pilot_paths["receipt"], manifest_path=result.output_dir / "manifest.json")
    with pytest.raises(lab.CommerceScenarioError, match="explicit run_manifest"):
        lab.score_set(aligned, world_manifest=result.output_dir / "manifest.json")
    with pytest.raises(lab.CommerceScenarioError, match="empty/placeholder runId"):
        lab.score_set(aligned, run_manifest={"runId": "not-run", "strategy": "BOUNDED_REACT", "modelRevision": "model-test", "promptRevision": "prompt-test", "worldRevision": manifest["catalogRevision"], "sealed": False}, world_manifest=result.output_dir / "manifest.json")
    report = lab.score_set(aligned, run_manifest={"runId": "run-pilot-test", "strategy": "BOUNDED_REACT", "modelRevision": "model-test", "promptRevision": "prompt-test", "worldRevision": manifest["catalogRevision"], "sealed": False}, world_manifest=result.output_dir / "manifest.json")
    assert report["aggregate"]["eligible"] == 0
    assert len(report["aggregate"]["byIntentStratum"]) == 15
    assert report["sourceSha256"]["world"] != "0" * 64


def test_document_dependency_requires_exact_knowledge_citation_and_ast_atom():
    product, knowledge, policies = _document_world()
    oracle = _document_oracle()
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "product-e1", "productId": "p1", "field": "brand", "value": "Apple", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}], documentCitations=[{"evidenceId": "document-e1", "documentType": "knowledge", "documentId": "knowledge-used-phone-brand", "field": "brand", "value": "Apple", "sourceRef": "synthetic://knowledge/used-phone-brand", "revision": "c", "factTier": "synthetic_fixture"}])
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=_receipt(prediction, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is True

    missing = dict(prediction)
    missing["documentCitations"] = []
    missing["claims"] = list(prediction["claims"])
    assert lab.verify_solution(oracle, missing, [product], fault=fault, receipt=_receipt(missing, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is False

    fake = dict(prediction)
    fake["documentCitations"] = [dict(prediction["documentCitations"][0], documentId="knowledge-forged")]
    assert lab.verify_solution(oracle, fake, [product], fault=fault, receipt=_receipt(fake, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is False

    no_atom = _document_oracle(ast_matches=False)
    assert lab.verify_solution(no_atom, prediction, [product], fault=_fault(no_atom["scenarioId"], "w", "c", "E0"), receipt=_receipt(prediction, no_atom, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is False


def test_policy_dependency_binds_selected_merchant_terms_and_version():
    product, knowledge, policies = _document_world()
    oracle = _document_oracle("policy")
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[{"evidenceId": "product-e1", "productId": "p1", "field": "category", "value": "used_phone", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}], documentCitations=[{"evidenceId": "document-e1", "documentType": "policy", "documentId": "policy-m1-returns", "field": "returnDays", "value": 14, "sourceRef": "synthetic://policy/m1/returns", "revision": "c", "documentVersion": "v1", "factTier": "synthetic_fixture"}])
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=_receipt(prediction, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is True

    foreign = dict(prediction)
    foreign["selectedProductIds"] = ["p2"]
    assert lab.verify_solution(oracle, foreign, [{**product, "productId": "p2", "merchantId": "m2"}], fault=fault, receipt=_receipt(foreign, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is False

    drift = dict(prediction)
    drift["documentCitations"] = [dict(prediction["documentCitations"][0], field="missingTerm")]
    assert lab.verify_solution(oracle, drift, [product], fault=fault, receipt=_receipt(drift, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is False
    drift = dict(prediction)
    drift["documentCitations"] = [dict(prediction["documentCitations"][0], documentVersion="v2")]
    assert lab.verify_solution(oracle, drift, [product], fault=fault, receipt=_receipt(drift, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is False


def test_document_citation_source_revision_and_global_evidence_collision_fail_closed():
    product, knowledge, policies = _document_world()
    oracle = _document_oracle()
    fault = _fault(oracle["scenarioId"], "w", "c", "E0")
    base_product = {"evidenceId": "same-evidence", "productId": "p1", "field": "brand", "value": "Apple", "sourceRef": "fixture", "revision": "c", "factTier": "source_claim"}
    base_document = {"evidenceId": "same-evidence", "documentType": "knowledge", "documentId": "knowledge-used-phone-brand", "field": "brand", "value": "Apple", "sourceRef": "synthetic://knowledge/used-phone-brand", "revision": "c", "factTier": "synthetic_fixture"}
    prediction = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[base_product], documentCitations=[base_document])
    assert lab.verify_solution(oracle, prediction, [product], fault=fault, receipt=_receipt(prediction, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is False
    for change in ({"sourceRef": "synthetic://forged"}, {"value": "Samsung"}, {"revision": "other-catalog"}):
        changed = dict(base_product)
        changed["evidenceId"] = "product-evidence"
        changed.update(change)
        doc = dict(base_document, evidenceId="document-evidence")
        candidate = _prediction(oracle["scenarioId"], "w", "c", "E0", selectedProductIds=["p1"], evidenceCitations=[changed], documentCitations=[doc])
        assert lab.verify_solution(oracle, candidate, [product], fault=fault, receipt=_receipt(candidate, oracle, fault), knowledge=knowledge, policies=policies, strict_world_binding=False)["passed"] is False


def test_document_private_ids_direct_nested_and_base64_are_rejected(tmp_path: Path):
    manifest, _ = _manifest(tmp_path)
    sid = "ACB-V1-DEV-knowledge_to_product-S1_FIXED_MULTISTEP-01"
    oracle = _ready_oracle(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    oracle["successConditions"]["documentDependencies"] = [{"documentType": "knowledge", "documentId": "knowledge-private-01", "field": "brand", "operator": "EQ", "value": "Apple", "binding": "product_constraint"}]
    fault = _fault(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    public = _input(sid, manifest["worldId"], manifest["catalogRevision"], "E0")
    encoded = base64.b64encode(b"knowledge-private-01").decode("ascii")
    for leaked in ("knowledge-private-01", {"nested": "knowledge-private-01"}, encoded):
        candidate = dict(public)
        candidate["turns"] = [{"turnId": "t1", "role": "user", "text": leaked if isinstance(leaked, str) else "normal"}]
        if isinstance(leaked, dict):
            candidate["metadata"] = leaked
        with pytest.raises(lab.CommerceScenarioError, match="private value"):
            lab.audit_public_private_value_leaks(candidate, oracle, fault)
