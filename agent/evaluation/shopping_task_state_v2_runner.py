"""Public-data runner boundary for Shopping Task State V2 strategies."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from time import perf_counter_ns
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from agent.evaluation.shopping_task_state_v2_contract import validate_record
from agent.evaluation.shopping_task_state_v2_public import load_scenarios


PUBLIC_PATH = (
    Path(__file__).resolve().parent
    / "assets"
    / "shopping_task_state_v2_mvp_r2_20260822"
    / "public"
    / "scenarios.jsonl"
)
STRATEGIES = frozenset({"FAST", "PAE", "BOUNDED_REACT"})


@dataclass(frozen=True)
class TurnContext:
    scenario_id: str
    turn_id: str
    session_id: str
    text: str
    prior_user_turns: tuple[str, ...]
    prior_predictions: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class StrategyTurnResult:
    prediction: Mapping[str, Any]


Strategy = Callable[[TurnContext], StrategyTurnResult]


def _plain_json(value: Any) -> Any:
    """Reject subclasses/non-JSON values before a strategy value crosses trust boundary."""
    if value is None or type(value) in {str, int, float, bool}:
        return value
    if type(value) is list:
        return [_plain_json(item) for item in value]
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise TypeError("strategy JSON object keys must be plain strings")
        return {key: _plain_json(item) for key, item in value.items()}
    raise TypeError("strategy output must contain only plain JSON values")


def _canonical_json(value: Any) -> dict[str, Any]:
    plain = _plain_json(value)
    if type(plain) is not dict:
        raise TypeError("strategy prediction must be a plain JSON object")
    return json.loads(json.dumps(plain, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")))


def _readonly(value: Any) -> Any:
    if type(value) is dict:
        return MappingProxyType({key: _readonly(item) for key, item in value.items()})
    if type(value) is list:
        return tuple(_readonly(item) for item in value)
    return value


def _turn_receipt(
    turn_id: str,
    latency_ms: int,
    error: str | None,
) -> dict[str, Any]:
    return {
        "turnId": turn_id,
        "usageStatus": "NOT_INSTRUMENTED",
        "modelCalls": None,
        "toolCalls": None,
        "latencyMs": None,
        "inputTokens": None,
        "outputTokens": None,
        "recoveryCount": None,
        "errorCode": error,
    }


def _totals(receipts: Sequence[Mapping[str, Any]]) -> dict[str, int | None]:
    fields = ("modelCalls", "toolCalls", "latencyMs", "inputTokens", "outputTokens", "recoveryCount")
    totals: dict[str, int | None] = {"latencyMs": None}
    for field in ("modelCalls", "toolCalls", "inputTokens", "outputTokens", "recoveryCount"):
        values = [row[field] for row in receipts]
        totals[field] = sum(int(value) for value in values) if all(value is not None for value in values) else None
    return {field: totals[field] for field in fields}


def run_public_scenarios(
    *,
    strategy: str,
    run_id: str,
    predict_turn: Strategy,
    public_path: Path = PUBLIC_PATH,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run one strategy over public turns; the runner owns envelopes and receipts."""

    if strategy not in STRATEGIES:
        raise ValueError(f"unsupported strategy: {strategy}")
    if not run_id:
        raise ValueError("run_id must be non-empty")

    predictions: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    for scenario in load_scenarios(public_path):
        prior_user_turns: list[str] = []
        turn_predictions: list[Mapping[str, Any]] = []
        turn_receipts: list[dict[str, Any]] = []
        status = "COMPLETED"
        for turn in scenario["turns"]:
            context = TurnContext(
                scenario_id=scenario["scenarioId"],
                turn_id=turn["turnId"],
                session_id=turn["sessionId"],
                text=turn["text"],
                prior_user_turns=tuple(prior_user_turns),
                prior_predictions=tuple(_readonly(item) for item in turn_predictions),
            )
            started = perf_counter_ns()
            try:
                result = predict_turn(context)
                if not isinstance(result, StrategyTurnResult):
                    raise TypeError("strategy must return StrategyTurnResult")
                # A strategy receives and returns caller-owned mutable values;
                # the runner freezes each boundary with a recursive copy so a
                # later turn cannot rewrite a past prediction.
                predicted = _canonical_json(result.prediction)
                if predicted.get("turnId") != turn["turnId"]:
                    raise ValueError("strategy turnId does not match runner turn")
                latency_ms = max(0, (perf_counter_ns() - started) // 1_000_000)
                validate_record({"scenarioId": scenario["scenarioId"], "schemaVersion": "shopping-task-state-prediction-v2-mvp", "runId": run_id, "strategy": strategy, "status": "COMPLETED", "turnPredictions": [predicted]}, "prediction")
                turn_predictions.append(_canonical_json(predicted))
                turn_receipts.append(_turn_receipt(turn["turnId"], latency_ms, None))
            except Exception:
                latency_ms = max(0, (perf_counter_ns() - started) // 1_000_000)
                status = "FAILED"
                turn_receipts.append(_turn_receipt(turn["turnId"], latency_ms, "strategy_error"))
                break
            prior_user_turns.append(turn["text"])

        prediction = {
            "scenarioId": scenario["scenarioId"],
            "schemaVersion": "shopping-task-state-prediction-v2-mvp",
            "runId": run_id,
            "strategy": strategy,
            "status": status,
            "turnPredictions": turn_predictions,
        }
        receipt = {
            "scenarioId": scenario["scenarioId"],
            "schemaVersion": "shopping-task-state-runner-receipt-v2-mvp",
            "runId": run_id,
            "strategy": strategy,
            "status": status,
            "usageStatus": (
                "NOT_INSTRUMENTED"
            ),
            "turnReceipts": turn_receipts,
            "totals": _totals(turn_receipts),
        }
        validate_record(prediction, "prediction")
        validate_record(receipt, "receipt")
        predictions.append(prediction)
        receipts.append(receipt)
    return predictions, receipts
