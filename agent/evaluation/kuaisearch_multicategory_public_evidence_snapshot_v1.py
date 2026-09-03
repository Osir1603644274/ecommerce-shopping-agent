from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = (
    REPO_ROOT
    / "agent/evaluation/schemas/kuaisearch_multicategory_public_evidence_snapshot_v1.schema.json"
)
MODULE_PATH = REPO_ROOT / "agent/evaluation/kuaisearch_multicategory_public_evidence_snapshot_v1.py"
TEST_PATH = REPO_ROOT / "agent/tests/test_kuaisearch_multicategory_public_evidence_snapshot_v1.py"

ATTEMPT_ID = "attempt002"
SNAPSHOT_SCHEMA_VERSION = "kuaisearch-multicategory-public-evidence-snapshot-v1"
MANIFEST_SCHEMA_VERSION = "kuaisearch-multicategory-public-evidence-snapshot-manifest-v1"
SNAPSHOT_ID = "kuaisearch-multicategory-public-evidence-snapshot-20260824-attempt002"
DEFAULT_OUTPUT_DIR = (
    REPO_ROOT / "outputs/kuaisearch-multicategory-public-evidence-snapshot-20260824/attempt002"
)
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
TODAY = date.today().isoformat()
BANNED_OUTPUT_KEYS = ("strategy", "rank", "docid", "grade", "category", "salt")
SENSITIVE_PATH_MARKERS = ("private", "oracle", "gold", "evaluator", "qrels", "secret", "credential", "env")
PHASE_A_PUBLIC_SAMPLE_ROWS = 508
JSONL_REQUIRED_SAFE_KEYS = frozenset(
    {
        "schemaVersion",
        "blindCandidateId",
        "queryId",
        "query",
        "title",
        "brand",
        "seller_name",
        "attr_value",
        "poolPosition",
    }
)

G0_DIR = REPO_ROOT / "data/benchmarks/ecommerce/kuaisearch_multicategory_retrieval_g0_20260824_r7"
G0_MANIFEST_PATH = G0_DIR / "manifest.json"
G0_DOCUMENTS_PATH = G0_DIR / "documents.jsonl"
G0_QUERIES_PATH = G0_DIR / "queries.jsonl"

BREADTH_DIR = REPO_ROOT / "data/benchmarks/ecommerce/kuaisearch_multicategory_breadth_v1_20260824_human_r1"
BREADTH_VERTICALS_PATH = BREADTH_DIR / "categories.jsonl"
BREADTH_DOCUMENTS_PATH = BREADTH_DIR / "documents.jsonl"
BREADTH_QUERIES_PATH = BREADTH_DIR / "queries.jsonl"

G1_DIR = REPO_ROOT / "data/derived/ecommerce/kuaisearch_multicategory_retrieval_g1_20260824_r1"
G1_MANIFEST_PATH = G1_DIR / "manifest.json"

G2_POOL_DIR = REPO_ROOT / "data/annotations/ecommerce/kuaisearch_multicategory_retrieval_g2_pool_20260824_r1/public"
G2_POOL_MANIFEST_PATH = G2_POOL_DIR / "manifest.json"
G2_POOL_ROWS_PATH = G2_POOL_DIR / "review_rows.jsonl"

G1_ACCEPTANCE_PATH = REPO_ROOT / "docs/acceptance/kuaisearch-full-corpus-retrieval-g1-2026-08-24.md"
G2_ACCEPTANCE_PATH = REPO_ROOT / "docs/acceptance/kuaisearch-g2-human-review-phase-a-2026-08-24.md"

FROZEN_SOURCE_METADATA = {
    str(G0_MANIFEST_PATH.resolve()): {
        "label": "g0_manifest",
        "sha256": "9e5ee5996649956e5d2799a4e93bd47db8320526e74c827c8df0cafb518a7e72",
        "bytes": 1651,
    },
    str(G0_DOCUMENTS_PATH.resolve()): {
        "label": "g0_documents",
        "sha256": "6b8f55f94fb292e9ff946014221244e07fa338e22cfa29de39c42f8c2d1e245f",
        "bytes": 13869461,
        "rows": 46079,
    },
    str(G0_QUERIES_PATH.resolve()): {
        "label": "g0_queries",
        "sha256": "6d27a6ae08f9de530936fe682deea97d8902ec99742f3b609e6ddcb3c7984e3d",
        "bytes": 47892,
        "rows": 507,
    },
    str(BREADTH_VERTICALS_PATH.resolve()): {
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
    str(G1_MANIFEST_PATH.resolve()): {
        "label": "g1_manifest",
        "sha256": "46214e5b09be01808b948d517acbdbd5ae6c0447c81394141658c81e2779e613",
        "bytes": 7153,
    },
    str(G2_POOL_MANIFEST_PATH.resolve()): {
        "label": "g2_public_manifest",
        "sha256": "e1ddc519fbf42ee5ceefc4127b2231a5d070bb063fee4a47f6e633a4c171ce35",
        "bytes": 244,
    },
    str(G2_POOL_ROWS_PATH.resolve()): {
        "label": "g2_public_rows",
        "sha256": "7007e4402c1140be47a05563e231e831ea579ffdf77c087b916be66054c533f8",
        "bytes": 11240310,
        "rows": 21459,
    },
    str(G1_ACCEPTANCE_PATH.resolve()): {
        "label": "g1_acceptance",
        "sha256": "37c1cb1b94576631ae4d6922c2d704343859afa71379081e4e4e187d425a7ae4",
        "bytes": 3514,
    },
    str(G2_ACCEPTANCE_PATH.resolve()): {
        "label": "g2_acceptance",
        "sha256": "71734caa4821a4a2dff7e006cf612b7490878e626e3bac369c98ab4c205e775f",
        "bytes": 5100,
    },
}

ALLOWED_SOURCE_PATHS = (
    G0_MANIFEST_PATH,
    G0_DOCUMENTS_PATH,
    G0_QUERIES_PATH,
    BREADTH_VERTICALS_PATH,
    BREADTH_DOCUMENTS_PATH,
    BREADTH_QUERIES_PATH,
    G1_MANIFEST_PATH,
    G2_POOL_MANIFEST_PATH,
    G2_POOL_ROWS_PATH,
    G1_ACCEPTANCE_PATH,
    G2_ACCEPTANCE_PATH,
)

NON_CLAIMS = [
    "This snapshot is public evidence only and does not choose a retrieval winner.",
    "This snapshot is separate from the used-phone-439 deep-category benchmark and does not prove Agent architecture lift.",
    "This snapshot performs no model call, tool call, network access, or business write.",
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
    if actual != {"manifest.json", "snapshot.json"}:
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
    if any(marker in part.lower() for marker in SENSITIVE_PATH_MARKERS for part in resolved.parts):
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


def _count_jsonl_and_query_ids(path: Path) -> tuple[int, dict[str, str]]:
    row_count = 0
    query_map: dict[str, str] = {}
    seen: set[str] = set()
    for raw_line in _read_text(path).splitlines():
        if not raw_line.strip():
            continue
        row_count += 1
        row = json.loads(raw_line)
        if set(row.keys()) != {"query", "queryId", "split"}:
            raise ValueError("public query key drift")
        if not isinstance(row["query"], str) or not row["query"]:
            raise ValueError("public query text drift")
        if not isinstance(row["queryId"], str) or not row["queryId"]:
            raise ValueError("public queryId drift")
        if row["split"] not in {"train", "test"}:
            raise ValueError("public query split drift")
        query_id = row["queryId"]
        if query_id in seen:
            raise ValueError("duplicate queryId in public queries")
        seen.add(query_id)
        query_map[query_id] = row["query"]
    expected_rows = _frozen_metadata_for_path(path).get("rows")
    if expected_rows is not None and row_count != expected_rows:
        raise ValueError("public query row count drift")
    return row_count, query_map


def _count_jsonl_rows(path: Path) -> int:
    count = 0
    for raw_line in _read_text(path).splitlines():
        if raw_line.strip():
            count += 1
    return count


def _load_g0_documents() -> tuple[int, set[str]]:
    row_count = 0
    doc_ids: set[str] = set()
    for raw_line in _read_text(G0_DOCUMENTS_PATH).splitlines():
        if not raw_line.strip():
            continue
        row_count += 1
        row = json.loads(raw_line)
        if set(row.keys()) != {"attr_value", "brand", "doc_id", "seller_name", "title"}:
            raise ValueError("g0 document key drift")
        if not all(isinstance(row[key], str) for key in row):
            raise ValueError("g0 document field type drift")
        doc_id = row["doc_id"]
        if not doc_id or doc_id in doc_ids:
            raise ValueError("g0 document identity drift")
        doc_ids.add(doc_id)
    if row_count != _frozen_metadata_for_path(G0_DOCUMENTS_PATH)["rows"]:
        raise ValueError("g0 document row count drift")
    return row_count, doc_ids


def _load_breadth_categories() -> dict[str, tuple[str, str, str]]:
    category_map: dict[str, tuple[str, str, str]] = {}
    row_count = 0
    for raw_line in _read_text(BREADTH_VERTICALS_PATH).splitlines():
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
        category_key = row["categoryKey"]
        category_path = row["categoryPath"]
        if (
            not isinstance(category_key, str)
            or not category_key
            or category_key in category_map
            or not isinstance(category_path, list)
            or len(category_path) != 3
            or not all(isinstance(item, str) for item in category_path)
        ):
            raise ValueError("breadth category identity drift")
        category_map[category_key] = tuple(category_path)
    if row_count != _frozen_metadata_for_path(BREADTH_VERTICALS_PATH)["rows"]:
        raise ValueError("breadth category row count drift")
    return category_map


def _validate_breadth_documents(category_map: dict[str, tuple[str, str, str]]) -> int:
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
        category_key = row["categoryKey"]
        category_path = row["categoryPath"]
        if (
            not isinstance(doc_id, str)
            or not doc_id
            or doc_id in doc_ids
            or category_key not in category_map
            or tuple(category_path) != category_map[category_key]
        ):
            raise ValueError("breadth document identity drift")
        doc_ids.add(doc_id)
    if row_count != _frozen_metadata_for_path(BREADTH_DOCUMENTS_PATH)["rows"]:
        raise ValueError("breadth document row count drift")
    return row_count


def _validate_breadth_queries(
    category_map: dict[str, tuple[str, str, str]], query_map: dict[str, str]
) -> int:
    row_count = 0
    seen: set[str] = set()
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
        if row["split"] not in {"train", "test"}:
            raise ValueError("breadth query split drift")
        query_id = row["queryId"]
        if (
            not isinstance(query_id, str)
            or not query_id
            or query_id in seen
            or query_map.get(query_id) != row["query"]
        ):
            raise ValueError("breadth query identity drift")
        linked_category_keys = row["linkedCategoryKeys"]
        if (
            not isinstance(linked_category_keys, list)
            or not linked_category_keys
            or not all(isinstance(item, str) and item in category_map for item in linked_category_keys)
        ):
            raise ValueError("breadth linked categories drift")
        seen.add(query_id)
    if row_count != _frozen_metadata_for_path(BREADTH_QUERIES_PATH)["rows"]:
        raise ValueError("breadth query row count drift")
    if seen != set(query_map):
        raise ValueError("breadth query coverage drift")
    return row_count


def _validate_public_blind_rows(query_map: dict[str, str]) -> tuple[int, int]:
    row_count = 0
    sample_count = 0
    seen_blind_ids: set[tuple[str, str]] = set()
    seen_positions: set[tuple[str, int]] = set()
    for raw_line in _read_text(G2_POOL_ROWS_PATH).splitlines():
        if not raw_line.strip():
            continue
        row_count += 1
        row = json.loads(raw_line)
        keys = set(row.keys())
        if keys != JSONL_REQUIRED_SAFE_KEYS:
            raise ValueError("public blind row key drift")
        if row["schemaVersion"] != "kuaisearch-multicategory-retrieval-g2-blind-pool-v1":
            raise ValueError("public blind row schema drift")
        query_id = row["queryId"]
        if query_id not in query_map:
            raise ValueError("public blind row queryId outside 507 set")
        if row["query"] != query_map[query_id]:
            raise ValueError("public blind row query text drift")
        blind_identity = (query_id, row["blindCandidateId"])
        if blind_identity in seen_blind_ids:
            raise ValueError("duplicate blind candidate identity")
        seen_blind_ids.add(blind_identity)
        position_identity = (query_id, row["poolPosition"])
        if position_identity in seen_positions:
            raise ValueError("duplicate blind pool position")
        seen_positions.add(position_identity)
        lowered_keys = {str(key).lower() for key in keys}
        if any(marker in lowered_keys for marker in BANNED_OUTPUT_KEYS):
            raise ValueError("public blind row contains banned key marker")
        sample_count += 1
    expected_rows = _frozen_metadata_for_path(G2_POOL_ROWS_PATH)["rows"]
    if row_count != expected_rows:
        raise ValueError("public blind row count drift")
    return row_count, sample_count


def _extract_decision(text: str) -> str:
    match = re.search(r"Decision:\s*`([^`]+)`", text)
    if match is None:
        raise ValueError("missing acceptance decision")
    return match.group(1)


def _build_source_pin(label: str, path: Path, rows: int | None = None) -> dict[str, Any]:
    payload = _read_bytes(path)
    pin: dict[str, Any] = {
        "label": label,
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
    }
    if rows is not None:
        pin["rows"] = rows
    return pin


def _build_decision_pin(label: str, path: Path, decision: str) -> dict[str, Any]:
    payload = _read_bytes(path)
    return {
        "label": label,
        "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "sha256": _sha256_bytes(payload),
        "bytes": len(payload),
        "decision": decision,
    }


def _validate_g0_manifest() -> dict[str, Any]:
    manifest = _read_json(G0_MANIFEST_PATH)
    if set(manifest.keys()) != {"artifacts", "contractVersion", "g0Version", "inputCount", "status"}:
        raise ValueError("g0 manifest key drift")
    if manifest["g0Version"] != "kuaisearch-multicategory-retrieval-g0-20260824-r7":
        raise ValueError("g0 version drift")
    if manifest["contractVersion"] != "kuaisearch-multicategory-retrieval-contract-v1":
        raise ValueError("g0 contract drift")
    if manifest["status"] != "PREPARED_NOT_RANKED":
        raise ValueError("unexpected g0 status")
    if manifest["inputCount"] != {"documents": 46079, "queries": 507}:
        raise ValueError("g0 input count drift")
    if not isinstance(manifest["artifacts"], list) or len(manifest["artifacts"]) < 10:
        raise ValueError("g0 artifacts drift")
    artifact_map = {artifact["path"]: artifact for artifact in manifest["artifacts"]}
    documents_artifact = artifact_map.get("documents.jsonl")
    queries_artifact = artifact_map.get("queries.jsonl")
    if documents_artifact is None or queries_artifact is None:
        raise ValueError("g0 artifact binding drift")
    expected_documents = _frozen_metadata_for_path(G0_DOCUMENTS_PATH)
    expected_queries = _frozen_metadata_for_path(G0_QUERIES_PATH)
    if documents_artifact != {
        "path": "documents.jsonl",
        "sha256": expected_documents["sha256"],
        "bytes": expected_documents["bytes"],
    }:
        raise ValueError("g0 documents artifact drift")
    if queries_artifact != {
        "path": "queries.jsonl",
        "sha256": expected_queries["sha256"],
        "bytes": expected_queries["bytes"],
    }:
        raise ValueError("g0 queries artifact drift")
    return manifest


def _has_expected_provenance_suffix(value: object, local_path: Path) -> bool:
    """Accept only the frozen repository-relative suffix of a historical path.

    G1 was produced in the Codex worktree and its immutable manifest records
    that historical absolute location.  The path is provenance only: no caller
    may resolve or read it.  The current F:\\agent local constant remains the
    sole filesystem authority.
    """
    if not isinstance(value, str):
        return False
    parts = [part for part in value.replace("\\", "/").split("/") if part]
    expected = local_path.relative_to(REPO_ROOT).as_posix().split("/")
    return ".." not in parts and len(parts) >= len(expected) and parts[-len(expected) :] == expected


def _validate_g1_input_provenance(
    binding: object,
    *,
    local_path: Path,
    expected: dict[str, Any],
    label: str,
) -> None:
    """Bind a historical G1 input claim to the code-owned local frozen file."""
    if not isinstance(binding, dict) or set(binding) != {"path", "rows", "sha256"}:
        raise ValueError(f"g1 input {label} binding shape drift")
    if not _has_expected_provenance_suffix(binding["path"], local_path):
        raise ValueError(f"g1 input {label} provenance path drift")
    # _read_bytes reads only an allowlisted, code-bound local constant and
    # verifies its frozen hash and byte count before this manifest claim is used.
    _read_bytes(local_path)
    if binding["rows"] != expected["rows"] or binding["sha256"] != expected["sha256"]:
        raise ValueError(f"g1 input {label} drift")


def _validate_g1_manifest() -> dict[str, Any]:
    manifest = _read_json(G1_MANIFEST_PATH)
    if "winner" in manifest or "status" in manifest:
        raise ValueError("g1 manifest forbidden status or winner")
    expected_keys = {
        "buildMs",
        "codeSha256",
        "contract",
        "datasetPins",
        "datasetRevision",
        "g0Binding",
        "hardware",
        "indexIdentity",
        "inputs",
        "latency",
        "memory",
        "models",
        "outputs",
        "packageVersions",
        "schemaVersion",
        "wallClockMs",
    }
    if set(manifest.keys()) != expected_keys:
        raise ValueError("g1 manifest key drift")
    if manifest["schemaVersion"] != "kuaisearch-multicategory-retrieval-baseline-v1":
        raise ValueError("g1 schema drift")
    if manifest["datasetRevision"] != "09807c773ce67360ed8df30842e372182fcf7ad9":
        raise ValueError("g1 dataset revision drift")
    if manifest["indexIdentity"] != "ce481bcff557ab206e532c820641f0f79fa9b0cd339888e797dc087eff2b40ff":
        raise ValueError("g1 index identity drift")
    if manifest["contract"] != {
        "bm25Fields": {"attributeText": 0.45, "brand": 0.25, "title": 1.0},
        "crossEncoderCandidateLimit": 20,
        "crossEncoderDoesNotExpandCandidates": True,
        "denseTextField": "title",
        "randomSeed": 0,
        "rrfK": 60,
        "topK": 100,
    }:
        raise ValueError("g1 ranking contract drift")
    expected_documents = _frozen_metadata_for_path(G0_DOCUMENTS_PATH)
    expected_queries = _frozen_metadata_for_path(G0_QUERIES_PATH)
    _validate_g1_input_provenance(
        manifest["inputs"].get("documents"),
        local_path=G0_DOCUMENTS_PATH,
        expected=expected_documents,
        label="documents",
    )
    _validate_g1_input_provenance(
        manifest["inputs"].get("queries"),
        local_path=G0_QUERIES_PATH,
        expected=expected_queries,
        label="queries",
    )
    if manifest["outputs"] != {
        "rankings_top100.jsonl": {
            "bytes": 8307475,
            "rows": 2535,
            "sha256": "a6a3e410e1244fa8292f4d4f18615960646e29bf9640802bb74f76ef19043ee6",
        }
    }:
        raise ValueError("g1 output identity drift")
    return manifest


def _validate_g2_public_manifest() -> dict[str, Any]:
    manifest = _read_json(G2_POOL_MANIFEST_PATH)
    if set(manifest.keys()) != {"artifact", "physicalSeparation", "schemaVersion"}:
        raise ValueError("g2 public manifest key drift")
    if manifest["schemaVersion"] != "kuaisearch-multicategory-retrieval-g2-blind-pool-v1":
        raise ValueError("g2 public manifest schema drift")
    if manifest["physicalSeparation"] is not True:
        raise ValueError("public blind pool physical separation drift")
    artifact = manifest["artifact"]
    expected_rows = _frozen_metadata_for_path(G2_POOL_ROWS_PATH)
    if artifact != {
        "bytes": expected_rows["bytes"],
        "path": "review_rows.jsonl",
        "rows": expected_rows["rows"],
        "sha256": expected_rows["sha256"],
    }:
        raise ValueError("g2 public manifest artifact drift")
    return manifest


def _extract_phase_a_public_sample_rows(text: str) -> int:
    match = re.search(r"Public sample: .*?,\s*([0-9]+)\s+rows", text)
    if match is None:
        raise ValueError("missing phase-a public sample rows")
    return int(match.group(1))


def _expected_snapshot() -> dict[str, Any]:
    g0_manifest = _validate_g0_manifest()
    _validate_g1_manifest()
    g2_pool_manifest = _validate_g2_public_manifest()
    g1_decision_text = _read_text(G1_ACCEPTANCE_PATH)
    g2_decision_text = _read_text(G2_ACCEPTANCE_PATH)
    g1_decision = _extract_decision(g1_decision_text)
    g2_decision = _extract_decision(g2_decision_text)
    phase_a_public_sample_rows = _extract_phase_a_public_sample_rows(g2_decision_text)

    g0_query_rows, g0_query_map = _count_jsonl_and_query_ids(G0_QUERIES_PATH)
    g0_document_rows, _ = _load_g0_documents()
    breadth_category_map = _load_breadth_categories()
    breadth_vertical_rows = len(breadth_category_map)
    breadth_document_rows = _validate_breadth_documents(breadth_category_map)
    breadth_query_rows = _validate_breadth_queries(breadth_category_map, g0_query_map)
    public_blind_rows, _ = _validate_public_blind_rows(g0_query_map)

    if g1_decision != "ACCEPT_G1_OUTPUT_G2_PENDING":
        raise ValueError("unexpected g1 decision")
    if g2_decision != "ACCEPT_HUMAN_REVIEW_PACKAGE / HUMAN_REVIEW_PENDING":
        raise ValueError("unexpected g2 decision")
    if g2_pool_manifest["artifact"]["rows"] != public_blind_rows:
        raise ValueError("public blind row count drift")
    if g0_document_rows != 46079 or g0_query_rows != 507:
        raise ValueError("unexpected g0 public counts")
    if breadth_vertical_rows != 12 or breadth_document_rows != 499 or breadth_query_rows != 507:
        raise ValueError("unexpected breadth public counts")
    if public_blind_rows != 21459:
        raise ValueError("unexpected public blind row count")
    if phase_a_public_sample_rows != PHASE_A_PUBLIC_SAMPLE_ROWS:
        raise ValueError("unexpected phase-a public sample rows")

    source_pins = [
        _build_source_pin("g0_manifest", G0_MANIFEST_PATH),
        _build_source_pin("g0_documents", G0_DOCUMENTS_PATH, g0_document_rows),
        _build_source_pin("g0_queries", G0_QUERIES_PATH, g0_query_rows),
        _build_source_pin("breadth_verticals", BREADTH_VERTICALS_PATH, breadth_vertical_rows),
        _build_source_pin("breadth_documents", BREADTH_DOCUMENTS_PATH, breadth_document_rows),
        _build_source_pin("breadth_queries", BREADTH_QUERIES_PATH, breadth_query_rows),
        _build_source_pin("g1_manifest", G1_MANIFEST_PATH),
        _build_source_pin("g2_public_manifest", G2_POOL_MANIFEST_PATH),
        _build_source_pin("g2_public_rows", G2_POOL_ROWS_PATH, public_blind_rows),
    ]
    decision_pins = [
        _build_decision_pin("g1_acceptance", G1_ACCEPTANCE_PATH, g1_decision),
        _build_decision_pin("g2_acceptance", G2_ACCEPTANCE_PATH, g2_decision),
    ]
    return {
        "schemaVersion": SNAPSHOT_SCHEMA_VERSION,
        "snapshotId": SNAPSHOT_ID,
        "attemptId": ATTEMPT_ID,
        "status": "PUBLIC_EVIDENCE_READY_BENCHMARK_DECISION_PENDING",
        "createdOn": TODAY,
        "separateFromUsedPhone439": True,
        "safety": {
            "modelCalls": 0,
            "toolCalls": 0,
            "networkCalls": 0,
            "businessWrites": 0,
        },
        "publicEvidence": {
            "corpusDocuments": g0_document_rows,
            "publicQueries": g0_query_rows,
            "breadthVerticals": breadth_vertical_rows,
            "breadthDocuments": breadth_document_rows,
            "publicBlindRows": public_blind_rows,
            "phaseAPublicSampleRows": phase_a_public_sample_rows,
        },
        "stageStates": {
            "g0": g0_manifest["status"],
            "g1": g1_decision,
            "g2": g2_decision,
            "publicBlindPoolPhysicalSeparation": True,
        },
        "sourcePins": source_pins,
        "decisionPins": decision_pins,
        "nonClaims": list(NON_CLAIMS),
    }


def _manifest_code_pins() -> list[dict[str, str]]:
    paths = [SCHEMA_PATH, MODULE_PATH, TEST_PATH]
    return [
        {
            "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "sha256": _sha256_bytes(path.read_bytes()),
        }
        for path in paths
    ]


def _artifact_entries(bundle_dir: Path) -> list[dict[str, Any]]:
    snapshot_path = bundle_dir / "snapshot.json"
    if not snapshot_path.exists():
        raise ValueError("missing snapshot artifact")
    payload = snapshot_path.read_bytes()
    return [{"path": "snapshot.json", "sha256": _sha256_bytes(payload), "bytes": len(payload)}]


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


def _scan_output_markers(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered in BANNED_OUTPUT_KEYS:
                raise ValueError("banned output key marker")
            _scan_output_markers(value)
        return
    if isinstance(node, list):
        for item in node:
            _scan_output_markers(item)
        return


def materialize_public_evidence_snapshot(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    schema = _load_schema()
    _assert_output_dir_ready_for_materialize(output_dir)
    snapshot = _expected_snapshot()
    snapshot_path = output_dir / "snapshot.json"
    _write_json(snapshot_path, snapshot)
    manifest = _manifest_payload(output_dir)
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    _validate_schema_ref(schema, "snapshot", snapshot)
    _validate_schema_ref(schema, "manifest", manifest)
    validate_bundle(output_dir)
    return {"snapshot": snapshot_path, "manifest": manifest_path}


def validate_bundle(bundle_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    schema = _load_schema()
    _assert_expected_output_file_set(bundle_dir)
    snapshot = json.loads((bundle_dir / "snapshot.json").read_text(encoding="utf-8"))
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    _scan_output_markers(snapshot)
    _scan_output_markers(manifest)
    _validate_schema_ref(schema, "snapshot", snapshot)
    _validate_schema_ref(schema, "manifest", manifest)
    expected_snapshot = _expected_snapshot()
    if snapshot != expected_snapshot:
        raise ValueError("snapshot drift")
    expected_manifest = _manifest_payload(bundle_dir)
    if manifest != expected_manifest:
        raise ValueError("manifest drift")
    return {"snapshot": snapshot, "manifest": manifest}
