from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    REPO_ROOT
    / "agent/evaluation/schemas/kuaisearch_multicategory_public_coverage_inventory_v1.schema.json"
)
MODULE_PATH = REPO_ROOT / "agent/evaluation/kuaisearch_multicategory_public_coverage_inventory_v1.py"
TEST_PATH = REPO_ROOT / "agent/tests/test_kuaisearch_multicategory_public_coverage_inventory_v1.py"

ATTEMPT_ID = "attempt001"
INVENTORY_SCHEMA_VERSION = "kuaisearch-multicategory-public-coverage-inventory-v1"
MANIFEST_SCHEMA_VERSION = "kuaisearch-multicategory-public-coverage-inventory-manifest-v1"
INVENTORY_ID = "kuaisearch-multicategory-public-coverage-inventory-20260824-attempt001"
DEFAULT_OUTPUT_DIR = (
    REPO_ROOT / "outputs/kuaisearch-multicategory-public-coverage-inventory-20260824/attempt001"
)
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
TODAY = date.today().isoformat()
SENSITIVE_PATH_MARKERS = ("private", "oracle", "gold", "evaluator", "qrels", "secret", "credential", "env")

TASK030_SNAPSHOT_PATH = (
    REPO_ROOT
    / "outputs/kuaisearch-multicategory-public-evidence-snapshot-20260824/attempt002/snapshot.json"
)
TASK030_MANIFEST_PATH = (
    REPO_ROOT
    / "outputs/kuaisearch-multicategory-public-evidence-snapshot-20260824/attempt002/manifest.json"
)
BREADTH_DIR = REPO_ROOT / "data/benchmarks/ecommerce/kuaisearch_multicategory_breadth_v1_20260824_human_r1"
BREADTH_CATEGORIES_PATH = BREADTH_DIR / "categories.jsonl"
BREADTH_DOCUMENTS_PATH = BREADTH_DIR / "documents.jsonl"
BREADTH_QUERIES_PATH = BREADTH_DIR / "queries.jsonl"

TASK030_SOURCE_PINS_DIGEST = "0a64811de285052b99032ee098c140cb62233a00098815b1967fbd5628fac6fe"

FROZEN_SOURCE_METADATA = {
    str(TASK030_SNAPSHOT_PATH.resolve()): {
        "label": "task030_snapshot",
        "sha256": "7b6df6d149dba321bc2407c4844cab6658bf9a7254ca0c1d155c65ff35b1847d",
        "bytes": 4394,
    },
    str(TASK030_MANIFEST_PATH.resolve()): {
        "label": "task030_manifest",
        "sha256": "33664da0e2c097fa57080451123592803af5ea447afa187b2a7707ce08d774cf",
        "bytes": 1027,
    },
    str(BREADTH_CATEGORIES_PATH.resolve()): {
        "label": "breadth_categories",
        "sha256": "3cd13ed702312799f8d721a1ac74e43d8baf121f03c521720774390d90398c1f",
        "bytes": 5099,
        "rows": 12,
    },
    str(BREADTH_DOCUMENTS_PATH.resolve()): {
        "label": "breadth_documents",
        "sha256": "03c11908505fce053b0e4e5b95b25a1df6cf191e712be13a32cb9f11300c49c3",
        "bytes": 306889,
        "rows": 499,
    },
    str(BREADTH_QUERIES_PATH.resolve()): {
        "label": "breadth_queries",
        "sha256": "2611b664ffb3d7a4e0b8e996a572c15646807aaa52f7c03063d1db5f3f70cec2",
        "bytes": 164637,
        "rows": 507,
    },
}

ALLOWED_SOURCE_PATHS = (
    TASK030_SNAPSHOT_PATH,
    TASK030_MANIFEST_PATH,
    BREADTH_CATEGORIES_PATH,
    BREADTH_DOCUMENTS_PATH,
    BREADTH_QUERIES_PATH,
)

NON_CLAIMS = [
    "This inventory covers only public category-document-query coverage and excludes relevance, grade, rank, strategy, and winner claims.",
    "This inventory does not read G2 blind rows or any private, gold, oracle, qrels, or human-label payload.",
    "This inventory does not claim G2 completion or any Agent architecture lift.",
]


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_schema() -> dict[str, Any]:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _validate_schema_ref(schema: dict[str, Any], def_name: str, payload: Any) -> None:
    Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": schema["$defs"]}).validate(
        payload
    )


def _bundle_dir_lstat(bundle_dir: Path):
    return os.lstat(bundle_dir)


def _path_lstat(path: Path):
    return os.lstat(path)


def _is_reparse_point_stat(stat_result: Any) -> bool:
    attributes = getattr(stat_result, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_bundle_dir_not_link_or_reparse(bundle_dir: Path) -> None:
    if bundle_dir.is_symlink():
        raise ValueError("bundle directory symlink rejected")
    stat_result = _bundle_dir_lstat(bundle_dir)
    if _is_reparse_point_stat(stat_result):
        raise ValueError("bundle directory reparse point rejected")


def _root_entries(bundle_dir: Path) -> list[Path]:
    return sorted(bundle_dir.iterdir(), key=lambda path: path.name)


def _assert_output_dir_ready_for_materialize(bundle_dir: Path) -> None:
    if bundle_dir.exists():
        _assert_bundle_dir_not_link_or_reparse(bundle_dir)
        if not bundle_dir.is_dir():
            raise ValueError("output path is not a directory")
        if any(True for _ in bundle_dir.iterdir()):
            raise ValueError("output directory must be empty before materialization")
        return
    bundle_dir.mkdir(parents=True, exist_ok=False)


def _assert_expected_output_file_set(bundle_dir: Path) -> None:
    if not bundle_dir.exists() or not bundle_dir.is_dir():
        raise ValueError("missing bundle directory")
    _assert_bundle_dir_not_link_or_reparse(bundle_dir)
    actual = {path.name for path in _root_entries(bundle_dir)}
    if actual != {"inventory.json", "manifest.json"}:
        raise ValueError("unexpected output artifact set")
    for path in _root_entries(bundle_dir):
        if path.is_symlink():
            raise ValueError("symlink output artifact rejected")
        if not path.is_file():
            raise ValueError("non-file output artifact rejected")
        stat_result = path.stat()
        if not stat.S_ISREG(stat_result.st_mode):
            raise ValueError("non-regular output artifact rejected")


def _frozen_metadata_for_path(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    metadata = FROZEN_SOURCE_METADATA.get(str(resolved))
    if metadata is None:
        raise ValueError(f"read outside allowlist: {resolved}")
    return metadata


def _assert_source_file_not_link_reparse_or_nonregular(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"source symlink rejected: {path}")
    stat_result = _path_lstat(path)
    if _is_reparse_point_stat(stat_result):
        raise ValueError(f"source reparse point rejected: {path}")
    if not stat.S_ISREG(stat_result.st_mode):
        raise ValueError(f"source non-regular file rejected: {path}")


def _read_bytes(path: Path) -> bytes:
    resolved = path.resolve()
    allowed = {candidate.resolve() for candidate in ALLOWED_SOURCE_PATHS}
    if resolved not in allowed:
        raise ValueError(f"read outside allowlist: {resolved}")
    if any(marker in part.lower() for part in resolved.parts for marker in SENSITIVE_PATH_MARKERS):
        raise ValueError(f"sensitive source path rejected: {resolved}")
    metadata = _frozen_metadata_for_path(path)
    _assert_source_file_not_link_reparse_or_nonregular(path)
    payload = path.read_bytes()
    if len(payload) != metadata["bytes"]:
        raise ValueError(f"source bytes drift: {resolved}")
    if _sha256_bytes(payload) != metadata["sha256"]:
        raise ValueError(f"source sha drift: {resolved}")
    return payload


def _read_text(path: Path) -> str:
    return _read_bytes(path).decode("utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(_read_text(path))


def _build_source_pin(path: Path) -> dict[str, Any]:
    metadata = _frozen_metadata_for_path(path)
    pin: dict[str, Any] = {
        "label": metadata["label"],
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "sha256": metadata["sha256"],
        "bytes": metadata["bytes"],
    }
    if "rows" in metadata:
        pin["rows"] = metadata["rows"]
    return pin


def _validate_task030_binding() -> dict[str, Any]:
    snapshot = _read_json(TASK030_SNAPSHOT_PATH)
    manifest = _read_json(TASK030_MANIFEST_PATH)
    expected_snapshot_keys = {
        "attemptId",
        "createdOn",
        "decisionPins",
        "nonClaims",
        "publicEvidence",
        "safety",
        "schemaVersion",
        "separateFromUsedPhone439",
        "snapshotId",
        "sourcePins",
        "stageStates",
        "status",
    }
    if set(snapshot.keys()) != expected_snapshot_keys:
        raise ValueError("task030 snapshot key drift")
    if snapshot["schemaVersion"] != "kuaisearch-multicategory-public-evidence-snapshot-v1":
        raise ValueError("task030 snapshot schema drift")
    if snapshot["attemptId"] != "attempt002":
        raise ValueError("task030 snapshot attempt drift")
    if snapshot["snapshotId"] != "kuaisearch-multicategory-public-evidence-snapshot-20260824-attempt002":
        raise ValueError("task030 snapshot id drift")
    if snapshot["status"] != "PUBLIC_EVIDENCE_READY_BENCHMARK_DECISION_PENDING":
        raise ValueError("task030 snapshot status drift")
    if snapshot["separateFromUsedPhone439"] is not True:
        raise ValueError("task030 used-phone separation drift")
    if snapshot["publicEvidence"] != {
        "breadthDocuments": 499,
        "breadthVerticals": 12,
        "corpusDocuments": 46079,
        "phaseAPublicSampleRows": 508,
        "publicBlindRows": 21459,
        "publicQueries": 507,
    }:
        raise ValueError("task030 public evidence drift")
    if snapshot["stageStates"] != {
        "g0": "PREPARED_NOT_RANKED",
        "g1": "ACCEPT_G1_OUTPUT_G2_PENDING",
        "g2": "ACCEPT_HUMAN_REVIEW_PACKAGE / HUMAN_REVIEW_PENDING",
        "publicBlindPoolPhysicalSeparation": True,
    }:
        raise ValueError("task030 stage state drift")
    source_pins_digest = _sha256_bytes(_canonical_json_bytes(snapshot["sourcePins"]))
    if source_pins_digest != TASK030_SOURCE_PINS_DIGEST:
        raise ValueError("task030 source pin digest drift")

    if set(manifest.keys()) != {
        "artifacts",
        "attemptId",
        "canonicalDigest",
        "codePins",
        "generatedOn",
        "schemaVersion",
    }:
        raise ValueError("task030 manifest key drift")
    if manifest["schemaVersion"] != "kuaisearch-multicategory-public-evidence-snapshot-manifest-v1":
        raise ValueError("task030 manifest schema drift")
    if manifest["attemptId"] != "attempt002":
        raise ValueError("task030 manifest attempt drift")
    if manifest["canonicalDigest"] != "e3e945f0d80c1926780e5a93b8db714ed5ec46f306dbc5ba5204fe422e02afb4":
        raise ValueError("task030 manifest canonical drift")
    if manifest["artifacts"] != [
        {
            "bytes": 4394,
            "path": "snapshot.json",
            "sha256": "7b6df6d149dba321bc2407c4844cab6658bf9a7254ca0c1d155c65ff35b1847d",
        }
    ]:
        raise ValueError("task030 manifest artifact drift")
    return {
        "snapshotSha256": _frozen_metadata_for_path(TASK030_SNAPSHOT_PATH)["sha256"],
        "manifestSha256": _frozen_metadata_for_path(TASK030_MANIFEST_PATH)["sha256"],
        "canonicalDigest": manifest["canonicalDigest"],
        "sourcePinsDigest": source_pins_digest,
    }


def _load_categories() -> dict[str, dict[str, Any]]:
    categories: dict[str, dict[str, Any]] = {}
    row_count = 0
    for raw_line in _read_text(BREADTH_CATEGORIES_PATH).splitlines():
        if not raw_line.strip():
            continue
        row_count += 1
        row = json.loads(raw_line)
        if set(row.keys()) != {
            "auditCandidateStatus",
            "categoryKey",
            "categoryPath",
            "claimBoundary",
            "datasetVersion",
            "humanDecision",
            "recordType",
            "schemaVersion",
        }:
            raise ValueError("breadth category key drift")
        if row["schemaVersion"] != "kuaisearch-multicategory-breadth-record-v1":
            raise ValueError("breadth category schema drift")
        if row["recordType"] != "CATEGORY":
            raise ValueError("breadth category type drift")
        if row["datasetVersion"] != "kuaisearch-multicategory-breadth-v1-20260824-human-r1":
            raise ValueError("breadth category dataset drift")
        key = row["categoryKey"]
        path = row["categoryPath"]
        if (
            not isinstance(key, str)
            or not key
            or key in categories
            or not isinstance(path, list)
            or len(path) != 3
            or not all(isinstance(item, str) and item for item in path)
        ):
            raise ValueError("breadth category identity drift")
        categories[key] = {
            "categoryPath": path,
            "documentCount": 0,
            "queryIds": set(),
            "trainQueryIds": set(),
            "testQueryIds": set(),
        }
    if row_count != _frozen_metadata_for_path(BREADTH_CATEGORIES_PATH)["rows"]:
        raise ValueError("breadth category row count drift")
    return categories


def _load_documents(categories: dict[str, dict[str, Any]]) -> int:
    row_count = 0
    doc_ids: set[str] = set()
    for raw_line in _read_text(BREADTH_DOCUMENTS_PATH).splitlines():
        if not raw_line.strip():
            continue
        row_count += 1
        row = json.loads(raw_line)
        if set(row.keys()) != {
            "brand",
            "categoryKey",
            "categoryPath",
            "datasetVersion",
            "docId",
            "evidenceItemId",
            "joinMethod",
            "rawAttrValue",
            "recordType",
            "schemaVersion",
            "seller",
            "title",
        }:
            raise ValueError("breadth document key drift")
        if row["schemaVersion"] != "kuaisearch-multicategory-breadth-record-v1":
            raise ValueError("breadth document schema drift")
        if row["recordType"] != "DOCUMENT":
            raise ValueError("breadth document type drift")
        if row["datasetVersion"] != "kuaisearch-multicategory-breadth-v1-20260824-human-r1":
            raise ValueError("breadth document dataset drift")
        doc_id = row["docId"]
        key = row["categoryKey"]
        if (
            not isinstance(doc_id, str)
            or not doc_id
            or doc_id in doc_ids
            or key not in categories
            or row["categoryPath"] != categories[key]["categoryPath"]
        ):
            raise ValueError("breadth document identity drift")
        doc_ids.add(doc_id)
        categories[key]["documentCount"] += 1
    if row_count != _frozen_metadata_for_path(BREADTH_DOCUMENTS_PATH)["rows"]:
        raise ValueError("breadth document row count drift")
    return row_count


def _load_queries(categories: dict[str, dict[str, Any]]) -> int:
    row_count = 0
    query_ids: set[str] = set()
    linked_total = 0
    for raw_line in _read_text(BREADTH_QUERIES_PATH).splitlines():
        if not raw_line.strip():
            continue
        row_count += 1
        row = json.loads(raw_line)
        if set(row.keys()) != {
            "datasetVersion",
            "linkedCategoryKeys",
            "query",
            "queryId",
            "recordType",
            "schemaVersion",
            "source",
            "split",
        }:
            raise ValueError("breadth query key drift")
        if row["schemaVersion"] != "kuaisearch-multicategory-breadth-record-v1":
            raise ValueError("breadth query schema drift")
        if row["recordType"] != "QUERY":
            raise ValueError("breadth query type drift")
        if row["datasetVersion"] != "kuaisearch-multicategory-breadth-v1-20260824-human-r1":
            raise ValueError("breadth query dataset drift")
        if row["source"] != "KUAISEARCH_RELEVANCE_SNAPSHOT":
            raise ValueError("breadth query source drift")
        split = row["split"]
        if split not in {"train", "test"}:
            raise ValueError("breadth query split drift")
        query_id = row["queryId"]
        if not isinstance(query_id, str) or not query_id or query_id in query_ids:
            raise ValueError("breadth query identity drift")
        query_ids.add(query_id)
        linked_category_keys = row["linkedCategoryKeys"]
        if (
            not isinstance(linked_category_keys, list)
            or not linked_category_keys
            or len(linked_category_keys) != len(set(linked_category_keys))
        ):
            raise ValueError("breadth linked category drift")
        for category_key in linked_category_keys:
            if not isinstance(category_key, str) or category_key not in categories:
                raise ValueError("breadth linked category drift")
            categories[category_key]["queryIds"].add(query_id)
            if split == "train":
                categories[category_key]["trainQueryIds"].add(query_id)
            else:
                categories[category_key]["testQueryIds"].add(query_id)
            linked_total += 1
    if row_count != _frozen_metadata_for_path(BREADTH_QUERIES_PATH)["rows"]:
        raise ValueError("breadth query row count drift")
    if len(query_ids) != 507:
        raise ValueError("breadth unique query count drift")
    if linked_total != 507:
        raise ValueError("breadth query-category deterministic link drift")
    return len(query_ids)


def _expected_inventory() -> dict[str, Any]:
    task030_binding = _validate_task030_binding()
    categories = _load_categories()
    document_count = _load_documents(categories)
    unique_query_count = _load_queries(categories)

    coverage = []
    for category_key in sorted(categories):
        category = categories[category_key]
        linked_query_count = len(category["queryIds"])
        train_query_count = len(category["trainQueryIds"])
        test_query_count = len(category["testQueryIds"])
        if linked_query_count != train_query_count + test_query_count:
            raise ValueError("train/test query split drift")
        coverage.append(
            {
                "categoryKey": category_key,
                "categoryPath": list(category["categoryPath"]),
                "documentCount": category["documentCount"],
                "linkedQueryCount": linked_query_count,
                "trainQueryCount": train_query_count,
                "testQueryCount": test_query_count,
            }
        )
    if len(coverage) != 12:
        raise ValueError("category coverage drift")
    if sum(row["documentCount"] for row in coverage) != 499 or document_count != 499:
        raise ValueError("document coverage drift")
    if sum(row["linkedQueryCount"] for row in coverage) != 507 or unique_query_count != 507:
        raise ValueError("query coverage drift")

    return {
        "schemaVersion": INVENTORY_SCHEMA_VERSION,
        "inventoryId": INVENTORY_ID,
        "attemptId": ATTEMPT_ID,
        "status": "PUBLIC_COVERAGE_INVENTORY_READY_G2_HUMAN_PENDING",
        "createdOn": TODAY,
        "summary": {
            "categories": 12,
            "documents": 499,
            "uniqueQueries": 507,
        },
        "coverage": coverage,
        "task030Binding": task030_binding,
        "sourcePins": [
            _build_source_pin(TASK030_SNAPSHOT_PATH),
            _build_source_pin(TASK030_MANIFEST_PATH),
            _build_source_pin(BREADTH_CATEGORIES_PATH),
            _build_source_pin(BREADTH_DOCUMENTS_PATH),
            _build_source_pin(BREADTH_QUERIES_PATH),
        ],
        "safety": {
            "modelCalls": 0,
            "toolCalls": 0,
            "networkCalls": 0,
            "businessWrites": 0,
        },
        "nonClaims": list(NON_CLAIMS),
    }


def _manifest_code_pins() -> list[dict[str, str]]:
    return [
        {
            "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "sha256": _sha256_bytes(path.read_bytes()),
        }
        for path in (SCHEMA_PATH, MODULE_PATH, TEST_PATH)
    ]


def _artifact_entries(bundle_dir: Path) -> list[dict[str, Any]]:
    inventory_path = bundle_dir / "inventory.json"
    if not inventory_path.exists():
        raise ValueError("missing inventory artifact")
    payload = inventory_path.read_bytes()
    return [{"path": "inventory.json", "sha256": _sha256_bytes(payload), "bytes": len(payload)}]


def _manifest_payload(bundle_dir: Path) -> dict[str, Any]:
    artifacts = _artifact_entries(bundle_dir)
    code_pins = _manifest_code_pins()
    return {
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
        "attemptId": ATTEMPT_ID,
        "generatedOn": TODAY,
        "artifacts": artifacts,
        "codePins": code_pins,
        "canonicalDigest": _sha256_bytes(
            _canonical_json_bytes({"artifacts": artifacts, "codePins": code_pins})
        ),
    }


def materialize_public_coverage_inventory(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    schema = _load_schema()
    _assert_output_dir_ready_for_materialize(output_dir)
    inventory = _expected_inventory()
    inventory_path = output_dir / "inventory.json"
    _write_json(inventory_path, inventory)
    manifest = _manifest_payload(output_dir)
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    _validate_schema_ref(schema, "inventory", inventory)
    _validate_schema_ref(schema, "manifest", manifest)
    validate_bundle(output_dir)
    return {"inventory": inventory_path, "manifest": manifest_path}


def validate_bundle(bundle_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    schema = _load_schema()
    _assert_expected_output_file_set(bundle_dir)
    inventory = json.loads((bundle_dir / "inventory.json").read_text(encoding="utf-8"))
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    _validate_schema_ref(schema, "inventory", inventory)
    _validate_schema_ref(schema, "manifest", manifest)
    expected_inventory = _expected_inventory()
    if inventory != expected_inventory:
        raise ValueError("inventory drift")
    expected_manifest = _manifest_payload(bundle_dir)
    if manifest != expected_manifest:
        raise ValueError("manifest drift")
    return {"inventory": inventory, "manifest": manifest}
