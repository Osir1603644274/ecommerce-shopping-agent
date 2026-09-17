"""Contract and scorer tests for Shopping Mission Benchmark V1 MVP."""

from __future__ import annotations

import copy
import inspect
import json
import shutil

import pytest
from jsonschema import Draft202012Validator

from agent.evaluation import shopping_mission_benchmark_v1 as benchmark


def _perfect_predictions():
    _, oracles, _ = benchmark.load_and_validate()
    return [
        {
            "scenarioId": oracle["scenarioId"],
            "schemaVersion": "shopping-mission-prediction-v1-mvp",
            "missionFamily": oracle["missionFamily"],
            "predictedGraph": copy.deepcopy(oracle["expectedGraph"]),
        }
        for oracle in oracles
    ]


def test_schemas_are_draft_202012_meta_valid():
    for path in benchmark.SCHEMA_PATHS.values():
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)


def test_dataset_is_closed_versioned_and_broader_than_travel():
    public, oracles, manifest = benchmark.load_and_validate()
    assert len(public) == len(oracles) == manifest["scenarioCount"] == 18
    assert {row["scenarioId"] for row in public} == {row["scenarioId"] for row in oracles}
    families = {row["missionFamily"] for row in oracles}
    assert families == {
        "BASKET_COMPOSITION", "EVENT_PREPARATION", "GIFT_OR_PROXY",
        "REPLACEMENT_OR_UPGRADE", "COMPATIBILITY_ECOSYSTEM",
        "HOUSEHOLD_OR_LEARNING_PROJECT", "REPLENISHMENT",
        "COMPARISON_DECISION", "CONTRACT_RED",
    }
    travel_rows = [row for row in public if "泰山" in json.dumps(row, ensure_ascii=False)]
    assert len(travel_rows) == 2
    assert sum(row["missionFamily"] == "EVENT_PREPARATION" for row in oracles) == 3


def test_graph_contract_covers_product_research_owned_non_purchase_and_decision_nodes():
    _, oracles, _ = benchmark.load_and_validate()
    kinds = {node["kind"] for row in oracles for node in row["expectedGraph"]["nodes"]}
    assert kinds == {
        "RESEARCH", "QUERY_PRODUCTS", "CHECK_OWNED", "NON_PURCHASE_ACTION",
        "CLARIFY", "COMPARE", "BASKET_VALIDATE",
    }
    dispositions = {
        node["purchaseDisposition"]
        for row in oracles for node in row["expectedGraph"]["nodes"]
    }
    assert dispositions == {"REQUIRED", "MAYBE", "NOT_APPLICABLE"}
    evidence = {
        item for row in oracles for node in row["expectedGraph"]["nodes"]
        for item in node["requiredEvidence"]
    }
    assert evidence == {
        "USER_STATE", "CATALOG_FACT", "BACKEND_DYNAMIC", "EXTERNAL_STABLE",
        "EXTERNAL_DYNAMIC", "POLICY_DOCUMENT",
    }


def test_research_only_contract_red_contains_no_product_query():
    _, oracles, _ = benchmark.load_and_validate()
    oracle = next(row for row in oracles if row["scenarioId"] == "SMB-V1-MVP-018")
    assert all(node["kind"] != "QUERY_PRODUCTS" for node in oracle["expectedGraph"]["nodes"])
    assert oracle["expectedGraph"]["requiredOutputModes"] == [
        "RESEARCH_SUMMARY", "NO_PURCHASE_GUIDANCE", "CLARIFICATION_QUESTION"
    ]


def test_owned_items_are_filters_not_product_queries():
    _, oracles, _ = benchmark.load_and_validate()
    oracle = next(row for row in oracles if row["scenarioId"] == "SMB-V1-MVP-003")
    constraints = oracle["expectedGraph"]["constraints"]
    owned = next(value for value in constraints if value["key"] == "exclude_already_owned")
    assert set(owned["value"]) == {"shell_jacket", "power_bank", "backpack"}
    categories = {
        node.get("category") for node in oracle["expectedGraph"]["nodes"]
        if node["kind"] == "QUERY_PRODUCTS"
    }
    assert categories == {"sneakers", "drinkware"}


def test_oracle_rejects_cycle_unknown_scope_and_spurious_clarification():
    _, oracles, _ = benchmark.load_and_validate()
    cyclic = copy.deepcopy(oracles[0])
    cyclic["expectedGraph"]["dependencies"].append({
        "before": "validate_bundle_total", "after": "query_android_phone", "reason": "REQUIRES"
    })
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="cycle"):
        benchmark._validate_graph(cyclic)

    unknown_scope = copy.deepcopy(oracles[0])
    unknown_scope["expectedGraph"]["constraints"][0]["scope"] = "unknown_node"
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="unknown node"):
        benchmark._validate_graph(unknown_scope)

    spurious = copy.deepcopy(oracles[0])
    spurious["expectedGraph"]["clarification"] = {
        "required": True, "focus": "color", "maxQuestions": 1,
    }
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="mismatch"):
        benchmark._validate_graph(spurious)


def test_public_contract_rejects_private_labels_and_non_contiguous_turns():
    public, _, _ = benchmark.load_and_validate()
    leaked = copy.deepcopy(public[0])
    leaked["turns"][0]["text"] += " routeClass"
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="private-contract"):
        benchmark._validate_public(leaked)
    skipped = copy.deepcopy(public[0])
    skipped["turns"][1]["turnId"] = "T3"
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="non-contiguous"):
        benchmark._validate_public(skipped)


def test_manifest_digest_tamper_fails_closed(tmp_path):
    copied = tmp_path / "dataset"
    shutil.copytree(benchmark.ASSET_DIR, copied)
    public_path = copied / "public" / "scenarios.jsonl"
    public_path.write_text(public_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError):
        benchmark.load_and_validate(copied)


def test_perfect_prediction_scores_one_on_all_metrics():
    report = benchmark.score_predictions(_perfect_predictions())
    assert report["scenarioCount"] == 18
    assert set(report["metrics"].values()) == {1.0}
    assert all(result["graphExact"] for result in report["scenarioResults"])


def test_set_valued_semantics_are_order_invariant():
    predictions = _perfect_predictions()
    changed = False
    for prediction in predictions:
        graph = prediction["predictedGraph"]
        graph["nodes"].reverse()
        graph["dependencies"].reverse()
        graph["constraints"].reverse()
        graph["blockingUnknowns"].reverse()
        graph["requiredOutputModes"].reverse()
        for node in graph["nodes"]:
            node["requiredEvidence"].reverse()
        for constraint in graph["constraints"]:
            if constraint["operator"] in {"in", "not_in"} and isinstance(constraint["value"], list):
                constraint["value"].reverse()
                changed = True
    assert changed
    assert benchmark.score_predictions(predictions)["metrics"]["graphExactRate"] == 1.0


def test_scorer_detects_missing_node_hard_constraint_evidence_and_unknown():
    predictions = _perfect_predictions()
    predictions[0]["predictedGraph"]["nodes"][0]["purchaseDisposition"] = "MAYBE"
    predictions[1]["predictedGraph"]["constraints"] = [
        value for value in predictions[1]["predictedGraph"]["constraints"]
        if value["key"] != "cart_total_cny"
    ]
    predictions[2]["predictedGraph"]["nodes"][0]["requiredEvidence"] = []
    predictions[3]["predictedGraph"]["blockingUnknowns"] = ["trip_duration"]
    predictions[3]["predictedGraph"]["clarification"]["focus"] = "trip_duration"
    report = benchmark.score_predictions(predictions)
    assert report["metrics"]["nodeMicroF1"] < 1.0
    assert report["metrics"]["hardConstraintRecall"] < 1.0
    assert report["metrics"]["evidenceNeedMicroF1"] < 1.0
    assert report["metrics"]["blockingUnknownMicroF1"] < 1.0
    assert report["metrics"]["graphExactRate"] < 1.0


def test_scorer_counts_false_clarification_and_rejects_duplicate_semantics():
    predictions = _perfect_predictions()
    direct = predictions[0]["predictedGraph"]
    direct["blockingUnknowns"] = ["color"]
    direct["clarification"] = {"required": True, "focus": "color", "maxQuestions": 1}
    report = benchmark.score_predictions(predictions)
    assert report["metrics"]["clarificationPrecision"] < 1.0

    predictions = _perfect_predictions()
    duplicate = copy.deepcopy(predictions[0]["predictedGraph"]["nodes"][0])
    predictions[0]["predictedGraph"]["nodes"].append(duplicate)
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="duplicate nodeKey"):
        benchmark.score_predictions(predictions)


def test_prediction_closure_and_extra_fields_fail_closed():
    predictions = _perfect_predictions()
    predictions.pop()
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="closure"):
        benchmark.score_predictions(predictions)

    predictions = _perfect_predictions()
    predictions[0]["receipt"] = {"toolCalls": 1}
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="Additional properties"):
        benchmark.score_predictions(predictions)

    predictions = _perfect_predictions()
    predictions[0]["predictedGraph"]["dependencies"].append({
        "before": "validate_bundle_total", "after": "query_android_phone", "reason": "REQUIRES"
    })
    with pytest.raises(benchmark.ShoppingMissionBenchmarkError, match="cycle"):
        benchmark.score_predictions(predictions)


def test_foundation_has_no_production_or_model_dependency():
    source = inspect.getsource(benchmark)
    assert "agent.app" not in source
    assert "openai" not in source.casefold()
    assert "deepseek" not in source.casefold()
