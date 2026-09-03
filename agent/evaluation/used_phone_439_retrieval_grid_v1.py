"""Build the frozen 439-item retrieval grid and its human-review pool.

This is an evaluator-owned experiment lane.  It does not change the web
runtime, Elasticsearch aliases, or production defaults.  Relevance scoring is
deliberately withheld until the v2 query pool has two independent human reviews
and adjudication.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import statistics
import subprocess
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx
import numpy as np
from fastembed import TextEmbedding


ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = (
    ROOT
    / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3"
)
CATALOG_PATH = CATALOG_DIR / "catalog.jsonl"
DOCUMENTS_PATH = CATALOG_DIR / "documents.jsonl"
PRICE_PATH = CATALOG_DIR / "prices.jsonl"
QUERY_PATH = (
    ROOT
    / "data/annotations/ecommerce/used_phone_human_qrel_v2/selected_scenarios_v1.jsonl"
)
SAFETY_FIXTURE_PATH = ROOT / "agent/evaluation/fixtures/commerce_world_v1_minimal.jsonl"
DEFAULT_OUTPUT_DIR = (
    ROOT / "agent/evaluation/runs/used_phone_439_retrieval_grid_v1_20260828_attempt001"
)
ELASTICSEARCH_URL = "http://127.0.0.1:19282"
ELASTICSEARCH_CONTAINER = "agent-retrieval-smartcn-exp-v1"
MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_CACHE_DIR = ROOT / "agent/.cache/fastembed"
RRF_K = 60
TOP_K = 50
POOL_DEPTH = 50
REPEATS = 3
INDEX_NAMES = {
    "standard": "used-phone-439-grid-v1-standard",
    "smartcn": "used-phone-439-grid-v1-smartcn",
}
WEIGHT_ARMS: dict[str, tuple[float, float]] = {
    "es_1_dense_0": (1.0, 0.0),
    "es_3_dense_1": (3.0, 1.0),
    "es_2_dense_1": (2.0, 1.0),
    "es_1_dense_1": (1.0, 1.0),
    "es_1_dense_2": (1.0, 2.0),
    "es_1_dense_3": (1.0, 3.0),
    "es_0_dense_1": (0.0, 1.0),
}
NON_HARD_PHONE_FIELDS = (
    "os",
    "battery_health",
    "screen_originality",
    "motherboard_repair",
    "battery_originality",
    "scratch_level",
    "shell_condition",
)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    path.write_text("".join(canonical(dict(row)) + "\n" for row in rows), encoding="utf-8")


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(float(ordered[lower]), 3)
    result = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return round(float(result), 3)


def weighted_rrf(
    ranked_lists: Sequence[Sequence[int]],
    weights: Sequence[float],
    *,
    k: int = RRF_K,
) -> list[tuple[int, float]]:
    if len(ranked_lists) != len(weights) or not ranked_lists:
        raise ValueError("ranked lists and weights must be non-empty and aligned")
    if k <= 0 or any(weight < 0 for weight in weights) or not any(weights):
        raise ValueError("RRF k and weights are invalid")
    scores: dict[int, float] = defaultdict(float)
    for ranked, weight in zip(ranked_lists, weights, strict=True):
        seen: set[int] = set()
        for rank, product_id in enumerate(ranked, 1):
            if product_id in seen or weight == 0:
                continue
            seen.add(product_id)
            scores[int(product_id)] += weight / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def active_final_weights(*, soft_preference_count: int) -> dict[str, float]:
    base = {"retrieval": 0.55, "soft": 0.35, "completeness": 0.10}
    if soft_preference_count <= 0:
        base.pop("soft")
    total = sum(base.values())
    return {key: value / total for key, value in base.items()}


def non_hard_structured_completeness(
    catalog_row: Mapping[str, Any], *, hard_keys: Iterable[str] = ()
) -> float:
    excluded = set(hard_keys)
    applicable = [key for key in NON_HARD_PHONE_FIELDS if key not in excluded]
    if not applicable:
        return 0.0
    attributes = catalog_row.get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    known = sum(
        isinstance(attributes.get(key), Mapping)
        and attributes[key].get("status") == "known"
        for key in applicable
    )
    return known / len(applicable)


def final_rerank_score(
    *,
    normalized_rrf: float,
    explicit_soft_match: float,
    completeness: float,
    soft_preference_count: int,
) -> tuple[float, dict[str, float]]:
    weights = active_final_weights(soft_preference_count=soft_preference_count)
    score = (
        weights["retrieval"] * normalized_rrf
        + weights.get("soft", 0.0) * explicit_soft_match
        + weights["completeness"] * completeness
    )
    return score, weights


def load_inputs() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    catalog_rows = read_jsonl(CATALOG_PATH)
    document_rows = read_jsonl(DOCUMENTS_PATH)
    query_rows = [
        row
        for row in read_jsonl(QUERY_PATH)
        if row.get("evaluationRole") == "primary_retrieval"
    ]
    if len(catalog_rows) != 439 or len(document_rows) != 439:
        raise ValueError("439 catalog/document identity mismatch")
    if len(query_rows) != 24:
        raise ValueError("expected 24 primary retrieval queries")
    if {row.get("split") for row in query_rows} != {
        "development", "validation", "sealed_test"
    }:
        raise ValueError("query split contract mismatch")
    catalog_by_id = {int(row["itemId"]): row for row in catalog_rows}
    if len(catalog_by_id) != 439:
        raise ValueError("duplicate catalog itemId")
    documents: list[dict[str, Any]] = []
    seen: set[int] = set()
    for source in document_rows:
        product_id = int(source["product_id"])
        if product_id in seen or product_id not in catalog_by_id:
            raise ValueError("document/catalog binding mismatch")
        seen.add(product_id)
        catalog = catalog_by_id[product_id]
        category = list(catalog.get("categoryPath") or [])
        documents.append({
            "id": product_id,
            "title": str(source.get("item_title") or source.get("catalog_title") or ""),
            "attributeText": str(source.get("attr_value") or ""),
            "brand": str(source.get("brand") or catalog.get("brand") or "").casefold(),
            "categoryL1": category[0] if len(category) > 0 else None,
            "categoryL2": category[1] if len(category) > 1 else None,
            "categoryL3": category[2] if len(category) > 2 else None,
        })
    if seen != set(catalog_by_id):
        raise ValueError("document/catalog item sets differ")
    return sorted(documents, key=lambda row: row["id"]), query_rows


def _container_identity() -> dict[str, Any]:
    raw = subprocess.check_output(
        ["docker", "inspect", ELASTICSEARCH_CONTAINER], text=True, encoding="utf-8"
    )
    value = json.loads(raw)[0]
    labels = value.get("Config", {}).get("Labels") or {}
    if labels.get("codex.retrieval-experiment") != "used-phone-439-v1":
        raise ValueError("refusing Elasticsearch container without experiment owner label")
    if value.get("State", {}).get("Running") is not True:
        raise ValueError("experiment Elasticsearch container is not running")
    return {
        "containerName": ELASTICSEARCH_CONTAINER,
        "containerId": value["Id"],
        "imageId": value["Image"],
        "configuredImage": value.get("Config", {}).get("Image"),
    }


def _mapping(analyzer: str) -> dict[str, Any]:
    return {
        "settings": {
            "analysis": {
                "normalizer": {
                    "lowercase_normalizer": {
                        "type": "custom",
                        "filter": ["lowercase"],
                    }
                }
            }
        },
        "mappings": {
            "properties": {
                "title": {"type": "text", "analyzer": analyzer},
                "attributeText": {"type": "text", "analyzer": analyzer},
                "brand": {
                    "type": "keyword",
                    "ignore_above": 256,
                    "normalizer": "lowercase_normalizer",
                },
                "categoryL1": {"type": "keyword"},
                "categoryL2": {"type": "keyword"},
                "categoryL3": {"type": "keyword"},
            }
        },
    }


def ensure_indices(client: httpx.Client, documents: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    plugins = client.get("/_cat/plugins", params={"format": "json"}).json()
    plugin_names = sorted({str(row.get("component")) for row in plugins})
    if "analysis-smartcn" not in plugin_names:
        raise ValueError("dedicated experiment Elasticsearch lacks analysis-smartcn")
    snapshots: dict[str, Any] = {}
    for analyzer, index_name in INDEX_NAMES.items():
        response = client.head(f"/{index_name}")
        if response.status_code == 404:
            created = client.put(f"/{index_name}", json=_mapping(analyzer))
            created.raise_for_status()
            payload_lines: list[str] = []
            for document in documents:
                payload_lines.append(canonical({"index": {"_index": index_name, "_id": document["id"]}}))
                payload_lines.append(canonical(document))
            bulk = client.post(
                "/_bulk",
                params={"refresh": "true"},
                content="\n".join(payload_lines) + "\n",
                headers={"Content-Type": "application/x-ndjson"},
            )
            bulk.raise_for_status()
            if bulk.json().get("errors") is True:
                raise ValueError(f"bulk indexing failed for {index_name}")
        elif response.status_code != 200:
            response.raise_for_status()
        count = client.get(f"/{index_name}/_count").json().get("count")
        if count != 439:
            raise ValueError(f"unexpected document count for {index_name}: {count}")
        mapping = client.get(f"/{index_name}/_mapping").json()
        actual = mapping[index_name]["mappings"]["properties"]
        if actual["title"].get("analyzer") != analyzer:
            raise ValueError(f"analyzer mismatch for {index_name}")
        snapshots[analyzer] = {"index": index_name, "count": count, "mapping": mapping}
    return {"plugins": plugin_names, "indices": snapshots}


def es_rank(client: httpx.Client, analyzer: str, query: str) -> list[int]:
    response = client.post(
        f"/{INDEX_NAMES[analyzer]}/_search",
        json={
            "size": TOP_K,
            "_source": False,
            "query": {
                "multi_match": {
                    "query": query,
                    "fields": ["title^2", "attributeText"],
                    "fuzziness": "AUTO",
                }
            },
        },
    )
    response.raise_for_status()
    return [int(hit["_id"]) for hit in response.json()["hits"]["hits"]]


def dense_rankings(
    documents: Sequence[Mapping[str, Any]], queries: Sequence[Mapping[str, Any]]
) -> tuple[dict[str, list[int]], dict[str, float]]:
    model = TextEmbedding(model_name=MODEL_NAME, cache_dir=str(MODEL_CACHE_DIR))
    document_ids = [int(row["id"]) for row in documents]
    started = time.perf_counter()
    document_vectors = np.asarray(list(model.embed([str(row["title"]) for row in documents])))
    build_ms = (time.perf_counter() - started) * 1000
    document_norms = np.linalg.norm(document_vectors, axis=1)
    rankings: dict[str, list[int]] = {}
    latencies: dict[str, float] = {}
    for query in queries:
        query_id = str(query["scenarioId"])
        started = time.perf_counter()
        vector = np.asarray(list(model.embed([str(query["retrievalQuery"])])))[0]
        denominator = np.linalg.norm(vector) * document_norms
        similarities = np.divide(
            document_vectors @ vector,
            denominator,
            out=np.full(len(document_ids), -1.0),
            where=denominator != 0,
        )
        order = sorted(range(len(document_ids)), key=lambda i: (-float(similarities[i]), document_ids[i]))
        rankings[query_id] = [document_ids[index] for index in order[:TOP_K]]
        latencies[query_id] = (time.perf_counter() - started) * 1000
    return rankings, {"buildMs": build_ms, "queryMs": latencies}


def build_review_pool(
    traces: Sequence[Mapping[str, Any]], documents: Sequence[Mapping[str, Any]], queries: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {int(row["id"]): row for row in documents}
    trace_by_query: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for trace in traces:
        if trace.get("phase") == "measured" and trace.get("repeat") == 1:
            trace_by_query[str(trace["queryId"])].append(trace)
    rows: list[dict[str, Any]] = []
    for query in queries:
        query_id = str(query["scenarioId"])
        ranks: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for trace in trace_by_query[query_id]:
            for rank, product_id in enumerate(trace["rankedIds"][:POOL_DEPTH], 1):
                ranks[int(product_id)].append({
                    "analyzer": trace["analyzer"],
                    "arm": trace["arm"],
                    "rank": rank,
                })
        candidates = []
        for product_id in sorted(ranks, key=lambda item: (min(x["rank"] for x in ranks[item]), item)):
            document = by_id[product_id]
            candidates.append({
                "productId": product_id,
                "title": document["title"],
                "brand": document["brand"],
                "attributeText": document["attributeText"],
                "poolWitnesses": sorted(
                    ranks[product_id], key=lambda item: (item["rank"], item["analyzer"], item["arm"])
                ),
                "review": {"relevance": None, "reason": None},
            })
        rows.append({
            "schemaVersion": "used-phone-439-retrieval-human-pool-v1",
            "queryId": query_id,
            "query": query["rawQuery"],
            "split": query["split"],
            "status": "PENDING_TWO_INDEPENDENT_HUMAN_REVIEWS_AND_ADJUDICATION",
            "candidateCount": len(candidates),
            "candidates": candidates,
        })
    return rows


def run(output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    documents, queries = load_inputs()
    output_dir.mkdir(parents=True, exist_ok=False)
    container = _container_identity()
    traces: list[dict[str, Any]] = []
    es_latencies: dict[str, list[float]] = defaultdict(list)
    with httpx.Client(base_url=ELASTICSEARCH_URL, timeout=60.0) as client:
        elasticsearch = ensure_indices(client, documents)
        dense, dense_latency = dense_rankings(documents, queries)
        for analyzer in INDEX_NAMES:
            for repeat in range(1, REPEATS + 1):
                for query in queries:
                    query_id = str(query["scenarioId"])
                    started = time.perf_counter()
                    lexical = es_rank(client, analyzer, str(query["retrievalQuery"]))
                    es_ms = (time.perf_counter() - started) * 1000
                    es_latencies[analyzer].append(es_ms)
                    for arm, weights in WEIGHT_ARMS.items():
                        started = time.perf_counter()
                        ranked = weighted_rrf([lexical, dense[query_id]], weights)[:TOP_K]
                        fusion_ms = (time.perf_counter() - started) * 1000
                        traces.append({
                            "schemaVersion": "used-phone-439-retrieval-grid-trace-v1",
                            "phase": "measured",
                            "queryId": query_id,
                            "split": query["split"],
                            "analyzer": analyzer,
                            "arm": arm,
                            "weights": {"es": weights[0], "dense": weights[1]},
                            "repeat": repeat,
                            "esTop50": lexical,
                            "denseTop50": dense[query_id],
                            "rankedIds": [product_id for product_id, _ in ranked],
                            "scores": [round(score, 12) for _, score in ranked],
                            "latencyMs": {"es": round(es_ms, 3), "fusion": round(fusion_ms, 3)},
                        })
    query_ids = [str(row["scenarioId"]) for row in queries]
    for analyzer in INDEX_NAMES:
        for arm in WEIGHT_ARMS:
            for query_id in query_ids:
                signatures = {
                    tuple(row["rankedIds"])
                    for row in traces
                    if row["analyzer"] == analyzer
                    and row["arm"] == arm
                    and row["queryId"] == query_id
                }
                if len(signatures) != 1:
                    raise ValueError(f"non-deterministic ranking: {analyzer}/{arm}/{query_id}")
    review_pool = build_review_pool(traces, documents, queries)
    trace_path = output_dir / "trace.jsonl"
    pool_path = output_dir / "human_review_pool.jsonl"
    write_jsonl(trace_path, traces)
    write_jsonl(pool_path, review_pool)
    report = {
        "schemaVersion": "used-phone-439-retrieval-grid-report-v1",
        "status": "HOLD_PENDING_FROZEN_TASKSTATE_AND_DOUBLE_HUMAN_QRELS",
        "decision": {
            "deterministicSafetyGate": "ACCEPT",
            "relevanceWinner": "NOT_EVALUATED",
            "productionDefaultSwitch": "HOLD",
            "reason": (
                "The 24-query v2 set has no frozen TaskState intents or human qrels. "
                "Rankings and the full Top-50 union pool are ready for two independent reviews."
            ),
        },
        "design": {
            "catalogRows": 439,
            "queryCount": 24,
            "querySplits": {
                split: sum(row["split"] == split for row in queries)
                for split in ("development", "validation", "sealed_test")
            },
            "analyzers": list(INDEX_NAMES),
            "weightArms": {
                arm: {"es": weights[0], "dense": weights[1]}
                for arm, weights in WEIGHT_ARMS.items()
            },
            "rrfK": RRF_K,
            "channelDepth": TOP_K,
            "humanPoolDepth": POOL_DEPTH,
            "repeats": REPEATS,
            "lexicalFields": ["title^2", "attributeText^1"],
            "denseFields": ["title"],
            "embeddingModel": MODEL_NAME,
            "crossEncoder": "disabled",
            "finalRerankTarget": (
                "0.55*normalizedRRF + 0.35*explicitSoftMatch + "
                "0.10*nonHardStructuredCompleteness; renormalize active weights"
            ),
        },
        "execution": {
            "traceRows": len(traces),
            "stableRankingCells": len(INDEX_NAMES) * len(WEIGHT_ARMS) * len(queries),
            "modelCalls": {"embedding": 1 + len(queries), "llm": 0, "taskManager": 0, "finalAnswer": 0},
            "tokens": {"prompt": 0, "completion": 0},
            "latencyMs": {
                "denseCatalogBuild": round(float(dense_latency["buildMs"]), 3),
                "denseQueryP50": percentile(list(dense_latency["queryMs"].values()), 0.50),
                "denseQueryP95": percentile(list(dense_latency["queryMs"].values()), 0.95),
                "es": {
                    analyzer: {
                        "p50": percentile(values, 0.50),
                        "p95": percentile(values, 0.95),
                    }
                    for analyzer, values in es_latencies.items()
                },
            },
            "humanPool": {
                "rows": len(review_pool),
                "candidatePairs": sum(row["candidateCount"] for row in review_pool),
                "minimumReviewers": 2,
                "adjudicationRequired": True,
            },
        },
        "elasticsearch": {"container": container, **elasticsearch},
        "inputPins": {
            path.relative_to(ROOT).as_posix(): {"bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in (CATALOG_PATH, DOCUMENTS_PATH, PRICE_PATH, QUERY_PATH, SAFETY_FIXTURE_PATH)
        },
        "limitations": [
            "No relevance metrics may be computed before qrel completion.",
            "Hard-filter and final-rerank production alignment awaits frozen TaskState intent labels.",
            "The 12-product fixture is a safety/routing fixture, not ranking-quality authority.",
            "The 252-item corpus and 46079-row KuaiSearch labels are excluded from winner selection.",
        ],
    }
    report_path = output_dir / "report.json"
    report_path.write_text(canonical(report) + "\n", encoding="utf-8")
    receipt = {
        "schemaVersion": "used-phone-439-retrieval-grid-receipt-v1",
        "status": report["status"],
        "artifacts": {
            "trace.jsonl": {"rows": len(traces), "sha256": sha256(trace_path)},
            "human_review_pool.jsonl": {"rows": len(review_pool), "sha256": sha256(pool_path)},
            "report.json": {"sha256": sha256(report_path)},
        },
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("fastembed", "onnxruntime", "numpy", "httpx")
        },
        "source": {
            "path": Path(__file__).resolve().relative_to(ROOT).as_posix(),
            "sha256": sha256(Path(__file__).resolve()),
        },
    }
    (output_dir / "receipt.json").write_text(canonical(receipt) + "\n", encoding="utf-8")
    return output_dir


def validate(output_dir: Path) -> None:
    report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    receipt = json.loads((output_dir / "receipt.json").read_text(encoding="utf-8"))
    if report["decision"]["deterministicSafetyGate"] != "ACCEPT":
        raise ValueError("deterministic safety gate is not accepted")
    if report["decision"]["productionDefaultSwitch"] != "HOLD":
        raise ValueError("unreviewed experiment cannot switch production")
    for name, pin in receipt["artifacts"].items():
        if sha256(output_dir / name) != pin["sha256"]:
            raise ValueError(f"artifact hash mismatch: {name}")
    traces = read_jsonl(output_dir / "trace.jsonl")
    if len(traces) != 24 * 2 * 7 * REPEATS:
        raise ValueError("trace grid is incomplete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        validate(args.output_dir)
        print(canonical({"status": "ACCEPT", "outputDir": str(args.output_dir)}))
        return 0
    output = run(args.output_dir)
    validate(output)
    print(canonical({"status": "ACCEPT", "outputDir": str(output)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
