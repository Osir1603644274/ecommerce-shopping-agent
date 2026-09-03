from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import fsspec
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
COMMIT = "7916cdf6ab75a462e77f20ab40428a10923998d5"
EXAMPLES_SHA256 = "4a735b693b4a424a6fc67f5be6e4c811495c488bbf66d02a602d308b2744263a"
PRODUCTS_SHA256 = "25124442d064d64b26f74082d6fa09438d679efc0c183cf28d19064a2b65a265"
# The LFS objects are bound by their official SHA-256 pointers below. `main` is
# used only as the byte transport because GitHub's commit-addressed media route
# intermittently ignores Range requests on this host.
MEDIA_ROOT = "https://media.githubusercontent.com/media/amazon-science/esci-data/main/shopping_queries_dataset"
EXAMPLES_URL = f"{MEDIA_ROOT}/shopping_queries_dataset_examples.parquet"
PRODUCTS_URL = f"{MEDIA_ROOT}/shopping_queries_dataset_products.parquet"
DATA_ROOT = ROOT / "label_free_datasets_attempt002"
RUN_ROOT = ROOT / "pool_runs_attempt002"
SAMPLE_MANIFEST = ROOT / "label_free_sample_manifest_attempt002.json"
POOL_MANIFEST = ROOT / "pool_build_manifest_attempt002.json"
ATTEMPT_ROOT = ROOT / "formal_attempts" / "attempt001"
SAMPLE_SEED = "esci-task1-public-eval-20260902-v1"
BOOTSTRAP_SEED = 20260902
QUERY_COUNT = 50
GAINS = {"E": 1.0, "S": 0.1, "C": 0.01, "I": 0.0}
SKILL_CLI = REPO_ROOT / ".agents" / "skills" / "build-retrieval-judgment-pool" / "scripts" / "pool_workflow.py"


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"immutable output already exists: {path}")
    path.write_bytes(canonical_bytes(value))


def write_jsonl_new(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"immutable output already exists: {path}")
    with path.open("wb") as handle:
        for row in rows:
            handle.write(canonical_bytes(row))


def git_content(path: str) -> tuple[dict[str, Any], bytes]:
    response = requests.get(
        f"https://api.github.com/repos/amazon-science/esci-data/contents/{path}",
        params={"ref": COMMIT}, timeout=30,
    )
    response.raise_for_status()
    item = response.json()
    import base64
    raw = base64.b64decode(item["content"])
    return item, raw


def lfs_pointer(raw: bytes) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for line in raw.decode("utf-8").splitlines():
        if line.startswith("oid sha256:"):
            values["sha256"] = line.split(":", 1)[1]
        elif line.startswith("size "):
            values["size"] = int(line.split()[1])
    return values


def range_probe(url: str) -> dict[str, Any]:
    head = requests.head(url, allow_redirects=True, timeout=30)
    head.raise_for_status()
    handle = fsspec.open(url, "rb", block_size=64 * 1024, cache_type="none").open()
    try:
        first = handle.read(4)
        handle.seek(int(head.headers["Content-Length"]) - 4)
        last = handle.read(4)
    finally:
        handle.close()
    return {
        "content_length": int(head.headers["Content-Length"]),
        "accept_ranges": head.headers.get("Accept-Ranges"),
        "etag": head.headers.get("ETag", "").strip('"'),
        "first_magic_hex": first.hex(),
        "last_magic_hex": last.hex(),
        "transport": "fsspec seek/read over HTTP Range",
    }


def open_parquet(url: str) -> tuple[Any, pq.ParquetFile]:
    handle = fsspec.open(url, "rb", block_size=1024 * 1024, cache_type="none").open()
    return handle, pq.ParquetFile(handle)


def parquet_metadata(url: str) -> dict[str, Any]:
    handle, parquet = open_parquet(url)
    try:
        metadata = parquet.metadata
        row_group = metadata.row_group(0)
        columns = []
        for index in range(row_group.num_columns):
            column = row_group.column(index)
            columns.append({
                "name": column.path_in_schema,
                "compressed_bytes": column.total_compressed_size,
                "uncompressed_bytes": column.total_uncompressed_size,
            })
        return {
            "rows": metadata.num_rows,
            "row_groups": metadata.num_row_groups,
            "serialized_metadata_bytes": metadata.serialized_size,
            "columns": columns,
        }
    finally:
        handle.close()


def audit_source() -> None:
    output = ROOT / "source_audit.json"
    if output.exists():
        raise RuntimeError("source audit is immutable")
    started = time.perf_counter()
    commit = requests.get(
        "https://api.github.com/repos/amazon-science/esci-data/commits/main", timeout=30
    ).json()["sha"]
    if commit != COMMIT:
        raise RuntimeError(f"main moved after preregistration: {commit}")
    objects = {}
    for name, expected_sha, expected_size in (
        ("shopping_queries_dataset/shopping_queries_dataset_examples.parquet", EXAMPLES_SHA256, 51286808),
        ("shopping_queries_dataset/shopping_queries_dataset_products.parquet", PRODUCTS_SHA256, 1108857465),
    ):
        item, raw = git_content(name)
        pointer = lfs_pointer(raw)
        if pointer != {"sha256": expected_sha, "size": expected_size}:
            raise RuntimeError(f"LFS pointer mismatch: {name}")
        objects[name] = {"git_blob_sha1": item["sha"], **pointer}
    license_item, license_raw = git_content("LICENSE")
    readme_item, readme_raw = git_content("README.md")
    notice_item, notice_raw = git_content("NOTICE")
    if b"Apache License" not in license_raw or b"manually annotated" not in readme_raw:
        raise RuntimeError("license or human-annotation authority missing")
    probes = {"examples": range_probe(EXAMPLES_URL), "products": range_probe(PRODUCTS_URL)}
    if any(value["accept_ranges"] != "bytes" for value in probes.values()):
        raise RuntimeError("HTTP Range is unavailable")
    if probes["examples"]["etag"] != EXAMPLES_SHA256 or probes["products"]["etag"] != PRODUCTS_SHA256:
        raise RuntimeError("media ETag does not match official LFS SHA-256")
    metadata = {"examples": parquet_metadata(EXAMPLES_URL), "products": parquet_metadata(PRODUCTS_URL)}
    if metadata["examples"]["row_groups"] != 1 or metadata["products"]["row_groups"] != 1:
        raise RuntimeError("upstream parquet layout drift")
    selected_product_columns = {"product_id", "product_title", "product_locale"}
    selected_bytes = sum(
        item["compressed_bytes"] for item in metadata["products"]["columns"]
        if item["name"] in selected_product_columns
    )
    if selected_bytes > 256 * 1024 * 1024:
        raise RuntimeError("selected product columns exceed preregistered stream budget")
    receipt = {
        "schema_version": "esci-source-audit-v1",
        "status": "SOURCE_GATE_PASS",
        "audited_before_label_read": True,
        "repository": "amazon-science/esci-data",
        "frozen_commit": COMMIT,
        "observed_main_commit": commit,
        "license": "Apache-2.0",
        "human_annotation_evidence": "official README says manually annotated",
        "task": "Task 1 Query-Product Ranking",
        "objects": objects,
        "authority_files": {
            "README.md": {"git_blob_sha1": readme_item["sha"], "sha256": sha256_bytes(readme_raw)},
            "LICENSE": {"git_blob_sha1": license_item["sha"], "sha256": sha256_bytes(license_raw)},
            "NOTICE": {"git_blob_sha1": notice_item["sha"], "sha256": sha256_bytes(notice_raw)},
        },
        "range_probes": probes,
        "parquet_metadata": metadata,
        "selected_product_compressed_bytes": selected_bytes,
        "full_objects_persisted": False,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    write_json_new(output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


def require_source_gate() -> dict[str, Any]:
    path = ROOT / "source_audit.json"
    if not path.exists():
        raise RuntimeError("source audit must pass first")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("status") != "SOURCE_GATE_PASS" or not value.get("audited_before_label_read"):
        raise RuntimeError("source gate not passed")
    return value


def sample_key(query_id: str) -> str:
    return sha256_bytes(f"{SAMPLE_SEED}\0{query_id}".encode("utf-8"))


def dataset_config(dataset_id: str, candidate_count: int) -> dict[str, Any]:
    return {
        "schema_version": "retrieval-judgment-pool-config-v1",
        "dataset": {"dataset_id": dataset_id, "revision": COMMIT, "visibility": "public", "contains_labels": False},
        "field_mapping": {
            "query_id": "query_id", "query_text": "text", "query_variants": "variants",
            "query_structured": "structured", "document_id": "document_id",
            "document_entity_type": "entity_type", "document_source": "source",
            "document_text_fields": ["title"], "blind_display_fields": ["product_id", "title"],
        },
        "retrievers": [
            {"retriever_id": "bm25f-title-v1", "kind": "lexical_bm25f", "depth": candidate_count,
             "model": "builtin-bm25f", "revision": "v1", "field_weights": {"title": 1.0}},
            {"retriever_id": "char-2-3-v1", "kind": "char_ngram", "depth": candidate_count,
             "model": "builtin-char-ngram", "revision": "v1", "ngram_min": 2, "ngram_max": 3},
            {"retriever_id": "semantic-hash-256-v1", "kind": "semantic_hash", "depth": candidate_count,
             "model": "builtin-hashed-subword-dense-substitute", "revision": "v1", "dimensions": 256},
        ],
        "fusion": {"method": "rrf", "k": 60},
        "reranker": {"kind": "deterministic_pair_overlap", "model": "builtin-pair-overlap", "revision": "v1", "pair_budget_per_query": candidate_count},
        "selection": {
            "raw_depth_per_retriever": candidate_count,
            "top_k_per_retriever": min(5, candidate_count),
            "unique_quota": min(5, candidate_count),
            "disagreement_quota": min(5, candidate_count),
            "hard_negative_quota": 0,
            "reranker_quota": min(5, candidate_count),
            "random_tail_quota": 0,
            "final_pool_budget_per_query": candidate_count,
        },
        "safety": {"output_label": "UNJUDGED", "outside_pool_is_negative": False,
                   "read_qrels": False, "read_sealed_or_hidden": False, "production_release_allowed": False},
    }


def read_columns(url: str, columns: list[str]) -> tuple[pa.Table, dict[str, int]]:
    handle, parquet = open_parquet(url)
    try:
        metadata = parquet.metadata.row_group(0)
        sizes = {
            metadata.column(index).path_in_schema: metadata.column(index).total_compressed_size
            for index in range(metadata.num_columns)
        }
        return parquet.read(columns=columns), {name: sizes[name] for name in columns}
    finally:
        handle.close()


def prepare_label_free() -> None:
    audit = require_source_gate()
    if DATA_ROOT.exists():
        raise RuntimeError("label-free dataset output already exists")
    started = time.perf_counter()
    free_before = shutil.disk_usage(ROOT).free
    example_columns = ["query", "query_id", "product_id", "product_locale", "small_version", "split"]
    examples, example_sizes = read_columns(EXAMPLES_URL, example_columns)
    mask = pc.and_kleene(
        pc.and_kleene(pc.equal(examples["product_locale"], "us"), pc.equal(examples["small_version"], 1)),
        pc.equal(examples["split"], "test"),
    )
    filtered = examples.filter(mask).select(["query", "query_id", "product_id", "product_locale"])
    grouped: dict[str, dict[str, Any]] = {}
    for batch in filtered.to_batches(max_chunksize=65536):
        for row in batch.to_pylist():
            query_id = str(row["query_id"])
            query = str(row["query"] or "").strip()
            product_id = str(row["product_id"])
            if not query:
                continue
            item = grouped.setdefault(query_id, {"query": query, "products": []})
            if item["query"] != query:
                raise RuntimeError(f"query text drift for {query_id}")
            if product_id not in item["products"]:
                item["products"].append(product_id)
    eligible = [qid for qid, item in grouped.items() if 2 <= len(item["products"]) <= 40]
    selected_qids = sorted(eligible, key=lambda value: (sample_key(value), value))[:QUERY_COUNT]
    if len(selected_qids) != QUERY_COUNT:
        raise RuntimeError("insufficient eligible queries")
    wanted_products = sorted({product for qid in selected_qids for product in grouped[qid]["products"]})
    products, product_sizes = read_columns(PRODUCTS_URL, ["product_id", "product_title", "product_locale"])
    product_mask = pc.and_kleene(
        pc.equal(products["product_locale"], "us"),
        pc.is_in(products["product_id"], value_set=pa.array(wanted_products)),
    )
    selected_products = products.filter(product_mask).to_pylist()
    titles = {str(row["product_id"]): str(row["product_title"] or "").strip() for row in selected_products}
    missing = sorted(product for product in wanted_products if not titles.get(product))
    if missing:
        raise RuntimeError(f"missing official product titles: {missing[:5]}")
    manifest_queries = []
    for ordinal, qid in enumerate(selected_qids, 1):
        dataset_id = f"esci-t1-us-test-{ordinal:03d}-q{qid}"
        dataset_dir = DATA_ROOT / dataset_id
        candidates = grouped[qid]["products"]
        query_row = {"query_id": f"esci-q-{qid}", "text": grouped[qid]["query"], "variants": [], "structured": {}}
        document_rows = [
            {"document_id": f"esci:us:{qid}:{product_id}", "entity_type": "query_product_candidate",
             "source": f"amazon-science/esci-data@{COMMIT}", "product_id": product_id, "title": titles[product_id]}
            for product_id in candidates
        ]
        write_jsonl_new(dataset_dir / "queries.jsonl", [query_row])
        write_jsonl_new(dataset_dir / "documents.jsonl", document_rows)
        write_json_new(dataset_dir / "config.json", dataset_config(dataset_id, len(candidates)))
        manifest_queries.append({
            "ordinal": ordinal, "source_query_id": qid, "query_id": query_row["query_id"],
            "dataset_ref": dataset_id, "sample_key": sample_key(qid), "candidate_count": len(candidates),
            "product_ids": candidates,
            "files": {name: sha256_file(dataset_dir / name) for name in ("queries.jsonl", "documents.jsonl", "config.json")},
        })
    leakage_hits = []
    forbidden_fields = {"esci_label", "label", "gain", "relevance", "relevance_grade"}
    for path in DATA_ROOT.rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line and forbidden_fields.intersection(json.loads(line)):
                leakage_hits.append(str(path.relative_to(ROOT)))
                break
    if leakage_hits:
        raise RuntimeError(f"label leakage detected: {leakage_hits}")
    free_after = shutil.disk_usage(ROOT).free
    largest = max((path.stat().st_size for path in DATA_ROOT.rglob("*") if path.is_file()), default=0)
    if largest > 32 * 1024 * 1024:
        raise RuntimeError("local artifact exceeds preregistered 32 MiB maximum")
    receipt = {
        "schema_version": "esci-label-free-sample-manifest-v1", "status": "LABEL_FREE_READY",
        "source_audit_sha256": sha256_file(ROOT / "source_audit.json"),
        "preregistration_sha256": sha256_file(ROOT / "PREREGISTRATION.md"),
        "sample_seed": SAMPLE_SEED, "query_count": len(manifest_queries),
        "candidate_pair_count": sum(item["candidate_count"] for item in manifest_queries),
        "unique_product_count": len(wanted_products), "queries": manifest_queries,
        "columns_read_before_unlock": {"examples": example_columns, "products": ["product_id", "product_title", "product_locale"]},
        "labels_read": False, "label_leakage_hits": leakage_hits,
        "planned_compressed_column_bytes": sum(example_sizes.values()) + sum(product_sizes.values()),
        "source_selected_product_compressed_bytes": audit["selected_product_compressed_bytes"],
        "full_objects_persisted": False, "largest_local_file_bytes": largest,
        "local_disk_delta_bytes": free_before - free_after,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    write_json_new(SAMPLE_MANIFEST, receipt)
    print(json.dumps({key: receipt[key] for key in ("status", "query_count", "candidate_pair_count", "unique_product_count", "planned_compressed_column_bytes", "elapsed_seconds")}, indent=2))


def run_skill(args: list[str]) -> dict[str, Any]:
    command = [sys.executable, str(SKILL_CLI), "--data-root", str(DATA_ROOT), "--run-root", str(RUN_ROOT), *args]
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode not in (0, 2):
        raise RuntimeError(f"Skill CLI failed: {result.stderr or result.stdout}")
    envelope = json.loads(result.stdout)
    if not envelope.get("ok"):
        raise RuntimeError(json.dumps(envelope["error"], ensure_ascii=False))
    return envelope["data"]


def build_pools() -> None:
    if not SAMPLE_MANIFEST.exists():
        raise RuntimeError("label-free sample must be prepared first")
    if POOL_MANIFEST.exists() or RUN_ROOT.exists():
        raise RuntimeError("pool build output already exists")
    sample = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    started = time.perf_counter()
    rows = []
    for query in sample["queries"]:
        item_started = time.perf_counter()
        created = run_skill(["create", "--dataset-ref", query["dataset_ref"]])
        run_id = created["run_id"]
        built = run_skill(["build", "--run-id", run_id])
        verified = run_skill(["verify", "--run-id", run_id])
        if built["status"] != "READY" or verified["status"] != "READY" or not verified["all_candidates_unjudged"]:
            raise RuntimeError(f"Skill verification failed for {query['dataset_ref']}")
        run_dir = RUN_ROOT / run_id
        rows.append({
            "source_query_id": query["source_query_id"], "query_id": query["query_id"],
            "dataset_ref": query["dataset_ref"], "run_id": run_id, "status": verified["status"],
            "completed_phase": "VERIFY", "verification": verified,
            "receipt_sha256": sha256_file(run_dir / "receipt.json"),
            "sha256s_sha256": sha256_file(run_dir / "SHA256SUMS.txt"),
            "elapsed_seconds": round(time.perf_counter() - item_started, 6),
        })
    receipt = {
        "schema_version": "esci-pool-build-manifest-v1", "status": "READY",
        "query_count": len(rows), "all_unjudged": True, "qrels_read": False,
        "sample_manifest_sha256": sha256_file(SAMPLE_MANIFEST),
        "runs": rows, "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    write_json_new(POOL_MANIFEST, receipt)
    print(json.dumps({"status": receipt["status"], "query_count": receipt["query_count"], "elapsed_seconds": receipt["elapsed_seconds"]}, indent=2))


def ndcg(ranked_product_ids: list[str], gain_by_product: dict[str, float], cutoff: int | None) -> float:
    limit = len(ranked_product_ids) if cutoff is None else min(cutoff, len(ranked_product_ids))
    dcg = sum(gain_by_product[product_id] / math.log2(rank + 1) for rank, product_id in enumerate(ranked_product_ids[:limit], 1))
    ideal = sorted(gain_by_product.values(), reverse=True)[:limit]
    idcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(ideal, 1))
    return dcg / idcg if idcg else 0.0


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def bootstrap_delta(deltas: list[float]) -> dict[str, float]:
    import random
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(deltas)
    replicates = [sum(deltas[rng.randrange(n)] for _ in range(n)) / n for _ in range(10000)]
    return {"mean": sum(deltas) / n, "ci95_low": percentile(replicates, 0.025), "ci95_high": percentile(replicates, 0.975)}


def product_id_from_document(document_id: str) -> str:
    return document_id.rsplit(":", 1)[1]


def formal_score_once() -> None:
    if ATTEMPT_ROOT.exists():
        raise RuntimeError("formal attempt001 already exists and cannot be overwritten")
    source = require_source_gate()
    sample = json.loads(SAMPLE_MANIFEST.read_text(encoding="utf-8"))
    builds = json.loads(POOL_MANIFEST.read_text(encoding="utf-8"))
    if source["status"] != "SOURCE_GATE_PASS" or builds["status"] != "READY" or len(builds["runs"]) != QUERY_COUNT:
        raise RuntimeError("integrity prerequisites are not READY")
    frozen_runs = {item["source_query_id"]: item for item in builds["runs"]}
    for item in builds["runs"]:
        verified = run_skill(["verify", "--run-id", item["run_id"]])
        if verified["status"] != "READY" or not verified["all_candidates_unjudged"] or verified["qrels_read"]:
            raise RuntimeError("pre-unlock run verification failed")
    started = time.perf_counter()
    ATTEMPT_ROOT.mkdir(parents=True, exist_ok=False)
    qrel_columns = ["query_id", "product_id", "product_locale", "small_version", "split", "esci_label"]
    examples, qrel_sizes = read_columns(EXAMPLES_URL, qrel_columns)
    selected_source_qids = {item["source_query_id"] for item in sample["queries"]}
    qrels: dict[str, dict[str, str]] = defaultdict(dict)
    for batch in examples.to_batches(max_chunksize=65536):
        for row in batch.to_pylist():
            qid = str(row["query_id"])
            if qid not in selected_source_qids or row["product_locale"] != "us" or int(row["small_version"]) != 1 or row["split"] != "test":
                continue
            product_id = str(row["product_id"])
            label = str(row["esci_label"])
            if label not in GAINS:
                raise RuntimeError(f"unknown ESCI label {label}")
            if product_id in qrels[qid] and qrels[qid][product_id] != label:
                raise RuntimeError("conflicting human judgments")
            qrels[qid][product_id] = label
    qrel_rows = []
    systems = ["bm25f-title-v1", "char-2-3-v1", "semantic-hash-256-v1", "rrf-v1", "pair-overlap-v1", "sha256-random-control"]
    per_query = []
    metric_values: dict[str, list[float]] = {system: [] for system in systems}
    full_values: dict[str, list[float]] = {system: [] for system in systems}
    for query in sample["queries"]:
        qid = query["source_query_id"]
        expected = query["product_ids"]
        if set(qrels[qid]) != set(expected):
            raise RuntimeError(f"label completeness mismatch for query {qid}")
        gain_by_product = {product_id: GAINS[qrels[qid][product_id]] for product_id in expected}
        for product_id in expected:
            qrel_rows.append({"query_id": query["query_id"], "product_id": product_id,
                              "esci_label": qrels[qid][product_id], "gain": gain_by_product[product_id],
                              "provenance": "human judgment from amazon-science/esci-data"})
        run_dir = RUN_ROOT / frozen_runs[qid]["run_id"]
        rankings: dict[str, list[str]] = {}
        for retriever in systems[:3]:
            rows = [json.loads(line) for line in (run_dir / "runs" / f"{retriever}.jsonl").read_text(encoding="utf-8").splitlines() if line]
            rankings[retriever] = [product_id_from_document(row["document_id"]) for row in sorted(rows, key=lambda row: row["rank"])]
        analysis = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
        rankings["rrf-v1"] = [product_id_from_document(row["document_id"]) for row in analysis["queries"][0]["rrf_ranking"]]
        reranker = [json.loads(line) for line in (run_dir / "reranker_scores.jsonl").read_text(encoding="utf-8").splitlines() if line]
        rankings["pair-overlap-v1"] = [product_id_from_document(row["document_id"]) for row in sorted(reranker, key=lambda row: (-row["score"], row["document_id"]))]
        rankings["sha256-random-control"] = sorted(expected, key=lambda product_id: sha256_bytes(f"{SAMPLE_SEED}\0{qid}\0{product_id}".encode("utf-8")))
        query_metrics = {}
        for system in systems:
            if set(rankings[system]) != set(expected) or len(rankings[system]) != len(expected):
                raise RuntimeError(f"ranking universe mismatch: {qid} {system}")
            at10 = ndcg(rankings[system], gain_by_product, 10)
            full = ndcg(rankings[system], gain_by_product, None)
            metric_values[system].append(at10)
            full_values[system].append(full)
            query_metrics[system] = {"ndcg_at_10": at10, "ndcg_full": full}
        per_query.append({"source_query_id": qid, "query_id": query["query_id"], "candidate_count": len(expected), "metrics": query_metrics})
    write_jsonl_new(ATTEMPT_ROOT / "human_qrels.jsonl", qrel_rows)
    write_jsonl_new(ATTEMPT_ROOT / "per_query_metrics.jsonl", per_query)
    control = metric_values["sha256-random-control"]
    aggregates = {}
    for system in systems:
        deltas = [value - control[index] for index, value in enumerate(metric_values[system])]
        aggregates[system] = {
            "mean_ndcg_at_10": sum(metric_values[system]) / QUERY_COUNT,
            "mean_ndcg_full": sum(full_values[system]) / QUERY_COUNT,
            "paired_delta_vs_random": bootstrap_delta(deltas),
        }
    non_control = systems[:-1]
    best = max(non_control, key=lambda system: (aggregates[system]["mean_ndcg_at_10"], system))
    delta = aggregates[best]["paired_delta_vs_random"]
    performance_pass = delta["mean"] >= 0.05 and delta["ci95_low"] > 0
    report = {
        "schema_version": "esci-task1-public-evaluation-result-v1",
        "attempt": "001", "formal_attempt_count": 1,
        "status": "EXTERNAL_EVIDENCE_ACCEPT" if performance_pass else "HOLD_WEAK_EXTERNAL_EVIDENCE",
        "source_gate": source["status"], "human_labels": True, "ai_labels": False,
        "label_unlock_after_pool_freeze": True, "query_count": QUERY_COUNT,
        "candidate_pair_count": len(qrel_rows), "metric": "Task1 nDCG",
        "gains": GAINS, "primary_cutoff": 10, "aggregates": aggregates,
        "best_system": best, "performance_gate": {
            "required_mean_delta": 0.05, "required_ci95_low_greater_than": 0,
            "observed": delta, "passed": performance_pass,
        },
        "cost": {
            "model_api_calls": 0, "model_cost_usd": 0.0,
            "qrel_selected_compressed_column_bytes": sum(qrel_sizes.values()),
            "elapsed_seconds": round(time.perf_counter() - started, 6),
        },
        "source_hashes": {"examples_sha256": EXAMPLES_SHA256, "products_sha256": PRODUCTS_SHA256,
                          "sample_manifest_sha256": sha256_file(SAMPLE_MANIFEST),
                          "pool_build_manifest_sha256": sha256_file(POOL_MANIFEST),
                          "qrels_sha256": sha256_file(ATTEMPT_ROOT / "human_qrels.jsonl")},
        "hold_boundaries": [
            "50-query US Task1 listwise sample, not the full benchmark",
            "reranking supplied candidate lists, not full-catalog retrieval",
            "built-in semantic-hash is a deterministic dense substitute, not a trained embedding model",
            "no production readiness, default-switch, trained-model superiority, or full-catalog recall authority",
        ],
    }
    write_json_new(ATTEMPT_ROOT / "result.json", report)
    sums = []
    for path in sorted(ATTEMPT_ROOT.iterdir(), key=lambda value: value.name):
        if path.name != "SHA256SUMS.txt" and path.is_file():
            sums.append(f"{sha256_file(path)}  {path.name}")
    (ATTEMPT_ROOT / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("source-audit", "prepare-label-free", "build-pools", "formal-score-once"))
    args = parser.parse_args()
    commands = {
        "source-audit": audit_source,
        "prepare-label-free": prepare_label_free,
        "build-pools": build_pools,
        "formal-score-once": formal_score_once,
    }
    commands[args.command]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
