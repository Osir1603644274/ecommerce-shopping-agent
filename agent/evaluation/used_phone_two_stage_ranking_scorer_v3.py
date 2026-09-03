"""Independent, identity-pinned public-dev scorer for two-stage ranking v3.

The SUT runner does not import this module or read judgments.  The scorer
accepts only a fresh, no-model, public-dev v3 run whose run contract, manifest,
prediction schema, production scope, and runner bytes all match this evaluator
identity.  The scorer takes one immutable-in-practice byte snapshot of each
run-owned input and authenticates every label-independent invariant before it
opens preregistration or judgments.  It then accepts only a separately
materialized D01-D10 judgment artifact.  The frozen v2 D/V/T source belongs
exclusively to the asset-build boundary and is outside both the SUT runtime and
scorer read boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

from .build_used_phone_two_stage_public_dev_assets_v3 import (
    DEFAULT_OUTPUT_DIR as DEFAULT_EVALUATOR_ASSET_DIR,
    PUBLIC_DEV_JUDGMENT_COUNT,
    PUBLIC_DEV_JUDGMENTS_BYTE_COUNT,
    PUBLIC_DEV_JUDGMENTS_FILENAME,
    PUBLIC_DEV_JUDGMENTS_SHA256,
    PUBLIC_DEV_PREREGISTRATION_FILENAME,
    TOP10_HARD_VIOLATION_RATE_DEFINITION,
    public_dev_preregistration,
)


SCHEMA_VERSION = (
    "used-phone-two-stage-ranking-score-v3-prediction-authenticated-before-labels"
)
EVALUATOR_IDENTITY_VERSION = (
    "used-phone-two-stage-ranking-evaluator-v3-prediction-first"
)
MANIFEST_SCHEMA_VERSION = "used-phone-public-agent-run-manifest-v1"
RUN_CONTRACT_SCHEMA_VERSION = "used-phone-public-agent-run-contract-v1"
PROTOCOL_VERSION = "used-phone-public-production-agent-runner-v3-two-stage-ranking-dev"
PREDICTION_SCHEMA_VERSION = "used-phone-two-stage-ranking-prediction-v3"
RANKING_CONTRACT_VERSION = "ecommerce-two-stage-ranking-v1"
NO_MODEL_NAME = "none-deterministic-production-control"
NO_MODEL_ENDPOINT = "disabled://no-model"
PRODUCTION_CODE_SCOPE_SHA256 = "192c9313927c4cc01e648426bbdc2c7a5d9e0ffc6444f00ce46725c2011a32ff"
PUBLIC_DEV_CASE_IDS = tuple(f"UPV2-RK-D{index:02d}" for index in range(1, 11))
PUBLIC_CASES_SHA256 = "2c05fcfcad843bc97a22b4d22a58c894f6574ef408cbcfca05cf6783fee2917c"
PUBLIC_CATALOG_SHA256 = "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50"
PUBLIC_DEV_PREREGISTRATION_SHA256 = "711316651c3db8b831c8172c87f56398775f04107bdc5b60e241bd52fee3d800"
ATTRIBUTE_CONTRACT_CODE_SHA256 = "545c4f7d528ed9875dd468d9d9bbf5e4e6034b6cdbcf0860800bfc2b4b18688a"
ATTRIBUTE_RULESET_VERSION = "used-phone-exact-token-seven-field-v2"
JAVA_CATALOG_VERSION = "used-phone-benchmark-v1-09807c773ce67360ed8df30842e372182fcf7ad9"
JAVA_CATALOG_CONTENT_SHA256 = "d60cdf433f035df73b2530a8b28e7352a3f92c7b4db2af5a18ad43fd4b8a1d00"
JAVA_PROJECTION_SHA256 = "566d619fb4ae6ef00d8ca1bd37cf173f196abbbf54cade078c1c5a5a478928ca"
REPO_ROOT = Path(__file__).resolve().parents[2]
PREDICTION_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "used_phone_two_stage_ranking_prediction_v3.schema.json"
)
PRODUCTION_RUNTIME_CONFIG = {
    "agentContextMode": "context_pack",
    "agentOrchestratorMode": "unified",
    "agentLegacyFallbackEnabled": False,
    "agentRequestDeadlineSeconds": 20.0,
    "agentToolTransportMode": "live",
    "ecommerceGuideEnabled": True,
    "agentTransactionEnabled": False,
    "evidenceCriticEnabled": False,
    "productRetrievalMode": "bm25",
    "backendBaseUrl": "http://127.0.0.1:18081",
}
RUNNER_FILES = {
    "agent/evaluation/used_phone_public_agent_runner_v1.py": (
        REPO_ROOT / "agent" / "evaluation" / "used_phone_public_agent_runner_v1.py"
    ),
    "agent/evaluation/used_phone_two_stage_ranking_runner_v3.py": (
        REPO_ROOT / "agent" / "evaluation" / "used_phone_two_stage_ranking_runner_v3.py"
    ),
    "agent/evaluation/schemas/used_phone_two_stage_ranking_prediction_v3.schema.json": (
        PREDICTION_SCHEMA_PATH
    ),
    "agent/evaluation/schemas/used_phone_ranking_prediction_v2.schema.json": (
        REPO_ROOT / "agent" / "evaluation" / "schemas"
        / "used_phone_ranking_prediction_v2.schema.json"
    ),
    "agent/scripts/run_used_phone_two_stage_ranking_v3.py": (
        REPO_ROOT / "agent" / "scripts" / "run_used_phone_two_stage_ranking_v3.py"
    ),
}
EVALUATOR_FILES = {
    "agent/evaluation/build_used_phone_two_stage_public_dev_assets_v3.py": (
        REPO_ROOT
        / "agent"
        / "evaluation"
        / "build_used_phone_two_stage_public_dev_assets_v3.py"
    ),
    "agent/evaluation/used_phone_two_stage_ranking_scorer_v3.py": Path(__file__).resolve(),
    "agent/evaluation/schemas/used_phone_two_stage_ranking_prediction_v3.schema.json": (
        PREDICTION_SCHEMA_PATH
    ),
    "agent/scripts/score_used_phone_two_stage_ranking_v3.py": (
        REPO_ROOT / "agent" / "scripts" / "score_used_phone_two_stage_ranking_v3.py"
    ),
}
EVALUATOR_ASSET_DIR = DEFAULT_EVALUATOR_ASSET_DIR


def canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_hashes(paths: Mapping[str, Path]) -> dict[str, str]:
    return {name: sha256_file(path) for name, path in paths.items()}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"non-object JSON document: {path.name}")
    return value


def _load_canonical_json_snapshot(
    path: Path, *, document: str,
) -> tuple[dict[str, Any], str]:
    payload = path.read_bytes()
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {document} JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{document} must be an object")
    if payload != canonical_bytes(value):
        raise ValueError(f"{document} is not canonical JSON")
    return value, hashlib.sha256(payload).hexdigest()


def _load_canonical_prediction_snapshot(
    path: Path,
) -> tuple[list[dict[str, Any]], str]:
    payload = path.read_bytes()
    rows: list[dict[str, Any]] = []
    try:
        lines = payload.splitlines(keepends=True)
        for line_number, raw in enumerate(lines, start=1):
            if not raw.strip() or not raw.endswith(b"\n"):
                raise ValueError(
                    f"invalid predictions JSONL line {line_number}"
                )
            row = json.loads(raw)
            if not isinstance(row, dict) or raw != canonical_bytes(row):
                raise ValueError(
                    f"non-canonical predictions JSONL line {line_number}"
                )
            rows.append(row)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid predictions JSONL") from exc
    return rows, hashlib.sha256(payload).hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("non-object JSONL row")
            rows.append(row)
    return rows


def _require_evaluator_asset(path: Path, filename: str) -> Path:
    resolved = path.resolve()
    if (
        resolved.name != filename
        or resolved.parent != EVALUATOR_ASSET_DIR.resolve()
    ):
        raise ValueError(
            f"{filename} must come from the pinned evaluator-owned asset directory"
        )
    return resolved


def _load_public_dev_judgments(path: Path) -> dict[str, dict[str, Any]]:
    resolved = _require_evaluator_asset(path, PUBLIC_DEV_JUDGMENTS_FILENAME)
    payload = resolved.read_bytes()
    if len(payload) != PUBLIC_DEV_JUDGMENTS_BYTE_COUNT:
        raise ValueError("public-dev judgments byte count mismatch")
    if hashlib.sha256(payload).hexdigest() != PUBLIC_DEV_JUDGMENTS_SHA256:
        raise ValueError("public-dev judgments SHA mismatch")
    judgments_by_case: dict[str, dict[str, Any]] = {
        case_id: {} for case_id in PUBLIC_DEV_CASE_IDS
    }
    observed_case_order: list[str] = []
    row_count = 0
    for line_number, raw in enumerate(payload.splitlines(keepends=True), start=1):
        if not raw.strip() or not raw.endswith(b"\n"):
            raise ValueError(f"invalid public-dev judgment line {line_number}")
        row = json.loads(raw)
        if not isinstance(row, dict) or raw != canonical_bytes(row):
            raise ValueError(f"non-canonical public-dev judgment line {line_number}")
        case_id = row.get("caseId")
        item_id = row.get("itemId")
        if (
            case_id not in judgments_by_case
            or row.get("split") != "dev"
            or row.get("schemaVersion") != "used-phone-ranking-judgment-v2"
            or not isinstance(item_id, str)
            or not item_id.isdigit()
            or item_id.startswith("0")
        ):
            raise ValueError(
                "public-dev judgments contain a non-dev, unknown, or invalid identity"
            )
        if not observed_case_order or observed_case_order[-1] != case_id:
            if case_id in observed_case_order:
                raise ValueError("public-dev judgment case block is non-contiguous")
            observed_case_order.append(case_id)
        if item_id in judgments_by_case[case_id]:
            raise ValueError("duplicate public-dev judgment")
        judgments_by_case[case_id][item_id] = row
        row_count += 1
    if (
        row_count != PUBLIC_DEV_JUDGMENT_COUNT
        or tuple(observed_case_order) != PUBLIC_DEV_CASE_IDS
        or any(len(rows) != 252 for rows in judgments_by_case.values())
    ):
        raise ValueError("public-dev judgment case/cardinality identity mismatch")
    item_universe = set(judgments_by_case[PUBLIC_DEV_CASE_IDS[0]])
    if len(item_universe) != 252 or any(
        set(rows) != item_universe for rows in judgments_by_case.values()
    ):
        raise ValueError("public-dev judgment item universe mismatch")
    return judgments_by_case


def _validate_public_dev_preregistration(path: Path) -> dict[str, Any]:
    resolved = _require_evaluator_asset(
        path, PUBLIC_DEV_PREREGISTRATION_FILENAME
    )
    payload = resolved.read_bytes()
    if hashlib.sha256(payload).hexdigest() != PUBLIC_DEV_PREREGISTRATION_SHA256:
        raise ValueError("public-dev preregistration SHA mismatch")
    preregistration = json.loads(payload)
    if not isinstance(preregistration, dict):
        raise ValueError("public-dev preregistration must be an object")
    if preregistration != public_dev_preregistration():
        raise ValueError("public-dev preregistration semantic mismatch")
    return preregistration


def _mean(values: Iterable[float]) -> float:
    rows = list(values)
    return sum(rows) / len(rows) if rows else 0.0


def _ndcg(
    ranked: list[str], judgments: Mapping[str, Mapping[str, Any]], k: int,
) -> float:
    gains = [judgments[item]["gain"] or 0 for item in ranked[:k]]
    dcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    ideal = sorted(
        (row["gain"] or 0 for row in judgments.values()), reverse=True
    )[:k]
    idcg = sum(gain / math.log2(index + 2) for index, gain in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def _validate_id_list(value: object, *, maximum: int, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or len(value) != len(set(value))
        or any(
            not isinstance(item, str)
            or not item.isdigit()
            or item.startswith("0")
            for item in value
        )
    ):
        raise ValueError(f"invalid {field}")
    return value


def _expected_java_catalog() -> dict[str, Any]:
    return {
        "attributeCount": 1547,
        "catalogVersion": JAVA_CATALOG_VERSION,
        "contentSha256": JAVA_CATALOG_CONTENT_SHA256,
        "javaProjectionSha256": JAVA_PROJECTION_SHA256,
        "productCount": 252,
        "resolveBatchCount": 26,
        "networkCallCount": 29,
        "businessWriteNetworkUsed": False,
    }


def _validate_run_identity(
    *, manifest: Mapping[str, Any], run_contract: Mapping[str, Any],
    prediction_sha256: str,
) -> dict[str, Any]:
    contract = run_contract.get("contract")
    if not isinstance(contract, dict):
        raise ValueError("run contract body is missing")
    contract_sha = hashlib.sha256(canonical_bytes(contract)).hexdigest()
    if (
        run_contract.get("schemaVersion") != RUN_CONTRACT_SCHEMA_VERSION
        or run_contract.get("contractSha256") != contract_sha
        or manifest.get("contractSha256") != contract_sha
        or manifest.get("contract") != contract
    ):
        raise ValueError("run contract/manifest identity mismatch")
    if manifest.get("schemaVersion") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("manifest schema mismatch")
    if manifest.get("selectedSplits") != ["dev"]:
        raise ValueError("v3 scorer only accepts public dev runs")
    if manifest.get("selectedCaseIds") != list(PUBLIC_DEV_CASE_IDS):
        raise ValueError("manifest public dev identity/order mismatch")
    execution = manifest.get("execution")
    if (
        not isinstance(execution, dict)
        or list(execution) != list(PUBLIC_DEV_CASE_IDS)
        or any(type(value) is not bool for value in execution.values())
    ):
        raise ValueError("manifest execution identity mismatch")
    succeeded = [case_id for case_id, ok in execution.items() if ok]
    failed = [case_id for case_id, ok in execution.items() if not ok]
    if (
        manifest.get("succeededCaseIds") != succeeded
        or manifest.get("failedCaseIds") != failed
    ):
        raise ValueError("manifest terminal case accounting mismatch")
    prediction = manifest.get("prediction")
    if (
        not isinstance(prediction, dict)
        or prediction.get("path") != "predictions.jsonl"
        or prediction.get("rowCount") != len(PUBLIC_DEV_CASE_IDS)
        or prediction.get("sha256") != prediction_sha256
    ):
        raise ValueError("manifest/prediction identity mismatch")
    if manifest.get("twoStagePredictionSchemaVersion") != PREDICTION_SCHEMA_VERSION:
        raise ValueError("two-stage prediction schema identity mismatch")
    if (
        manifest.get("hiddenArtifactsRead") is not False
        or manifest.get("businessWriteNetworkUsed") is not False
        or manifest.get("modelCallCount") != 0
        or manifest.get("modelNetworkCallCount") != 0
        or manifest.get("modelNetworkUsed") is not False
    ):
        raise ValueError("v3 run exceeded the hidden/write/no-model boundary")

    expected_contract = {
        "allowSealedTest": False,
        "attributeContractCodeSha256": ATTRIBUTE_CONTRACT_CODE_SHA256,
        "attributeRuleset": ATTRIBUTE_RULESET_VERSION,
        "caseCount": 30,
        "javaCatalog": _expected_java_catalog(),
        "model": NO_MODEL_NAME,
        "modelEndpoint": NO_MODEL_ENDPOINT,
        "productionCodeScopeSha256": PRODUCTION_CODE_SCOPE_SHA256,
        "productionRuntimeConfig": PRODUCTION_RUNTIME_CONFIG,
        "protocolVersion": PROTOCOL_VERSION,
        "publicCasesSha256": PUBLIC_CASES_SHA256,
        "publicCatalogSha256": PUBLIC_CATALOG_SHA256,
        "runnerCodeSha256": _code_hashes(RUNNER_FILES),
        "selectedCaseIds": list(PUBLIC_DEV_CASE_IDS),
        "selectedSplits": ["dev"],
    }
    if set(contract) != {*expected_contract, "timeoutSeconds"}:
        raise ValueError("run contract fields mismatch")
    timeout = contract.get("timeoutSeconds")
    if type(timeout) not in {int, float} or timeout <= 0:
        raise ValueError("run timeout contract mismatch")
    if {key: contract.get(key) for key in expected_contract} != expected_contract:
        raise ValueError("run protocol/code/data contract mismatch")
    return contract


def _authenticate_prediction_before_labels(
    *, predictions_path: Path, manifest_path: Path,
) -> tuple[
    list[dict[str, Any]], str, str, str,
]:
    resolved_manifest = manifest_path.resolve()
    resolved_predictions = predictions_path.resolve()
    if resolved_manifest.name != "manifest.json":
        raise ValueError("manifest path must end in manifest.json")
    expected_predictions = (resolved_manifest.parent / "predictions.jsonl").resolve()
    if resolved_predictions != expected_predictions:
        raise ValueError("predictions path must be the manifest-bound predictions.jsonl")
    run_contract_path = resolved_manifest.parent / "run_contract.json"

    manifest, manifest_sha256 = _load_canonical_json_snapshot(
        resolved_manifest, document="manifest"
    )
    run_contract, run_contract_sha256 = _load_canonical_json_snapshot(
        run_contract_path, document="run contract"
    )
    predictions, prediction_sha256 = _load_canonical_prediction_snapshot(
        resolved_predictions
    )
    _validate_run_identity(
        manifest=manifest,
        run_contract=run_contract,
        prediction_sha256=prediction_sha256,
    )

    case_ids = [row.get("caseId") for row in predictions]
    if tuple(case_ids) != PUBLIC_DEV_CASE_IDS:
        raise ValueError("public dev prediction identity/order mismatch")
    schema = _load_json(PREDICTION_SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    prediction_validator = Draft202012Validator(schema)
    for prediction in predictions:
        prediction_validator.validate(prediction)
        if prediction.get("rankingContractVersion") != RANKING_CONTRACT_VERSION:
            raise ValueError("ranking contract version mismatch")
        pool = _validate_id_list(
            prediction.get("candidatePoolIds"),
            maximum=50,
            field="candidatePoolIds",
        )
        ranked = _validate_id_list(
            prediction.get("rankedItemIds"), maximum=20, field="rankedItemIds"
        )
        if not set(ranked).issubset(pool):
            raise ValueError("rankedItemIds outside candidatePoolIds")
        seen_citations: set[tuple[Any, ...]] = set()
        for citation in prediction.get("evidenceCitations", []):
            identity = tuple(
                citation.get(key)
                for key in (
                    "itemId", "group", "source", "field", "lineNumber", "rawValue"
                )
            )
            if identity in seen_citations:
                raise ValueError("duplicate evidence citation")
            seen_citations.add(identity)
            if citation.get("itemId") not in ranked:
                raise ValueError("evidence citation outside rankedItemIds")
    return (
        predictions,
        prediction_sha256,
        manifest_sha256,
        run_contract_sha256,
    )


def score_public_dev(
    *, predictions_path: Path, manifest_path: Path,
    public_dev_judgments_path: Path,
    public_dev_preregistration_path: Path,
) -> dict[str, Any]:
    (
        predictions,
        prediction_sha256,
        manifest_sha256,
        run_contract_sha256,
    ) = _authenticate_prediction_before_labels(
        predictions_path=predictions_path,
        manifest_path=manifest_path,
    )
    _validate_public_dev_preregistration(public_dev_preregistration_path)
    judgments_by_case = _load_public_dev_judgments(public_dev_judgments_path)
    ranking_rows = []
    citation_correct = citation_total = 0
    for prediction in predictions:
        case_id = prediction["caseId"]
        judgments = judgments_by_case[case_id]
        pool = _validate_id_list(
            prediction.get("candidatePoolIds"), maximum=50, field="candidatePoolIds"
        )
        ranked = _validate_id_list(
            prediction.get("rankedItemIds"), maximum=20, field="rankedItemIds"
        )
        if any(item not in judgments for item in [*pool, *ranked]):
            raise ValueError("prediction item outside judgment universe")
        positives = {
            item for item, row in judgments.items() if row["eligible"] is True
        }
        pool_recall = len(set(pool) & positives) / len(positives)
        final_recall = len(set(ranked) & positives) / len(positives)
        ranking_rows.append({
            "candidatePoolRecallAt50": pool_recall,
            "caseId": case_id,
            "finalRecallAt20": final_recall,
            "ndcgAt10": _ndcg(ranked, judgments, 10),
            "rerankingRecallDelta": final_recall - pool_recall,
            "top10HardViolationRate": (
                sum(
                    judgments[item]["judgmentStratum"] == "hard_fail"
                    for item in ranked[:10]
                ) / 10
            ),
        })
        valid_citations = {
            (
                item_id,
                check["group"],
                ref["source"],
                ref["field"],
                ref["lineNumber"],
                ref["rawValue"],
            )
            for item_id, row in judgments.items()
            for check in row["checks"]
            for ref in check["evidenceRefs"]
        }
        for citation in prediction.get("evidenceCitations", []):
            identity = tuple(
                citation.get(key)
                for key in (
                    "itemId", "group", "source", "field", "lineNumber", "rawValue"
                )
            )
            citation_total += 1
            citation_correct += identity in valid_citations

    metrics = {
        "candidatePoolRecallAt50": _mean(
            row["candidatePoolRecallAt50"] for row in ranking_rows
        ),
        "citationAccuracy": citation_correct / citation_total if citation_total else None,
        "citationCount": citation_total,
        "finalRecallAt20": _mean(row["finalRecallAt20"] for row in ranking_rows),
        "ndcgAt10": _mean(row["ndcgAt10"] for row in ranking_rows),
        "rerankingRecallDelta": _mean(
            row["rerankingRecallDelta"] for row in ranking_rows
        ),
        "top10HardViolationRate": _mean(
            row["top10HardViolationRate"] for row in ranking_rows
        ),
    }
    return {
        "evaluatorIdentity": {
            "evaluatorCodeSha256": _code_hashes(EVALUATOR_FILES),
            "evaluatorIdentityVersion": EVALUATOR_IDENTITY_VERSION,
            "publicDevJudgmentsSha256": PUBLIC_DEV_JUDGMENTS_SHA256,
            "publicDevPreregistrationSha256": PUBLIC_DEV_PREREGISTRATION_SHA256,
            "predictionSchemaVersion": PREDICTION_SCHEMA_VERSION,
            "productionCodeScopeSha256": PRODUCTION_CODE_SCOPE_SHA256,
            "protocolVersion": PROTOCOL_VERSION,
            "rankingContractVersion": RANKING_CONTRACT_VERSION,
        },
        "inputs": {
            "manifestSha256": manifest_sha256,
            "predictionSha256": prediction_sha256,
            "publicDevJudgmentsSha256": PUBLIC_DEV_JUDGMENTS_SHA256,
            "publicDevPreregistrationSha256": PUBLIC_DEV_PREREGISTRATION_SHA256,
            "runContractSha256": run_contract_sha256,
        },
        "labelBoundary": "AI-designed product-grounded, not human gold",
        "metricDefinitions": {
            "top10HardViolationRate": TOP10_HARD_VIOLATION_RATE_DEFINITION,
        },
        "metrics": metrics,
        "perCase": ranking_rows,
        "schemaVersion": SCHEMA_VERSION,
        "split": "dev",
    }
