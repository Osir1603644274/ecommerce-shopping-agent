"""Prediction-first, node-id-insensitive scorer for baseline comparison V1."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

from agent.evaluation import shopping_mission_benchmark_v1 as benchmark


DATASET_ID = "shopping-mission-benchmark-v1-mvp-20260823"
EVALUATION_DIR = Path(__file__).resolve().parent
PUBLIC_PATH = EVALUATION_DIR / "assets" / "shopping_mission_benchmark_v1_mvp_20260823" / "public" / "scenarios.jsonl"
RUNNER_PATH = EVALUATION_DIR / "shopping_mission_baseline_runner_v1.py"
SCORE_SCHEMA_PATH = EVALUATION_DIR / "schemas" / "shopping_mission_baseline_score_v1.schema.json"
PREDICTION_SCHEMA_PATH = EVALUATION_DIR / "schemas" / "shopping_mission_prediction_v1.schema.json"
PREREGISTRATION_PATH = Path(__file__).resolve().parents[2] / "docs" / "analysis-briefs" / "shopping-mission-baseline-comparison-2026-08-23.md"
EXPECTED_LANGUAGE_EVIDENCE_SHA = "59d564a95b633dc14c6f8e8674be057e03280500ef735101e8c1db7353f0abf0"
EXPECTED_SEMANTIC_EVIDENCE_SHA = "40275bd6b52332e8ad966284cacef8c9d5f78b4fe63aec6ffd69623cccb7f35e"


class BaselineScoreError(ValueError):
    """Fail-closed run authentication or score error."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _read_jsonl_bytes(raw: bytes, label: str) -> list[dict[str, Any]]:
    if not raw or not raw.endswith(b"\n"):
        raise BaselineScoreError(f"invalid {label} JSONL framing")
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BaselineScoreError(f"invalid {label} JSONL") from exc
    if any(type(row) is not dict for row in rows):
        raise BaselineScoreError(f"invalid {label} row type")
    return rows


def _index(rows: Sequence[Mapping[str, Any]], label: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        scenario_id = str(row.get("scenarioId", ""))
        if not scenario_id or scenario_id in result:
            raise BaselineScoreError(f"duplicate or missing {label} scenarioId")
        result[scenario_id] = row
    return result


def authenticate_run(run_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Authenticate all public run artifacts before any private oracle read."""

    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        case_bytes = (run_dir / "cases.jsonl").read_bytes()
        receipt_bytes = (run_dir / "receipts.jsonl").read_bytes()
    except (OSError, json.JSONDecodeError) as exc:
        raise BaselineScoreError("cannot read run artifacts") from exc
    required = {
        "schemaVersion", "datasetId", "runId", "profile", "model", "endpointOrigin",
        "temperature", "startedAt", "completedAt", "scenarioCount", "completedCount",
        "failedCount", "publicSha256", "humanLanguageEvidenceSha256",
        "humanSemanticEvidenceSha256", "runnerSha256", "casesSha256", "receiptsSha256",
        "predictionSchemaSha256", "preregistrationSha256", "modelRepairCalls", "toolCalls",
    }
    if type(manifest) is not dict or set(manifest) != required:
        raise BaselineScoreError("manifest contract mismatch")
    if manifest["schemaVersion"] != "shopping-mission-baseline-run-manifest-v1" or manifest["datasetId"] != DATASET_ID:
        raise BaselineScoreError("manifest identity mismatch")
    if manifest["profile"] not in {"DIRECT_ONE_SHOT", "STATEFUL_CONTEXT"}:
        raise BaselineScoreError("manifest profile mismatch")
    if manifest["endpointOrigin"] != "https://api.deepseek.com" or manifest["temperature"] != 0:
        raise BaselineScoreError("model contract mismatch")
    if manifest["modelRepairCalls"] != 0 or manifest["toolCalls"] != 0:
        raise BaselineScoreError("run used forbidden repair or tools")
    if (
        manifest["humanLanguageEvidenceSha256"] != EXPECTED_LANGUAGE_EVIDENCE_SHA
        or manifest["humanSemanticEvidenceSha256"] != EXPECTED_SEMANTIC_EVIDENCE_SHA
    ):
        raise BaselineScoreError("human review evidence identity mismatch")
    if manifest["casesSha256"] != _sha256_bytes(case_bytes) or manifest["receiptsSha256"] != _sha256_bytes(receipt_bytes):
        raise BaselineScoreError("run artifact digest mismatch")
    if manifest["publicSha256"] != _sha256_path(PUBLIC_PATH) or manifest["runnerSha256"] != _sha256_path(RUNNER_PATH):
        raise BaselineScoreError("public or runner identity drift")
    if (
        manifest["predictionSchemaSha256"] != _sha256_path(PREDICTION_SCHEMA_PATH)
        or manifest["preregistrationSha256"] != _sha256_path(PREREGISTRATION_PATH)
    ):
        raise BaselineScoreError("schema or preregistration identity drift")

    cases = _read_jsonl_bytes(case_bytes, "case")
    receipts = _read_jsonl_bytes(receipt_bytes, "receipt")
    public_rows = _read_jsonl_bytes(PUBLIC_PATH.read_bytes(), "public")
    expected_ids = {row["scenarioId"] for row in public_rows}
    case_index = _index(cases, "case")
    receipt_index = _index(receipts, "receipt")
    if set(case_index) != expected_ids or set(receipt_index) != expected_ids or len(expected_ids) != 18:
        raise BaselineScoreError("run scenario closure mismatch")
    if manifest["scenarioCount"] != 18:
        raise BaselineScoreError("manifest scenario count mismatch")
    completed = failed = 0
    for scenario_id in sorted(expected_ids):
        case = case_index[scenario_id]
        receipt = receipt_index[scenario_id]
        for row in (case, receipt):
            if row.get("runId") != manifest["runId"] or row.get("profile") != manifest["profile"]:
                raise BaselineScoreError("run binding mismatch")
        if case.get("status") != receipt.get("status") or case.get("status") not in {"COMPLETED", "FAILED"}:
            raise BaselineScoreError("case/receipt status mismatch")
        calls = receipt.get("calls")
        if type(calls) is not list or not calls:
            raise BaselineScoreError("receipt calls missing")
        if receipt.get("modelCalls") != len(calls):
            raise BaselineScoreError("model call count mismatch")
        for field in ("inputTokens", "outputTokens", "latencyMs"):
            if receipt.get(field) != sum(int(item[field]) for item in calls):
                raise BaselineScoreError(f"receipt {field} mismatch")
        expected_calls = 1 if manifest["profile"] == "DIRECT_ONE_SHOT" else 2
        if case["status"] == "COMPLETED" and len(calls) != expected_calls:
            raise BaselineScoreError("completed case call count mismatch")
        if case["status"] == "COMPLETED":
            if case.get("errorCode") is not None or type(case.get("prediction")) is not dict:
                raise BaselineScoreError("completed case envelope mismatch")
            benchmark.validate_record(case["prediction"], "prediction")
            if case["prediction"]["scenarioId"] != scenario_id:
                raise BaselineScoreError("prediction scenario identity mismatch")
            completed += 1
        else:
            if case.get("prediction") is not None or not isinstance(case.get("errorCode"), str):
                raise BaselineScoreError("failed case envelope mismatch")
            failed += 1
    if (completed, failed) != (manifest["completedCount"], manifest["failedCount"]):
        raise BaselineScoreError("manifest completion counts mismatch")
    return manifest, cases, receipts


def _node_signature(node: Mapping[str, Any]) -> str:
    return _canonical({
        "kind": node["kind"], "category": node.get("category"),
        "purchaseDisposition": node["purchaseDisposition"],
    })


def _unique_node_map(nodes: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], set[str]]:
    by_signature: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        by_signature[_node_signature(node)].append(node["nodeKey"])
    mapping = {keys[0]: signature for signature, keys in by_signature.items() if len(keys) == 1}
    return mapping, set(by_signature)


def _semantic_set(values: Iterable[Any]) -> set[str]:
    canonical = [_canonical(value) for value in values]
    if len(canonical) != len(set(canonical)):
        raise BaselineScoreError("duplicate semantic entry")
    return set(canonical)


def _mapped_dependencies(graph: Mapping[str, Any], node_map: Mapping[str, str]) -> set[str]:
    values = []
    for edge in graph["dependencies"]:
        before = node_map.get(edge["before"])
        after = node_map.get(edge["after"])
        if before is not None and after is not None:
            values.append({"before": before, "after": after, "reason": edge["reason"]})
    return _semantic_set(values)


def _normalized_value(value: Any, operator: str) -> Any:
    if operator in {"in", "not_in"} and isinstance(value, list):
        return [json.loads(item) for item in sorted({_canonical(item) for item in value})]
    return value


def _mapped_constraints(graph: Mapping[str, Any], node_map: Mapping[str, str]) -> set[str]:
    values = []
    for constraint in graph["constraints"]:
        scope = constraint["scope"]
        if scope != "GLOBAL":
            scope = node_map.get(scope)
            if scope is None:
                continue
        values.append({
            "scope": scope, "key": constraint["key"], "operator": constraint["operator"],
            "value": _normalized_value(constraint["value"], constraint["operator"]),
            "priority": constraint["priority"], "polarity": constraint["polarity"],
        })
    return _semantic_set(values)


def _evidence_set(graph: Mapping[str, Any], node_map: Mapping[str, str]) -> set[tuple[str, str]]:
    result: set[tuple[str, str]] = set()
    for node in graph["nodes"]:
        signature = node_map.get(node["nodeKey"])
        if signature is not None:
            result.update((signature, evidence) for evidence in node["requiredEvidence"])
    return result


UNKNOWN_ALIASES = {
    "exact_visit_date": "exact_date", "exact_travel_dates": "exact_date", "visit_date": "exact_date",
    "travel_dates": "exact_date", "residence_city": "location", "city": "location",
    "current_lighting_condition": "current_condition", "lighting_condition": "current_condition",
    "monthly_folder_notebook_usage": "usage_rate", "monthly_usage": "usage_rate",
}


def _unknowns(values: Iterable[str]) -> set[str]:
    return {UNKNOWN_ALIASES.get(value, value) for value in values}


def _clarification(value: Mapping[str, Any]) -> dict[str, Any]:
    focus = value["focus"]
    if focus is not None:
        focus = UNKNOWN_ALIASES.get(focus, focus)
    return {"required": value["required"], "focus": focus, "maxQuestions": value["maxQuestions"]}


def _prf(tp: int, predicted: int, gold: int) -> dict[str, float]:
    precision = tp / predicted if predicted else 0.0
    recall = tp / gold if gold else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": precision, "recall": recall, "f1": f1}


def score_run(run_dir: Path) -> dict[str, Any]:
    manifest, cases, receipts = authenticate_run(run_dir)
    # Private oracle access begins only after complete prediction authentication.
    _, oracles, _ = benchmark.load_and_validate()
    oracle_index = _index(oracles, "oracle")
    case_index = _index(cases, "case")
    receipt_index = _index(receipts, "receipt")

    totals: Counter[str] = Counter()
    node_tp = node_pred = node_gold = 0
    dep_exact = constraint_tp = constraint_pred = constraint_gold = 0
    hard_tp = hard_gold = evidence_tp = evidence_pred = evidence_gold = 0
    unknown_tp = unknown_pred = unknown_gold = 0
    clarify_tp = clarify_pred = clarify_gold = 0
    family_total: Counter[str] = Counter()
    family_exact: Counter[str] = Counter()
    scenario_results: list[dict[str, Any]] = []

    for scenario_id in sorted(oracle_index):
        oracle = oracle_index[scenario_id]
        gold = oracle["expectedGraph"]
        case = case_index[scenario_id]
        family = oracle["missionFamily"]
        family_total[family] += 1
        gold_map, gold_nodes = _unique_node_map(gold["nodes"])
        gold_deps = _mapped_dependencies(gold, gold_map)
        gold_constraints = _mapped_constraints(gold, gold_map)
        gold_hard_constraints = _mapped_constraints(
            {**gold, "constraints": [item for item in gold["constraints"] if item["priority"] == "hard"]}, gold_map
        )
        gold_evidence = _evidence_set(gold, gold_map)
        gold_unknowns = _unknowns(gold["blockingUnknowns"])
        gold_clarify = gold["clarification"]["required"]

        if case["status"] == "COMPLETED":
            prediction = case["prediction"]
            predicted = prediction["predictedGraph"]
            pred_map, pred_nodes = _unique_node_map(predicted["nodes"])
            pred_deps = _mapped_dependencies(predicted, pred_map)
            pred_constraints = _mapped_constraints(predicted, pred_map)
            pred_evidence = _evidence_set(predicted, pred_map)
            pred_unknowns = _unknowns(predicted["blockingUnknowns"])
            pred_clarify = predicted["clarification"]["required"]
            checks = {
                "missionFamily": prediction["missionFamily"] == family,
                "route": predicted["routeClass"] == gold["routeClass"],
                "nodes": pred_nodes == gold_nodes,
                "dependencies": pred_deps == gold_deps,
                "constraints": pred_constraints == gold_constraints,
                "evidence": pred_evidence == gold_evidence,
                "unknowns": pred_unknowns == gold_unknowns,
                "clarification": _clarification(predicted["clarification"]) == _clarification(gold["clarification"]),
                "outputModes": set(predicted["requiredOutputModes"]) == set(gold["requiredOutputModes"]),
            }
        else:
            pred_nodes = pred_deps = pred_constraints = pred_evidence = pred_unknowns = set()
            pred_clarify = False
            checks = {name: False for name in (
                "missionFamily", "route", "nodes", "dependencies", "constraints", "evidence",
                "unknowns", "clarification", "outputModes",
            )}

        node_tp += len(pred_nodes & gold_nodes)
        node_pred += len(pred_nodes)
        node_gold += len(gold_nodes)
        dep_exact += int(checks["dependencies"])
        constraint_tp += len(pred_constraints & gold_constraints)
        constraint_pred += len(pred_constraints)
        constraint_gold += len(gold_constraints)
        hard_tp += len(pred_constraints & gold_hard_constraints)
        hard_gold += len(gold_hard_constraints)
        evidence_tp += len(pred_evidence & gold_evidence)
        evidence_pred += len(pred_evidence)
        evidence_gold += len(gold_evidence)
        unknown_tp += len(pred_unknowns & gold_unknowns)
        unknown_pred += len(pred_unknowns)
        unknown_gold += len(gold_unknowns)
        clarify_tp += int(pred_clarify and gold_clarify)
        clarify_pred += int(pred_clarify)
        clarify_gold += int(gold_clarify)
        for name, passed in checks.items():
            totals[f"{name}.passed"] += int(passed)
        graph_exact = all(checks.values())
        family_exact[family] += int(graph_exact)
        scenario_results.append({
            "scenarioId": scenario_id, "missionFamily": family, "status": case["status"],
            "graphExact": graph_exact, "passedChecks": sum(checks.values()), "totalChecks": 9,
        })

    count = len(oracles)
    node_prf = _prf(node_tp, node_pred, node_gold)
    constraint_prf = _prf(constraint_tp, constraint_pred, constraint_gold)
    evidence_prf = _prf(evidence_tp, evidence_pred, evidence_gold)
    unknown_prf = _prf(unknown_tp, unknown_pred, unknown_gold)
    clarification_prf = _prf(clarify_tp, clarify_pred, clarify_gold)
    completed_count = manifest["completedCount"]
    metrics = {
        "caseCompletionRate": completed_count / count,
        "missionFamilyAccuracy": totals["missionFamily.passed"] / count,
        "routeAccuracy": totals["route.passed"] / count,
        "nodeMicroF1": node_prf["f1"],
        "dependencyExactRate": dep_exact / count,
        "constraintMicroF1": constraint_prf["f1"],
        "hardConstraintRecall": hard_tp / hard_gold if hard_gold else 0.0,
        "evidenceNeedMicroF1": evidence_prf["f1"],
        "blockingUnknownMicroF1": unknown_prf["f1"],
        "clarificationPrecision": clarification_prf["precision"],
        "clarificationRecall": clarification_prf["recall"],
        "outputModeExactRate": totals["outputModes.passed"] / count,
        "graphExactRate": sum(item["graphExact"] for item in scenario_results) / count,
        "caseFailureRate": manifest["failedCount"] / count,
    }
    model_calls = sum(receipt_index[sid]["modelCalls"] for sid in receipt_index)
    input_tokens = sum(receipt_index[sid]["inputTokens"] for sid in receipt_index)
    output_tokens = sum(receipt_index[sid]["outputTokens"] for sid in receipt_index)
    latency_ms = sum(receipt_index[sid]["latencyMs"] for sid in receipt_index)
    resources = {
        "usageTrust": "API_REPORTED", "modelCalls": model_calls,
        "inputTokens": input_tokens, "outputTokens": output_tokens, "latencyMs": latency_ms,
        "averageModelCallsPerScenario": model_calls / count,
        "averageTokensPerScenario": (input_tokens + output_tokens) / count,
        "averageLatencyMsPerScenario": latency_ms / count,
    }
    report = {
        "schemaVersion": "shopping-mission-baseline-score-v1", "datasetId": DATASET_ID,
        "runId": manifest["runId"], "profile": manifest["profile"], "scenarioCount": count,
        "completedCount": completed_count, "failedCount": manifest["failedCount"],
        "metrics": metrics, "resources": resources,
        "familyGraphExact": {family: family_exact[family] / family_total[family] for family in sorted(family_total)},
        "scenarioResults": scenario_results,
    }
    schema = json.loads(SCORE_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(report), key=lambda item: list(item.path))
    if errors:
        raise BaselineScoreError(f"score report schema mismatch: {errors[0].message}")
    return report


def write_score_new(run_dir: Path, report: Mapping[str, Any]) -> Path:
    path = run_dir / "score.json"
    if path.exists():
        raise BaselineScoreError("score output already exists")
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    return path
