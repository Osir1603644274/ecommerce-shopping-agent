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
    / "agent/evaluation/schemas/kuaisearch_g2_public_review_sample_integrity_receipt_v1.schema.json"
)
MODULE_PATH = REPO_ROOT / "agent/evaluation/kuaisearch_g2_public_review_sample_integrity_receipt_v1.py"
TEST_PATH = REPO_ROOT / "agent/tests/test_kuaisearch_g2_public_review_sample_integrity_receipt_v1.py"

ATTEMPT_ID = "attempt001"
RECEIPT_SCHEMA_VERSION = "kuaisearch-g2-public-review-sample-integrity-receipt-v1"
MANIFEST_SCHEMA_VERSION = "kuaisearch-g2-public-review-sample-integrity-receipt-manifest-v1"
RECEIPT_ID = "kuaisearch-g2-public-review-sample-integrity-receipt-20260824-attempt001"
DEFAULT_OUTPUT_DIR = (
    REPO_ROOT / "outputs/kuaisearch-g2-public-review-sample-integrity-receipt-20260824/attempt001"
)
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
TODAY = date.today().isoformat()
SENSITIVE_PATH_MARKERS = ("private", "oracle", "gold", "evaluator", "qrels", "secret", "credential", "env")
BANNED_OUTPUT_KEYS = ("relevance", "grade", "rank", "strategy", "docid", "category", "salt")

TASK030_SNAPSHOT_PATH = (
    REPO_ROOT
    / "outputs/kuaisearch-multicategory-public-evidence-snapshot-20260824/attempt002/snapshot.json"
)
TASK030_MANIFEST_PATH = (
    REPO_ROOT
    / "outputs/kuaisearch-multicategory-public-evidence-snapshot-20260824/attempt002/manifest.json"
)
G2_POOL_PUBLIC_MANIFEST_PATH = (
    REPO_ROOT
    / "data/annotations/ecommerce/kuaisearch_multicategory_retrieval_g2_pool_20260824_r1/public/manifest.json"
)
G2_POOL_PUBLIC_ROWS_PATH = (
    REPO_ROOT
    / "data/annotations/ecommerce/kuaisearch_multicategory_retrieval_g2_pool_20260824_r1/public/review_rows.jsonl"
)
G2_PHASE_A_PUBLIC_SAMPLE_PATH = (
    REPO_ROOT
    / "data/annotations/ecommerce/kuaisearch_multicategory_retrieval_g2_phase_a_20260824_r1/public_review_sample.jsonl"
)
G2_PHASE_A_ACCEPTANCE_PATH = REPO_ROOT / "docs/acceptance/kuaisearch-g2-human-review-phase-a-2026-08-24.md"

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
    str(G2_POOL_PUBLIC_MANIFEST_PATH.resolve()): {
        "label": "g2_public_manifest",
        "sha256": "e1ddc519fbf42ee5ceefc4127b2231a5d070bb063fee4a47f6e633a4c171ce35",
        "bytes": 244,
    },
    str(G2_POOL_PUBLIC_ROWS_PATH.resolve()): {
        "label": "g2_public_rows",
        "sha256": "7007e4402c1140be47a05563e231e831ea579ffdf77c087b916be66054c533f8",
        "bytes": 11240310,
        "rows": 21459,
    },
    str(G2_PHASE_A_PUBLIC_SAMPLE_PATH.resolve()): {
        "label": "g2_phase_a_public_sample",
        "sha256": "5bcf44cc523b2222f88bbe9331d9439a997087790f9827f9dd6ea95bc4598af8",
        "bytes": 290832,
        "rows": 508,
    },
    str(G2_PHASE_A_ACCEPTANCE_PATH.resolve()): {
        "label": "g2_phase_a_acceptance",
        "sha256": "71734caa4821a4a2dff7e006cf612b7490878e626e3bac369c98ab4c205e775f",
        "bytes": 5100,
    },
}

ALLOWED_SOURCE_PATHS = (
    TASK030_SNAPSHOT_PATH,
    TASK030_MANIFEST_PATH,
    G2_POOL_PUBLIC_MANIFEST_PATH,
    G2_POOL_PUBLIC_ROWS_PATH,
    G2_PHASE_A_PUBLIC_SAMPLE_PATH,
    G2_PHASE_A_ACCEPTANCE_PATH,
)

NON_CLAIMS = [
    "This receipt only checks public review-sample integrity and does not claim G2 completion.",
    "This receipt includes no human judgment result, no reviewer workbook data, and no retrieval winner claim.",
    "This receipt exposes no relevance, grade, rank, strategy, docId, category, salt, or human-label payload.",
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
    if actual != {"manifest.json", "receipt.json"}:
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


def _scan_output_keys(node: Any) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            if lowered in BANNED_OUTPUT_KEYS:
                raise ValueError("banned output key marker")
            _scan_output_keys(value)
        return
    if isinstance(node, list):
        for item in node:
            _scan_output_keys(item)


def _validate_task030_binding() -> dict[str, Any]:
    snapshot = _read_json(TASK030_SNAPSHOT_PATH)
    manifest = _read_json(TASK030_MANIFEST_PATH)
    if snapshot["status"] != "PUBLIC_EVIDENCE_READY_BENCHMARK_DECISION_PENDING":
        raise ValueError("task030 snapshot status drift")
    if snapshot["publicEvidence"] != {
        "breadthDocuments": 499,
        "breadthVerticals": 12,
        "corpusDocuments": 46079,
        "phaseAPublicSampleRows": 508,
        "publicBlindRows": 21459,
        "publicQueries": 507,
    }:
        raise ValueError("task030 public evidence drift")
    source_pins_digest = _sha256_bytes(_canonical_json_bytes(snapshot["sourcePins"]))
    if source_pins_digest != TASK030_SOURCE_PINS_DIGEST:
        raise ValueError("task030 source pin digest drift")
    if manifest["canonicalDigest"] != "e3e945f0d80c1926780e5a93b8db714ed5ec46f306dbc5ba5204fe422e02afb4":
        raise ValueError("task030 manifest canonical drift")
    return {
        "snapshotSha256": _frozen_metadata_for_path(TASK030_SNAPSHOT_PATH)["sha256"],
        "manifestSha256": _frozen_metadata_for_path(TASK030_MANIFEST_PATH)["sha256"],
        "canonicalDigest": manifest["canonicalDigest"],
        "sourcePinsDigest": source_pins_digest,
    }


def _validate_public_pool_manifest() -> None:
    manifest = _read_json(G2_POOL_PUBLIC_MANIFEST_PATH)
    if set(manifest.keys()) != {"artifact", "physicalSeparation", "schemaVersion"}:
        raise ValueError("g2 public manifest key drift")
    if manifest["schemaVersion"] != "kuaisearch-multicategory-retrieval-g2-blind-pool-v1":
        raise ValueError("g2 public manifest schema drift")
    if manifest["physicalSeparation"] is not True:
        raise ValueError("g2 public manifest separation drift")
    expected_rows = _frozen_metadata_for_path(G2_POOL_PUBLIC_ROWS_PATH)
    if manifest["artifact"] != {
        "bytes": expected_rows["bytes"],
        "path": "review_rows.jsonl",
        "rows": expected_rows["rows"],
        "sha256": expected_rows["sha256"],
    }:
        raise ValueError("g2 public manifest artifact drift")


def _load_public_pool_rows() -> dict[tuple[str, str], dict[str, Any]]:
    pool_map: dict[tuple[str, str], dict[str, Any]] = {}
    row_count = 0
    positions: set[tuple[str, int]] = set()
    for raw_line in _read_text(G2_POOL_PUBLIC_ROWS_PATH).splitlines():
        if not raw_line.strip():
            continue
        row_count += 1
        row = json.loads(raw_line)
        if set(row.keys()) != {
            "schemaVersion",
            "blindCandidateId",
            "queryId",
            "query",
            "title",
            "brand",
            "seller_name",
            "attr_value",
            "poolPosition",
        }:
            raise ValueError("public pool row key drift")
        if row["schemaVersion"] != "kuaisearch-multicategory-retrieval-g2-blind-pool-v1":
            raise ValueError("public pool row schema drift")
        pair = (row["queryId"], row["blindCandidateId"])
        if pair in pool_map:
            raise ValueError("duplicate public pool pair")
        position_pair = (row["queryId"], row["poolPosition"])
        if position_pair in positions:
            raise ValueError("duplicate public pool position")
        positions.add(position_pair)
        pool_map[pair] = row
    if row_count != _frozen_metadata_for_path(G2_POOL_PUBLIC_ROWS_PATH)["rows"]:
        raise ValueError("public pool row count drift")
    return pool_map


def _extract_decision(text: str) -> str:
    match = re.search(r"Decision:\s*`([^`]+)`", text)
    if match is None:
        raise ValueError("missing g2 phase-a decision")
    return match.group(1)


def _extract_public_sample_rows_expected(text: str) -> int:
    match = re.search(r"Public sample: .*?,\s*([0-9]+)\s+rows", text)
    if match is None:
        raise ValueError("missing public sample rows")
    return int(match.group(1))


def _extract_unique_query_count_expected(text: str) -> int:
    match = re.search(r"48 queries", text)
    if match is None:
        raise ValueError("missing unique query count statement")
    return 48


def _extract_max_per_query_expected(text: str) -> int:
    match = re.search(r"maximum\s+([0-9]+)\s+per query", text)
    if match is None:
        raise ValueError("missing max per query statement")
    return int(match.group(1))


def _extract_train_test_query_counts_expected(text: str) -> tuple[int, int]:
    match = re.search(r"48 queries:\s*([0-9]+)\s+train\s+and\s+([0-9]+)\s+test", text)
    if match is None:
        raise ValueError("missing train/test query statement")
    return int(match.group(1)), int(match.group(2))


def _load_public_sample_rows(pool_map: dict[tuple[str, str], dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    rows: list[dict[str, Any]] = []
    pair_seen: set[tuple[str, str]] = set()
    review_ids: set[str] = set()
    per_query_counts: dict[str, int] = {}
    per_query_split: dict[str, str] = {}
    query_ids: set[str] = set()
    train_queries: set[str] = set()
    test_queries: set[str] = set()
    for raw_line in _read_text(G2_PHASE_A_PUBLIC_SAMPLE_PATH).splitlines():
        if not raw_line.strip():
            continue
        row = json.loads(raw_line)
        if set(row.keys()) != {
            "schemaVersion",
            "reviewItemId",
            "queryId",
            "query",
            "blindCandidateId",
            "title",
            "attr_value",
            "brand",
            "seller_name",
            "stratum",
            "split",
            "queryShape",
        }:
            raise ValueError("public sample row key drift")
        if row["schemaVersion"] != "kuaisearch-g2-human-review-phase-a-v1":
            raise ValueError("public sample row schema drift")
        if not re.fullmatch(r"G2A-[0-9]{3}", row["reviewItemId"]):
            raise ValueError("public sample review item id drift")
        if row["reviewItemId"] in review_ids:
            raise ValueError("duplicate public sample review item id")
        review_ids.add(row["reviewItemId"])
        if row["stratum"] not in {f"C{i:02d}" for i in range(1, 13)}:
            raise ValueError("public sample stratum drift")
        if row["split"] not in {"train", "test"}:
            raise ValueError("public sample split drift")
        if row["queryShape"] not in {"long", "seeded_fallback", "short", "test_holdout", "train"}:
            raise ValueError("public sample query shape drift")
        pair = (row["queryId"], row["blindCandidateId"])
        if pair in pair_seen:
            raise ValueError("duplicate public sample pair")
        pool_row = pool_map.get(pair)
        if pool_row is None:
            raise ValueError("public sample pair outside pool")
        for field in ("queryId", "query", "blindCandidateId", "title", "attr_value", "brand", "seller_name"):
            if row[field] != pool_row[field]:
                raise ValueError("public sample field drift against pool")
        pair_seen.add(pair)
        rows.append(row)
        query_id = row["queryId"]
        query_ids.add(query_id)
        per_query_counts[query_id] = per_query_counts.get(query_id, 0) + 1
        if per_query_counts[query_id] > 17:
            raise ValueError("public sample exceeds max per query")
        previous_split = per_query_split.get(query_id)
        if previous_split is None:
            per_query_split[query_id] = row["split"]
        elif previous_split != row["split"]:
            raise ValueError("public sample query split drift")
        if row["split"] == "train":
            train_queries.add(query_id)
        else:
            test_queries.add(query_id)
    expected_rows = _frozen_metadata_for_path(G2_PHASE_A_PUBLIC_SAMPLE_PATH)["rows"]
    if len(rows) != expected_rows:
        raise ValueError("public sample row count drift")
    if len(query_ids) != 48:
        raise ValueError("public sample unique query count drift")
    if len(train_queries) != 36 or len(test_queries) != 12:
        raise ValueError("public sample train/test query count drift")
    max_rows_per_query = max(per_query_counts.values(), default=0)
    return sorted(rows, key=lambda row: row["reviewItemId"]), {
        "uniqueQueries": len(query_ids),
        "trainQueries": len(train_queries),
        "testQueries": len(test_queries),
        "maxRowsPerQuery": max_rows_per_query,
    }


def _expected_receipt() -> dict[str, Any]:
    task030_binding = _validate_task030_binding()
    _validate_public_pool_manifest()
    pool_map = _load_public_pool_rows()
    acceptance_text = _read_text(G2_PHASE_A_ACCEPTANCE_PATH)
    if _extract_decision(acceptance_text) != "ACCEPT_HUMAN_REVIEW_PACKAGE / HUMAN_REVIEW_PENDING":
        raise ValueError("g2 phase-a acceptance drift")
    if _extract_public_sample_rows_expected(acceptance_text) != 508:
        raise ValueError("g2 public sample count statement drift")
    if _extract_unique_query_count_expected(acceptance_text) != 48:
        raise ValueError("g2 unique query statement drift")
    train_expected, test_expected = _extract_train_test_query_counts_expected(acceptance_text)
    if (train_expected, test_expected) != (36, 12):
        raise ValueError("g2 train/test statement drift")
    max_expected = _extract_max_per_query_expected(acceptance_text)
    sample_rows, sample_stats = _load_public_sample_rows(pool_map)
    if sample_stats["maxRowsPerQuery"] > max_expected:
        raise ValueError("g2 sample max-per-query drift")

    receipt = {
        "schemaVersion": RECEIPT_SCHEMA_VERSION,
        "receiptId": RECEIPT_ID,
        "attemptId": ATTEMPT_ID,
        "status": "G2_PUBLIC_REVIEW_SAMPLE_INTEGRITY_READY_HUMAN_REVIEW_PENDING",
        "createdOn": TODAY,
        "task030Binding": task030_binding,
        "summary": {
            "poolRows": 21459,
            "sampleRows": 508,
            "uniqueQueries": sample_stats["uniqueQueries"],
            "trainQueries": sample_stats["trainQueries"],
            "testQueries": sample_stats["testQueries"],
            "maxRowsPerQuery": sample_stats["maxRowsPerQuery"],
        },
        "sourcePins": [
            _build_source_pin(TASK030_SNAPSHOT_PATH),
            _build_source_pin(TASK030_MANIFEST_PATH),
            _build_source_pin(G2_POOL_PUBLIC_MANIFEST_PATH),
            _build_source_pin(G2_POOL_PUBLIC_ROWS_PATH),
            _build_source_pin(G2_PHASE_A_PUBLIC_SAMPLE_PATH),
            _build_source_pin(G2_PHASE_A_ACCEPTANCE_PATH),
        ],
        "publicReviewRows": sample_rows,
        "safety": {
            "modelCalls": 0,
            "toolCalls": 0,
            "networkCalls": 0,
            "businessWrites": 0,
        },
        "nonClaims": list(NON_CLAIMS),
    }
    _scan_output_keys(receipt)
    return receipt


def _manifest_code_pins() -> list[dict[str, str]]:
    return [
        {
            "path": str(path.relative_to(REPO_ROOT)).replace("\\", "/"),
            "sha256": _sha256_bytes(path.read_bytes()),
        }
        for path in (SCHEMA_PATH, MODULE_PATH, TEST_PATH)
    ]


def _artifact_entries(bundle_dir: Path) -> list[dict[str, Any]]:
    receipt_path = bundle_dir / "receipt.json"
    if not receipt_path.exists():
        raise ValueError("missing receipt artifact")
    payload = receipt_path.read_bytes()
    return [{"path": "receipt.json", "sha256": _sha256_bytes(payload), "bytes": len(payload)}]


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


def materialize_integrity_receipt(output_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Path]:
    schema = _load_schema()
    _assert_output_dir_ready_for_materialize(output_dir)
    receipt = _expected_receipt()
    receipt_path = output_dir / "receipt.json"
    _write_json(receipt_path, receipt)
    manifest = _manifest_payload(output_dir)
    manifest_path = output_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    _validate_schema_ref(schema, "receipt", receipt)
    _validate_schema_ref(schema, "manifest", manifest)
    validate_bundle(output_dir)
    return {"receipt": receipt_path, "manifest": manifest_path}


def validate_bundle(bundle_dir: Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    schema = _load_schema()
    _assert_expected_output_file_set(bundle_dir)
    receipt = json.loads((bundle_dir / "receipt.json").read_text(encoding="utf-8"))
    manifest = json.loads((bundle_dir / "manifest.json").read_text(encoding="utf-8"))
    _scan_output_keys(receipt)
    _validate_schema_ref(schema, "receipt", receipt)
    _validate_schema_ref(schema, "manifest", manifest)
    expected_receipt = _expected_receipt()
    if receipt != expected_receipt:
        raise ValueError("receipt drift")
    expected_manifest = _manifest_payload(bundle_dir)
    if manifest != expected_manifest:
        raise ValueError("manifest drift")
    return {"receipt": receipt, "manifest": manifest}
