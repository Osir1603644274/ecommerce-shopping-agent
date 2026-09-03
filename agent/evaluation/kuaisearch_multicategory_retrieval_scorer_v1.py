"""Closed-source-qrel diagnostics and blind G2 pooling for KuaiSearch.

Rankings are intentionally loaded before labels.  This module never treats an
unjudged document as a negative and only reports source-pair diagnostics;
Hit@K/MRR here are not exhaustive retrieval quality measures.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "kuaisearch-multicategory-retrieval-score-v1"
POOL_SCHEMA_VERSION = "kuaisearch-multicategory-retrieval-g2-blind-pool-v1"
RANKING_STRATEGIES = (
    "bm25_fields",
    "bm25_title",
    "dense_title",
    "rrf_bm25_dense",
    "rrf_bm25_dense_ce",
)
RANKING_SCHEMA_VERSION = "kuaisearch-multicategory-retrieval-baseline-v1"
EXPECTED_QUERY_COUNT = 507
EXPECTED_DOCUMENT_COUNT = 46079
EXPECTED_RANKING_ROWS = 2535
EXPECTED_LABELS_DATASET_MANIFEST_SHA256 = "a3662a065043202e09fc604abf351a679b56fe151232b771de81dfd3158e2eba"
EXPECTED_LABEL_ARTIFACTS = {
    "source_qrels.jsonl": {"sha256": "a6eb6659a8534ad1c3aa4baff0a7a96625e78194e6653cf3fe38a816d329c77c", "rows": 507},
    "categories.jsonl": {"sha256": "3cd13ed702312799f8d721a1ac74e43d8baf121f03c521720774390d90398c1f", "rows": 12},
    "human_sample_labels.jsonl": {"sha256": "f328fc436fbc1bbe893ca7cceacda7fbba2e577f36be43952dfe8d7038c03b19", "rows": 90},
}
K_VALUES = (1, 5, 10, 20, 50, 100)


class ScoringInputError(ValueError):
    """Raised when frozen rankings or labels violate the scoring contract."""


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScoringInputError(f"missing input: {path}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ScoringInputError(f"invalid JSONL at {path}:{number}") from exc
        if not isinstance(row, dict):
            raise ScoringInputError(f"JSONL row is not an object at {path}:{number}")
        rows.append(row)
    return rows


def load_rankings(path: Path, *, expected_strategies: Sequence[str] = RANKING_STRATEGIES, strict: bool = True) -> list[dict[str, Any]]:
    """Load canonical ``rankings_top100.jsonl`` rows.

    The canonical interface has six fields.  ``split`` is accepted solely as
    a backward-compatible baseline extension and is retained for stratified
    reporting; no label/category data enters this loader.
    """
    rows = read_jsonl(path)
    if not rows:
        raise ScoringInputError("rankings must not be empty")
    allowed = {"schemaVersion", "queryId", "strategy", "rankedDocIds", "queryLatencyMs", "split"}
    seen: set[tuple[str, str]] = set()
    schema_version: str | None = None
    for row in rows:
        if set(row) - allowed or not {"schemaVersion", "queryId", "strategy", "rankedDocIds", "queryLatencyMs"}.issubset(row):
            raise ScoringInputError("ranking rows must use the canonical field set")
        if not isinstance(row["schemaVersion"], str) or not row["schemaVersion"]:
            raise ScoringInputError("ranking schemaVersion must be non-empty")
        if strict and row["schemaVersion"] != RANKING_SCHEMA_VERSION:
            raise ScoringInputError("ranking schemaVersion must be the pinned baseline-v1 version")
        schema_version = schema_version or row["schemaVersion"]
        if row["schemaVersion"] != schema_version:
            raise ScoringInputError("ranking schemaVersion drift")
        query_id, strategy = str(row["queryId"]), str(row["strategy"])
        if not query_id or strategy not in expected_strategies:
            raise ScoringInputError("unknown or empty query/strategy")
        key = (query_id, strategy)
        if key in seen:
            raise ScoringInputError(f"duplicate ranking row: {key}")
        seen.add(key)
        ids = row["rankedDocIds"]
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(doc, str) or not doc for doc in ids):
            raise ScoringInputError("rankedDocIds must be a list of at most 100 non-empty strings")
        if len(ids) != len(set(ids)):
            raise ScoringInputError(f"duplicate document in ranking: {key}")
        latency = row["queryLatencyMs"]
        if isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0:
            raise ScoringInputError("queryLatencyMs must be a non-negative number")
        if "split" in row and (not isinstance(row["split"], str) or not row["split"]):
            raise ScoringInputError("split must be a non-empty string when present")
    if strict:
        if len(rows) != EXPECTED_RANKING_ROWS:
            raise ScoringInputError(f"expected {EXPECTED_RANKING_ROWS} ranking rows, got {len(rows)}")
        query_ids = {str(row["queryId"]) for row in rows}
        if len(query_ids) != EXPECTED_QUERY_COUNT:
            raise ScoringInputError(f"expected {EXPECTED_QUERY_COUNT} unique queries, got {len(query_ids)}")
        if any(len(row["rankedDocIds"]) != 100 for row in rows):
            raise ScoringInputError("formal rankings require exactly 100 unique documents per row")
        for strategy in expected_strategies:
            strategy_rows = [row for row in rows if row["strategy"] == strategy]
            if len(strategy_rows) != EXPECTED_QUERY_COUNT or {str(row["queryId"]) for row in strategy_rows} != query_ids:
                raise ScoringInputError(f"strategy must cover exactly 507 queries: {strategy}")
        by_query_strategy = {(str(row["queryId"]), str(row["strategy"])): row for row in rows}
        for query_id in sorted(query_ids):
            rrf = by_query_strategy[(query_id, "rrf_bm25_dense")]["rankedDocIds"]
            ce = by_query_strategy[(query_id, "rrf_bm25_dense_ce")]["rankedDocIds"]
            if set(ce[:20]) != set(rrf[:20]) or ce[20:] != rrf[20:]:
                raise ScoringInputError(f"CE must reorder only the RRF top20 for query {query_id}")
    return rows


def load_source_qrels(path: Path) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(path)
    if not rows:
        raise ScoringInputError("source qrels must not be empty")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        required = {"queryId", "docId", "sourceRelevance", "split", "categoryKey"}
        if not required.issubset(row) or row.get("labelScope") != "SPARSE_SOURCE_LABEL_NOT_EXHAUSTIVE_GOLD":
            raise ScoringInputError("source qrel is missing identity or sparse-label scope")
        qid = str(row["queryId"])
        if not qid or qid in result:
            raise ScoringInputError(f"source qrels must contain one row per query: {qid}")
        grade = row["sourceRelevance"]
        if isinstance(grade, bool) or not isinstance(grade, int) or grade not in (0, 1, 2, 3):
            raise ScoringInputError("sourceRelevance must be an integer grade 0..3")
        result[qid] = {
            "queryId": qid, "docId": str(row["docId"]), "grade": grade,
            "split": str(row["split"]), "categoryKey": str(row["categoryKey"]),
        }
    return result


def load_categories(path: Path) -> dict[str, dict[str, Any]]:
    rows = read_jsonl(path)
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("categoryKey") or "")
        if not key or key in result:
            raise ScoringInputError(f"duplicate or empty categoryKey: {key}")
        result[key] = row
    if not result:
        raise ScoringInputError("categories must not be empty")
    return result


def load_human_sample_labels(path: Path) -> list[dict[str, Any]]:
    rows = [row for row in read_jsonl(path) if row.get("sampleKind") == "QUERY_LABEL"]
    seen: set[str] = set()
    for row in rows:
        qid = str(row.get("queryId") or "")
        if not qid or qid in seen:
            raise ScoringInputError(f"human query sample IDs must be unique: {qid}")
        if not row.get("docId") or row.get("sourceRelevance") not in (0, 1, 2, 3):
            raise ScoringInputError("human query sample has invalid source label")
        seen.add(qid)
    return rows


def load_labels_dataset_dir(path: Path) -> dict[str, Any]:
    """Validate and resolve the immutable frozen label dataset."""
    root = Path(path).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file() or sha256_file(manifest_path) != EXPECTED_LABELS_DATASET_MANIFEST_SHA256:
        raise ScoringInputError("labels dataset manifest SHA mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoringInputError("invalid labels dataset manifest") from exc
    artifacts = {str(row.get("path")): row for row in manifest.get("artifacts", []) if isinstance(row, dict)}
    for name, expected in EXPECTED_LABEL_ARTIFACTS.items():
        row = artifacts.get(name)
        artifact_path = root / name
        if not isinstance(row, dict) or row.get("sha256") != expected["sha256"] or row.get("rows") != expected["rows"] or not artifact_path.is_file() or sha256_file(artifact_path) != expected["sha256"] or len(read_jsonl(artifact_path)) != expected["rows"]:
            raise ScoringInputError(f"labels dataset artifact pin mismatch: {name}")
    human_rows = load_human_sample_labels(root / "human_sample_labels.jsonl")
    if len(human_rows) != 54:
        raise ScoringInputError("labels dataset must contain exactly 54 QUERY_LABEL rows")
    return {"root": root, "manifest": manifest_path, "manifestSha256": EXPECTED_LABELS_DATASET_MANIFEST_SHA256, "sourceQrels": root / "source_qrels.jsonl", "categories": root / "categories.jsonl", "humanSampleLabels": root / "human_sample_labels.jsonl"}


def _validate_g0_binding(g0_dir: Path, binding: Mapping[str, Any]) -> dict[str, Any]:
    """Run the shared read-only G0 validator and require exact binding identity."""
    try:
        from evaluation.kuaisearch_multicategory_retrieval_baseline_v1 import validate_g0_directory
        verified = validate_g0_directory(Path(g0_dir))
    except Exception as exc:
        raise ScoringInputError(f"G0 validation failed closed: {exc}") from exc
    expected = verified.get("g0Binding")
    if not isinstance(binding, Mapping) or binding != expected:
        raise ScoringInputError("ranking manifest g0Binding does not match the supplied G0 bundle")
    if len(binding.get("codePins", {})) != 6:
        raise ScoringInputError("G0 binding must contain six code pins")
    return verified


def _validate_ranking_manifest(path: Path, rankings_path: Path, *, g0_dir: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoringInputError(f"invalid ranking manifest: {path}") from exc
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != RANKING_SCHEMA_VERSION:
        raise ScoringInputError("ranking manifest schemaVersion must be the pinned baseline-v1 version")
    binding = manifest.get("g0Binding")
    if not isinstance(binding, dict):
        raise ScoringInputError("ranking manifest g0Binding is required")
    verified_g0 = _validate_g0_binding(Path(g0_dir), binding)
    outputs = manifest.get("outputs")
    output = outputs.get("rankings_top100.jsonl") if isinstance(outputs, dict) else None
    if not isinstance(output, dict) or output.get("rows") != EXPECTED_RANKING_ROWS:
        raise ScoringInputError("ranking manifest output row count must be 2535")
    actual_sha = sha256_file(Path(rankings_path))
    actual_bytes = Path(rankings_path).stat().st_size
    if output.get("sha256") != actual_sha or output.get("bytes") != actual_bytes:
        raise ScoringInputError("ranking manifest rankings output SHA/bytes mismatch")
    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise ScoringInputError("ranking manifest inputs are required")
    query_input = inputs.get("queries")
    document_input = inputs.get("documents")
    if not isinstance(query_input, dict) or query_input.get("rows") != EXPECTED_QUERY_COUNT:
        raise ScoringInputError("ranking manifest must pin 507 query rows")
    if not isinstance(document_input, dict) or document_input.get("rows") != EXPECTED_DOCUMENT_COUNT:
        raise ScoringInputError("ranking manifest must pin 46079 document rows")
    g0_root = Path(g0_dir).resolve()
    for label, row, expected_path in (("queries", query_input, g0_root / "queries.jsonl"), ("documents", document_input, g0_root / "documents.jsonl")):
        if Path(str(row.get("path", ""))).resolve() != expected_path.resolve():
            raise ScoringInputError(f"ranking manifest {label} path is not the supplied G0 projection")
        expected_pin = verified_g0["g0Binding"]["inputsManifestSha256"]
        if not expected_pin or row.get("sha256") != sha256_file(expected_path):
            raise ScoringInputError(f"ranking manifest {label} SHA does not match the supplied G0 projection")
    models = manifest.get("models")
    if not isinstance(models, dict) or not models.get("dense") or not models.get("crossEncoder"):
        raise ScoringInputError("ranking manifest must include Dense and CrossEncoder model pins")
    try:
        g0_models = json.loads((Path(g0_dir) / "models.manifest.json").read_text(encoding="utf-8"))
        g0_prereg = json.loads((Path(g0_dir) / "preregistration.json").read_text(encoding="utf-8"))
        g0_inputs = json.loads((Path(g0_dir) / "inputs.manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScoringInputError("G0 model/preregistration manifests are invalid") from exc
    systems = g0_prereg.get("systems", {})
    if manifest.get("packageVersions") != g0_prereg.get("environment", {}).get("packages") or manifest.get("hardware") != g0_prereg.get("environment", {}).get("hardware"):
        raise ScoringInputError("ranking runtime package/hardware evidence does not match G0 environment pin")
    expected_code_names = {"run_kuaisearch_multicategory_retrieval_baseline_v1.py", "kuaisearch_multicategory_retrieval_baseline_v1.py"}
    code_sha = manifest.get("codeSha256")
    code_pins = binding.get("codePins", {})
    code_by_basename = {Path(str(path)).name: value for path, value in code_sha.items()} if isinstance(code_sha, dict) else {}
    if set(code_by_basename) != expected_code_names:
        raise ScoringInputError("ranking codeSha256 must contain exactly runner and baseline pins")
    if any(code_by_basename.get(path) != code_pins.get(path, {}).get("sha256") for path in expected_code_names):
        raise ScoringInputError("ranking runner/baseline code pins do not match G0 binding")
    build_ms = manifest.get("buildMs")
    latency = manifest.get("latency")
    if not isinstance(build_ms, dict) or not all(isinstance(build_ms.get(key), (int, float)) and not isinstance(build_ms.get(key), bool) and float(build_ms[key]) >= 0 for key in ("bm25", "dense", "crossEncoderLoad")) or not isinstance(latency, dict) or not isinstance(latency.get("coldBuildMs"), (int, float)) or isinstance(latency.get("coldBuildMs"), bool) or float(latency["coldBuildMs"]) < 0:
        raise ScoringInputError("ranking build/latency evidence has invalid non-negative values")
    expected_cold = sum(float(build_ms[key]) for key in ("bm25", "dense", "crossEncoderLoad"))
    if abs(float(latency["coldBuildMs"]) - expected_cold) > 0.01:
        raise ScoringInputError("ranking coldBuildMs does not equal the buildMs component sum")
    memory = manifest.get("memory")
    prereg_memory = g0_prereg.get("memoryMeasurement")
    if not isinstance(memory, dict) or not isinstance(prereg_memory, dict) or memory.get("unit") != prereg_memory.get("unit") or memory.get("isTruePeak") != prereg_memory.get("isTruePeak") or memory.get("samplingPoints") != prereg_memory.get("points"):
        raise ScoringInputError("ranking memory evidence does not match G0 preregistration")
    dataset_pins = manifest.get("datasetPins")
    if not isinstance(dataset_pins, dict) or dataset_pins.get("documentsSha256") != g0_inputs.get("documents", {}).get("sha256") or dataset_pins.get("breadthManifestSha256") != g0_inputs.get("breadthManifest", {}).get("sha256"):
        raise ScoringInputError("ranking dataset pins do not match G0 input pins")
    g0_dense, g0_cross, g0_deps = g0_models.get("dense"), g0_models.get("crossEncoder"), g0_models.get("crossEncoderDependencies")
    rank_dense, rank_cross = models.get("dense"), models.get("crossEncoder")
    if not all(isinstance(value, dict) for value in (g0_dense, g0_cross, g0_deps, rank_dense, rank_cross)):
        raise ScoringInputError("G0 and ranking model pins are incomplete")
    def normalized_cache_files(value: Mapping[str, Any]) -> list[dict[str, Any]]:
        files = value.get("files")
        if isinstance(files, dict):
            return [{"path": name, **dict(pin)} for name, pin in sorted(files.items())]
        return list(files) if isinstance(files, list) else []

    if rank_dense.get("name") != systems.get("dense", {}).get("model") or normalized_cache_files(rank_dense.get("cache", {})) != normalized_cache_files(g0_dense) or Path(str(rank_dense.get("cache", {}).get("path", ""))).resolve() != Path(str(g0_dense.get("root", ""))).resolve():
        raise ScoringInputError("ranking Dense identity/cache does not match G0 preregistration")
    if rank_cross.get("name") != systems.get("crossEncoder", {}).get("model") or rank_cross.get("revision") != systems.get("crossEncoder", {}).get("revision") or normalized_cache_files(rank_cross.get("cache", {})) != normalized_cache_files(g0_cross) or Path(str(rank_cross.get("cache", {}).get("path", ""))).resolve() != Path(str(g0_cross.get("root", ""))).resolve():
        raise ScoringInputError("ranking CrossEncoder identity/cache does not match G0 preregistration")
    dependencies = rank_cross.get("dependenciesSnapshot")
    if not isinstance(dependencies, dict) or dependencies.get("snapshotSha256") != g0_deps.get("snapshotSha256") or dependencies.get("fileCount") != g0_deps.get("fileCount") or Path(str(dependencies.get("root", ""))).resolve() != Path(str(g0_deps.get("root", ""))).resolve():
        raise ScoringInputError("ranking dependency snapshot does not match G0")
    if rank_cross.get("dependenciesPath") and Path(str(rank_cross["dependenciesPath"])).resolve() != Path(str(g0_deps.get("root", ""))).resolve():
        raise ScoringInputError("ranking dependency root does not match G0")
    if manifest.get("indexIdentity") != g0_prereg.get("indexIdentity"):
        raise ScoringInputError("ranking indexIdentity does not match G0 preregistration")
    contract = manifest.get("contract")
    prereg_bm25 = systems.get("bm25", {})
    prereg_dense = systems.get("dense", {})
    prereg_ce = systems.get("crossEncoder", {})
    if not isinstance(contract, dict) or contract.get("topK") != g0_prereg.get("topK") or contract.get("rrfK") != systems.get("rrf", {}).get("k") or contract.get("bm25Fields") != prereg_bm25.get("fieldWeights") or contract.get("denseTextField") != prereg_dense.get("input") or contract.get("crossEncoderCandidateLimit") != prereg_ce.get("candidateLimit") or contract.get("crossEncoderDoesNotExpandCandidates") is not prereg_ce.get("reordersOnly") or contract.get("randomSeed") != g0_prereg.get("seed"):
        raise ScoringInputError("ranking manifest contract does not match G0 preregistration")
    candidate_limit = contract.get("candidateLimit", contract.get("crossEncoderCandidateLimit"))
    if candidate_limit != 20:
        raise ScoringInputError("ranking manifest CrossEncoder candidate limit must be 20")
    latency = manifest.get("latency")
    repeat = latency.get("crossEncoderRepeatVerification") if isinstance(latency, dict) else None
    if not isinstance(repeat, dict) or repeat.get("repeatVerifiedQueries") != EXPECTED_QUERY_COUNT or repeat.get("repeatMismatchCount") != 0:
        raise ScoringInputError("ranking manifest repeat verification must be 507 queries with zero mismatch")
    return manifest


def load_safe_queries(path: Path) -> dict[str, dict[str, str]]:
    """Load only the G0 query projection (queryId/query/split)."""
    rows = read_jsonl(path)
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        if set(row) != {"queryId", "query", "split"}:
            raise ScoringInputError("G0 queries must contain exactly queryId/query/split")
        query_id = str(row["queryId"])
        if not query_id or query_id in result or not all(isinstance(row[key], str) and row[key] for key in ("queryId", "query", "split")):
            raise ScoringInputError("G0 query projection has invalid or duplicate identity")
        result[query_id] = {key: row[key] for key in ("queryId", "query", "split")}
    if not result:
        raise ScoringInputError("G0 queries must not be empty")
    return result


def load_safe_documents(path: Path) -> dict[str, dict[str, str]]:
    """Load only public document fields from the G0 document projection."""
    rows = read_jsonl(path)
    result: dict[str, dict[str, str]] = {}
    allowed = {"doc_id", "title", "attr_value", "brand", "seller_name"}
    for row in rows:
        if set(row) != allowed:
            raise ScoringInputError("G0 documents contain fields outside the safe projection")
        doc_id = str(row["doc_id"])
        if not doc_id or doc_id in result:
            raise ScoringInputError("G0 document IDs must be unique and non-empty")
        result[doc_id] = {key: str(row[key] or "") for key in ("title", "attr_value", "brand", "seller_name")}
    if not result:
        raise ScoringInputError("G0 documents must not be empty")
    return result


def _rank_of(ranked: Sequence[str], target: str) -> int | None:
    try:
        return list(ranked).index(target) + 1
    except ValueError:
        return None


def _bundle(records: Sequence[dict[str, Any]], ranked_by_query: Mapping[str, Sequence[str]], *, positive_floor: int) -> dict[str, Any]:
    rows = [record for record in records if int(record["grade"]) >= positive_floor]
    result: dict[str, Any] = {"queryCount": len(rows)}
    ranks = [_rank_of(ranked_by_query.get(row["queryId"], ()), row["docId"]) for row in rows]
    for cutoff in K_VALUES:
        hits = sum(rank is not None and rank <= cutoff for rank in ranks)
        result[f"hitAt{cutoff}"] = {"hits": hits, "rate": hits / len(rows) if rows else None}
    result["mrr"] = sum((1.0 / rank) if rank else 0.0 for rank in ranks) / len(rows) if rows else None
    return result


def _negative_exposure(records: Sequence[dict[str, Any]], ranked_by_query: Mapping[str, Sequence[str]]) -> dict[str, Any]:
    result: dict[str, Any] = {"queryCount": len(records)}
    ranks = [_rank_of(ranked_by_query.get(row["queryId"], ()), row["docId"]) for row in records]
    for cutoff in K_VALUES:
        exposed = sum(rank is not None and rank <= cutoff for rank in ranks)
        result[f"at{cutoff}"] = {"exposed": exposed, "rate": exposed / len(records) if records else None}
    return result


def _aggregate_bundles(bundles: Sequence[dict[str, Any]]) -> dict[str, Any]:
    bundles = [bundle for bundle in bundles if bundle.get("queryCount", 0)]
    if not bundles:
        return {"queryCount": 0}
    result: dict[str, Any] = {"categoryCount": len(bundles)}
    for cutoff in K_VALUES:
        values = [bundle[f"hitAt{cutoff}"]["rate"] for bundle in bundles if bundle[f"hitAt{cutoff}"]["rate"] is not None]
        result[f"hitAt{cutoff}"] = sum(values) / len(values) if values else None
    values = [bundle["mrr"] for bundle in bundles if bundle.get("mrr") is not None]
    result["mrr"] = sum(values) / len(values) if values else None
    return result


def _latency_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = sorted(float(row["queryLatencyMs"]) for row in rows)
    if not values:
        return {"count": 0, "meanMs": None, "medianMs": None, "p95Ms": None}
    return {"count": len(values), "meanMs": statistics.mean(values), "medianMs": statistics.median(values), "p95Ms": values[min(len(values) - 1, max(0, int((len(values) * 0.95 + 0.999999) - 1)))]}


def _metric_vector(record: dict[str, Any], ranked: Sequence[str], *, floor: int) -> dict[str, float]:
    if int(record["grade"]) < floor:
        return {f"hitAt{k}": 0.0 for k in K_VALUES} | {"mrr": 0.0}
    rank = _rank_of(ranked, record["docId"])
    return {f"hitAt{k}": float(rank is not None and rank <= k) for k in K_VALUES} | {"mrr": 1.0 / rank if rank else 0.0}


def paired_bootstrap_ci(records: Sequence[dict[str, Any]], rankings_by_strategy: Mapping[str, Mapping[str, Sequence[str]]], *, floor: int = 1, samples: int = 2000, seed: int = 0) -> list[dict[str, Any]]:
    """Return deterministic paired query bootstrap CIs for allowed metrics only."""
    if samples <= 0:
        raise ValueError("bootstrap samples must be positive")
    output: list[dict[str, Any]] = []
    strategies = list(rankings_by_strategy)
    rng = random.Random(seed)
    vectors = {strategy: [_metric_vector(record, rankings_by_strategy[strategy].get(record["queryId"], ()), floor=floor) for record in records] for strategy in strategies}
    for left_index, left in enumerate(strategies):
        for right in strategies[left_index + 1:]:
            diffs = {metric: [vectors[left][i][metric] - vectors[right][i][metric] for i in range(len(records))] for metric in (*[f"hitAt{k}" for k in K_VALUES], "mrr")}
            for metric, values in diffs.items():
                if not values:
                    mean_difference = None
                    interval = [None, None]
                else:
                    mean_difference = sum(values) / len(values)
                    boot = []
                    for _ in range(samples):
                        boot.append(sum(values[rng.randrange(len(values))] for _ in values) / len(values))
                    boot.sort()
                    interval = [boot[int(0.025 * samples)], boot[min(samples - 1, int(0.975 * samples))]]
                output.append({"leftStrategy": left, "rightStrategy": right, "metric": metric, "meanDifference": mean_difference, "ci95": interval, "samples": samples})
    return output


def _group_rows(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, list[str]]]:
    grouped: dict[str, dict[str, list[str]]] = defaultdict(dict)
    for row in rows:
        grouped[str(row["strategy"])][str(row["queryId"])] = list(row["rankedDocIds"])
    return grouped


def score_rankings(*, rankings_path: Path, source_qrels_path: Path | None = None, categories_path: Path | None = None, human_sample_labels_path: Path | None = None, labels_dataset_dir: Path | None = None, ranking_manifest_path: Path | None = None, g0_dir: Path | None = None, bootstrap_samples: int = 2000, bootstrap_seed: int = 0, strict_rankings: bool = True) -> dict[str, Any]:
    """Score frozen rankings against sparse source labels and human pair subset."""
    # Deliberately first read rankings; labels are not available to ranking code.
    if strict_rankings and (ranking_manifest_path is None or g0_dir is None):
        raise ScoringInputError("ranking manifest and G0 directory are required for formal scoring")
    if strict_rankings and labels_dataset_dir is None:
        raise ScoringInputError("labels dataset directory is required for formal scoring")
    if strict_rankings:
        labels = load_labels_dataset_dir(Path(labels_dataset_dir))
        source_qrels_path, categories_path, human_sample_labels_path = labels["sourceQrels"], labels["categories"], labels["humanSampleLabels"]
    elif source_qrels_path is None or categories_path is None or human_sample_labels_path is None:
        raise ScoringInputError("fixture scoring requires explicit label paths")
    ranking_manifest = _validate_ranking_manifest(Path(ranking_manifest_path), Path(rankings_path), g0_dir=Path(g0_dir)) if ranking_manifest_path is not None and g0_dir is not None and strict_rankings else None
    ranking_rows = load_rankings(Path(rankings_path), strict=strict_rankings)
    if strict_rankings:
        g0_documents = read_jsonl(Path(g0_dir) / "documents.jsonl")
        g0_doc_ids = {str(row.get("doc_id") or "") for row in g0_documents}
        if len(g0_documents) != EXPECTED_DOCUMENT_COUNT or len(g0_doc_ids) != EXPECTED_DOCUMENT_COUNT or "" in g0_doc_ids:
            raise ScoringInputError("G0 documents do not contain exactly 46079 unique doc IDs")
        unknown = sorted({doc_id for row in ranking_rows for doc_id in row["rankedDocIds"] if doc_id not in g0_doc_ids})
        if unknown:
            raise ScoringInputError(f"ranking contains doc IDs outside the G0 corpus: {unknown[:3]}")
    qrels = load_source_qrels(Path(source_qrels_path))
    categories = load_categories(Path(categories_path))
    human_rows = load_human_sample_labels(Path(human_sample_labels_path))
    by_strategy = _group_rows(ranking_rows)
    query_ids = set(qrels)
    if set().union(*(set(mapping) for mapping in by_strategy.values())) != query_ids:
        raise ScoringInputError("ranking queries must exactly cover source qrels")
    if set(by_strategy) != set(RANKING_STRATEGIES):
        raise ScoringInputError("rankings must contain all five preregistered strategies")
    for strategy in RANKING_STRATEGIES:
        if set(by_strategy[strategy]) != query_ids:
            raise ScoringInputError(f"strategy does not cover all source qrels: {strategy}")
    human_ids = {str(row["queryId"]) for row in human_rows}
    if not human_ids.issubset(query_ids):
        raise ScoringInputError("human query sample contains an unknown query")
    for row in human_rows:
        qrel = qrels[str(row["queryId"])]
        if str(row["docId"]) != qrel["docId"] or int(row["sourceRelevance"]) != int(qrel["grade"]) or ("categoryKey" in row and str(row["categoryKey"]) != qrel["categoryKey"]):
            raise ScoringInputError("human query sample must preserve the frozen source pair")
    records = list(qrels.values())
    missing_categories = sorted({row["categoryKey"] for row in records} - set(categories))
    if missing_categories:
        raise ScoringInputError(f"source qrels reference unknown categories: {missing_categories}")
    positive = [row for row in records if row["grade"] > 0]
    grade2 = [row for row in records if row["grade"] >= 2]
    negative = [row for row in records if row["grade"] == 0]
    report: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "labelBoundary": "SPARSE_SOURCE_LABEL_NOT_EXHAUSTIVE_GOLD",
        "qualityConclusionPolicy": "source-pair-diagnostics-only; no winner conclusion from sparse labels",
        "counts": {"queries": len(records), "gradePositive": len(positive), "gradeAtLeast2": len(grade2), "gradeZero": len(negative), "humanReviewedQueryPairs": len(human_ids), "categories": len(categories)},
        "strategies": {},
        "pairedBootstrapCI": {},
        "inputs": {"rankings": {"path": str(Path(rankings_path).resolve()), "rows": len(ranking_rows), "sha256": sha256_file(Path(rankings_path))}, "sourceQrels": {"path": str(Path(source_qrels_path).resolve()), "rows": len(records), "sha256": sha256_file(Path(source_qrels_path))}, "categories": {"path": str(Path(categories_path).resolve()), "rows": len(categories), "sha256": sha256_file(Path(categories_path))}, "humanSampleLabels": {"path": str(Path(human_sample_labels_path).resolve()), "rows": len(human_rows), "sha256": sha256_file(Path(human_sample_labels_path))}},
    }
    if ranking_manifest_path is not None:
        report["inputs"]["rankingManifest"] = {"path": str(Path(ranking_manifest_path).resolve()), "sha256": sha256_file(Path(ranking_manifest_path))}
        report["rankingManifestSha256"] = sha256_file(Path(ranking_manifest_path))
    if g0_dir is not None:
        g0_manifest_path = Path(g0_dir) / "manifest.json"
        report["inputs"]["g0Manifest"] = {"path": str(g0_manifest_path.resolve()), "sha256": sha256_file(g0_manifest_path)}
        report["g0ManifestSha256"] = sha256_file(g0_manifest_path)
    if labels_dataset_dir is not None:
        labels_manifest_path = Path(labels_dataset_dir).resolve() / "manifest.json"
        report["inputs"]["labelsDatasetManifest"] = {"path": str(labels_manifest_path), "sha256": sha256_file(labels_manifest_path)}
        report["labelsDatasetManifestSha256"] = sha256_file(labels_manifest_path)
    for strategy in RANKING_STRATEGIES:
        mapping = by_strategy[strategy]
        split_report: dict[str, Any] = {}
        for split in sorted({row["split"] for row in records}):
            split_rows = [row for row in records if row["split"] == split]
            split_report[split] = {"gradePositive": _bundle(split_rows, mapping, positive_floor=1), "gradeAtLeast2": _bundle(split_rows, mapping, positive_floor=2), "gradeZeroKnownNegativeExposure": _negative_exposure([row for row in split_rows if row["grade"] == 0], mapping)}
        category_report: dict[str, Any] = {}
        category_bundles = []
        for category in sorted(categories):
            category_rows = [row for row in records if row["categoryKey"] == category]
            bundle = _bundle(category_rows, mapping, positive_floor=1)
            category_report[category] = {"gradePositive": bundle, "gradeAtLeast2": _bundle(category_rows, mapping, positive_floor=2), "gradeZeroKnownNegativeExposure": _negative_exposure([row for row in category_rows if row["grade"] == 0], mapping)}
            category_bundles.append(bundle)
        human_records = [qrels[qid] for qid in sorted(human_ids)]
        report["strategies"][strategy] = {"overall": {"gradePositive": _bundle(positive, mapping, positive_floor=1), "gradeAtLeast2": _bundle(grade2, mapping, positive_floor=2), "gradeZeroKnownNegativeExposure": _negative_exposure(negative, mapping)}, "bySplit": split_report, "byCategory": category_report, "categoryMacro": {"gradePositive": _aggregate_bundles(category_bundles), "gradeAtLeast2": _aggregate_bundles([_bundle([row for row in records if row["categoryKey"] == category], mapping, positive_floor=2) for category in sorted(categories)])}, "humanReviewedQueryPairSubset": {"gradePositive": _bundle(human_records, mapping, positive_floor=1), "gradeAtLeast2": _bundle(human_records, mapping, positive_floor=2), "gradeZeroKnownNegativeExposure": _negative_exposure([row for row in human_records if row["grade"] == 0], mapping)}, "latency": _latency_summary([row for row in ranking_rows if row["strategy"] == strategy])}
    for floor, name in ((1, "gradePositive"), (2, "gradeAtLeast2")):
        subset = [row for row in records if row["grade"] >= floor]
        report["pairedBootstrapCI"][name] = paired_bootstrap_ci(subset, by_strategy, floor=floor, samples=bootstrap_samples, seed=bootstrap_seed + floor)
    return report


def build_blind_pool(rankings: Sequence[Mapping[str, Any]], source_qrels: Mapping[str, Mapping[str, Any]], *, blind_secret: bytes, depth: int = 20, queries: Mapping[str, Mapping[str, Any]] | None = None, documents: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, list[dict[str, Any]]]:
    """Build physically separable public rows and evaluator-private provenance."""
    if depth <= 0:
        raise ValueError("pool depth must be positive")
    if not isinstance(blind_secret, bytes) or len(blind_secret) != 32:
        raise ValueError("blind_secret must be exactly 32 random bytes")
    rows = load_rankings_from_objects(rankings)
    grouped = _group_rows(rows)
    query_ids = set(source_qrels)
    if not query_ids or any(query_id not in source_qrels for query_id in query_ids):
        raise ScoringInputError("source pair mapping must not be empty")
    if not set().union(*(set(values) for values in grouped.values())) == query_ids:
        raise ScoringInputError("pool rankings must cover every source query")
    if set(grouped) != set(RANKING_STRATEGIES) or any(set(grouped[strategy]) != query_ids for strategy in RANKING_STRATEGIES):
        raise ScoringInputError("G2 pool rankings must contain all five strategies for every query")
    if queries is None:
        raise ScoringInputError("G0 safe queries are required to materialize public review rows")
    if set(queries) != query_ids:
        raise ScoringInputError("G0 safe queries must exactly cover source queries")
    if documents is None:
        raise ScoringInputError("G0 safe documents are required to materialize public review rows")
    public: list[dict[str, Any]] = []
    private: list[dict[str, Any]] = []
    for query_id in sorted(query_ids):
        source_doc = str(source_qrels[query_id]["docId"])
        docs: set[str] = {source_doc}
        ranks: dict[str, dict[str, int]] = defaultdict(dict)
        for strategy in sorted(grouped):
            for rank, doc_id in enumerate(grouped[strategy][query_id][:depth], 1):
                docs.add(doc_id)
                ranks[doc_id][strategy] = rank
        def blind_order(doc: str) -> str:
            return hmac.new(blind_secret, f"{query_id}|{doc}".encode("utf-8"), hashlib.sha256).hexdigest()

        ordered = sorted(docs, key=blind_order)
        for position, doc_id in enumerate(ordered, 1):
            token = blind_order(doc_id)
            if doc_id not in documents:
                raise ScoringInputError(f"G0 documents do not resolve pooled candidate: {doc_id}")
            query_text = str(queries[query_id]["query"])
            public.append({"schemaVersion": POOL_SCHEMA_VERSION, "queryId": query_id, "query": query_text, "blindCandidateId": token, "poolPosition": position, "title": str(documents[doc_id].get("title", "")), "attr_value": str(documents[doc_id].get("attr_value", "")), "brand": str(documents[doc_id].get("brand", "")), "seller_name": str(documents[doc_id].get("seller_name", ""))})
            private.append({"schemaVersion": POOL_SCHEMA_VERSION, "queryId": query_id, "blindCandidateId": token, "docId": doc_id, "sourcePairIncluded": doc_id == source_doc, "strategies": sorted(ranks.get(doc_id, {})), "ranksByStrategy": ranks.get(doc_id, {})})
    return {"publicReviewRows": public, "privateProvenance": private}


def load_rankings_from_objects(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate in-memory ranking rows with the same contract as JSONL input."""
    # Reuse the file validator without a temporary artifact, keeping this path
    # deterministic and avoiding filesystem writes in unit tests.
    values = [dict(row) for row in rows]
    if not values:
        raise ScoringInputError("rankings must not be empty")
    allowed = {"schemaVersion", "queryId", "strategy", "rankedDocIds", "queryLatencyMs", "split"}
    seen: set[tuple[str, str]] = set()
    for row in values:
        if set(row) - allowed or not {"schemaVersion", "queryId", "strategy", "rankedDocIds", "queryLatencyMs"}.issubset(row):
            raise ScoringInputError("ranking rows must use the canonical field set")
        key = (str(row["queryId"]), str(row["strategy"]))
        if not key[0] or not isinstance(row["schemaVersion"], str) or not row["schemaVersion"] or key in seen or key[1] not in RANKING_STRATEGIES:
            raise ScoringInputError("duplicate or unknown ranking strategy")
        seen.add(key)
        ranked = row["rankedDocIds"]
        latency = row["queryLatencyMs"]
        if not isinstance(ranked, list) or len(ranked) > 100 or any(not isinstance(doc, str) or not doc for doc in ranked) or len(ranked) != len(set(ranked)) or isinstance(latency, bool) or not isinstance(latency, (int, float)) or latency < 0:
            raise ScoringInputError("invalid rankedDocIds")
    return values


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    payload = b"".join(canonical_json(dict(row)) for row in rows)
    Path(path).write_bytes(payload)
    return {"path": Path(path).name, "rows": payload.count(b"\n"), "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}


def write_blind_pool(output_dir: Path, pool: Mapping[str, Sequence[Mapping[str, Any]]], *, salt_sha256: str, input_pins: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(salt_sha256, str) or len(salt_sha256) != 64 or any(char not in "0123456789abcdef" for char in salt_sha256):
        raise ValueError("salt_sha256 must be a lowercase SHA-256 digest")
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"refusing to overwrite non-empty output: {output_dir}")
    public_dir, private_dir = output_dir / "public", output_dir / "private"
    public_dir.mkdir(parents=True, exist_ok=True)
    private_dir.mkdir(parents=True, exist_ok=True)
    public_artifact = write_jsonl(public_dir / "review_rows.jsonl", pool["publicReviewRows"])
    private_artifact = write_jsonl(private_dir / "provenance.jsonl", pool["privateProvenance"])
    (public_dir / "manifest.json").write_bytes(canonical_json({"schemaVersion": POOL_SCHEMA_VERSION, "physicalSeparation": True, "artifact": public_artifact}))
    (private_dir / "manifest.json").write_bytes(canonical_json({"schemaVersion": POOL_SCHEMA_VERSION, "physicalSeparation": True, "artifact": private_artifact, "containsEvaluatorPrivateProvenance": True, "saltSha256": salt_sha256, "inputPins": dict(input_pins or {})}))
    return {"public": public_artifact, "private": private_artifact}


__all__ = ["EXPECTED_LABELS_DATASET_MANIFEST_SHA256", "EXPECTED_LABEL_ARTIFACTS", "K_VALUES", "POOL_SCHEMA_VERSION", "RANKING_STRATEGIES", "SCHEMA_VERSION", "ScoringInputError", "build_blind_pool", "load_categories", "load_human_sample_labels", "load_labels_dataset_dir", "load_rankings", "load_rankings_from_objects", "load_safe_documents", "load_safe_queries", "load_source_qrels", "paired_bootstrap_ci", "score_rankings", "sha256_file", "write_blind_pool"]
