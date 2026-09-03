"""Fail-closed G0 contract for KuaiSearch full-corpus retrieval.

This module only prepares immutable inputs and preregistration metadata.  It
does not import a ranker, read relevance labels, or execute retrieval.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping


CONTRACT_VERSION = "kuaisearch-multicategory-retrieval-contract-v1"
G0_VERSION = "kuaisearch-multicategory-retrieval-g0-20260824-r7"
EXPECTED_QUERY_ROWS = 507
EXPECTED_DOCUMENT_ROWS = 46079
EXPECTED_BREADTH_MANIFEST_SHA256 = "a3662a065043202e09fc604abf351a679b56fe151232b771de81dfd3158e2eba"
EXPECTED_DOCUMENTS_SHA256 = "6b8f55f94fb292e9ff946014221244e07fa338e22cfa29de39c42f8c2d1e245f"
CROSS_ENCODER_REVISION = "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e"
BM25_FIELD_WEIGHTS = {"title": 1.0, "attributeText": 0.45, "brand": 0.25}
FINAL_CODE_PIN_BASENAMES = (
    "kuaisearch_multicategory_retrieval_contract_v1.py",
    "prepare_kuaisearch_multicategory_retrieval_g0_v1.py",
    "kuaisearch_multicategory_retrieval_baseline_v1.py",
    "run_kuaisearch_multicategory_retrieval_baseline_v1.py",
    "kuaisearch_multicategory_retrieval_scorer_v1.py",
    "score_kuaisearch_multicategory_retrieval_g1_v1.py",
)
SCHEMA_BASENAMES = (
    "kuaisearch_multicategory_retrieval_g0_v1.schema.json",
    "kuaisearch_multicategory_retrieval_models_v1.schema.json",
    "kuaisearch_multicategory_retrieval_preregistration_v1.schema.json",
    "kuaisearch_multicategory_retrieval_query_v1.schema.json",
    "kuaisearch_multicategory_retrieval_score_v1.schema.json",
)
QUERY_KEYS = ("queryId", "query", "split")
FORBIDDEN_OUTPUT_KEYS = {
    "linkedcategory", "linkedcategorykeys", "category", "categorykey", "categorypath",
    "qrel", "qrels", "relevance", "sourcerelevance", "human", "humandecision",
    "humansamplelabel", "label", "gold", "oracle", "fault",
}


class ContractError(ValueError):
    """Input failed a G0 safety gate."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_index_identity(documents_sha256: str) -> str:
    """Match baseline.build_receipt's canonical index identity exactly."""
    payload = {
        "documentsSha256": documents_sha256,
        "fields": BM25_FIELD_WEIGHTS,
        "tokenizer": "mixed-latin-single-chinese-plus-bigram-v1",
        "denseTextField": "title",
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def schema_source_paths() -> list[Path]:
    root = Path(__file__).resolve().parent / "schemas"
    return [root / name for name in SCHEMA_BASENAMES]


def schema_pins() -> list[dict[str, Any]]:
    pins = []
    for path in schema_source_paths():
        _ensure_file(path, "schema source")
        pins.append({"path": f"schemas/{path.name}", "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return pins


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"missing input: {path}") from exc
    with handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ContractError(f"invalid JSONL at {path}:{number}") from exc
            if not isinstance(row, dict):
                raise ContractError(f"JSONL row is not an object at {path}:{number}")
            yield row


def _ensure_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise ContractError(f"missing {label}: {path}")


def _ensure_empty_directory(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise FileExistsError(f"refusing to overwrite non-empty output: {path}")


def _key_names(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key).replace("_", "").lower()
            yield from _key_names(child)
    elif isinstance(value, list):
        for child in value:
            yield from _key_names(child)


def project_queries(path: Path, *, expected_rows: int = EXPECTED_QUERY_ROWS) -> list[dict[str, str]]:
    """Project breadth rows before any ranker-facing call.

    The source rows intentionally contain category linkage.  It is accepted as
    an input-only field, then discarded; the returned objects have an exact
    three-key shape and are safe to pass to retrieval code.
    """
    projected: list[dict[str, str]] = []
    ids: set[str] = set()
    for row in read_jsonl(path):
        for key in QUERY_KEYS:
            if key not in row or not isinstance(row[key], str) or not row[key].strip():
                raise ContractError(f"query missing non-empty {key}")
        query_id = row["queryId"]
        if query_id in ids:
            raise ContractError(f"duplicate queryId: {query_id}")
        ids.add(query_id)
        projected.append({key: row[key] for key in QUERY_KEYS})
    if len(projected) != expected_rows or len(ids) != expected_rows:
        raise ContractError(f"expected {expected_rows} unique queries, got {len(projected)}")
    if any(set(row) != set(QUERY_KEYS) for row in projected):
        raise ContractError("query projection contains non-contract fields")
    if any(key in FORBIDDEN_OUTPUT_KEYS for row in projected for key in _key_names(row)):
        raise ContractError("forbidden label/category field escaped query projection")
    return projected


def freeze_jsonl(path: Path, *, label: str, expected_sha256: str | None = None,
                 expected_rows: int | None = None) -> dict[str, Any]:
    _ensure_file(path, label)
    actual_sha = sha256_file(path)
    if expected_sha256 and actual_sha != expected_sha256:
        raise ContractError(f"{label} SHA drift: {actual_sha}")
    rows = sum(1 for _ in read_jsonl(path)) if expected_rows is not None else None
    if expected_rows is not None and rows != expected_rows:
        raise ContractError(f"{label} row drift: {rows}")
    result: dict[str, Any] = {"path": path.as_posix(), "sha256": actual_sha, "bytes": path.stat().st_size}
    if rows is not None:
        result["rows"] = rows
    return result


def freeze_model_snapshot(path: Path, *, label: str) -> dict[str, Any]:
    if not path.is_dir():
        raise ContractError(f"missing {label} cache: {path}")
    def stable_file(item: Path) -> bool:
        relative = item.relative_to(path)
        parts = {part.lower() for part in relative.parts}
        name = item.name.lower()
        return item.is_file() and "__pycache__" not in parts and not name.endswith((".pyc", ".pyo", ".tmp", ".temp", ".swp", ".lock", "~"))

    files = [item for item in path.rglob("*") if stable_file(item)]
    if not files:
        raise ContractError(f"empty {label} cache: {path}")
    records = []
    for item in sorted(files, key=lambda p: p.relative_to(path).as_posix()):
        relative = item.relative_to(path).as_posix()
        records.append({"path": relative, "sha256": sha256_file(item), "bytes": item.stat().st_size})
    if label.lower().startswith("dense") and not any(item["path"].lower().endswith(".onnx") for item in records):
        raise ContractError("Dense cache has no ONNX file")
    return {"label": label, "root": path.as_posix(), "files": records,
            "fileCount": len(records), "snapshotSha256": hashlib.sha256(canonical_json(records)).hexdigest()}


def verify_snapshot_pin(snapshot: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    actual = freeze_model_snapshot(Path(str(snapshot["root"])), label=label)
    expected_files = snapshot.get("files")
    if not isinstance(expected_files, list) or actual["files"] != expected_files:
        raise ContractError(f"{label} SHA drift")
    return actual


def _bundle_distribution_version(root: Path, distribution: str) -> str:
    normalized = distribution.lower().replace("-", "_")
    candidates = []
    for metadata in root.rglob("METADATA"):
        if ".dist-info" not in metadata.parent.name.lower():
            continue
        fields: dict[str, str] = {}
        for line in metadata.read_text(encoding="utf-8", errors="strict").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                fields[key.strip().lower()] = value.strip()
        if fields.get("name", "").lower().replace("-", "_") == normalized and fields.get("version"):
            candidates.append(fields["version"])
    if len(set(candidates)) != 1:
        raise ContractError(f"missing or ambiguous runtime dependency version: {distribution}")
    return candidates[0]


def environment_pin(cross_deps: Path) -> dict[str, Any]:
    packages: dict[str, str | None] = {}
    for name in ("fastembed", "onnxruntime", "torch", "transformers", "huggingface_hub"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    if os.environ.get("HF_HUB_OFFLINE", "").lower() not in {"1", "true", "yes"}:
        raise ContractError("formal G0 requires HF_HUB_OFFLINE=true")
    total_ram: int | None = None
    try:
        import psutil
        total_ram = int(psutil.virtual_memory().total)
    except (ImportError, AttributeError, OSError):
        pass
    gpu_name: str | None = None
    gpu_vram: int | None = None
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = str(torch.cuda.get_device_name(0))
            gpu_vram = int(torch.cuda.get_device_properties(0).total_memory)
    except (ImportError, AttributeError, RuntimeError):
        pass
    runtime_overlay = {"huggingface_hub": _bundle_distribution_version(cross_deps, "huggingface_hub")}
    host_packages = dict(packages)
    packages.update(runtime_overlay)
    return {"python": sys.version.split()[0], "platform": platform.platform(), "packages": packages,
            "hostPackages": host_packages, "runtimeOverlay": runtime_overlay,
            "offline": True, "networkPolicy": "NO_NETWORK_CACHE_ONLY",
            "hardware": {"gpu": {"name": gpu_name, "totalVramBytes": gpu_vram},
                         "cpu": platform.processor(), "platform": platform.platform(),
                         "totalRamBytes": total_ram, "python": sys.version.split()[0]}}


def verify_environment_binding(expected: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    """Require exact equality for every frozen environment evidence field."""
    if dict(expected) != dict(current):
        raise ContractError("runtime environment evidence drift")


def build_preregistration(*, code_paths: list[Path], dense: Mapping[str, Any], cross_encoder: Mapping[str, Any],
                          dependencies: Mapping[str, Any], documents_sha256: str,
                          schema_pin_rows: list[dict[str, Any]], top_k: int, seed: int) -> dict[str, Any]:
    if top_k <= 0 or seed < 0:
        raise ContractError("invalid topK or seed")
    if {path.name for path in code_paths} != set(FINAL_CODE_PIN_BASENAMES):
        raise ContractError("code pin must cover the six final contract/baseline/runner/scorer files")
    code = []
    for path in code_paths:
        _ensure_file(path, "code pin")
        code.append({"path": path.as_posix(), "sha256": sha256_file(path), "bytes": path.stat().st_size})
    return {
        "contractVersion": CONTRACT_VERSION,
        "systems": {
            "bm25": {"fieldWeights": {"title": 1.0, "attributeText": 0.45, "brand": 0.25},
                     "titleControl": {"enabled": True, "field": "title"}},
            "dense": {"model": "BAAI/bge-small-zh-v1.5", "input": "title", "dimension": 512,
                      "normalization": "L2", "normalize": True},
            "rrf": {"k": 60, "topK": top_k, "systems": ["bm25", "dense"]},
            "crossEncoder": {"model": "BAAI/bge-reranker-v2-m3", "revision": CROSS_ENCODER_REVISION,
                              "input": "rrf_top20", "candidateLimit": 20, "reordersOnly": True,
                              "dependencies": dict(dependencies)},
        },
        "topK": top_k, "seed": seed, "indexIdentity": compute_index_identity(documents_sha256),
        "expectedOutputs": {"queryCount": EXPECTED_QUERY_ROWS, "strategies": 5, "topK": 100,
                            "crossEncoderRepeatQueries": EXPECTED_QUERY_ROWS,
                            "crossEncoderRepeatMismatchCount": 0},
        "memoryMeasurement": {"unit": "process RSS sampled", "isTruePeak": False,
                               "points": ["before_build", "after_bm25", "after_dense", "after_ce_load", "after_query_batch"]},
        "code": code, "schemaPins": schema_pin_rows, "dense": dict(dense), "crossEncoder": dict(cross_encoder),
        "environment": environment_pin(Path(str(dependencies["root"]))),
    }


def write_json(path: Path, value: Any) -> None:
    path.write_bytes(canonical_json(value))


def prepare_g0(*, output_dir: Path, breadth_manifest: Path, breadth_queries: Path, documents: Path,
              dense_cache: Path, cross_encoder_cache: Path, cross_encoder_deps: Path,
              code_paths: list[Path], top_k: int = 100, seed: int = 0) -> dict[str, Any]:
    """Prepare G0 artifacts. Every validation happens before output is created."""
    _ensure_empty_directory(output_dir)
    manifest_pin = freeze_jsonl(breadth_manifest, label="breadth manifest", expected_sha256=EXPECTED_BREADTH_MANIFEST_SHA256)
    document_pin = freeze_jsonl(documents, label="full documents", expected_sha256=EXPECTED_DOCUMENTS_SHA256,
                                expected_rows=EXPECTED_DOCUMENT_ROWS)
    queries = project_queries(breadth_queries)
    dense = freeze_model_snapshot(dense_cache, label="Dense ONNX")
    cross_encoder = freeze_model_snapshot(cross_encoder_cache, label="CrossEncoder snapshot")
    dependencies = freeze_model_snapshot(cross_encoder_deps, label="CrossEncoder dependencies")
    schema_pin_rows = schema_pins()
    prereg = build_preregistration(code_paths=code_paths, dense=dense, cross_encoder=cross_encoder,
                                   dependencies=dependencies, documents_sha256=document_pin["sha256"],
                                   schema_pin_rows=schema_pin_rows, top_k=top_k, seed=seed)

    output_dir.mkdir(parents=True)
    schemas_dir = output_dir / "schemas"
    schemas_dir.mkdir()
    for source in schema_source_paths():
        shutil.copyfile(source, schemas_dir / source.name)
    (output_dir / "queries.jsonl").write_bytes(b"".join(canonical_json(row) for row in queries))
    (output_dir / "documents.jsonl").write_bytes(documents.read_bytes())
    write_json(output_dir / "inputs.manifest.json", {"contractVersion": CONTRACT_VERSION, "queryCount": len(queries),
        "queries": {"path": "queries.jsonl", "sha256": sha256_file(output_dir / "queries.jsonl"), "rows": len(queries)},
        "documents": {**document_pin, "path": "documents.jsonl"}, "breadthManifest": manifest_pin})
    write_json(output_dir / "models.manifest.json", {"contractVersion": CONTRACT_VERSION, "dense": dense,
        "crossEncoder": cross_encoder, "crossEncoderDependencies": dependencies})
    write_json(output_dir / "preregistration.json", prereg)
    artifacts = []
    for item in sorted((item for item in output_dir.rglob("*") if item.is_file()), key=lambda p: p.relative_to(output_dir).as_posix()):
        artifacts.append({"path": item.relative_to(output_dir).as_posix(), "sha256": sha256_file(item), "bytes": item.stat().st_size})
    freeze = {"g0Version": G0_VERSION, "contractVersion": CONTRACT_VERSION, "status": "PREPARED_NOT_RANKED",
              "inputCount": {"queries": len(queries), "documents": EXPECTED_DOCUMENT_ROWS}, "artifacts": artifacts}
    write_json(output_dir / "manifest.json", freeze)
    return freeze


def _load_json(path: Path, label: str) -> dict[str, Any]:
    _ensure_file(path, label)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} is not an object: {path}")
    return value


def _validate_schema(instance: Mapping[str, Any], schema_name: str) -> None:
    _validate_schema_path(instance, Path(__file__).resolve().parent / "schemas" / schema_name, schema_name)


def _validate_schema_path(instance: Mapping[str, Any], schema_path: Path, schema_name: str) -> None:
    try:
        from jsonschema import Draft202012Validator
        schema = _load_json(schema_path, f"schema {schema_name}")
        Draft202012Validator(schema).validate(instance)
    except ImportError as exc:
        raise ContractError("jsonschema is required for formal G0 verification") from exc
    except Exception as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError(f"schema validation failed: {schema_name}: {exc}") from exc


def _resolve_from(base: Path, raw: str) -> Path:
    candidate = Path(raw)
    if candidate.exists():
        return candidate
    return base / candidate


def _verify_current_snapshot(snapshot: Mapping[str, Any], *, label: str, base: Path) -> dict[str, Any]:
    root = _resolve_from(base, str(snapshot.get("root", "")))
    expected = dict(snapshot)
    expected["root"] = root.as_posix()
    actual = freeze_model_snapshot(root, label=label)
    if actual["files"] != snapshot.get("files") or actual["snapshotSha256"] != snapshot.get("snapshotSha256"):
        raise ContractError(f"{label} SHA drift")
    return actual


def verify_g0_bundle(*, g0_dir: Path, runtime_code_paths: list[Path], dense_cache: Path,
                     cross_cache: Path, cross_deps: Path, strict: bool = False) -> dict[str, Any]:
    """Verify a prepared r7 bundle immediately before a formal run.

    This is deliberately read-only and ranker-free: it validates the frozen
    artifacts, code/model pins, safe query projection, and preregistered
    runtime semantics before any runner is allowed to load them.
    """
    g0_dir = Path(g0_dir)
    manifest = _load_json(g0_dir / "manifest.json", "G0 manifest")
    embedded_schema_dir = g0_dir / "schemas"
    embedded_manifest_schema = embedded_schema_dir / "kuaisearch_multicategory_retrieval_g0_v1.schema.json"
    _validate_schema_path(manifest, embedded_manifest_schema, embedded_manifest_schema.name)
    if manifest.get("g0Version") != G0_VERSION:
        raise ContractError("G0 version mismatch")
    artifact_rows = manifest.get("artifacts")
    if not isinstance(artifact_rows, list) or len(artifact_rows) != 10:
        raise ContractError("G0 manifest must pin ten artifacts including five schemas")
    expected_names = {"documents.jsonl", "queries.jsonl", "inputs.manifest.json", "models.manifest.json", "preregistration.json"} | {f"schemas/{name}" for name in SCHEMA_BASENAMES}
    if {row.get("path") for row in artifact_rows if isinstance(row, dict)} != expected_names:
        raise ContractError("G0 artifact set drift")
    for row in artifact_rows:
        if not isinstance(row, dict):
            raise ContractError("invalid G0 artifact pin")
        path = g0_dir / str(row.get("path"))
        _ensure_file(path, "G0 artifact")
        if path.stat().st_size != row.get("bytes") or sha256_file(path) != row.get("sha256"):
            raise ContractError(f"G0 artifact SHA/bytes drift: {path.name}")
    actual_files = {path.relative_to(g0_dir).as_posix() for path in g0_dir.rglob("*") if path.is_file()}
    if actual_files != expected_names | {"manifest.json"}:
        raise ContractError("unexpected G0 bundle file")

    prereg = _load_json(g0_dir / "preregistration.json", "preregistration")
    schema_pin_rows = prereg.get("schemaPins")
    if not isinstance(schema_pin_rows, list) or {row.get("path") for row in schema_pin_rows if isinstance(row, dict)} != {f"schemas/{name}" for name in SCHEMA_BASENAMES}:
        raise ContractError("G0 schema pin set drift")
    source_by_name = {path.name: path for path in schema_source_paths()}
    for row in schema_pin_rows:
        embedded = g0_dir / str(row["path"])
        source = source_by_name[Path(str(row["path"])).name]
        if sha256_file(embedded) != row.get("sha256") or embedded.stat().st_size != row.get("bytes"):
            raise ContractError(f"embedded schema SHA drift: {embedded.name}")
        if sha256_file(source) != row.get("sha256") or source.stat().st_size != row.get("bytes"):
            raise ContractError(f"current schema SHA drift: {source.name}")

    inputs = _load_json(g0_dir / "inputs.manifest.json", "inputs manifest")
    queries_path = g0_dir / str(inputs.get("queries", {}).get("path", ""))
    documents_path = g0_dir / str(inputs.get("documents", {}).get("path", ""))
    query_pin = freeze_jsonl(queries_path, label="G0 queries", expected_rows=EXPECTED_QUERY_ROWS)
    docs_pin = freeze_jsonl(documents_path, label="G0 documents", expected_sha256=EXPECTED_DOCUMENTS_SHA256,
                            expected_rows=EXPECTED_DOCUMENT_ROWS)
    if query_pin["sha256"] != inputs.get("queries", {}).get("sha256") or query_pin["rows"] != inputs.get("queries", {}).get("rows"):
        raise ContractError("G0 query input pin drift")
    if docs_pin["sha256"] != inputs.get("documents", {}).get("sha256"):
        raise ContractError("G0 document input pin drift")
    projected = list(read_jsonl(queries_path))
    if len(projected) != EXPECTED_QUERY_ROWS or len({row.get("queryId") for row in projected}) != EXPECTED_QUERY_ROWS:
        raise ContractError("G0 query count/identity drift")
    if any(set(row) != set(QUERY_KEYS) for row in projected):
        raise ContractError("G0 query contains non-projection fields")
    for row in projected:
        _validate_schema_path(row, embedded_schema_dir / "kuaisearch_multicategory_retrieval_query_v1.schema.json", "embedded query schema")
    if any(key in FORBIDDEN_OUTPUT_KEYS for row in projected for key in _key_names(row)):
        raise ContractError("G0 query contains forbidden label/category field")

    models = _load_json(g0_dir / "models.manifest.json", "models manifest")
    _validate_schema_path(models, embedded_schema_dir / "kuaisearch_multicategory_retrieval_models_v1.schema.json", "embedded models schema")
    _validate_schema_path(prereg, embedded_schema_dir / "kuaisearch_multicategory_retrieval_preregistration_v1.schema.json", "embedded preregistration schema")
    code = prereg.get("code")
    if not isinstance(code, list) or {Path(str(row.get("path"))).name for row in code} != set(FINAL_CODE_PIN_BASENAMES):
        raise ContractError("G0 code pin set drift")
    if {path.name for path in runtime_code_paths} != set(FINAL_CODE_PIN_BASENAMES):
        raise ContractError("runtime code pin set is incomplete")
    runtime_by_name = {path.name: path for path in runtime_code_paths}
    prereg_by_name = {Path(str(row["path"])).name: row for row in code}
    for name in FINAL_CODE_PIN_BASENAMES:
        current = runtime_by_name[name]
        _ensure_file(current, "runtime code pin")
        if sha256_file(current) != prereg_by_name[name].get("sha256"):
            raise ContractError(f"runtime code SHA drift: {name}")

    dense = models["dense"]
    cross = models["crossEncoder"]
    deps = models["crossEncoderDependencies"]
    _verify_current_snapshot(dense, label="Dense ONNX", base=Path.cwd())
    _verify_current_snapshot(cross, label="CrossEncoder snapshot", base=Path.cwd())
    _verify_current_snapshot(deps, label="CrossEncoder dependencies", base=Path.cwd())
    if prereg.get("dense", {}).get("snapshotSha256") != dense.get("snapshotSha256") or prereg.get("crossEncoder", {}).get("snapshotSha256") != cross.get("snapshotSha256"):
        raise ContractError("prereg model snapshot drift")
    if prereg.get("systems", {}).get("crossEncoder", {}).get("dependencies", {}).get("snapshotSha256") != deps.get("snapshotSha256"):
        raise ContractError("prereg dependency snapshot drift")
    if dense.get("root") and _resolve_from(Path.cwd(), str(dense["root"])).resolve() != Path(dense_cache).resolve():
        raise ContractError("Dense cache path drift")
    if cross.get("root") and _resolve_from(Path.cwd(), str(cross["root"])).resolve() != Path(cross_cache).resolve():
        raise ContractError("CrossEncoder cache path drift")
    if deps.get("root") and _resolve_from(Path.cwd(), str(deps["root"])).resolve() != Path(cross_deps).resolve():
        raise ContractError("CrossEncoder dependency path drift")

    systems = prereg.get("systems", {})
    if systems.get("bm25") != {"fieldWeights": BM25_FIELD_WEIGHTS, "titleControl": {"enabled": True, "field": "title"}}:
        raise ContractError("BM25 preregistration drift")
    if systems.get("dense") != {"model": "BAAI/bge-small-zh-v1.5", "input": "title", "dimension": 512, "normalization": "L2", "normalize": True}:
        raise ContractError("Dense preregistration drift")
    if systems.get("rrf") != {"k": 60, "topK": 100, "systems": ["bm25", "dense"]}:
        raise ContractError("RRF preregistration drift")
    if systems.get("crossEncoder", {}).get("revision") != CROSS_ENCODER_REVISION or systems.get("crossEncoder", {}).get("input") != "rrf_top20" or systems.get("crossEncoder", {}).get("candidateLimit") != 20 or systems.get("crossEncoder", {}).get("reordersOnly") is not True:
        raise ContractError("CrossEncoder preregistration drift")
    if prereg.get("indexIdentity") != compute_index_identity(docs_pin["sha256"]):
        raise ContractError("indexIdentity drift")
    if prereg.get("topK") != 100 or prereg.get("seed") != 0:
        raise ContractError("topK/seed preregistration drift")
    if prereg.get("environment", {}).get("offline") is not True or prereg.get("environment", {}).get("networkPolicy") != "NO_NETWORK_CACHE_ONLY":
        raise ContractError("formal environment is not offline fail-closed")
    runtime_root = _resolve_from(Path.cwd(), str(deps.get("root", "")))
    runtime_version = _bundle_distribution_version(runtime_root, "huggingface_hub")
    environment = prereg.get("environment", {})
    if environment.get("runtimeOverlay", {}).get("huggingface_hub") != runtime_version or environment.get("packages", {}).get("huggingface_hub") != runtime_version:
        raise ContractError("runtime huggingface_hub version drift")
    verify_environment_binding(environment, environment_pin(runtime_root))
    expected = prereg.get("expectedOutputs")
    if expected != {"queryCount": 507, "strategies": 5, "topK": 100, "crossEncoderRepeatQueries": 507, "crossEncoderRepeatMismatchCount": 0}:
        raise ContractError("expected output contract drift")
    memory = prereg.get("memoryMeasurement")
    if memory != {"unit": "process RSS sampled", "isTruePeak": False, "points": ["before_build", "after_bm25", "after_dense", "after_ce_load", "after_query_batch"]}:
        raise ContractError("memory measurement contract drift")
    binding = {
        "manifestSha256": sha256_file(g0_dir / "manifest.json"),
        "inputsManifestSha256": sha256_file(g0_dir / "inputs.manifest.json"),
        "modelsManifestSha256": sha256_file(g0_dir / "models.manifest.json"),
        "preregistrationSha256": sha256_file(g0_dir / "preregistration.json"),
        "codePins": [{"path": name, "sha256": prereg_by_name[name]["sha256"]} for name in FINAL_CODE_PIN_BASENAMES],
    }
    if strict and (len(binding["codePins"]) != 6 or any(len(item["sha256"]) != 64 for item in binding["codePins"])):
        raise ContractError("incomplete strict G0 binding")
    return {"status": "VERIFIED", "g0Version": G0_VERSION, "queries": EXPECTED_QUERY_ROWS,
            "documents": EXPECTED_DOCUMENT_ROWS, "indexIdentity": prereg["indexIdentity"],
            "artifactCount": len(artifact_rows), "g0Binding": binding}
