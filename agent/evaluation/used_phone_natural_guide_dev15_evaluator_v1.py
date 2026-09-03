"""Prediction-first evaluator for the public natural shopping-guide Dev15.

Run-owned bytes and production identities are authenticated before the public
AI-designed expectations are opened.  The labels are development contracts,
not human gold and not an open-domain retrieval-quality claim.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator


SCHEMA_VERSION = "used-phone-natural-guide-dev15-score-v1"
PROTOCOL_VERSION = "used-phone-natural-guide-dev15-production-agent-v1"
POSTFIX_PROTOCOL_VERSION = "used-phone-natural-guide-dev15-production-agent-v2-taskstate-fix"
SUPPORT_PROTOCOL_VERSION = "used-phone-natural-guide-dev15-production-agent-v3-support-evidence"
ANSWER_PROTOCOL_VERSION = "used-phone-natural-guide-dev15-production-agent-v5-unknown-risk"
PREDICTION_SCHEMA_VERSION = "used-phone-natural-guide-dev15-prediction-v1"
PUBLIC_CASES_SHA256 = "f26a94f9034ec0da56710b78060b45c62b33ad72e9b46cebfa0fe7723fa8b6f4"
PUBLIC_CATALOG_SHA256 = "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50"
PREREGISTRATION_SHA256 = "b6311ba953a9e01ab47dabb71b43d50dabeca0db579d8f95251f87e068c86db7"
PRODUCTION_CODE_SCOPE_SHA256 = "8aedc5739c01c59b751c773212f90ef5fdb8eb1e4a55f52639bf52b03d9b6fc2"
CASE_IDS = tuple(f"UPNG-D{index:02d}" for index in range(1, 16))
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "used_phone_natural_guide_dev15_prediction_v1.schema.json"
)
RUNNER_FILES = {
    "agent/evaluation/used_phone_public_agent_runner_v1.py": (
        REPO_ROOT / "agent" / "evaluation" / "used_phone_public_agent_runner_v1.py"
    ),
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": (
        REPO_ROOT / "agent" / "evaluation" / "used_phone_natural_guide_dev15_runner_v1.py"
    ),
    "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": SCHEMA_PATH,
    "agent/scripts/run_used_phone_natural_guide_dev15_v1.py": (
        REPO_ROOT / "agent" / "scripts" / "run_used_phone_natural_guide_dev15_v1.py"
    ),
}
BASELINE_RUNNER_HASHES = {
    "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": "4b744cdd5dc7e095b7430726bbd734b8ded9ca5becd8a8f709d7cb5b1467a028",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": "b19c061acd0cd3c5bbfb6abfa704809005ae179de30d3c18af355afe93788c61",
    "agent/evaluation/used_phone_public_agent_runner_v1.py": "0c4a1b48199b2a8db002bd2687eedd83dbb0061e5a3877e4a7fbf9fbe5e8776a",
    "agent/scripts/run_used_phone_natural_guide_dev15_v1.py": "b55a55a6343afcfbb12ba6a71e08a10b4014acd5950326e930fb6b40bba09a81",
}
POSTFIX_PRODUCTION_CODE_SCOPE_SHA256 = "aa72d4bbcaa0f6c7dad7fa39437d003111fcd214c4b0d3d6397113f63e716204"
POSTFIX_RUNNER_FILES = {
    "agent/evaluation/used_phone_public_agent_runner_v1.py": (
        REPO_ROOT / "agent" / "evaluation" / "used_phone_public_agent_runner_v1.py"
    ),
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": (
        REPO_ROOT / "agent" / "evaluation" / "used_phone_natural_guide_dev15_runner_v1.py"
    ),
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v2.py": (
        REPO_ROOT / "agent" / "evaluation" / "used_phone_natural_guide_dev15_runner_v2.py"
    ),
    "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": SCHEMA_PATH,
    "agent/scripts/run_used_phone_natural_guide_dev15_v2.py": (
        REPO_ROOT / "agent" / "scripts" / "run_used_phone_natural_guide_dev15_v2.py"
    ),
}
POSTFIX_RUNNER_HASHES = {
    "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": "4b744cdd5dc7e095b7430726bbd734b8ded9ca5becd8a8f709d7cb5b1467a028",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": "b19c061acd0cd3c5bbfb6abfa704809005ae179de30d3c18af355afe93788c61",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v2.py": "46fdee80d4621190207f8a35d9108eb236a754fdb8ff51393c8d8e3402e724f3",
    "agent/evaluation/used_phone_public_agent_runner_v1.py": "0c4a1b48199b2a8db002bd2687eedd83dbb0061e5a3877e4a7fbf9fbe5e8776a",
    "agent/scripts/run_used_phone_natural_guide_dev15_v2.py": "cc37715c9aff3b33277c7868ca52711e9ac6c5ea1ee1c31f92ee2f2a2e59f372",
}
SUPPORT_PRODUCTION_CODE_SCOPE_SHA256 = "0f539f89e9ab3f7fe4d934e49805c58ead307aa3395ca6e2fdf2c3d310257bf0"
SUPPORT_RUNNER_HASHES = {
    "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": "4b744cdd5dc7e095b7430726bbd734b8ded9ca5becd8a8f709d7cb5b1467a028",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": "b19c061acd0cd3c5bbfb6abfa704809005ae179de30d3c18af355afe93788c61",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v3.py": "89c6a43eb1a57fa703d6ae4c850a8c8f7ec1faa5ed126af72d93adcd852856ab",
    "agent/evaluation/used_phone_public_agent_runner_v1.py": "7c552a5b2f7dc0b42d61a7d37ce35688ce6b3be88f2517660885e383be9e7c09",
    "agent/scripts/run_used_phone_natural_guide_dev15_v3.py": "b82b834f1cd4d64fa3bdeae168315209b4e5ec36e9929f298e570d9e798ed9bf",
}
COMPLETION_PROTOCOL_VERSION = "used-phone-natural-guide-dev15-production-agent-v4-answer-completion"
COMPLETION_PRODUCTION_CODE_SCOPE_SHA256 = "aeebe7f81a20463e3ec06ede42ec26fc23e5480b57b38dbc74a69151229c386f"
COMPLETION_RUNNER_HASHES = {
    "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": "4b744cdd5dc7e095b7430726bbd734b8ded9ca5becd8a8f709d7cb5b1467a028",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": "b19c061acd0cd3c5bbfb6abfa704809005ae179de30d3c18af355afe93788c61",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v4.py": "fce5e0425a61e56afcf4e0189df96846fc0c495bd9f197e170fe8956c7e784e0",
    "agent/evaluation/used_phone_public_agent_runner_v1.py": "7c552a5b2f7dc0b42d61a7d37ce35688ce6b3be88f2517660885e383be9e7c09",
    "agent/scripts/run_used_phone_natural_guide_dev15_v4.py": "37dbf80d8cb8699ef72801261720b9ede387adb7f842fa1f0fcce040833cfd0a",
}
ANSWER_PRODUCTION_CODE_SCOPE_SHA256 = "a1ddc4bb58198a62dc87cf191edada842bfbcd1c973822b2a05b418f541d9381"
ANSWER_RUNNER_HASHES = {
    "agent/evaluation/schemas/used_phone_natural_guide_dev15_prediction_v1.schema.json": "4b744cdd5dc7e095b7430726bbd734b8ded9ca5becd8a8f709d7cb5b1467a028",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v1.py": "b19c061acd0cd3c5bbfb6abfa704809005ae179de30d3c18af355afe93788c61",
    "agent/evaluation/used_phone_natural_guide_dev15_runner_v4.py": "d0f2e9cfb673f1bffbdf27bb4c3a14737e311116c677be04080fb06ced7197b9",
    "agent/evaluation/used_phone_public_agent_runner_v1.py": "7c552a5b2f7dc0b42d61a7d37ce35688ce6b3be88f2517660885e383be9e7c09",
    "agent/scripts/run_used_phone_natural_guide_dev15_v4.py": "2eb03f02ea6bc11e560e3a3c6e728b04d3bee456cb9cb3433ed7f8c163e3afa9",
}
CONTROLLED_GROUPS = frozenset({
    "battery_health", "battery_originality", "motherboard_repair", "os",
    "scratch_level", "screen_originality", "shell_condition",
})
PRODUCT_TOOLS = frozenset({"search_products", "get_product_details", "compare_products"})
EXPECTED_JAVA_CATALOG = {
    "attributeCount": 1547,
    "businessWriteNetworkUsed": False,
    "catalogVersion": "used-phone-benchmark-v1-09807c773ce67360ed8df30842e372182fcf7ad9",
    "contentSha256": "d60cdf433f035df73b2530a8b28e7352a3f92c7b4db2af5a18ad43fd4b8a1d00",
    "javaProjectionSha256": "566d619fb4ae6ef00d8ca1bd37cf173f196abbbf54cade078c1c5a5a478928ca",
    "networkCallCount": 29,
    "productCount": 252,
    "resolveBatchCount": 26,
}
EXPECTED_RUNTIME_CONFIG = {
    "agentContextMode": "context_pack",
    "agentLegacyFallbackEnabled": False,
    "agentOrchestratorMode": "unified",
    "agentRequestDeadlineSeconds": 20.0,
    "agentToolTransportMode": "live",
    "agentTransactionEnabled": False,
    "backendBaseUrl": "http://127.0.0.1:18081",
    "ecommerceGuideEnabled": True,
    "evidenceCriticEnabled": False,
    "productRetrievalMode": "bm25",
}


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_hashes(paths: Mapping[str, Path]) -> dict[str, str]:
    return {name: sha256_file(path) for name, path in paths.items()}


def _load_canonical_json(path: Path, document: str) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {document} JSON") from exc
    if not isinstance(value, dict) or payload != canonical_bytes(value):
        raise ValueError(f"{document} must be a canonical JSON object")
    return value, hashlib.sha256(payload).hexdigest()


def _load_predictions(path: Path) -> tuple[list[dict[str, Any]], str]:
    payload = path.read_bytes()
    rows: list[dict[str, Any]] = []
    try:
        for line_number, raw in enumerate(payload.splitlines(keepends=True), 1):
            value = json.loads(raw)
            if (
                not raw.endswith(b"\n")
                or not isinstance(value, dict)
                or raw != canonical_bytes(value)
            ):
                raise ValueError(f"non-canonical prediction row {line_number}")
            rows.append(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid predictions JSONL") from exc
    return rows, hashlib.sha256(payload).hexdigest()


def _load_catalog(path: Path) -> dict[str, dict[str, Any]]:
    if path.name != "catalog.jsonl" or sha256_file(path) != PUBLIC_CATALOG_SHA256:
        raise ValueError("public catalog identity mismatch")
    rows: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        row = json.loads(line)
        item_id = row.get("itemId") if isinstance(row, dict) else None
        if not isinstance(item_id, str) or item_id in rows:
            raise ValueError(f"invalid catalog row {line_number}")
        rows[item_id] = row
    if len(rows) != 252:
        raise ValueError("public catalog count mismatch")
    return rows


def _require_run_paths(
    predictions_path: Path, manifest_path: Path,
) -> tuple[Path, Path, Path]:
    predictions = predictions_path.resolve()
    manifest = manifest_path.resolve()
    if (
        predictions.name != "predictions.jsonl"
        or manifest.name != "manifest.json"
        or predictions.parent != manifest.parent
    ):
        raise ValueError("prediction and manifest must come from one run directory")
    return predictions, manifest, manifest.parent


def _authenticate_prediction_before_expectations(
    *, predictions_path: Path, manifest_path: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]], dict[str, str]]:
    predictions_file, manifest_file, run_dir = _require_run_paths(
        predictions_path, manifest_path,
    )
    predictions, prediction_sha = _load_predictions(predictions_file)
    manifest, manifest_sha = _load_canonical_json(manifest_file, "manifest")
    contract_wrapper, contract_file_sha = _load_canonical_json(
        run_dir / "run_contract.json", "run contract",
    )
    contract = contract_wrapper.get("contract")
    if not isinstance(contract, dict):
        raise ValueError("run contract payload is missing")
    protocol = contract.get("protocolVersion")
    if protocol == PROTOCOL_VERSION:
        expected_scope = PRODUCTION_CODE_SCOPE_SHA256
        expected_runner_hashes = BASELINE_RUNNER_HASHES
    elif protocol == POSTFIX_PROTOCOL_VERSION:
        expected_scope = POSTFIX_PRODUCTION_CODE_SCOPE_SHA256
        expected_runner_hashes = POSTFIX_RUNNER_HASHES
    elif protocol == SUPPORT_PROTOCOL_VERSION:
        expected_scope = SUPPORT_PRODUCTION_CODE_SCOPE_SHA256
        expected_runner_hashes = SUPPORT_RUNNER_HASHES
    elif protocol == COMPLETION_PROTOCOL_VERSION:
        expected_scope = COMPLETION_PRODUCTION_CODE_SCOPE_SHA256
        expected_runner_hashes = COMPLETION_RUNNER_HASHES
    elif protocol == ANSWER_PROTOCOL_VERSION:
        expected_scope = ANSWER_PRODUCTION_CODE_SCOPE_SHA256
        expected_runner_hashes = ANSWER_RUNNER_HASHES
    else:
        raise ValueError("unsupported Dev15 run protocol")
    contract_sha = hashlib.sha256(canonical_bytes(contract)).hexdigest()
    if (
        contract_wrapper.get("schemaVersion") != "used-phone-public-agent-run-contract-v1"
        or contract_wrapper.get("contractSha256") != contract_sha
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
        "protocolVersion": protocol,
        "publicCasesSha256": PUBLIC_CASES_SHA256,
        "publicCatalogSha256": PUBLIC_CATALOG_SHA256,
        "productionCodeScopeSha256": expected_scope,
        "runnerCodeSha256": expected_runner_hashes,
        "attributeContractCodeSha256": "545c4f7d528ed9875dd468d9d9bbf5e4e6034b6cdbcf0860800bfc2b4b18688a",
        "attributeRuleset": "used-phone-exact-token-seven-field-v2",
        "javaCatalog": EXPECTED_JAVA_CATALOG,
        "modelEndpoint": "https://api.deepseek.com",
        "productionRuntimeConfig": EXPECTED_RUNTIME_CONFIG,
        "selectedCaseIds": list(CASE_IDS),
        "selectedSplits": ["dev"],
        "allowSealedTest": False,
        "caseCount": 15,
    }:
        raise ValueError("run identity mismatch")
    if (
        manifest.get("selectedCaseIds") != list(CASE_IDS)
        or manifest.get("selectedSplits") != ["dev"]
        or manifest.get("prediction") != {
            "path": "predictions.jsonl", "rowCount": 15, "sha256": prediction_sha,
        }
        or [row.get("caseId") for row in predictions] != list(CASE_IDS)
        or manifest.get("hiddenArtifactsRead") is not False
        or manifest.get("businessWriteNetworkUsed") is not False
    ):
        raise ValueError("manifest public/no-write identity mismatch")

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    terminals: dict[str, dict[str, Any]] = {}
    execution = manifest.get("execution")
    if not isinstance(execution, dict) or list(execution) != list(CASE_IDS):
        raise ValueError("manifest execution identity mismatch")
    for case_id, prediction in zip(CASE_IDS, predictions, strict=True):
        validator.validate(prediction)
        attempt_dir = run_dir / "cases" / case_id / "attempt-001"
        if not attempt_dir.is_dir():
            raise ValueError(f"missing immutable attempt for {case_id}")
        extra_attempts = list((run_dir / "cases" / case_id).glob("attempt-*"))
        if extra_attempts != [attempt_dir]:
            raise ValueError(f"unexpected attempt layout for {case_id}")
        terminal_name = "success.json" if execution[case_id] is True else "failure.json"
        terminal, _terminal_sha = _load_canonical_json(
            attempt_dir / terminal_name, f"{case_id} terminal",
        )
        if (
            terminal.get("caseId") != case_id
            or terminal.get("attempt") != 1
            or terminal.get("protocolVersion") != protocol
            or terminal.get("contractSha256") != contract_sha
            or terminal.get("businessWriteNetworkUsed") is not False
        ):
            raise ValueError(f"terminal provenance mismatch for {case_id}")
        if execution[case_id] is True:
            case_prediction, case_prediction_sha = _load_canonical_json(
                attempt_dir / "prediction.json", f"{case_id} prediction",
            )
            trace, trace_sha = _load_canonical_json(
                attempt_dir / "trace.json", f"{case_id} trace",
            )
            if (
                terminal.get("terminalState") != "success"
                or case_prediction != prediction
                or terminal.get("prediction") != prediction
                or terminal.get("predictionSha256") != case_prediction_sha
                or terminal.get("trace") != trace
                or terminal.get("traceSha256") != trace_sha
                or trace.get("caseId") != case_id
                or trace.get("protocolVersion") != protocol
                or trace.get("businessWriteNetworkUsed") is not False
            ):
                raise ValueError(f"success bytes/provenance mismatch for {case_id}")
            terminals[case_id] = {**terminal, "trace": trace}
        else:
            if (
                terminal.get("terminalState") != "failure"
                or prediction != {"caseId": case_id, "rankedItemIds": []}
            ):
                raise ValueError(f"failure denominator mismatch for {case_id}")
            terminals[case_id] = terminal
    return predictions, manifest, terminals, {
        "manifestSha256": manifest_sha,
        "predictionSha256": prediction_sha,
        "runContractFileSha256": contract_file_sha,
        "runContractSha256": contract_sha,
    }


def _load_preregistration(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if (
        resolved.name != "dev15_preregistration_v1.json"
        or sha256_file(resolved) != PREREGISTRATION_SHA256
    ):
        raise ValueError("Dev15 preregistration identity mismatch")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schemaVersion") != "used-phone-natural-guide-dev15-preregistration-v1"
        or value.get("orderedCaseIds") != list(CASE_IDS)
        or value.get("status") != "FROZEN_PUBLIC_DEV_AI_DESIGNED_NOT_HUMAN_GOLD"
        or value.get("caseCount") != 15
        or value.get("inputIdentity") != {
            "publicCasesSha256": PUBLIC_CASES_SHA256,
            "publicCatalogSha256": PUBLIC_CATALOG_SHA256,
            "productionCodeScopeFileCount": 90,
            "productionCodeScopeSha256": PRODUCTION_CODE_SCOPE_SHA256,
        }
        or [row.get("caseId") for row in value.get("cases", [])] != list(CASE_IDS)
    ):
        raise ValueError("Dev15 preregistration semantic mismatch")
    return value


def _atom_identity(atom: Mapping[str, Any]) -> tuple[Any, ...]:
    group = atom.get("group")
    operator = atom.get("operator")
    allowed = tuple(sorted(atom.get("allowedValues", [])))
    # These two public controlled fields are binary when known.  The runtime
    # treats unknown/conflict as non-passing for both spellings, so requiring
    # the positive value is semantically equivalent to excluding its sole
    # negative complement.
    binary_complements = {
        "motherboard_repair": {"not_repaired": "repaired", "repaired": "not_repaired"},
        "screen_originality": {"original": "non_original", "non_original": "original"},
        "battery_originality": {"original": "non_original", "non_original": "original"},
        "shell_condition": {"normal": "damaged", "damaged": "normal"},
    }
    if operator == "NOT_IN" and len(allowed) == 1:
        complement = binary_complements.get(group, {}).get(allowed[0])
        if complement is not None:
            operator, allowed = "IN", (complement,)
    return (
        group, operator, allowed,
    )


def _constraints_equal(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, dict) or not isinstance(expected, dict):
        return False
    return all(
        sorted(_atom_identity(atom) for atom in actual.get(importance, []))
        == sorted(_atom_identity(atom) for atom in expected.get(importance, []))
        for importance in ("hard", "soft")
    )


def _final_successful_product_traces(terminal: Mapping[str, Any]) -> list[dict[str, Any]]:
    trace = terminal.get("trace")
    if not isinstance(trace, dict):
        return []
    turn_count = len(trace.get("turns", []))
    result = []
    for item in trace.get("toolTraces", []):
        if (
            isinstance(item, dict)
            and item.get("publicTurnIndex") == turn_count - 1
            and item.get("tool") in PRODUCT_TOOLS
            and item.get("ok") is True
            and isinstance(item.get("detail"), dict)
        ):
            result.append(item)
    return result


def _tool_contract(terminal: Mapping[str, Any], expected_tool: str) -> bool:
    observed = [item.get("tool") for item in _final_successful_product_traces(terminal)]
    return observed == ([] if expected_tool == "none" else [expected_tool])


def _citation_binding(
    prediction: Mapping[str, Any], expectation: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
) -> bool:
    citations = prediction.get("evidenceCitations", [])
    if not isinstance(citations, list) or len(citations) < expectation.get("minimumCitationCount", 0):
        return False
    ranked = set(prediction.get("rankedItemIds", []))
    compared = set((prediction.get("comparison") or {}).get("candidateItemIds", []))
    allowed_items = compared if expectation.get("expectedTool") == "compare_products" else ranked
    for citation in citations:
        if not isinstance(citation, dict):
            return False
        item_id, group = citation.get("itemId"), citation.get("group")
        product = catalog.get(item_id)
        observation = (
            product.get("attributes", {}).get(group)
            if isinstance(product, dict) and group in CONTROLLED_GROUPS else None
        )
        refs = observation.get("evidenceRefs") if isinstance(observation, dict) else None
        if (
            item_id not in allowed_items
            or not isinstance(observation, dict)
            or observation.get("status") != "known"
            or not isinstance(refs, list)
            or not any(
                ref.get("source") == citation.get("source")
                and ref.get("field") == citation.get("field")
                and ref.get("lineNumber") == citation.get("lineNumber")
                and ref.get("rawValue") == citation.get("rawValue")
                for ref in refs if isinstance(ref, dict)
            )
        ):
            return False
    return True


def _known_hard_violation(
    item_id: str, hard_atoms: Sequence[Mapping[str, Any]],
    catalog: Mapping[str, Mapping[str, Any]],
) -> bool:
    product = catalog.get(item_id, {})
    attributes = product.get("attributes", {}) if isinstance(product, dict) else {}
    for atom in hard_atoms:
        observation = attributes.get(atom.get("group"), {})
        if observation.get("status") != "known":
            continue
        actual = observation.get("value")
        allowed = set(atom.get("allowedValues", []))
        if (
            atom.get("operator") == "IN" and actual not in allowed
        ) or (
            atom.get("operator") == "NOT_IN" and actual in allowed
        ):
            return True
    return False


def _hard_support_boundary(
    prediction: Mapping[str, Any], terminal: Mapping[str, Any],
    expectation: Mapping[str, Any], catalog: Mapping[str, Mapping[str, Any]],
) -> bool:
    """No-match cases must not present hard-unknown candidates as matches."""

    if expectation.get("requireNoHardViolationInRanked") is not True:
        return True
    hard = expectation["expectedFinalConstraints"]["hard"]
    ranked = prediction.get("rankedItemIds", [])
    has_known_complete = any(
        not _known_hard_violation(item_id, hard, catalog)
        and all(
            catalog.get(item_id, {}).get("attributes", {}).get(atom["group"], {}).get("status") == "known"
            for atom in hard
        )
        for item_id in ranked
    )
    if has_known_complete:
        return True
    trace = terminal.get("trace", {})
    answers = trace.get("answers", []) if isinstance(trace, dict) else []
    answer = answers[-1] if isinstance(answers, list) and answers else ""
    boundary_terms = ("没有找到", "无法确认", "证据不足", "未知", "不能把未知当作满足")
    return (
        prediction.get("predictedAction") == "ABSTAIN_OR_EXPLAIN"
        and isinstance(answer, str)
        and any(term in answer for term in boundary_terms)
    )


def _answer_completion(
    terminal: Mapping[str, Any], expectation: Mapping[str, Any]
) -> bool:
    trace = terminal.get("trace", {})
    answers = trace.get("answers", []) if isinstance(trace, dict) else []
    answer = answers[-1] if isinstance(answers, list) and answers else ""
    if not isinstance(answer, str) or not answer.strip():
        return False
    if expectation.get("expectedTool") == "compare_products":
        return (
            "可靠性校验" not in answer
            and "商品" in answer
            and any(term in answer for term in ("电池", "屏幕", "主板", "未知", "冲突"))
        )
    return True


def _unknown_risk_ordering(
    terminal: Mapping[str, Any], expectation: Mapping[str, Any]
) -> bool:
    if expectation.get("expectedTool") != "compare_products":
        return True
    trace = terminal.get("trace", {})
    answers = trace.get("answers", []) if isinstance(trace, dict) else []
    answer = answers[-1] if isinstance(answers, list) and answers else ""
    if "风险更低" in answer and "未知/冲突" not in answer:
        return False
    if "1068548" in answer and "1092202" in answer:
        return "商品 1068548 风险更低" not in answer and "商品 1068548 更适合作为当前选择" not in answer
    return True
def _failure_stage(checks: Mapping[str, bool]) -> str | None:
    for stage, names in (
        ("execution", ("execution",)),
        ("task_state", ("finalConstraintExact",)),
        ("planner_executor_tool", ("toolContract", "comparisonBinding", "substituteExclusion", "hardViolationFree")),
        ("validator_projection", ("action",)),
        ("evidence_answer", ("citationBinding", "hardSupportBoundary", "answerCompletion", "unknownRiskOrdering")),
    ):
        if any(checks.get(name) is False for name in names):
            return stage
    return None


def score_dev15(
    *, predictions_path: Path, manifest_path: Path,
    public_catalog_path: Path, preregistration_path: Path,
) -> dict[str, Any]:
    predictions, manifest, terminals, identities = (
        _authenticate_prediction_before_expectations(
            predictions_path=predictions_path, manifest_path=manifest_path,
        )
    )
    catalog = _load_catalog(public_catalog_path.resolve())
    prereg = _load_preregistration(preregistration_path)
    expectations = {row["caseId"]: row for row in prereg["cases"]}
    execution = manifest["execution"]
    per_case = []
    for prediction in predictions:
        case_id = prediction["caseId"]
        expectation = expectations[case_id]
        expected_constraints = expectation["expectedFinalConstraints"]
        checks = {
            "execution": execution[case_id] is True,
            "action": prediction.get("predictedAction") in expectation["allowedActions"],
            "finalConstraintExact": _constraints_equal(
                prediction.get("predictedConstraints"), expected_constraints,
            ),
            "toolContract": _tool_contract(terminals[case_id], expectation["expectedTool"]),
            "comparisonBinding": (
                expectation.get("expectedComparedIds") is None
                or (prediction.get("comparison") or {}).get("candidateItemIds")
                == expectation["expectedComparedIds"]
            ),
            "substituteExclusion": not bool(
                set(prediction.get("rankedItemIds", []))
                & set(expectation.get("prohibitedRankedIds", []))
            ),
            "hardViolationFree": not (
                expectation.get("requireNoHardViolationInRanked") is True
                and any(
                    _known_hard_violation(
                        item_id, expected_constraints["hard"], catalog,
                    )
                    for item_id in prediction.get("rankedItemIds", [])
                )
            ),
            "citationBinding": _citation_binding(prediction, expectation, catalog),
            "hardSupportBoundary": _hard_support_boundary(
                prediction, terminals[case_id], expectation, catalog,
            ),
            "answerCompletion": _answer_completion(terminals[case_id], expectation),
            "unknownRiskOrdering": _unknown_risk_ordering(terminals[case_id], expectation),
        }
        per_case.append({
            "caseId": case_id,
            "checks": checks,
            "failureStage": _failure_stage(checks),
            "modelCallCount": terminals[case_id].get("modelCallCount", 0),
            "predictedAction": prediction.get("predictedAction"),
            "rankedItemCount": len(prediction.get("rankedItemIds", [])),
        })

    def accuracy(name: str) -> float:
        return sum(row["checks"][name] for row in per_case) / len(per_case)

    comparison_rows = [row for row in per_case if expectations[row["caseId"]].get("expectedComparedIds")]
    substitute_rows = [row for row in per_case if expectations[row["caseId"]].get("prohibitedRankedIds")]
    metrics = {
        "actionAccuracy": accuracy("action"),
        "citationBindingAccuracy": accuracy("citationBinding"),
        "comparisonBindingAccuracy": (
            sum(row["checks"]["comparisonBinding"] for row in comparison_rows) / len(comparison_rows)
        ),
        "executionCoverage": accuracy("execution"),
        "finalConstraintExactAccuracy": accuracy("finalConstraintExact"),
        "hiddenArtifactReadCount": int(manifest.get("hiddenArtifactsRead") is not False),
        "businessWriteNetworkCallCount": int(manifest.get("businessWriteNetworkUsed") is not False),
        "substituteExclusionAccuracy": (
            sum(row["checks"]["substituteExclusion"] for row in substitute_rows) / len(substitute_rows)
        ),
        "toolContractAccuracy": accuracy("toolContract"),
        "hardSupportBoundaryAccuracy": accuracy("hardSupportBoundary"),
        "answerCompletionAccuracy": accuracy("answerCompletion"),
        "unknownRiskOrderingAccuracy": accuracy("unknownRiskOrdering"),
    }
    thresholds = prereg["gate"]
    gate_checks = {
        key: (
            metrics[key] == value
            if key.endswith("Count") else metrics[key] >= value
        )
        for key, value in thresholds.items()
    }
    # Supplemental safety-quality check added after attempt-002 exposed that
    # the original public preregistration covered known hard violations but
    # not hard-unknown candidates presented as matches. Original bytes and
    # thresholds remain frozen; this stricter check is reported separately.
    gate_checks["hardSupportBoundaryAccuracy"] = (
        metrics["hardSupportBoundaryAccuracy"] == 1.0
    )
    gate_checks["answerCompletionAccuracy"] = (
        metrics["answerCompletionAccuracy"] == 1.0
    )
    gate_checks["unknownRiskOrderingAccuracy"] = (
        metrics["unknownRiskOrderingAccuracy"] == 1.0
    )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "benchmarkId": prereg["benchmarkId"],
        "labelBoundary": "AI-designed public development contracts, not human gold",
        "predictionAuthenticatedBeforeExpectations": True,
        "inputs": {
            **identities,
            "publicCatalogSha256": PUBLIC_CATALOG_SHA256,
            "preregistrationSha256": PREREGISTRATION_SHA256,
        },
        "metrics": metrics,
        "gateChecks": gate_checks,
        "gatePassed": all(gate_checks.values()),
        "failureClusters": dict(sorted(Counter(
            row["failureStage"] for row in per_case if row["failureStage"] is not None
        ).items())),
        "perCase": per_case,
    }


__all__ = ["canonical_bytes", "score_dev15"]
