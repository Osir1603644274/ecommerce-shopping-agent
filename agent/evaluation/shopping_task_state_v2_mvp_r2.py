"""Validator for the human-adjudicated Shopping Task State V2 MVP r2."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent.evaluation import shopping_task_state_v2_mvp as frozen_v1
from agent.evaluation.shopping_task_state_v2_public import load_scenarios


ROOT = Path(__file__).resolve().parents[2]
DATASET = Path(__file__).resolve().parent / "assets" / "shopping_task_state_v2_mvp_r2_20260822"
PUBLIC_PATH = DATASET / "public" / "scenarios.jsonl"
PRIVATE_PATH = DATASET / "private" / "state_oracle.private.jsonl"
MANIFEST_PATH = DATASET / "manifest.json"
REVIEW_PATH = DATASET / "reviews" / "human_colloquial_adjudication_attempt001.json"
EXPECTED_ARTIFACT_RECORD_COUNTS = {
    "public/scenarios.jsonl": 14,
    "private/state_oracle.private.jsonl": 14,
    "reviews/human_colloquial_adjudication_attempt001.json": 12,
    "README.md": None,
    "reviews/ai_blind_language_review_attempt001.json": None,
    "reviews/human_language_review_summary_attempt001.json": None,
}
EXPECTED_MANIFEST_KEYS = {
    "datasetId", "schemaVersion", "status", "authoringMethod", "language", "scenarioCount", "turnCount", "sessionCount",
    "humanLanguageReviewStatus", "productOracleStatus", "strategyRunStatus", "baseDataset", "humanReviewEvidence", "artifacts", "schemas",
}
EXPECTED_SCHEMA_PATHS = {
    "agent/evaluation/schemas/shopping_task_state_public_v2.schema.json",
    "agent/evaluation/schemas/shopping_task_state_oracle_private_v2.schema.json",
    "agent/evaluation/schemas/shopping_task_state_prediction_v2.schema.json",
    "agent/evaluation/schemas/shopping_task_state_runner_receipt_v2.schema.json",
    "agent/evaluation/schemas/shopping_task_state_score_report_v2.schema.json",
}
EXPECTED_MANIFEST_SCHEMA_VERSION = "shopping-task-state-v2-mvp-manifest-v1"
EXPECTED_AUTHORING_METHOD = "ai_assisted_source_with_human_targeted_colloquial_adjudication"
EXPECTED_REVIEW_SHA256 = "5f1dc277ef6f9e0733009d9eb8c316f83fb4ce4b9547cbce05003737a2a52b0c"
EXPECTED_FROZEN_PUBLIC_SHA256 = "07910fd7212257f386d77c616cf5bb72765615022c0b18c28b8387886efab4fc"
EXPECTED_FROZEN_PRIVATE_SHA256 = "c8ec63600766cfc913e612c2500b5fb03ed5b8418445099788f5ddf2e87bfdbd"
EXPECTED_BASE_DATASET_KEYS = {
    "datasetId", "sourceVersion", "publicSha256", "privateOracleSha256",
    "scenarioCount", "turnCount", "sessionCount",
}
EXPECTED_BASE_DATASET = {
    "datasetId": "shopping-task-state-v2-mvp-20260822",
    "sourceVersion": "shopping-task-state-v2-mvp-20260822",
    "scenarioCount": 14,
    "turnCount": 56,
    "sessionCount": 18,
}
EXPECTED_HUMAN_REVIEW_KEYS = {
    "reviewId", "reviewType", "sourceVersion", "completedAt", "path",
    "decisionCount", "decision", "sha256",
}
EXPECTED_HUMAN_REVIEW = {
    "reviewId": "sts-v2-mvp-human-colloquial-adjudication-attempt001",
    "reviewType": "HUMAN_TARGETED_COLLOQUIAL_ADJUDICATION",
    "sourceVersion": "shopping-task-state-v2-mvp-20260822",
    "completedAt": "2026-08-22T08:30:08.234Z",
    "decisionCount": 12,
    "decision": "APPLY_ALL_PROPOSED_REWRITES_IN_NEW_VERSION",
}
EXPECTED_ARTIFACTS = {
    "public/scenarios.jsonl": (14, "public_input", "public_scenarios"),
    "private/state_oracle.private.jsonl": (14, "scorer_private_oracle", "state_oracle"),
    "reviews/human_colloquial_adjudication_attempt001.json": (12, "human_review_evidence", "human_colloquial_adjudication"),
    "README.md": (None, "dataset_readme", "r2_readme"),
    "reviews/ai_blind_language_review_attempt001.json": (None, "review_context", "ai_blind_language_review"),
    "reviews/human_language_review_summary_attempt001.json": (None, "review_context", "human_language_review_summary"),
}
EXPECTED_ARTIFACT_KEYS = {"path", "recordCount", "byteLength", "sha256", "role", "name"}
EXPECTED_SCHEMAS = {
    "agent/evaluation/schemas/shopping_task_state_public_v2.schema.json": ("public_schema", "public_contract"),
    "agent/evaluation/schemas/shopping_task_state_oracle_private_v2.schema.json": ("private_schema", "oracle_contract"),
    "agent/evaluation/schemas/shopping_task_state_prediction_v2.schema.json": ("prediction_schema", "prediction_contract"),
    "agent/evaluation/schemas/shopping_task_state_runner_receipt_v2.schema.json": ("receipt_schema", "runner_receipt_contract"),
    "agent/evaluation/schemas/shopping_task_state_score_report_v2.schema.json": ("report_schema", "score_report_contract"),
}
EXPECTED_SCHEMA_KEYS = {"path", "sha256", "role", "name"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _turn_index(rows: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    return {
        (scenario["scenarioId"], turn["turnId"]): turn["text"]
        for scenario in rows
        for turn in scenario["turns"]
    }


def _require_exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label} key closure mismatch")
    return value


def validate_human_adjudication() -> dict[str, Any]:
    """Prove that r2 applies exactly the recorded human language decisions."""

    if _sha256(REVIEW_PATH) != EXPECTED_REVIEW_SHA256:
        raise ValueError("human review bytes are not the frozen adjudication evidence")

    frozen_public, _ = frozen_v1.load_and_validate()
    revised_public = load_scenarios(PUBLIC_PATH)
    frozen_turns = _turn_index(frozen_public)
    revised_turns = _turn_index(revised_public)
    if set(frozen_turns) != set(revised_turns):
        raise ValueError("r2 changed scenario or turn identity")

    review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
    if any(review.get(key) != EXPECTED_HUMAN_REVIEW[key] for key in ("reviewId", "reviewType", "sourceVersion", "completedAt")):
        raise ValueError("unexpected human review type")
    if review.get("summary", {}).get("decision") != EXPECTED_HUMAN_REVIEW["decision"]:
        raise ValueError("human review decision is not the frozen legal decision")
    results = review.get("results", [])
    if review.get("resultCount") != 12 or len(results) != 12:
        raise ValueError("human adjudication must contain exactly 12 decisions")

    reviewed_keys: set[tuple[str, str]] = set()
    for result in results:
        key = (result["scenarioId"], result["turnId"])
        if key in reviewed_keys or key not in frozen_turns:
            raise ValueError(f"invalid or duplicate human decision: {key}")
        reviewed_keys.add(key)
        if result.get("decision") != "REWRITE":
            raise ValueError(f"unsupported decision in r2: {key}")
        if frozen_turns[key] != result.get("original"):
            raise ValueError(f"review original does not bind frozen public text: {key}")
        if revised_turns[key] != result.get("proposedRewrite"):
            raise ValueError(f"r2 does not apply the accepted rewrite: {key}")

    changed_keys = {
        key for key, frozen_text in frozen_turns.items()
        if revised_turns[key] != frozen_text
    }
    if changed_keys != reviewed_keys:
        raise ValueError("r2 public changes are not exactly the human-adjudicated set")
    return review


def validate_private_oracle_identity() -> None:
    """Fail closed unless the r2 private oracle is byte-identical to frozen v1."""

    if _sha256(frozen_v1.PUBLIC_PATH) != EXPECTED_FROZEN_PUBLIC_SHA256 or _sha256(frozen_v1.PRIVATE_PATH) != EXPECTED_FROZEN_PRIVATE_SHA256:
        raise ValueError("frozen v1 source bytes are not the audited identity")
    if _sha256(PRIVATE_PATH) != EXPECTED_FROZEN_PRIVATE_SHA256 or PRIVATE_PATH.read_bytes() != frozen_v1.PRIVATE_PATH.read_bytes():
        raise ValueError("r2 private oracle differs from frozen v1")


def validate_manifest() -> dict[str, Any]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if set(manifest) != EXPECTED_MANIFEST_KEYS:
        raise ValueError("r2 manifest top-level key closure mismatch")
    if manifest.get("datasetId") != "shopping-task-state-v2-mvp-r2-20260822":
        raise ValueError("unexpected r2 datasetId")
    if manifest.get("schemaVersion") != EXPECTED_MANIFEST_SCHEMA_VERSION:
        raise ValueError("unexpected r2 manifest schemaVersion")
    if manifest.get("authoringMethod") != EXPECTED_AUTHORING_METHOD:
        raise ValueError("r2 authoringMethod is not the frozen process")
    if manifest.get("status") != "READY_FOR_HIGH_REVIEW":
        raise ValueError("r2 must remain pending independent high review")
    if manifest.get("humanLanguageReviewStatus") != "TARGETED_ADJUDICATION_APPLIED":
        raise ValueError("r2 human language status is not bound")
    if manifest.get("strategyRunStatus") != "NOT_RUN":
        raise ValueError("r2 cannot claim a strategy run")
    if manifest.get("productOracleStatus") != "NOT_BOUND" or manifest.get("language") != "zh-CN":
        raise ValueError("r2 manifest fixed semantic status/language mismatch")
    public_rows = load_scenarios(PUBLIC_PATH)
    if (manifest.get("scenarioCount"), manifest.get("turnCount"), manifest.get("sessionCount")) != (
        len(public_rows), sum(len(row["turns"]) for row in public_rows), len({turn["sessionId"] for row in public_rows for turn in row["turns"]}),
    ):
        raise ValueError("r2 manifest public-derived counts mismatch")

    base = _require_exact_keys(manifest.get("baseDataset"), EXPECTED_BASE_DATASET_KEYS, "baseDataset")
    if any(base.get(key) != value for key, value in EXPECTED_BASE_DATASET.items()):
        raise ValueError("baseDataset metadata is not the frozen source binding")
    frozen_manifest = json.loads(frozen_v1.MANIFEST_PATH.read_text(encoding="utf-8"))
    if frozen_manifest.get("datasetId") != base["datasetId"]:
        raise ValueError("baseDataset datasetId drift")
    if frozen_manifest.get("authoringMethod") != "sentence_by_sentence_ai_assisted_no_template_renderer":
        raise ValueError("unexpected frozen source authoring method")
    frozen_public_rows, frozen_private_rows = frozen_v1.load_and_validate()
    frozen_counts = (
        len(frozen_public_rows),
        sum(len(row["turns"]) for row in frozen_public_rows),
        len({turn["sessionId"] for row in frozen_public_rows for turn in row["turns"]}),
    )
    if (base["scenarioCount"], base["turnCount"], base["sessionCount"]) != frozen_counts:
        raise ValueError("baseDataset counts are not bound to frozen public data")
    if len(frozen_private_rows) != base["scenarioCount"]:
        raise ValueError("baseDataset private count is not bound to frozen oracle")
    if base.get("publicSha256") != EXPECTED_FROZEN_PUBLIC_SHA256 or base.get("publicSha256") != _sha256(frozen_v1.PUBLIC_PATH):
        raise ValueError("base public hash mismatch")
    if base.get("privateOracleSha256") != EXPECTED_FROZEN_PRIVATE_SHA256 or base.get("privateOracleSha256") != _sha256(frozen_v1.PRIVATE_PATH):
        raise ValueError("base private oracle hash mismatch")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("r2 artifacts must be a list")
    for index, artifact in enumerate(artifacts):
        _require_exact_keys(artifact, EXPECTED_ARTIFACT_KEYS, f"artifact[{index}]")
    artifact_paths = [artifact.get("path") for artifact in artifacts]
    if len(artifact_paths) != len(set(artifact_paths)):
        raise ValueError("duplicate r2 artifact path")
    if set(artifact_paths) != set(EXPECTED_ARTIFACT_RECORD_COUNTS):
        raise ValueError("r2 artifact path closure mismatch")
    disk_paths = {
        path.relative_to(DATASET).as_posix()
        for path in DATASET.rglob("*") if path.is_file() and path.relative_to(DATASET).as_posix() != "manifest.json"
    }
    if disk_paths != set(EXPECTED_ARTIFACT_RECORD_COUNTS):
        raise ValueError("r2 on-disk artifact closure mismatch")
    for artifact in artifacts:
        expected_count, expected_role, expected_name = EXPECTED_ARTIFACTS[artifact["path"]]
        if artifact["recordCount"] != expected_count or artifact["role"] != expected_role or artifact["name"] != expected_name:
            raise ValueError(f"artifact metadata binding mismatch: {artifact['path']}")
        path = DATASET / artifact["path"]
        if not path.is_file():
            raise ValueError(f"missing artifact: {artifact['path']}")
        payload = path.read_bytes()
        if len(payload) != artifact["byteLength"]:
            raise ValueError(f"byte length mismatch: {artifact['path']}")
        if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
            raise ValueError(f"SHA-256 mismatch: {artifact['path']}")
        if artifact["path"] == str(REVIEW_PATH.relative_to(DATASET)).replace("\\", "/"):
            records = json.loads(payload.decode("utf-8")).get("resultCount")
        elif EXPECTED_ARTIFACT_RECORD_COUNTS[artifact["path"]] is None:
            records = None
        else:
            records = sum(1 for line in payload.decode("utf-8").splitlines() if line.strip())
        if records != artifact["recordCount"]:
            raise ValueError(f"record count mismatch: {artifact['path']}")
        if records != EXPECTED_ARTIFACT_RECORD_COUNTS[artifact["path"]]:
            raise ValueError(f"unexpected record count: {artifact['path']}")

    evidence = _require_exact_keys(manifest.get("humanReviewEvidence"), EXPECTED_HUMAN_REVIEW_KEYS, "humanReviewEvidence")
    review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
    for key in ("reviewId", "reviewType", "sourceVersion", "completedAt"):
        expected = EXPECTED_HUMAN_REVIEW[key]
        if evidence.get(key) != expected or review.get(key) != expected:
            raise ValueError(f"human review evidence {key} mismatch")
    if evidence.get("path") != str(REVIEW_PATH.relative_to(DATASET)).replace("\\", "/"):
        raise ValueError("human review evidence path mismatch")
    if evidence.get("decisionCount") != review.get("resultCount") or evidence.get("decision") != review.get("summary", {}).get("decision"):
        raise ValueError("human review decision binding mismatch")
    if evidence.get("sha256") != EXPECTED_REVIEW_SHA256 or evidence.get("sha256") != _sha256(REVIEW_PATH):
        raise ValueError("human review evidence binding mismatch")

    schemas = manifest.get("schemas")
    if not isinstance(schemas, list):
        raise ValueError("r2 schemas must be a list")
    for index, schema in enumerate(schemas):
        _require_exact_keys(schema, EXPECTED_SCHEMA_KEYS, f"schema[{index}]")
    schema_paths = [schema.get("path") for schema in schemas]
    if len(schema_paths) != len(set(schema_paths)):
        raise ValueError("duplicate r2 schema path")
    if set(schema_paths) != EXPECTED_SCHEMA_PATHS:
        raise ValueError("r2 schema path closure mismatch")
    for schema in schemas:
        expected_role, expected_name = EXPECTED_SCHEMAS[schema["path"]]
        if schema["role"] != expected_role or schema["name"] != expected_name:
            raise ValueError(f"schema metadata binding mismatch: {schema['path']}")
        path = ROOT / schema["path"]
        if not path.is_file():
            raise ValueError(f"missing schema: {schema['path']}")
        if _sha256(path) != schema["sha256"]:
            raise ValueError(f"schema SHA-256 mismatch: {schema['path']}")
    return manifest


def load_and_validate() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate r2 plus its immutable semantic inheritance from frozen v1."""

    revised_public = load_scenarios(PUBLIC_PATH)
    frozen_public, frozen_private = frozen_v1.load_and_validate()
    if len(revised_public) != len(frozen_public) or len(revised_public) != 14:
        raise ValueError("r2 scenario count mismatch")
    if sum(len(row["turns"]) for row in revised_public) != 56:
        raise ValueError("r2 turn count mismatch")
    validate_human_adjudication()
    validate_private_oracle_identity()
    validate_manifest()
    return revised_public, frozen_private


def dataset_summary() -> dict[str, Any]:
    public_rows, private_rows = load_and_validate()
    return {
        "scenarioCount": len(public_rows),
        "turnCount": sum(len(row["turns"]) for row in public_rows),
        "sessionCount": len({
            turn["sessionId"] for row in public_rows for turn in row["turns"]
        }),
        "humanRewriteCount": 12,
        "privateOracleSha256": _sha256(PRIVATE_PATH),
        "coverageTags": sorted({tag for row in private_rows for tag in row["coverageTags"]}),
    }
