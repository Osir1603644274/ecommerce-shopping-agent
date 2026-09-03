"""Contracts, loader and deterministic scorer for Shopping Mission Benchmark V1.

This foundation evaluates mission understanding, not product relevance or final
shopping success.  Public conversations and private mission-graph oracles are
physically separated.  Existing ShoppingTaskStateV2 remains the turn-level
state-transition track; this module scores the higher-level task graph.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator


DATASET_ID = "shopping-mission-benchmark-v1-mvp-20260823"
SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"
ASSET_DIR = Path(__file__).resolve().parent / "assets" / "shopping_mission_benchmark_v1_mvp_20260823"
SCHEMA_PATHS = {
    "public": SCHEMA_DIR / "shopping_mission_public_v1.schema.json",
    "oracle": SCHEMA_DIR / "shopping_mission_oracle_private_v1.schema.json",
    "prediction": SCHEMA_DIR / "shopping_mission_prediction_v1.schema.json",
    "report": SCHEMA_DIR / "shopping_mission_score_report_v1.schema.json",
}
FORBIDDEN_PUBLIC_TOKENS = (
    "missionfamily", "routeclass", "nodekey", "oracle", "private",
    "blockingunknowns", "purchasedisposition", "score",
)


class ShoppingMissionBenchmarkError(ValueError):
    """Fail-closed contract or dataset error."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShoppingMissionBenchmarkError(f"cannot read JSON: {path}") from exc


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ShoppingMissionBenchmarkError(f"cannot read JSONL: {path}") from exc
    if not raw or not raw.endswith(b"\n"):
        raise ShoppingMissionBenchmarkError(f"JSONL must be non-empty and newline terminated: {path}")
    rows: list[dict[str, Any]] = []
    try:
        for line in raw.decode("utf-8").splitlines():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ShoppingMissionBenchmarkError("every JSONL row must be an object")
            rows.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ShoppingMissionBenchmarkError(f"invalid UTF-8 JSONL: {path}") from exc
    return rows


def _validator(kind: str) -> Draft202012Validator:
    try:
        schema = _read_json(SCHEMA_PATHS[kind])
    except KeyError as exc:
        raise ShoppingMissionBenchmarkError(f"unknown schema kind: {kind}") from exc
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_record(record: Mapping[str, Any], kind: str) -> None:
    errors = sorted(_validator(kind).iter_errors(record), key=lambda item: list(item.path))
    if errors:
        path = "/".join(str(part) for part in errors[0].path) or "<root>"
        raise ShoppingMissionBenchmarkError(f"invalid {kind} at {path}: {errors[0].message}")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _unique_index(rows: Sequence[Mapping[str, Any]], label: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        scenario_id = str(row["scenarioId"])
        if scenario_id in result:
            raise ShoppingMissionBenchmarkError(f"duplicate {label} scenarioId: {scenario_id}")
        result[scenario_id] = row
    return result


def _assert_unique_semantics(values: Iterable[Mapping[str, Any]], label: str) -> None:
    canonical = [_canonical(value) for value in values]
    if len(canonical) != len(set(canonical)):
        raise ShoppingMissionBenchmarkError(f"duplicate {label}")


def _validate_public(row: Mapping[str, Any]) -> None:
    validate_record(row, "public")
    turns = row["turns"]
    expected_turn_ids = [f"T{index}" for index in range(1, len(turns) + 1)]
    if [turn["turnId"] for turn in turns] != expected_turn_ids:
        raise ShoppingMissionBenchmarkError(f"non-contiguous public turns: {row['scenarioId']}")
    normalized_text = _canonical(row).casefold()
    leaked = [token for token in FORBIDDEN_PUBLIC_TOKENS if token in normalized_text]
    if leaked:
        raise ShoppingMissionBenchmarkError(f"private-contract token in public row: {leaked[0]}")
    if len({turn["text"].strip() for turn in turns}) != len(turns):
        raise ShoppingMissionBenchmarkError(f"duplicate public turn text: {row['scenarioId']}")


def _validate_graph(row: Mapping[str, Any]) -> None:
    validate_record(row, "oracle")
    graph = row["expectedGraph"]
    nodes = {node["nodeKey"]: node for node in graph["nodes"]}
    if len(nodes) != len(graph["nodes"]):
        raise ShoppingMissionBenchmarkError(f"duplicate nodeKey: {row['scenarioId']}")
    for node in nodes.values():
        kind = node["kind"]
        if kind == "QUERY_PRODUCTS" and "category" not in node:
            raise ShoppingMissionBenchmarkError(f"product query lacks category: {row['scenarioId']}")
        if kind != "QUERY_PRODUCTS" and "category" in node:
            raise ShoppingMissionBenchmarkError(f"non-query node carries category: {row['scenarioId']}")
        if kind == "NON_PURCHASE_ACTION" and node["purchaseDisposition"] != "NOT_APPLICABLE":
            raise ShoppingMissionBenchmarkError(f"non-purchase node marked purchasable: {row['scenarioId']}")
    _assert_unique_semantics(graph["constraints"], "mission constraints")
    _assert_unique_semantics(graph["dependencies"], "mission dependencies")
    for constraint in graph["constraints"]:
        scope = constraint["scope"]
        if scope != "GLOBAL" and scope not in nodes:
            raise ShoppingMissionBenchmarkError(f"constraint references unknown node: {row['scenarioId']}")
    outgoing: dict[str, list[str]] = {node_key: [] for node_key in nodes}
    indegree = {node_key: 0 for node_key in nodes}
    for edge in graph["dependencies"]:
        source, target = edge["before"], edge["after"]
        if source not in nodes or target not in nodes or source == target:
            raise ShoppingMissionBenchmarkError(f"invalid dependency endpoint: {row['scenarioId']}")
        outgoing[source].append(target)
        indegree[target] += 1
    queue = [node for node, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        node = queue.pop()
        visited += 1
        for target in outgoing[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if visited != len(nodes):
        raise ShoppingMissionBenchmarkError(f"mission graph contains a cycle: {row['scenarioId']}")
    clarification = graph["clarification"]
    blocking = graph["blockingUnknowns"]
    if clarification["required"] != bool(blocking):
        raise ShoppingMissionBenchmarkError(f"clarification/blocking mismatch: {row['scenarioId']}")
    if clarification["required"] and clarification["maxQuestions"] != 1:
        raise ShoppingMissionBenchmarkError(f"clarification must be minimal: {row['scenarioId']}")
    if not clarification["required"] and (clarification["focus"] is not None or clarification["maxQuestions"] != 0):
        raise ShoppingMissionBenchmarkError(f"spurious clarification metadata: {row['scenarioId']}")


def load_and_validate(base_dir: Path = ASSET_DIR) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    public_path = base_dir / "public" / "scenarios.jsonl"
    oracle_path = base_dir / "private" / "mission_oracle.private.jsonl"
    manifest_path = base_dir / "manifest.json"
    public_rows = _read_jsonl(public_path)
    oracle_rows = _read_jsonl(oracle_path)
    manifest = _read_json(manifest_path)
    for row in public_rows:
        _validate_public(row)
    for row in oracle_rows:
        _validate_graph(row)
    public_index = _unique_index(public_rows, "public")
    oracle_index = _unique_index(oracle_rows, "oracle")
    if set(public_index) != set(oracle_index):
        raise ShoppingMissionBenchmarkError("public/private scenario closure mismatch")
    if manifest.get("datasetId") != DATASET_ID or manifest.get("scenarioCount") != len(public_rows):
        raise ShoppingMissionBenchmarkError("manifest identity/count mismatch")
    expected_files = {
        "public/scenarios.jsonl": _sha256(public_path),
        "private/mission_oracle.private.jsonl": _sha256(oracle_path),
    }
    if manifest.get("files") != expected_files:
        raise ShoppingMissionBenchmarkError("manifest file digest mismatch")
    actual_families = Counter(row["missionFamily"] for row in oracle_rows)
    if manifest.get("missionFamilyCounts") != dict(sorted(actual_families.items())):
        raise ShoppingMissionBenchmarkError("manifest mission-family matrix mismatch")
    if len(public_rows) != 18:
        raise ShoppingMissionBenchmarkError("MVP must contain exactly 18 scenarios")
    return public_rows, oracle_rows, manifest


def _semantic_set(values: Iterable[Mapping[str, Any]]) -> set[str]:
    canonical = [_canonical(value) for value in values]
    if len(canonical) != len(set(canonical)):
        raise ShoppingMissionBenchmarkError("duplicate predicted semantic entry")
    return set(canonical)


def _normalized_node(node: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(node)
    normalized["requiredEvidence"] = sorted(set(node["requiredEvidence"]))
    return normalized


def _normalized_constraint(constraint: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(constraint)
    if constraint["operator"] in {"in", "not_in"} and isinstance(constraint["value"], list):
        normalized["value"] = [
            json.loads(value) for value in sorted({_canonical(value) for value in constraint["value"]})
        ]
    return normalized


def _prf(tp: int, predicted: int, gold: int) -> dict[str, float]:
    precision = tp / predicted if predicted else 0.0
    recall = tp / gold if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def score_predictions(predictions: Sequence[Mapping[str, Any]], base_dir: Path = ASSET_DIR) -> dict[str, Any]:
    public_rows, oracle_rows, _ = load_and_validate(base_dir)
    for prediction in predictions:
        validate_record(prediction, "prediction")
        _validate_graph({
            "scenarioId": prediction["scenarioId"],
            "schemaVersion": "shopping-mission-oracle-private-v1-mvp",
            "missionFamily": prediction["missionFamily"],
            "coverageTags": ["prediction_structure_validation"],
            "expectedGraph": prediction["predictedGraph"],
        })
    prediction_index = _unique_index(predictions, "prediction")
    oracle_index = _unique_index(oracle_rows, "oracle")
    if set(prediction_index) != {row["scenarioId"] for row in public_rows}:
        raise ShoppingMissionBenchmarkError("prediction scenario closure mismatch")

    counts: Counter[str] = Counter()
    node_tp = node_predicted = node_gold = 0
    constraint_tp = constraint_predicted = constraint_gold = hard_tp = hard_gold = 0
    evidence_tp = evidence_predicted = evidence_gold = 0
    unknown_tp = unknown_predicted = unknown_gold = 0
    clarification_tp = clarification_predicted = clarification_gold = 0
    scenario_results: list[dict[str, Any]] = []

    for scenario_id in sorted(prediction_index):
        prediction = prediction_index[scenario_id]
        oracle = oracle_index[scenario_id]
        gold = oracle["expectedGraph"]
        predicted = prediction["predictedGraph"]
        checks = {
            "missionFamily": prediction["missionFamily"] == oracle["missionFamily"],
            "goal": predicted["goalKey"] == gold["goalKey"],
            "route": predicted["routeClass"] == gold["routeClass"],
            "nodes": _semantic_set(_normalized_node(value) for value in predicted["nodes"]) == _semantic_set(_normalized_node(value) for value in gold["nodes"]),
            "dependencies": _semantic_set(predicted["dependencies"]) == _semantic_set(gold["dependencies"]),
            "constraints": _semantic_set(_normalized_constraint(value) for value in predicted["constraints"]) == _semantic_set(_normalized_constraint(value) for value in gold["constraints"]),
            "unknowns": set(predicted["blockingUnknowns"]) == set(gold["blockingUnknowns"]),
            "clarification": _canonical(predicted["clarification"]) == _canonical(gold["clarification"]),
            "outputModes": set(predicted["requiredOutputModes"]) == set(gold["requiredOutputModes"]),
        }
        for name, passed in checks.items():
            counts[f"{name}.total"] += 1
            counts[f"{name}.passed"] += int(passed)

        gold_nodes = _semantic_set(_normalized_node(value) for value in gold["nodes"])
        predicted_nodes = _semantic_set(_normalized_node(value) for value in predicted["nodes"])
        node_tp += len(gold_nodes & predicted_nodes)
        node_predicted += len(predicted_nodes)
        node_gold += len(gold_nodes)

        gold_constraints = _semantic_set(_normalized_constraint(value) for value in gold["constraints"])
        predicted_constraints = _semantic_set(_normalized_constraint(value) for value in predicted["constraints"])
        constraint_tp += len(gold_constraints & predicted_constraints)
        constraint_predicted += len(predicted_constraints)
        constraint_gold += len(gold_constraints)
        gold_hard = _semantic_set(
            _normalized_constraint(value) for value in gold["constraints"] if value["priority"] == "hard"
        )
        hard_tp += len(gold_hard & predicted_constraints)
        hard_gold += len(gold_hard)

        gold_evidence = {(node["nodeKey"], evidence) for node in gold["nodes"] for evidence in node["requiredEvidence"]}
        predicted_evidence = {(node["nodeKey"], evidence) for node in predicted["nodes"] for evidence in node["requiredEvidence"]}
        evidence_tp += len(gold_evidence & predicted_evidence)
        evidence_predicted += len(predicted_evidence)
        evidence_gold += len(gold_evidence)

        gold_unknowns = set(gold["blockingUnknowns"])
        predicted_unknowns = set(predicted["blockingUnknowns"])
        unknown_tp += len(gold_unknowns & predicted_unknowns)
        unknown_predicted += len(predicted_unknowns)
        unknown_gold += len(gold_unknowns)
        gold_clarify = gold["clarification"]["required"]
        predicted_clarify = predicted["clarification"]["required"]
        clarification_tp += int(gold_clarify and predicted_clarify)
        clarification_predicted += int(predicted_clarify)
        clarification_gold += int(gold_clarify)
        scenario_results.append({
            "scenarioId": scenario_id,
            "graphExact": all(checks.values()),
            "passedChecks": sum(checks.values()),
            "totalChecks": len(checks),
        })

    total = len(prediction_index)
    node_prf = _prf(node_tp, node_predicted, node_gold)
    constraint_prf = _prf(constraint_tp, constraint_predicted, constraint_gold)
    evidence_prf = _prf(evidence_tp, evidence_predicted, evidence_gold)
    unknown_prf = _prf(unknown_tp, unknown_predicted, unknown_gold)
    clarification_prf = _prf(clarification_tp, clarification_predicted, clarification_gold)
    metrics = {
        "missionFamilyAccuracy": counts["missionFamily.passed"] / total,
        "routeAccuracy": counts["route.passed"] / total,
        "nodeMicroF1": node_prf["f1"],
        "dependencyExactRate": counts["dependencies.passed"] / total,
        "constraintMicroF1": constraint_prf["f1"],
        "hardConstraintRecall": hard_tp / hard_gold if hard_gold else 0.0,
        "evidenceNeedMicroF1": evidence_prf["f1"],
        "blockingUnknownMicroF1": unknown_prf["f1"],
        "clarificationPrecision": clarification_prf["precision"],
        "clarificationRecall": clarification_prf["recall"],
        "outputModeExactRate": counts["outputModes.passed"] / total,
        "graphExactRate": sum(result["graphExact"] for result in scenario_results) / total,
    }
    report = {
        "schemaVersion": "shopping-mission-score-report-v1-mvp",
        "datasetId": DATASET_ID,
        "scenarioCount": total,
        "metrics": metrics,
        "scenarioResults": scenario_results,
    }
    validate_record(report, "report")
    return report
