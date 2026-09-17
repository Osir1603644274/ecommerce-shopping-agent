"""Tests for Shopping Task State V2 contracts, runner and scorer."""

from __future__ import annotations

import copy
import inspect
import json

import pytest
from jsonschema import Draft202012Validator

from agent.evaluation import shopping_task_state_v2_mvp_r2 as dataset
from agent.evaluation import shopping_task_state_v2_runner as runner
from agent.evaluation import shopping_task_state_v2_scorer as scorer
from agent.evaluation.shopping_task_state_v2_contract import SCHEMA_PATHS, validate_record


def _atom(requirement):
    return {field: requirement[field] for field in scorer.ATOM_FIELDS}


def _perfect_artifacts(strategy="PAE", run_id="perfect-run"):
    _, oracle_rows = dataset.load_and_validate()
    predictions = []
    receipts = []
    turn_lookup = {}
    for oracle in oracle_rows:
        definitions = {row["requirementId"]: row for row in oracle["requirementDefinitions"]}
        turns = []
        turn_receipts = []
        for annotation in oracle["turnAnnotations"]:
            delta = []
            for item in annotation["delta"]:
                projected = {
                    "op": item["op"],
                    "requirement": _atom(definitions[item["requirementId"]]),
                }
                if "replacesRequirementId" in item:
                    projected["replaces"] = _atom(definitions[item["replacesRequirementId"]])
                delta.append(projected)
            memory_events = []
            for event in annotation["memoryEvents"]:
                projected_event = {
                    "op": event["op"],
                    "memoryKey": event["memoryKey"],
                    "scope": event["scope"],
                }
                if "value" in event:
                    projected_event["value"] = event["value"]
                memory_events.append(projected_event)
            prediction = {
                "turnId": annotation["turnId"],
                "intent": annotation["intent"],
                "delta": delta,
                "activeRequirements": [
                    _atom(definitions[item]) for item in annotation["activeRequirementIds"]
                ],
                "informationSufficiency": copy.deepcopy(annotation["informationSufficiency"]),
                "routeClass": annotation["routeClass"],
                "clarification": copy.deepcopy(annotation["clarification"]),
                "memoryEvents": memory_events,
                "candidateScope": annotation["candidateScope"],
                "environmentRevision": annotation["environmentRevision"],
            }
            turns.append(prediction)
            turn_lookup[(oracle["scenarioId"], annotation["turnId"])] = prediction
            turn_receipts.append({
                "turnId": annotation["turnId"],
                "usageStatus": "NOT_INSTRUMENTED",
                "modelCalls": None,
                "toolCalls": None,
                "latencyMs": None,
                "inputTokens": None,
                "outputTokens": None,
                "recoveryCount": None,
                "errorCode": None,
            })
        predictions.append({
            "scenarioId": oracle["scenarioId"],
            "schemaVersion": "shopping-task-state-prediction-v2-mvp",
            "runId": run_id,
            "strategy": strategy,
            "status": "COMPLETED",
            "turnPredictions": turns,
        })
        receipts.append({
            "scenarioId": oracle["scenarioId"],
            "schemaVersion": "shopping-task-state-runner-receipt-v2-mvp",
            "runId": run_id,
            "strategy": strategy,
            "status": "COMPLETED",
            "usageStatus": "NOT_INSTRUMENTED",
            "turnReceipts": turn_receipts,
            "totals": {
                "modelCalls": None,
                "toolCalls": None,
                "latencyMs": None,
                "inputTokens": None,
                "outputTokens": None,
                "recoveryCount": None,
            },
        })
    return predictions, receipts, turn_lookup


def test_new_schemas_are_draft_202012_meta_valid():
    for path in SCHEMA_PATHS.values():
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)


def test_perfect_semantic_prediction_scores_one_on_exact_metrics():
    predictions, receipts, _ = _perfect_artifacts()
    report = scorer.score_predictions(predictions, receipts)
    assert report["scenarioCount"] == 14
    assert report["turnCount"] == 56
    for metric in (
        "intentAccuracy", "intentMacroF1", "deltaExactRate", "activeStateExactRate",
        "activeRequirementMicroF1", "hardRequirementActiveRecall",
        "negativeRequirementActiveRecall", "informationSufficiencyExactRate",
        "clarificationPrecision", "clarificationRecall", "clarificationF1",
        "routeAccuracy", "memoryEventExactRate", "candidateScopeAccuracy",
        "environmentRevisionAccuracy", "turnExactRate",
    ):
        assert report["metrics"][metric] == 1.0
    assert set(report["metrics"]["deltaOperationExactRate"].values()) == {1.0}
    assert report["resourceUsage"]["usageTrust"] == "UNATTESTED"
    assert report["resourceUsage"]["modelCalls"] is None


def test_scorer_detects_active_state_and_lifecycle_error():
    predictions, receipts, _ = _perfect_artifacts()
    predictions[0]["turnPredictions"][2]["activeRequirements"].pop()
    predictions[1]["turnPredictions"][1]["delta"] = []
    report = scorer.score_predictions(predictions, receipts)
    assert report["metrics"]["activeStateExactRate"] < 1.0
    assert report["metrics"]["deltaExactRate"] < 1.0
    assert report["metrics"]["deltaOperationExactRate"]["revoke"] < 1.0


def test_extra_predicted_lifecycle_operation_is_scored_as_a_false_positive():
    predictions, receipts, _ = _perfect_artifacts()
    turn = predictions[0]["turnPredictions"][0]
    turn["delta"].append({"op": "suppress", "requirement": copy.deepcopy(turn["activeRequirements"][0])})
    report = scorer.score_predictions(predictions, receipts)
    assert report["metrics"]["deltaOperationExactRate"]["suppress"] < 1.0


def test_set_valued_requirements_and_unknown_lists_are_order_invariant():
    predictions, receipts, _ = _perfect_artifacts()
    changed = False
    for scenario in predictions:
        for turn in scenario["turnPredictions"]:
            for requirement in [*turn["activeRequirements"], *(item["requirement"] for item in turn["delta"])]:
                if requirement["operator"] in {"in", "not_in"} and isinstance(requirement["value"], list):
                    requirement["value"].reverse()
                    changed = True
            turn["informationSufficiency"]["blockingUnknowns"].reverse()
            turn["informationSufficiency"]["optionalUnknowns"].reverse()
    assert changed
    assert scorer.score_predictions(predictions, receipts)["metrics"]["turnExactRate"] == 1.0


def test_scorer_rejects_duplicate_semantic_entries():
    predictions, receipts, _ = _perfect_artifacts()
    duplicate = copy.deepcopy(predictions[0]["turnPredictions"][0]["activeRequirements"][0])
    predictions[0]["turnPredictions"][0]["activeRequirements"].append(duplicate)
    with pytest.raises(ValueError, match="duplicate semantic entry"):
        scorer.score_predictions(predictions, receipts)


def test_scorer_rejects_forged_receipt_totals():
    predictions, receipts, _ = _perfect_artifacts()
    receipts[0]["totals"]["toolCalls"] = 999
    with pytest.raises(ValueError):
        scorer.score_predictions(predictions, receipts)


def test_scorer_rejects_uninstrumented_self_reported_cost_and_completed_error():
    predictions, receipts, _ = _perfect_artifacts()
    receipt = receipts[0]
    receipt["usageStatus"] = "NOT_INSTRUMENTED"
    for turn in receipt["turnReceipts"]:
        turn["usageStatus"] = "NOT_INSTRUMENTED"
        turn["modelCalls"] = 1
    receipt["totals"]["modelCalls"] = 1
    with pytest.raises(ValueError):
        scorer.score_predictions(predictions, receipts)

    predictions, receipts, _ = _perfect_artifacts()
    receipts[0]["turnReceipts"][0]["errorCode"] = "strategy_error"
    with pytest.raises(ValueError, match="errorCode"):
        scorer.score_predictions(predictions, receipts)


def test_prediction_contract_rejects_embedded_receipt():
    predictions, _, _ = _perfect_artifacts()
    predictions[0]["receipt"] = {"toolCalls": 0}
    with pytest.raises(ValueError, match="Additional properties"):
        validate_record(predictions[0], "prediction")


def test_public_runner_has_no_scorer_data_dependency():
    source = inspect.getsource(runner).casefold()
    assert "state_oracle" not in source
    assert "oracle_private" not in source
    assert "private_path" not in source
    assert "shopping_task_state_v2_scorer" not in source


def test_public_runner_owns_envelopes_and_receipts():
    _, _, turn_lookup = _perfect_artifacts(strategy="FAST", run_id="ignored")

    def predict(context):
        return runner.StrategyTurnResult(
            prediction=copy.deepcopy(turn_lookup[(context.scenario_id, context.turn_id)]),
        )

    predictions, receipts = runner.run_public_scenarios(
        strategy="FAST", run_id="runner-owned", predict_turn=predict
    )
    assert len(predictions) == len(receipts) == 14
    assert all(record["runId"] == "runner-owned" for record in predictions + receipts)
    assert all(record["status"] == "COMPLETED" for record in predictions + receipts)
    report = scorer.score_predictions(predictions, receipts)
    assert report["metrics"]["turnExactRate"] == 1.0
    assert report["resourceUsage"]["usageTrust"] == "UNATTESTED"
    assert report["resourceUsage"]["modelCalls"] is None


def test_runner_recursively_isolates_returned_prediction_and_prior_history():
    _, _, turn_lookup = _perfect_artifacts(strategy="FAST", run_id="ignored")
    returned = {}

    def predict(context):
        if context.prior_predictions:
            context.prior_predictions[0]["informationSufficiency"]["optionalUnknowns"].append("poison")
            returned[(context.scenario_id, "T1")]["informationSufficiency"]["optionalUnknowns"].append("rewrite")
        value = copy.deepcopy(turn_lookup[(context.scenario_id, context.turn_id)])
        returned[(context.scenario_id, context.turn_id)] = value
        return runner.StrategyTurnResult(prediction=value)

    predictions, receipts = runner.run_public_scenarios(
        strategy="FAST", run_id="immutable-history", predict_turn=predict
    )
    assert all(receipt["usageStatus"] == "NOT_INSTRUMENTED" for receipt in receipts)
    # A scorer-facing artifact is both valid and byte-independent from strategy-owned values.
    for prediction in predictions:
        expected = turn_lookup[(prediction["scenarioId"], "T1")]
        assert prediction["turnPredictions"][0] == expected


def test_public_runner_fails_closed_on_strategy_turn_mismatch():
    def wrong_turn(_context):
        return runner.StrategyTurnResult(prediction={"turnId": "T5"})

    predictions, receipts = runner.run_public_scenarios(
        strategy="FAST", run_id="bad-run", predict_turn=wrong_turn
    )
    assert all(record["status"] == "FAILED" for record in predictions)
    assert all(record["status"] == "FAILED" for record in receipts)
    assert all(record["turnPredictions"] == [] for record in predictions)
    assert all(record["turnReceipts"][0]["errorCode"] == "strategy_error" for record in receipts)
