"""Deterministic scorer for Shopping Task State V2 semantic transitions."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping, Sequence

from agent.evaluation import shopping_task_state_v2_mvp_r2 as dataset
from agent.evaluation.shopping_task_state_v2_contract import validate_record


ATOM_FIELDS = (
    "key", "operator", "value", "priority", "polarity", "sourceType", "memoryScope",
)
DELTA_OPS = ("add", "retain", "override", "revoke", "suppress")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atom(requirement: Mapping[str, Any]) -> dict[str, Any]:
    atom = {field: requirement[field] for field in ATOM_FIELDS}
    if atom["operator"] in {"in", "not_in"}:
        values = atom["value"]
        if not isinstance(values, list):
            raise ValueError("in/not_in requirement values must be lists")
        atom["value"] = [json.loads(item) for item in sorted({_canonical(item) for item in values})]
    return atom


def _oracle_delta(
    delta: Mapping[str, Any], definitions: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    projected: dict[str, Any] = {
        "op": delta["op"],
        "requirement": _atom(definitions[delta["requirementId"]]),
    }
    replaced = delta.get("replacesRequirementId")
    if replaced is not None:
        projected["replaces"] = _atom(definitions[replaced])
    return projected


def _oracle_memory_event(event: Mapping[str, Any]) -> dict[str, Any]:
    projected = {field: event[field] for field in ("op", "memoryKey", "scope")}
    if "value" in event:
        projected["value"] = event["value"]
    return projected


def _normalized_delta(delta: Mapping[str, Any]) -> dict[str, Any]:
    normalized = {"op": delta["op"], "requirement": _atom(delta["requirement"])}
    if "replaces" in delta:
        normalized["replaces"] = _atom(delta["replaces"])
    return normalized


def _normalized_information_sufficiency(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": value["status"],
        "blockingUnknowns": sorted(set(value["blockingUnknowns"])),
        "optionalUnknowns": sorted(set(value["optionalUnknowns"])),
    }


def _set(values: Iterable[Mapping[str, Any]]) -> set[str]:
    canonical = [_canonical(value) for value in values]
    if len(canonical) != len(set(canonical)):
        raise ValueError("duplicate semantic entry")
    return set(canonical)


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _prf(true_positive: int, predicted_positive: int, gold_positive: int) -> dict[str, float]:
    precision = _safe_ratio(true_positive, predicted_positive)
    recall = _safe_ratio(true_positive, gold_positive)
    f1 = _safe_ratio(2 * precision * recall, precision + recall)
    return {"precision": precision, "recall": recall, "f1": f1}


def _macro_f1(gold: Sequence[str], predicted: Sequence[str]) -> float:
    labels = sorted(set(gold) | set(predicted))
    scores: list[float] = []
    for label in labels:
        tp = sum(g == label and p == label for g, p in zip(gold, predicted))
        predicted_positive = sum(p == label for p in predicted)
        gold_positive = sum(g == label for g in gold)
        scores.append(_prf(tp, predicted_positive, gold_positive)["f1"])
    return sum(scores) / len(scores) if scores else 0.0


def _index_unique(records: Sequence[Mapping[str, Any]], label: str) -> dict[str, Mapping[str, Any]]:
    indexed: dict[str, Mapping[str, Any]] = {}
    for record in records:
        scenario_id = record["scenarioId"]
        if scenario_id in indexed:
            raise ValueError(f"duplicate {label} scenarioId: {scenario_id}")
        indexed[scenario_id] = record
    return indexed


def _validate_receipt_totals(receipt: Mapping[str, Any]) -> None:
    counters = ("modelCalls", "toolCalls", "inputTokens", "outputTokens", "recoveryCount")
    status = receipt["usageStatus"]
    if receipt["status"] == "COMPLETED" and any(turn["errorCode"] is not None for turn in receipt["turnReceipts"]):
        raise ValueError(f"completed receipt contains an errorCode: {receipt['scenarioId']}")
    if any(turn["usageStatus"] != status for turn in receipt["turnReceipts"]):
        raise ValueError(f"receipt usage instrumentation mismatch: {receipt['scenarioId']}")
    if status == "NOT_INSTRUMENTED":
        if any(turn[field] is not None for turn in receipt["turnReceipts"] for field in counters):
            raise ValueError(f"uninstrumented receipt cannot claim strategy costs: {receipt['scenarioId']}")
        recomputed: dict[str, int | None] = {field: None for field in counters}
    else:
        if any(turn[field] is None for turn in receipt["turnReceipts"] for field in counters):
            raise ValueError(f"instrumented receipt requires complete runner counters: {receipt['scenarioId']}")
        recomputed = {
            field: sum(int(turn[field]) for turn in receipt["turnReceipts"])
            for field in counters
        }
    if any(turn["latencyMs"] is not None for turn in receipt["turnReceipts"]):
        raise ValueError(f"unattested receipt cannot claim latency: {receipt['scenarioId']}")
    recomputed["latencyMs"] = None
    if recomputed != receipt["totals"]:
        raise ValueError(f"receipt totals mismatch: {receipt['scenarioId']}")


def score_predictions(
    predictions: Sequence[Mapping[str, Any]],
    receipts: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Score complete, aligned strategy outputs without exposing scorer inputs."""

    public_rows, oracle_rows = dataset.load_and_validate()
    for prediction in predictions:
        validate_record(prediction, "prediction")
    for receipt in receipts:
        validate_record(receipt, "receipt")
        _validate_receipt_totals(receipt)

    prediction_by_id = _index_unique(predictions, "prediction")
    receipt_by_id = _index_unique(receipts, "receipt")
    oracle_by_id = _index_unique(oracle_rows, "oracle")
    expected_ids = {row["scenarioId"] for row in public_rows}
    if set(prediction_by_id) != expected_ids or set(receipt_by_id) != expected_ids:
        raise ValueError("prediction/receipt scenario closure mismatch")

    run_ids = {record["runId"] for record in predictions} | {record["runId"] for record in receipts}
    strategies = {record["strategy"] for record in predictions} | {record["strategy"] for record in receipts}
    if len(run_ids) != 1 or len(strategies) != 1:
        raise ValueError("one score report must bind exactly one runId and strategy")
    if any(record["status"] != "COMPLETED" for record in predictions):
        raise ValueError("only complete predictions are scoreable")
    if any(record["status"] != "COMPLETED" for record in receipts):
        raise ValueError("only complete receipts are scoreable")

    check_counts: Counter[str] = Counter()
    intent_gold: list[str] = []
    intent_predicted: list[str] = []
    lifecycle_turns: Counter[str] = Counter()
    lifecycle_exact: Counter[str] = Counter()
    active_tp = active_predicted = active_gold = 0
    hard_tp = hard_gold = negative_tp = negative_gold = 0
    clarification_tp = clarification_predicted = clarification_gold = 0
    clarification_questions = 0
    scenario_results: list[dict[str, Any]] = []

    for scenario_id in sorted(expected_ids):
        prediction = prediction_by_id[scenario_id]
        receipt = receipt_by_id[scenario_id]
        oracle = oracle_by_id[scenario_id]
        if prediction["runId"] != receipt["runId"] or prediction["strategy"] != receipt["strategy"]:
            raise ValueError(f"prediction/receipt run binding mismatch: {scenario_id}")

        predicted_turns = {turn["turnId"]: turn for turn in prediction["turnPredictions"]}
        receipt_turns = {turn["turnId"]: turn for turn in receipt["turnReceipts"]}
        oracle_turns = {turn["turnId"]: turn for turn in oracle["turnAnnotations"]}
        if len(predicted_turns) != len(prediction["turnPredictions"]):
            raise ValueError(f"duplicate prediction turnId: {scenario_id}")
        if len(receipt_turns) != len(receipt["turnReceipts"]):
            raise ValueError(f"duplicate receipt turnId: {scenario_id}")
        if set(predicted_turns) != set(oracle_turns) or set(receipt_turns) != set(oracle_turns):
            raise ValueError(f"turn closure mismatch: {scenario_id}")

        definitions = {row["requirementId"]: row for row in oracle["requirementDefinitions"]}
        scenario_exact = 0
        for turn_id in sorted(oracle_turns, key=lambda value: int(value[1:])):
            gold = oracle_turns[turn_id]
            predicted = predicted_turns[turn_id]
            intent_gold.append(gold["intent"])
            intent_predicted.append(predicted["intent"])

            gold_delta = [_oracle_delta(item, definitions) for item in gold["delta"]]
            predicted_delta = [_normalized_delta(item) for item in predicted["delta"]]
            gold_active = [_atom(definitions[item]) for item in gold["activeRequirementIds"]]
            predicted_active = [_atom(item) for item in predicted["activeRequirements"]]
            gold_memory = [_oracle_memory_event(item) for item in gold["memoryEvents"]]
            predicted_memory = predicted["memoryEvents"]

            checks = {
                "intent": predicted["intent"] == gold["intent"],
                "delta": _set(predicted_delta) == _set(gold_delta),
                "activeState": _set(predicted_active) == _set(gold_active),
                "informationSufficiency": _canonical(_normalized_information_sufficiency(predicted["informationSufficiency"])) == _canonical(_normalized_information_sufficiency(gold["informationSufficiency"])),
                "route": predicted["routeClass"] == gold["routeClass"],
                "clarification": _canonical(predicted["clarification"]) == _canonical(gold["clarification"]),
                "memoryEvents": _set(predicted_memory) == _set(gold_memory),
                "candidateScope": predicted["candidateScope"] == gold["candidateScope"],
                "environmentRevision": predicted["environmentRevision"] == gold["environmentRevision"],
            }
            for name, passed in checks.items():
                check_counts[f"{name}.total"] += 1
                check_counts[f"{name}.passed"] += int(passed)
            if all(checks.values()):
                scenario_exact += 1

            gold_active_set = _set(gold_active)
            predicted_active_set = _set(predicted_active)
            active_tp += len(gold_active_set & predicted_active_set)
            active_predicted += len(predicted_active_set)
            active_gold += len(gold_active_set)
            gold_hard = _set(item for item in gold_active if item["priority"] == "hard")
            gold_negative = _set(item for item in gold_active if item["polarity"] == "negative")
            hard_tp += len(gold_hard & predicted_active_set)
            hard_gold += len(gold_hard)
            negative_tp += len(gold_negative & predicted_active_set)
            negative_gold += len(gold_negative)

            for op in DELTA_OPS:
                gold_for_op = [item for item in gold_delta if item["op"] == op]
                predicted_for_op = [item for item in predicted_delta if item["op"] == op]
                if gold_for_op or predicted_for_op:
                    lifecycle_turns[op] += 1
                    lifecycle_exact[op] += int(_set(gold_for_op) == _set(predicted_for_op))

            gold_clarify = gold["clarification"]["required"]
            predicted_clarify = predicted["clarification"]["required"]
            clarification_tp += int(gold_clarify and predicted_clarify)
            clarification_predicted += int(predicted_clarify)
            clarification_gold += int(gold_clarify)
            clarification_questions += predicted["clarification"]["maxQuestions"]

        scenario_results.append({
            "scenarioId": scenario_id,
            "turnCount": len(oracle_turns),
            "exactTurnCount": scenario_exact,
            "turnExactRate": scenario_exact / len(oracle_turns),
        })

    turn_count = len(intent_gold)
    clarification_prf = _prf(
        clarification_tp, clarification_predicted, clarification_gold
    )
    active_prf = _prf(active_tp, active_predicted, active_gold)
    metrics = {
        "intentAccuracy": _safe_ratio(check_counts["intent.passed"], turn_count),
        "intentMacroF1": _macro_f1(intent_gold, intent_predicted),
        "deltaExactRate": _safe_ratio(check_counts["delta.passed"], turn_count),
        "activeStateExactRate": _safe_ratio(check_counts["activeState.passed"], turn_count),
        "activeRequirementMicroPrecision": active_prf["precision"],
        "activeRequirementMicroRecall": active_prf["recall"],
        "activeRequirementMicroF1": active_prf["f1"],
        "hardRequirementActiveRecall": _safe_ratio(hard_tp, hard_gold),
        "negativeRequirementActiveRecall": _safe_ratio(negative_tp, negative_gold),
        "informationSufficiencyExactRate": _safe_ratio(check_counts["informationSufficiency.passed"], turn_count),
        "clarificationPrecision": clarification_prf["precision"],
        "clarificationRecall": clarification_prf["recall"],
        "clarificationF1": clarification_prf["f1"],
        "averageClarificationQuestionsPerTurn": _safe_ratio(clarification_questions, turn_count),
        "routeAccuracy": _safe_ratio(check_counts["route.passed"], turn_count),
        "memoryEventExactRate": _safe_ratio(check_counts["memoryEvents.passed"], turn_count),
        "candidateScopeAccuracy": _safe_ratio(check_counts["candidateScope.passed"], turn_count),
        "environmentRevisionAccuracy": _safe_ratio(check_counts["environmentRevision.passed"], turn_count),
        "turnExactRate": _safe_ratio(sum(row["exactTurnCount"] for row in scenario_results), turn_count),
        "deltaOperationExactRate": {
            op: _safe_ratio(lifecycle_exact[op], lifecycle_turns[op])
            for op in DELTA_OPS
        },
    }

    usage_fields = ("modelCalls", "toolCalls", "inputTokens", "outputTokens", "recoveryCount")
    if any(receipt["usageStatus"] != "NOT_INSTRUMENTED" for receipt in receipts):
        raise ValueError("foundation receipts cannot claim attested strategy costs")
    resource_usage: dict[str, int | str | None] = {
        "usageTrust": "UNATTESTED", "usageStatus": "NOT_INSTRUMENTED", "latencyMs": None,
    }
    for field in usage_fields:
        resource_usage[field] = None
    report = {
        "schemaVersion": "shopping-task-state-score-report-v2-mvp",
        "datasetId": "shopping-task-state-v2-mvp-r2-20260822",
        "runId": next(iter(run_ids)),
        "strategy": next(iter(strategies)),
        "scenarioCount": len(expected_ids),
        "turnCount": turn_count,
        "metrics": metrics,
        "resourceUsage": resource_usage,
        "scenarioResults": scenario_results,
    }
    validate_record(report, "report")
    return report
