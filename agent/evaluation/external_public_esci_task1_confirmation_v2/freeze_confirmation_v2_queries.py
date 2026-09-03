from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import aiohttp
import fsspec
import pyarrow.compute as pc
import pyarrow.parquet as pq
import requests


ROOT = Path(__file__).resolve().parent
REPO_ROOT = ROOT.parents[2]
PRIOR_MANIFEST = REPO_ROOT / "agent" / "evaluation" / "external_public_esci_task1_20260902_attempt001" / "label_free_sample_manifest_attempt002.json"
OUTPUT = ROOT / "FROZEN_QUERY_SET.json"
COMMIT = "7916cdf6ab75a462e77f20ab40428a10923998d5"
EXAMPLES_SHA256 = "4a735b693b4a424a6fc67f5be6e4c811495c488bbf66d02a602d308b2744263a"
EXAMPLES_URL = "https://media.githubusercontent.com/media/amazon-science/esci-data/main/shopping_queries_dataset/shopping_queries_dataset_examples.parquet"
SEED = "esci-task1-confirmation-v2-query-sample-20260902"
PROJECTED_COLUMNS = ["query", "query_id", "product_id", "product_locale", "small_version", "split"]


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def order_key(query_id: str) -> tuple[str, str]:
    return sha256_bytes(f"{SEED}\0{query_id}".encode("utf-8")), query_id


def main() -> int:
    if OUTPUT.exists():
        raise RuntimeError("frozen query set already exists")
    if sha256_file(PRIOR_MANIFEST) != "121ab3d012d4cabeb8a4f70c2dc23a134405f7a6d54b7ec53d60e47e88c63b31":
        raise RuntimeError("prior query manifest drift")
    main_commit = requests.get(
        "https://api.github.com/repos/amazon-science/esci-data/commits/main", timeout=30
    ).json()["sha"]
    if main_commit != COMMIT:
        raise RuntimeError(f"official main moved: {main_commit}")
    head = requests.head(EXAMPLES_URL, timeout=30)
    head.raise_for_status()
    if head.headers.get("ETag", "").strip('"') != EXAMPLES_SHA256:
        raise RuntimeError("examples LFS object hash drift")
    prior = json.loads(PRIOR_MANIFEST.read_text(encoding="utf-8"))
    excluded = {str(item["source_query_id"]) for item in prior["queries"]}
    started = time.perf_counter()
    http = fsspec.filesystem(
        "http",
        client_kwargs={"timeout": aiohttp.ClientTimeout(total=900, sock_read=900)},
    )
    handle = http.open(EXAMPLES_URL, "rb", block_size=1024 * 1024, cache_type="none")
    grouped: dict[str, dict[str, Any]] = {}
    try:
        parquet = pq.ParquetFile(handle)
        row_group = parquet.metadata.row_group(0)
        compressed_sizes = {
            row_group.column(index).path_in_schema: row_group.column(index).total_compressed_size
            for index in range(row_group.num_columns)
        }
        for batch in parquet.iter_batches(batch_size=65536, columns=PROJECTED_COLUMNS):
            mask = pc.and_kleene(
                pc.and_kleene(pc.equal(batch["product_locale"], "us"), pc.equal(batch["small_version"], 1)),
                pc.equal(batch["split"], "test"),
            )
            filtered = batch.filter(mask).select(["query", "query_id", "product_id"])
            for row in filtered.to_pylist():
                query_id = str(row["query_id"])
                query = str(row["query"] or "").strip()
                product_id = str(row["product_id"])
                if not query:
                    continue
                item = grouped.setdefault(query_id, {"query": query, "products": []})
                if item["query"] != query:
                    raise RuntimeError(f"query text drift: {query_id}")
                if product_id not in item["products"]:
                    item["products"].append(product_id)
    finally:
        handle.close()
    eligible = [
        query_id for query_id, item in grouped.items()
        if query_id not in excluded and 2 <= len(item["products"]) <= 40
    ]
    selected = sorted(eligible, key=order_key)[:50]
    if len(selected) != 50 or excluded.intersection(selected):
        raise RuntimeError("failed to freeze 50 disjoint queries")
    output = {
        "schema_version": "esci-task1-confirmation-v2-frozen-query-set-v1",
        "status": "QUERY_IDS_FROZEN_LABEL_FREE",
        "source_commit": COMMIT,
        "examples_lfs_sha256": EXAMPLES_SHA256,
        "prior_manifest_sha256": sha256_file(PRIOR_MANIFEST),
        "selection_contract_sha256": sha256_file(ROOT / "SELECTION_CONTRACT.md"),
        "selection_runner_sha256": sha256_file(Path(__file__)),
        "seed": SEED,
        "algorithm": "sort eligible disjoint IDs by SHA256(seed + NUL + query_id), then query_id; take first 50",
        "projected_columns": PROJECTED_COLUMNS,
        "projected_compressed_bytes": sum(compressed_sizes[name] for name in PROJECTED_COLUMNS),
        "source_extraction_method": "HTTP Range with cache_type=none + Parquet column projection followed by in-memory batch processing",
        "relevance_values_read": False,
        "prior_query_count": len(excluded),
        "overlap_with_prior_count": 0,
        "query_count": len(selected),
        "queries": [
            {
                "ordinal": index,
                "source_query_id": query_id,
                "query_sha256": sha256_bytes(grouped[query_id]["query"].encode("utf-8")),
                "candidate_count": len(grouped[query_id]["products"]),
                "candidate_product_ids": grouped[query_id]["products"],
                "selection_key": order_key(query_id)[0],
            }
            for index, query_id in enumerate(selected, 1)
        ],
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    OUTPUT.write_bytes(canonical_bytes(output))
    print(json.dumps({
        "status": output["status"], "query_count": output["query_count"],
        "overlap_with_prior_count": output["overlap_with_prior_count"],
        "query_ids": selected,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
