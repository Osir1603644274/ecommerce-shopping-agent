"""One-shot external Shopping Memory V13.7 builder and aggregate scorer.

This source contains no oracle rows, product identifiers, labels, or answers.
The official test file and all derived hidden rows must remain under an
authority-owned temporary directory outside the repository.  The only allowed
repository output of a quality run is one aggregate receipt.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# These imports are deliberate contract bindings.  The helper normalization
# and the development ranking/statistics implementation must not be copied or
# independently reimplemented here.
from agent.evaluation.build_shopping_memory_v13_dataset import (  # noqa: E402
    attribute_key,
    ground_truth,
    scenario,
    slug,
)
from agent.evaluation.shopping_memory_v13_single_product_dev_v1 import (  # noqa: E402
    CANDIDATE_DEPTH,
    DeterministicBM25,
    build_documents,
    catalog_value_set,
    cluster_bootstrap,
    eligibility,
    mean_metrics,
    ranking_metrics,
    rerank,
    zero_metrics,
)


SCHEMA_VERSION = "shopping-memory-v13-external-confirmation-aggregate-v1"
CONTRACT_VERSION = "shopping-memory-contract-v13.7"
DATASET_ID = "yuzhan2205/Shopping-companion"
DATASET_REVISION = "9a8a2a1c13f0d88de070238352bcf71f98ca851f"
DATASET_FILE = "data/test.parquet"
OFFICIAL_TEST_SHA256 = "65cb4e36a182b52fc7e671770d8601e4c6cca6ecb0bccd2efb62cdea8f600a8d"
EXPECTED_AUTHORITY_DATASET_ROWS = 400
EXPECTED_HIDDEN_TARGET_ROWS = 367
EXPECTED_PUBLIC_TRAIN_ROWS = 1449
EXPECTED_INTERSECTION_ROWS = 0
EXPECTED_UNION_ROWS = 1816
EXPECTED_ALL_SINGLE_PRODUCT = 100
EXPECTED_DISTINCT_CORPUS_HASH_COUNT = 1
FIXED_LAMBDA = 0.08
INTERVAL_LABEL = "CONFIRMATORY_EXTERNAL_SEALED"
MAXIMUM_EXTERNAL_VERDICT = "TRANSDUCTIVE_RERANKING_CONFIRMATION_ACCEPT"
HOLD_VERDICT = "HOLD_EXTERNAL_CONFIRMATION"

AUTHORITY_DIR = Path(__file__).resolve().parent
PRE_RUN_RECEIPT_PATH = AUTHORITY_DIR / "pre-run-receipt.json"
STARTED_MARKER_PATH = AUTHORITY_DIR / "quality-execution-started.json"
AGGREGATE_RECEIPT_PATH = AUTHORITY_DIR / "aggregate-receipt.json"
PUBLIC_TRAIN_PATH = (
    ROOT / "agent" / "evaluation" / "assets" /
    "shopping_memory_v13_20260830" / "train-target-catalog.jsonl"
)
CATALOG_VALUES_PATH = PUBLIC_TRAIN_PATH.with_name("catalog-values.jsonl")

DEPENDENCY_PATHS = {
    "source": Path(__file__).resolve(),
    "datasetBuilder": ROOT / "agent" / "evaluation" / "build_shopping_memory_v13_dataset.py",
    "developmentRunner": ROOT / "agent" / "evaluation" / "shopping_memory_v13_single_product_dev_v1.py",
    "datasetManifest": PUBLIC_TRAIN_PATH.with_name("manifest.json"),
    "publicTrainCorpus": PUBLIC_TRAIN_PATH,
    "catalogValues": CATALOG_VALUES_PATH,
    "preregistration": ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" / "PREREGISTRATION.md",
    **{
        f"amendment{index:03d}": (
            ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" /
            f"PREREGISTRATION_AMENDMENT_{index:03d}.md"
        )
        for index in range(1, 9)
    },
    "contractV13": ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" / "contract-v13.json",
    **{
        f"contractV13_{index}": (
            ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" /
            f"contract-v13.{index}.json"
        )
        for index in range(1, 8)
    },
    "externalConfirmationHandoff": (
        ROOT / "docs" / "experiments" / "shopping-memory-v13-2026-08-29" /
        "EXTERNAL_CONFIRMATION_HANDOFF.md"
    ),
}

BYTE_ONCE_DEPENDENCIES = frozenset({"publicTrainCorpus", "catalogValues"})


class AuthorityError(RuntimeError):
    """Fail-closed error with a non-sensitive aggregate code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _jsonl_bytes(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return b"".join(_canonical(row) + b"\n" for row in rows)


def _read_jsonl_bytes(payload: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in payload.splitlines():
        if not raw.strip():
            raise AuthorityError("BLANK_PUBLIC_JSONL_LINE")
        value = json.loads(raw)
        if type(value) is not dict:
            raise AuthorityError("INVALID_PUBLIC_JSONL_OBJECT")
        rows.append(value)
    return rows


def _dependency_snapshot(
    byte_once_inputs: Mapping[str, bytes],
) -> tuple[dict[str, str], dict[str, bytes]]:
    hashes: dict[str, str] = {}
    payloads: dict[str, bytes] = {}
    if frozenset(byte_once_inputs) != BYTE_ONCE_DEPENDENCIES:
        raise AuthorityError("BYTE_ONCE_DEPENDENCY_SET_INVALID")
    for name, path in DEPENDENCY_PATHS.items():
        if name in BYTE_ONCE_DEPENDENCIES:
            payload = byte_once_inputs[name]
        else:
            if not path.is_file():
                raise AuthorityError("PRE_RUN_DEPENDENCY_MISSING")
            payload = path.read_bytes()
        payloads[name] = payload
        hashes[name] = _sha256_bytes(payload)
    return hashes, payloads


def _post_dependency_hashes(
    byte_once_inputs: Mapping[str, bytes],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name, path in DEPENDENCY_PATHS.items():
        if name in BYTE_ONCE_DEPENDENCIES:
            payload = byte_once_inputs[name]
        else:
            if not path.is_file():
                raise AuthorityError("POST_RUN_DEPENDENCY_MISSING")
            payload = path.read_bytes()
        hashes[name] = _sha256_bytes(payload)
    return hashes


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_paths(official_test_path: Path) -> Path:
    test_path = official_test_path.resolve()
    temp_dir = test_path.parent
    if _is_within(temp_dir, ROOT) or not temp_dir.is_dir():
        raise AuthorityError("AUTHORITY_TEMP_DIR_NOT_EXTERNAL")
    if not _is_within(test_path, temp_dir) or not test_path.is_file():
        raise AuthorityError("OFFICIAL_TEST_NOT_IN_AUTHORITY_TEMP_DIR")
    if AGGREGATE_RECEIPT_PATH.exists():
        raise AuthorityError("AGGREGATE_RECEIPT_ALREADY_EXISTS")
    return test_path


def _validate_pre_run_receipt() -> tuple[dict[str, Any], str]:
    if not PRE_RUN_RECEIPT_PATH.is_file():
        raise AuthorityError("PRE_RUN_RECEIPT_MISSING")
    receipt_bytes = PRE_RUN_RECEIPT_PATH.read_bytes()
    receipt = json.loads(receipt_bytes)
    if type(receipt) is not dict:
        raise AuthorityError("PRE_RUN_RECEIPT_INVALID")
    expected_official = {
        "dataset": DATASET_ID,
        "revision": DATASET_REVISION,
        "file": DATASET_FILE,
        "sha256": OFFICIAL_TEST_SHA256,
        "rowCount": EXPECTED_AUTHORITY_DATASET_ROWS,
    }
    expected_constants = {
        "hiddenTargetRows": EXPECTED_HIDDEN_TARGET_ROWS,
        "publicTrainRows": EXPECTED_PUBLIC_TRAIN_ROWS,
        "intersectionRows": EXPECTED_INTERSECTION_ROWS,
        "unionRows": EXPECTED_UNION_ROWS,
        "allSingleProduct": EXPECTED_ALL_SINGLE_PRODUCT,
        "expectedDistinctCorpusHashCount": EXPECTED_DISTINCT_CORPUS_HASH_COUNT,
        "fixedLambda": FIXED_LAMBDA,
    }
    dependency_hashes = receipt.get("dependencySha256")
    if type(dependency_hashes) is not dict:
        raise AuthorityError("PRE_RUN_DEPENDENCY_IDENTITY_INVALID")
    source_identity = dependency_hashes.get("source")
    validations = (
        receipt.get("schemaVersion") == "shopping-memory-v13-external-confirmation-pre-run-receipt-v1",
        receipt.get("status") == "FROZEN_PRE_RUN_AWAITING_INDEPENDENT_REVIEW",
        receipt.get("contractVersion") == CONTRACT_VERSION,
        receipt.get("officialDataset") == expected_official,
        receipt.get("frozenStructuralConstants") == expected_constants,
        receipt.get("builderSha256") == source_identity,
        receipt.get("scorerSha256") == source_identity,
        dependency_hashes.get("contractV13_7") is not None,
        receipt.get("qualityExecutionCount") == 0,
        receipt.get("qualityExecutionStarted") is False,
        receipt.get("qualityExecutionCompleted") is False,
        receipt.get("officialTestStructuralReadCount") == 1,
        receipt.get("sourceContainsOracleOrPerCaseData") is False,
        receipt.get("hiddenCorpusWrittenToWorkspace") is False,
        receipt.get("perCaseDataWrittenToWorkspace") is False,
    )
    if not all(validations):
        raise AuthorityError("PRE_RUN_TOP_LEVEL_IDENTITY_MISMATCH")
    return receipt, _sha256_bytes(receipt_bytes)


def _validate_contract_v13_7(payload: bytes) -> None:
    contract = json.loads(payload)
    if type(contract) is not dict:
        raise AuthorityError("CONTRACT_V13_7_INVALID")
    expected_hidden = {
        "type": "all_official_test_targets_union_public_train_catalog",
        "hiddenTargetRows": EXPECTED_HIDDEN_TARGET_ROWS,
        "publicTrainRows": EXPECTED_PUBLIC_TRAIN_ROWS,
        "intersectionRows": EXPECTED_INTERSECTION_ROWS,
        "unionRows": EXPECTED_UNION_ROWS,
        "queryConditionedMutationAllowed": False,
        "expectedDistinctCorpusHashCount": EXPECTED_DISTINCT_CORPUS_HASH_COUNT,
    }
    validations = (
        contract.get("schemaVersion") == CONTRACT_VERSION,
        contract.get("status") == "FROZEN_BEFORE_EXTERNAL_QUALITY_EXECUTION",
        contract.get("officialTestSha256") == OFFICIAL_TEST_SHA256,
        contract.get("hiddenCorpus") == expected_hidden,
        contract.get("fixedLambda") == FIXED_LAMBDA,
        contract.get("intervalLabel") == INTERVAL_LABEL,
        contract.get("maximumExternalVerdict") == MAXIMUM_EXTERNAL_VERDICT,
        contract.get("globalDefault") == "OFF",
    )
    if not all(validations):
        raise AuthorityError("CONTRACT_V13_7_CONSTANT_MISMATCH")


def _project_product(raw: Mapping[str, Any]) -> dict[str, Any]:
    required = ("product_id", "product_name", "price", "category", "aspects")
    if any(key not in raw for key in required):
        raise AuthorityError("HIDDEN_PRODUCT_REQUIRED_FIELD_MISSING")
    if type(raw["aspects"]) is not list:
        raise AuthorityError("HIDDEN_PRODUCT_ASPECTS_INVALID")
    aspects: list[dict[str, str]] = []
    for pair in raw["aspects"]:
        if type(pair) is not list or len(pair) != 2:
            raise AuthorityError("HIDDEN_PRODUCT_ASPECT_PAIR_INVALID")
        key, value = pair
        if type(key) is not str or type(value) is not str or not key or not value:
            raise AuthorityError("HIDDEN_PRODUCT_ASPECT_VALUE_INVALID")
        aspects.append({
            "attributeKey": attribute_key(key),
            "normalizedValue": slug(value, limit=128),
            "displayValue": str(value),
        })
    product_id = raw["product_id"]
    product_name = raw["product_name"]
    category = raw["category"]
    if product_id is None or product_name is None or category is None:
        raise AuthorityError("HIDDEN_PRODUCT_IDENTITY_INVALID")
    return {
        "productId": str(product_id),
        "productName": str(product_name),
        "price": raw["price"],
        "categoryId": slug(category, limit=64),
        "aspects": aspects,
    }


def _targets_from_truth(value: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if "product_id" in value:
        return [value]
    preferences = value.get("preferences")
    if type(preferences) is not list or not preferences:
        raise AuthorityError("GROUND_TRUTH_TARGETS_EMPTY")
    if any(type(item) is not dict for item in preferences):
        raise AuthorityError("GROUND_TRUTH_TARGET_INVALID")
    return preferences


def _build_hidden_and_union(
    dataframe: pd.DataFrame,
    public_train: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    hidden_by_id: dict[str, dict[str, Any]] = {}
    hidden_bytes_by_id: dict[str, bytes] = {}
    hidden_duplicate_count = 0
    collision_count = 0
    for _, row in dataframe.iterrows():
        truth = ground_truth(row)
        for raw_product in _targets_from_truth(truth):
            product = _project_product(raw_product)
            product_id = product["productId"]
            product_bytes = _canonical(product)
            previous = hidden_bytes_by_id.get(product_id)
            if previous is not None:
                hidden_duplicate_count += 1
                if previous != product_bytes:
                    collision_count += 1
            else:
                hidden_by_id[product_id] = product
                hidden_bytes_by_id[product_id] = product_bytes

    train_by_id: dict[str, dict[str, Any]] = {}
    train_bytes_by_id: dict[str, bytes] = {}
    train_duplicate_count = 0
    for raw_product in public_train:
        product = dict(raw_product)
        product_id = product.get("productId")
        if type(product_id) is not str or not product_id:
            raise AuthorityError("PUBLIC_TRAIN_PRODUCT_ID_INVALID")
        product_bytes = _canonical(product)
        previous = train_bytes_by_id.get(product_id)
        if previous is not None:
            train_duplicate_count += 1
            if previous != product_bytes:
                collision_count += 1
        else:
            train_by_id[product_id] = product
            train_bytes_by_id[product_id] = product_bytes

    cross_ids = set(hidden_by_id).intersection(train_by_id)
    for product_id in cross_ids:
        if hidden_bytes_by_id[product_id] != train_bytes_by_id[product_id]:
            collision_count += 1
    if collision_count:
        raise AuthorityError("PRODUCT_PROJECTION_COLLISION")

    union_by_id = dict(train_by_id)
    union_by_id.update(hidden_by_id)
    hidden = [hidden_by_id[key] for key in sorted(hidden_by_id)]
    union = [union_by_id[key] for key in sorted(union_by_id)]
    counts = {
        "hiddenDuplicateCount": hidden_duplicate_count,
        "trainDuplicateCount": train_duplicate_count,
        "crossSetDuplicateCount": len(cross_ids),
        "collisionCount": collision_count,
    }
    return hidden, union, counts


def _confirmation_scenarios(dataframe: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, row in dataframe.iterrows():
        info = row.get("extra_info")
        if type(info) is not dict:
            raise AuthorityError("OFFICIAL_EXTRA_INFO_INVALID")
        if row.get("ability") == "stage_1" and info.get("question_type") == "single_product":
            rows.append(scenario(row))
    return rows


def _score(
    scenarios: Sequence[Mapping[str, Any]],
    union_rows: Sequence[Mapping[str, Any]],
    catalog_rows: Sequence[Mapping[str, Any]],
    union_sha256: str,
) -> dict[str, Any]:
    documents = build_documents(union_rows)
    document_map = {item.product_id: item for item in documents}
    catalog = catalog_value_set(catalog_rows)
    retriever = DeterministicBM25(documents)
    reason_counts: Counter[str] = Counter()
    eligible_contexts: list[dict[str, Any]] = []
    corpus_hashes: set[str] = set()
    for row in scenarios:
        accepted, reasons, context = eligibility(
            row,
            corpus=document_map,
            catalog=catalog,
        )
        if accepted and context is not None:
            eligible_contexts.append(context)
        else:
            reason_counts.update(reasons)
        corpus_hashes.add(union_sha256)

    arm_a: list[dict[str, float]] = []
    arm_b: list[dict[str, float]] = []
    arm_c: list[dict[str, float]] = []
    deltas: list[tuple[str, float]] = []
    base_no_match_count = 0
    bc_equal_count = 0
    candidate_set_mutation_count = 0
    for context in eligible_contexts:
        raw = retriever.raw_scores(context["query"])
        no_match = not raw or raw[0][1] == 0
        if no_match:
            base_no_match_count += 1
        base = raw[:CANDIDATE_DEPTH]
        base_ids = [product_id for product_id, _ in base]
        b_ids = rerank(
            base,
            documents=document_map,
            preferences=context["preferences"],
            weight=FIXED_LAMBDA,
        )
        c_ids = rerank(
            base,
            documents=document_map,
            preferences=context["preferences"],
            weight=FIXED_LAMBDA,
        )
        if b_ids == c_ids:
            bc_equal_count += 1
        base_set = set(base_ids)
        candidate_set_mutation_count += len(base_set.symmetric_difference(b_ids))
        candidate_set_mutation_count += len(base_set.symmetric_difference(c_ids))
        target = context["targetProductId"]
        a_metrics = zero_metrics() if no_match else ranking_metrics(base_ids, target)
        b_metrics = zero_metrics() if no_match else ranking_metrics(b_ids, target)
        c_metrics = zero_metrics() if no_match else ranking_metrics(c_ids, target)
        arm_a.append(a_metrics)
        arm_b.append(b_metrics)
        arm_c.append(c_metrics)
        deltas.append((
            context["conversationId"],
            c_metrics["hitAt10"] - a_metrics["hitAt10"],
        ))

    all_single_product = len(scenarios)
    eligible = len(eligible_contexts)
    coverage = eligible / all_single_product if all_single_product else 0.0
    means = {
        "A": mean_metrics(arm_a),
        "B": mean_metrics(arm_b),
        "C": mean_metrics(arm_c),
    }
    bootstrap = cluster_bootstrap(deltas) if deltas else None
    bc_rate = bc_equal_count / eligible if eligible else 0.0
    gates = {
        "eligibleAtLeast75": eligible >= 75,
        "coverageAtLeast75Percent": coverage >= 0.75,
        "baseTargetRecallAt50AtLeast095": means["A"]["targetRecallAt50"] >= 0.95,
        "baseNoMatchCountZero": base_no_match_count == 0,
        "armBEqualsArmC": bc_equal_count == eligible,
        "candidateSetMutationCountZero": candidate_set_mutation_count == 0,
        "bootstrapLowerBoundPositive": bool(bootstrap and bootstrap["p2_5"] > 0),
        "distinctCorpusHashCountOne": len(corpus_hashes) == EXPECTED_DISTINCT_CORPUS_HASH_COUNT,
    }
    return {
        "eligible": eligible,
        "allSingleProduct": all_single_product,
        "coverage": coverage,
        "eligibilityReasonCounts": dict(sorted(reason_counts.items())),
        "baseTargetRecallAt50": means["A"]["targetRecallAt50"],
        "armA": {
            "hitAt10": means["A"]["hitAt10"],
            "nDCGAt10": means["A"]["nDCGAt10"],
            "MRRAt50": means["A"]["MRRAt50"],
        },
        "armC": {
            "hitAt10": means["C"]["hitAt10"],
            "nDCGAt10": means["C"]["nDCGAt10"],
            "MRRAt50": means["C"]["MRRAt50"],
        },
        "cMinusAHitAt10ConversationClusterBootstrap95CI": None if bootstrap is None else {
            "label": INTERVAL_LABEL,
            **bootstrap,
        },
        "bCExactEqualityRate": bc_rate,
        "candidateSetMutationCount": candidate_set_mutation_count,
        "baseNoMatchCount": base_no_match_count,
        "distinctCorpusHashCount": len(corpus_hashes),
        "gates": gates,
    }


def _exclusive_write_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = _canonical(value) + b"\n"
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _start_execution(pre_run_receipt_sha256: str) -> None:
    _exclusive_write_json(STARTED_MARKER_PATH, {
        "schemaVersion": "shopping-memory-v13-quality-execution-started-v1",
        "preRunReceiptSha256": pre_run_receipt_sha256,
        "qualityExecutionStarted": True,
        "qualityExecutionCompleted": False,
    })


def _safe_failure_receipt(
    failure_code: str,
    pre_run_receipt_sha256: str,
) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "contractVersion": CONTRACT_VERSION,
        "decision": HOLD_VERDICT,
        "failureCode": failure_code,
        "authorityDataset": {
            "dataset": DATASET_ID,
            "revision": DATASET_REVISION,
            "file": DATASET_FILE,
            "expectedSha256": OFFICIAL_TEST_SHA256,
        },
        "preRunReceiptSha256": pre_run_receipt_sha256,
        "preRunIdentityMatch": False,
        "qualityExecutionStarted": True,
        "qualityExecutionCompleted": False,
        "qualityExecutionCount": 1,
        "hiddenCorpusWrittenToWorkspace": False,
        "perCaseDataWrittenToWorkspace": False,
        "productionDefaultChanged": False,
    }


def evaluate_once(
    official_test_path: Path,
) -> Path:
    test_path = _validate_paths(official_test_path)
    pre_run, pre_run_sha256 = _validate_pre_run_receipt()
    try:
        _start_execution(pre_run_sha256)
    except FileExistsError as error:
        raise AuthorityError("QUALITY_EXECUTION_ALREADY_STARTED") from error

    try:
        # Each sensitive or public evaluation input is read exactly once.  Its
        # hash and parser consume this same immutable in-memory byte snapshot.
        official_bytes = test_path.read_bytes()
        public_train_bytes = PUBLIC_TRAIN_PATH.read_bytes()
        catalog_values_bytes = CATALOG_VALUES_PATH.read_bytes()
        byte_once_inputs = {
            "publicTrainCorpus": public_train_bytes,
            "catalogValues": catalog_values_bytes,
        }
        dependency_hashes, dependency_payloads = _dependency_snapshot(
            byte_once_inputs,
        )
        if pre_run.get("dependencySha256") != dependency_hashes:
            raise AuthorityError("PRE_RUN_DEPENDENCY_IDENTITY_MISMATCH")
        _validate_contract_v13_7(dependency_payloads["contractV13_7"])

        official_sha256 = _sha256_bytes(official_bytes)
        if official_sha256 != OFFICIAL_TEST_SHA256:
            raise AuthorityError("OFFICIAL_TEST_SHA256_MISMATCH")
        dataframe = pd.read_parquet(io.BytesIO(official_bytes))
        if len(dataframe) != EXPECTED_AUTHORITY_DATASET_ROWS:
            raise AuthorityError("OFFICIAL_TEST_ROW_COUNT_MISMATCH")
        public_train = _read_jsonl_bytes(public_train_bytes)
        catalog_values = _read_jsonl_bytes(catalog_values_bytes)
        hidden_rows, union_rows, duplicate_counts = _build_hidden_and_union(
            dataframe,
            public_train,
        )
        scenarios = _confirmation_scenarios(dataframe)
        hidden_bytes = _jsonl_bytes(hidden_rows)
        union_bytes = _jsonl_bytes(union_rows)
        hidden_sha256 = _sha256_bytes(hidden_bytes)
        union_sha256 = _sha256_bytes(union_bytes)

        frozen_counts_match = (
            len(hidden_rows) == EXPECTED_HIDDEN_TARGET_ROWS
            and len(public_train) == EXPECTED_PUBLIC_TRAIN_ROWS
            and duplicate_counts["crossSetDuplicateCount"] == EXPECTED_INTERSECTION_ROWS
            and len(union_rows) == EXPECTED_UNION_ROWS
            and len(scenarios) == EXPECTED_ALL_SINGLE_PRODUCT
            and duplicate_counts["collisionCount"] == 0
        )
        if not frozen_counts_match:
            raise AuthorityError("FROZEN_STRUCTURAL_CONSTANT_MISMATCH")

        quality = _score(scenarios, union_rows, catalog_values, union_sha256)
        post_dependency_hashes = _post_dependency_hashes(byte_once_inputs)
        if post_dependency_hashes != dependency_hashes:
            raise AuthorityError("DEPENDENCY_CHANGED_DURING_EXECUTION")
        pre_run_identity_match = (
            pre_run.get("dependencySha256") == dependency_hashes
            and pre_run.get("builderSha256") == dependency_hashes["source"]
            and pre_run.get("scorerSha256") == dependency_hashes["source"]
        )
        all_gates = (
            pre_run_identity_match
            and quality["distinctCorpusHashCount"] == EXPECTED_DISTINCT_CORPUS_HASH_COUNT
            and all(quality["gates"].values())
        )
        decision = MAXIMUM_EXTERNAL_VERDICT if all_gates else HOLD_VERDICT
        source_sha256 = dependency_hashes["source"]
        receipt = {
            "schemaVersion": SCHEMA_VERSION,
            "contractVersion": CONTRACT_VERSION,
            "decision": decision,
            "authorityDataset": {
                "dataset": DATASET_ID,
                "revision": DATASET_REVISION,
                "file": DATASET_FILE,
                "sha256": official_sha256,
                "rowCount": len(dataframe),
            },
            "hiddenTargetSetSha256": hidden_sha256,
            "hiddenTargetRows": len(hidden_rows),
            "publicTrainSha256": _sha256_bytes(public_train_bytes),
            "publicTrainRows": len(public_train),
            "unionCorpusSha256": union_sha256,
            "unionCorpusRows": len(union_rows),
            "distinctCorpusHashCount": quality["distinctCorpusHashCount"],
            "contractSha256": dependency_hashes["contractV13_7"],
            "builderSha256": source_sha256,
            "scorerSha256": source_sha256,
            "preRunReceiptSha256": pre_run_sha256,
            "preRunIdentityMatch": pre_run_identity_match,
            **duplicate_counts,
            "eligible": quality["eligible"],
            "allSingleProduct": quality["allSingleProduct"],
            "coverage": quality["coverage"],
            "baseTargetRecallAt50": quality["baseTargetRecallAt50"],
            "armA": quality["armA"],
            "armC": quality["armC"],
            "cMinusAHitAt10ConversationClusterBootstrap95CI": (
                quality["cMinusAHitAt10ConversationClusterBootstrap95CI"]
            ),
            "bCExactEqualityRate": quality["bCExactEqualityRate"],
            "candidateSetMutationCount": quality["candidateSetMutationCount"],
            "baseNoMatchCount": quality["baseNoMatchCount"],
            "eligibilityReasonCounts": quality["eligibilityReasonCounts"],
            "gates": quality["gates"],
            "fixedLambda": FIXED_LAMBDA,
            "officialTestStructuralReadCount": 1,
            "qualityExecutionStarted": True,
            "qualityExecutionCompleted": True,
            "qualityExecutionCount": 1,
            "hiddenCorpusWrittenToWorkspace": False,
            "perCaseDataWrittenToWorkspace": False,
            "productionDefaultChanged": False,
            "claimsExcluded": [
                "open_world_retrieval",
                "unseen_product_generalization",
                "production_retrieval_quality",
                "global_default_enablement",
            ],
        }
        _exclusive_write_json(AGGREGATE_RECEIPT_PATH, receipt)
        return AGGREGATE_RECEIPT_PATH
    except BaseException as error:
        failure_code = (
            error.code
            if isinstance(error, AuthorityError)
            else "UNEXPECTED_SCORER_FAILURE"
        )
        failure_receipt = _safe_failure_receipt(
            failure_code,
            pre_run_sha256,
        )
        _exclusive_write_json(AGGREGATE_RECEIPT_PATH, failure_receipt)
        return AGGREGATE_RECEIPT_PATH


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--official-test-parquet", required=True, type=Path)
    args = parser.parse_args()
    path = evaluate_once(args.official_test_parquet)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
