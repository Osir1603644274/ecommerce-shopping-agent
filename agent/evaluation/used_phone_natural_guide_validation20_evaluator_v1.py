"""Prediction-first evaluator for held-out natural-guide Validation20."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator

from . import used_phone_natural_guide_dev15_evaluator_v1 as _shared


SCHEMA_VERSION = "used-phone-natural-guide-validation20-score-v1"
PROTOCOL_VERSION = "used-phone-natural-guide-validation20-production-agent-v1"
PUBLIC_CASES_SHA256 = "0462b2addc1d0c7f69461bab5d0075fce855bec57572cb184d217ff62841437d"
PUBLIC_CATALOG_SHA256 = "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50"
PREREGISTRATION_SHA256 = "9ad8a9131cb70d6d2acd86cceaf528bb7d505dbb27783e83b15e134306b3b9b6"
PRODUCTION_CODE_SCOPE_SHA256 = "a1ddc4bb58198a62dc87cf191edada842bfbcd1c973822b2a05b418f541d9381"
CASE_IDS = tuple(f"UPNG-V{index:02d}" for index in range(1, 21))
AUTHORIZED_RUN_DIRECTORY = "used-phone-natural-guide-validation20-e2e-20260814-attempt-001"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "used_phone_natural_guide_validation20_prediction_v1.schema.json"
RUNNER_HASHES = {
    "agent/evaluation/used_phone_public_agent_runner_v1.py": "7c552a5b2f7dc0b42d61a7d37ce35688ce6b3be88f2517660885e383be9e7c09",
    "agent/evaluation/used_phone_natural_guide_validation20_runner_v1.py": "05e681b5debce881856e5cf515e525f8ffb3fc2c6b18f24861d3aa6801985afb",
    "agent/evaluation/schemas/used_phone_natural_guide_validation20_prediction_v1.schema.json": "9949a936728cca75704a7e72e2e6efaeefb442ca69932e3b77d0825297eaf376",
    "agent/scripts/run_used_phone_natural_guide_validation20_v1.py": "93a473411f0de63f228961c7748a23830b5dcb771d545827f8e0e693c4ec74f2",
}

canonical_bytes = _shared.canonical_bytes
sha256_file = _shared.sha256_file


def _load_canonical_json(path: Path, name: str) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    value = json.loads(payload)
    if not isinstance(value, dict) or payload != canonical_bytes(value):
        raise ValueError(f"{name} must be canonical JSON")
    return value, hashlib.sha256(payload).hexdigest()


def _load_predictions(path: Path) -> tuple[list[dict[str, Any]], str]:
    payload = path.read_bytes()
    rows = [json.loads(line) for line in payload.splitlines()]
    if payload != b"".join(canonical_bytes(row) for row in rows):
        raise ValueError("predictions must be canonical JSONL")
    return rows, hashlib.sha256(payload).hexdigest()


def _load_catalog(path: Path) -> dict[str, dict[str, Any]]:
    if path.name != "catalog.jsonl" or sha256_file(path) != PUBLIC_CATALOG_SHA256:
        raise ValueError("public catalog identity mismatch")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    by_id = {str(row.get("itemId")): row for row in rows}
    if len(rows) != 252 or len(by_id) != 252:
        raise ValueError("public catalog cardinality mismatch")
    return by_id


def _authenticate_prediction_before_expectations(
    *, predictions_path: Path, manifest_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]], dict[str, str]]:
    predictions_file, manifest_file = predictions_path.resolve(), manifest_path.resolve()
    run_dir = manifest_file.parent
    if (
        predictions_file.name != "predictions.jsonl"
        or manifest_file.name != "manifest.json"
        or predictions_file.parent != run_dir
        or run_dir.name != AUTHORIZED_RUN_DIRECTORY
    ):
        raise ValueError("prediction/manifest must come from the authorized run directory")
    predictions, prediction_sha = _load_predictions(predictions_file)
    manifest, manifest_sha = _load_canonical_json(manifest_file, "manifest")
    wrapper, contract_file_sha = _load_canonical_json(run_dir / "run_contract.json", "run contract")
    contract = wrapper.get("contract")
    if not isinstance(contract, dict):
        raise ValueError("run contract payload is missing")
    contract_sha = hashlib.sha256(canonical_bytes(contract)).hexdigest()
    if (
        wrapper.get("schemaVersion") != "used-phone-public-agent-run-contract-v1"
        or wrapper.get("contractSha256") != contract_sha
        or manifest.get("contractSha256") != contract_sha
        or manifest.get("contract") != contract
    ):
        raise ValueError("run contract/manifest binding mismatch")
    if {
        "protocolVersion": contract.get("protocolVersion"),
        "publicCasesSha256": contract.get("publicCasesSha256"),
        "publicCatalogSha256": contract.get("publicCatalogSha256"),
        "productionCodeScopeSha256": contract.get("productionCodeScopeSha256"),
        "runnerCodeSha256": contract.get("runnerCodeSha256"),
        "attributeContractCodeSha256": contract.get("attributeContractCodeSha256"),
        "attributeRuleset": contract.get("attributeRuleset"),
        "javaCatalog": contract.get("javaCatalog"),
        "modelEndpoint": contract.get("modelEndpoint"),
        "productionRuntimeConfig": contract.get("productionRuntimeConfig"),
        "selectedCaseIds": contract.get("selectedCaseIds"),
        "selectedSplits": contract.get("selectedSplits"),
        "allowSealedTest": contract.get("allowSealedTest"),
        "caseCount": contract.get("caseCount"),
    } != {
        "protocolVersion": PROTOCOL_VERSION,
        "publicCasesSha256": PUBLIC_CASES_SHA256,
        "publicCatalogSha256": PUBLIC_CATALOG_SHA256,
        "productionCodeScopeSha256": PRODUCTION_CODE_SCOPE_SHA256,
        "runnerCodeSha256": RUNNER_HASHES,
        "attributeContractCodeSha256": "545c4f7d528ed9875dd468d9d9bbf5e4e6034b6cdbcf0860800bfc2b4b18688a",
        "attributeRuleset": "used-phone-exact-token-seven-field-v2",
        "javaCatalog": _shared.EXPECTED_JAVA_CATALOG,
        "modelEndpoint": "https://api.deepseek.com",
        "productionRuntimeConfig": _shared.EXPECTED_RUNTIME_CONFIG,
        "selectedCaseIds": list(CASE_IDS),
        "selectedSplits": ["validation"],
        "allowSealedTest": False,
        "caseCount": 20,
    }:
        raise ValueError("run identity mismatch")
    if (
        manifest.get("selectedCaseIds") != list(CASE_IDS)
        or manifest.get("selectedSplits") != ["validation"]
        or manifest.get("prediction") != {"path":"predictions.jsonl","rowCount":20,"sha256":prediction_sha}
        or [row.get("caseId") for row in predictions] != list(CASE_IDS)
        or manifest.get("hiddenArtifactsRead") is not False
        or manifest.get("businessWriteNetworkUsed") is not False
    ):
        raise ValueError("manifest public/no-write identity mismatch")
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    execution = manifest.get("execution")
    if not isinstance(execution, dict) or list(execution) != list(CASE_IDS):
        raise ValueError("manifest execution identity mismatch")
    terminals: dict[str, dict[str, Any]] = {}
    for case_id, prediction in zip(CASE_IDS, predictions, strict=True):
        validator.validate(prediction)
        attempt_dir = run_dir / "cases" / case_id / "attempt-001"
        if not attempt_dir.is_dir() or list((run_dir / "cases" / case_id).glob("attempt-*")) != [attempt_dir]:
            raise ValueError(f"unexpected attempt layout for {case_id}")
        terminal_name = "success.json" if execution[case_id] is True else "failure.json"
        terminal, _ = _load_canonical_json(attempt_dir / terminal_name, f"{case_id} terminal")
        if (
            terminal.get("caseId") != case_id or terminal.get("attempt") != 1
            or terminal.get("protocolVersion") != PROTOCOL_VERSION
            or terminal.get("contractSha256") != contract_sha
            or terminal.get("businessWriteNetworkUsed") is not False
        ):
            raise ValueError(f"terminal provenance mismatch for {case_id}")
        if execution[case_id] is True:
            case_prediction, case_sha = _load_canonical_json(attempt_dir / "prediction.json", f"{case_id} prediction")
            trace, trace_sha = _load_canonical_json(attempt_dir / "trace.json", f"{case_id} trace")
            if (
                terminal.get("terminalState") != "success" or case_prediction != prediction
                or terminal.get("prediction") != prediction or terminal.get("predictionSha256") != case_sha
                or terminal.get("trace") != trace or terminal.get("traceSha256") != trace_sha
                or trace.get("caseId") != case_id or trace.get("protocolVersion") != PROTOCOL_VERSION
                or trace.get("businessWriteNetworkUsed") is not False
            ):
                raise ValueError(f"success bytes/provenance mismatch for {case_id}")
            terminals[case_id] = {**terminal, "trace": trace}
        else:
            if terminal.get("terminalState") != "failure" or prediction != {"caseId":case_id,"rankedItemIds":[]}:
                raise ValueError(f"failure denominator mismatch for {case_id}")
            terminals[case_id] = terminal
    return predictions, manifest, terminals, {
        "manifestSha256": manifest_sha, "predictionSha256": prediction_sha,
        "runContractFileSha256": contract_file_sha, "runContractSha256": contract_sha,
    }


def _load_preregistration(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if resolved.name != "validation20_preregistration_v1.json" or sha256_file(resolved) != PREREGISTRATION_SHA256:
        raise ValueError("Validation20 preregistration identity mismatch")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if (
        value.get("schemaVersion") != "used-phone-natural-guide-validation20-preregistration-v1"
        or value.get("status") != "FROZEN_HELD_OUT_VALIDATION_AI_DESIGNED_NOT_HUMAN_GOLD"
        or value.get("orderedCaseIds") != list(CASE_IDS) or value.get("caseCount") != 20
        or value.get("inputIdentity") != {
            "publicCasesSha256":PUBLIC_CASES_SHA256,"publicCatalogSha256":PUBLIC_CATALOG_SHA256,
            "productionCodeScopeFileCount":90,"productionCodeScopeSha256":PRODUCTION_CODE_SCOPE_SHA256,
        }
        or [row.get("caseId") for row in value.get("cases", [])] != list(CASE_IDS)
    ):
        raise ValueError("Validation20 preregistration semantic mismatch")
    return value


def _unknown_count(item_id: str, catalog: Mapping[str, Mapping[str, Any]]) -> int:
    attributes = catalog.get(item_id, {}).get("attributes", {})
    return sum(attributes.get(group, {}).get("status") != "known" for group in _shared.CONTROLLED_GROUPS)


def _unknown_risk_ordering(
    terminal: Mapping[str, Any], expectation: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]],
) -> bool:
    if expectation.get("requirePreferFewerUnknowns") is not True:
        return True
    pair = expectation["expectedComparedIds"]
    counts = {item_id: _unknown_count(item_id, catalog) for item_id in pair}
    best = min(pair, key=lambda item_id: (counts[item_id], pair.index(item_id)))
    worse = [item_id for item_id in pair if counts[item_id] > counts[best]]
    trace = terminal.get("trace", {})
    answers = trace.get("answers", []) if isinstance(trace, dict) else []
    answer = answers[-1] if answers else ""
    return all(
        f"商品 {item_id} 风险更低" not in answer
        and f"商品 {item_id} 更适合作为当前选择" not in answer
        for item_id in worse
    )


def score_validation20(
    *, predictions_path: Path, manifest_path: Path,
    public_catalog_path: Path, preregistration_path: Path,
) -> dict[str, Any]:
    predictions, manifest, terminals, identities = _authenticate_prediction_before_expectations(
        predictions_path=predictions_path, manifest_path=manifest_path,
    )
    catalog = _load_catalog(public_catalog_path.resolve())
    prereg = _load_preregistration(preregistration_path)
    expectations = {row["caseId"]: row for row in prereg["cases"]}
    execution = manifest["execution"]
    per_case = []
    for prediction in predictions:
        case_id = prediction["caseId"]
        expectation = expectations[case_id]
        constraints = expectation["expectedFinalConstraints"]
        checks = {
            "execution": execution[case_id] is True,
            "action": prediction.get("predictedAction") in expectation["allowedActions"],
            "finalConstraintExact": _shared._constraints_equal(prediction.get("predictedConstraints"), constraints),
            "toolContract": _shared._tool_contract(terminals[case_id], expectation["expectedTool"]),
            "comparisonBinding": expectation.get("expectedComparedIds") is None or (prediction.get("comparison") or {}).get("candidateItemIds") == expectation["expectedComparedIds"],
            "substituteExclusion": not bool(set(prediction.get("rankedItemIds", [])) & set(expectation.get("prohibitedRankedIds", []))),
            "hardViolationFree": not (expectation.get("requireNoHardViolationInRanked") is True and any(_shared._known_hard_violation(item_id, constraints["hard"], catalog) for item_id in prediction.get("rankedItemIds", []))),
            "citationBinding": _shared._citation_binding(prediction, expectation, catalog),
            "hardSupportBoundary": _shared._hard_support_boundary(prediction, terminals[case_id], expectation, catalog),
            "answerCompletion": _shared._answer_completion(terminals[case_id], expectation),
            "unknownRiskOrdering": _unknown_risk_ordering(terminals[case_id], expectation, catalog),
        }
        per_case.append({"caseId":case_id,"checks":checks,"failureStage":_shared._failure_stage(checks),"modelCallCount":terminals[case_id].get("modelCallCount",0),"predictedAction":prediction.get("predictedAction"),"rankedItemCount":len(prediction.get("rankedItemIds",[]))})

    def accuracy(name: str, rows=per_case) -> float:
        return sum(row["checks"][name] for row in rows) / len(rows)

    comparison = [row for row in per_case if expectations[row["caseId"]].get("expectedComparedIds")]
    substitutes = [row for row in per_case if expectations[row["caseId"]].get("prohibitedRankedIds")]
    metrics = {
        "actionAccuracy":accuracy("action"), "citationBindingAccuracy":accuracy("citationBinding"),
        "comparisonBindingAccuracy":accuracy("comparisonBinding",comparison), "executionCoverage":accuracy("execution"),
        "finalConstraintExactAccuracy":accuracy("finalConstraintExact"),
        "hiddenArtifactReadCount":int(manifest.get("hiddenArtifactsRead") is not False),
        "businessWriteNetworkCallCount":int(manifest.get("businessWriteNetworkUsed") is not False),
        "substituteExclusionAccuracy":accuracy("substituteExclusion",substitutes),
        "toolContractAccuracy":accuracy("toolContract"), "hardSupportBoundaryAccuracy":accuracy("hardSupportBoundary"),
        "answerCompletionAccuracy":accuracy("answerCompletion"), "unknownRiskOrderingAccuracy":accuracy("unknownRiskOrdering"),
        "knownHardViolationFreeAccuracy":accuracy("hardViolationFree"),
    }
    gate_checks = {key:(metrics[key] == value if key.endswith("Count") else metrics[key] >= value) for key,value in prereg["gate"].items()}
    # The frozen V10 case explicitly preregistered requireNoHardViolationInRanked.
    # Keep the original score immutable, but make this already-declared safety
    # contract visible as a supplemental exact gate in subsequent replays.
    gate_checks["knownHardViolationFreeAccuracy"] = metrics["knownHardViolationFreeAccuracy"] == 1.0
    return {
        "schemaVersion":SCHEMA_VERSION,"benchmarkId":prereg["benchmarkId"],
        "labelBoundary":"AI-designed held-out validation contracts, not human gold",
        "predictionAuthenticatedBeforeExpectations":True,
        "inputs":{**identities,"publicCatalogSha256":PUBLIC_CATALOG_SHA256,"preregistrationSha256":PREREGISTRATION_SHA256},
        "metrics":metrics,"gateChecks":gate_checks,"gatePassed":all(gate_checks.values()),
        "failureClusters":dict(sorted(Counter(row["failureStage"] for row in per_case if row["failureStage"] is not None).items())),
        "perCase":per_case,
    }


__all__ = ["canonical_bytes", "score_validation20"]
