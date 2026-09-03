"""Production-aligned, evidence-bound product retrieval experiment.

The ranking phase reads only an evaluator-owned public query bundle. Qrels are
opened after all predictions. Unknown/unjudged candidates are not negatives;
pooled metrics are descriptive and never authorize an online default switch.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from agent.app.domains.ecommerce.models import expand_product_query
from agent.app.domains.ecommerce.tools import search_products_tool, warm_local_product_vector_cache
from agent.app.settings import settings


ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = ROOT / "data/derived/ecommerce/product_retrieval_nonsealed_bundle_v1_20260828_r1"
PUBLIC_QUERY_PATH = BUNDLE_DIR / "queries_public.jsonl"
EVALUATOR_QREL_PATH = BUNDLE_DIR / "qrels_evaluator.jsonl"
BUNDLE_MANIFEST_PATH = BUNDLE_DIR / "manifest.json"
DEFAULT_OUTPUT_DIR = ROOT / "agent/evaluation/runs/product_retrieval_architecture_v1_20260828_attempt007"
BACKEND_CONTAINER = "agent-used-phone-demo-search-backend-v1"
ELASTICSEARCH_CONTAINER = "agent-used-phone-demo-elasticsearch-v1"
ELASTICSEARCH_URL = "http://127.0.0.1:19281"
MODES = ("bm25", "hybrid")
REPEATS = 3
PACKAGE_NAMES = ("fastembed", "onnxruntime", "numpy", "httpx", "pydantic")
SUT_SOURCE_PATHS = (
    ROOT / "agent/app/domains/ecommerce/tools.py",
    ROOT / "agent/app/domains/ecommerce/models.py",
    ROOT / "agent/app/domains/ecommerce/ranking_contract.py",
    ROOT / "agent/app/domains/ecommerce/synthetic_prices.py",
    ROOT / "agent/app/domains/ecommerce/title_relevance.py",
    ROOT / "agent/app/rag.py",
    ROOT / "agent/app/settings.py",
    ROOT / "agent/app/main.py",
    ROOT / "docker-compose.yml",
    ROOT / "scripts/used-phone-demo.ps1",
    Path(__file__),
)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def artifact_pin(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    pin: dict[str, Any] = {"bytes": path.stat().st_size, "sha256": sha256(path)}
    if rows is not None:
        pin["rows"] = rows
    return pin


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = math.ceil(fraction * len(ordered)) - 1
    return ordered[max(0, min(index, len(ordered) - 1))]


def dcg(grades: list[int]) -> float:
    return sum(((2**grade) - 1) / math.log2(index + 2) for index, grade in enumerate(grades))


def load_public_queries() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate only ranking-visible assets; never open evaluator qrels here."""
    manifest = json.loads(BUNDLE_MANIFEST_PATH.read_text(encoding="utf-8"))
    queries = read_jsonl(PUBLIC_QUERY_PATH)
    expected_ids = manifest.get("queryIds")
    if manifest.get("sealedRowsAbsent") is not True:
        raise ValueError("bundle does not assert sealed-row absence")
    if [row.get("queryId") for row in queries] != expected_ids:
        raise ValueError("public query order or identity mismatch")
    if any(row.get("schemaVersion") != "product-retrieval-nonsealed-query-v1" for row in queries):
        raise ValueError("public query schema mismatch")
    if any("split" in row or "evaluationRole" in row for row in queries):
        raise ValueError("source-only metadata leaked into ranking bundle")
    if manifest["artifacts"][PUBLIC_QUERY_PATH.name] != artifact_pin(PUBLIC_QUERY_PATH, rows=len(queries)):
        raise ValueError("public query artifact binding mismatch")
    return queries, manifest


def load_evaluator_qrels(query_ids: list[str], manifest: dict[str, Any]) -> dict[str, dict[int, int]]:
    rows = read_jsonl(EVALUATOR_QREL_PATH)
    if manifest["artifacts"][EVALUATOR_QREL_PATH.name] != artifact_pin(EVALUATOR_QREL_PATH, rows=len(rows)):
        raise ValueError("evaluator qrel artifact binding mismatch")
    qrels: dict[str, dict[int, int]] = {query_id: {} for query_id in query_ids}
    for row in rows:
        if row.get("schemaVersion") != "product-retrieval-nonsealed-qrel-v1":
            raise ValueError("evaluator qrel schema mismatch")
        query_id = row.get("queryId")
        if query_id not in qrels:
            raise ValueError("evaluator qrel crossed public query boundary")
        grade = row.get("grade")
        if type(grade) is not int or not 0 <= grade <= 3:
            raise ValueError("invalid evaluator grade")
        qrels[query_id][int(row["productId"])] = grade
    if any(not values for values in qrels.values()):
        raise ValueError("evaluator qrel coverage incomplete")
    return qrels


def score_mode(
    rows: list[dict[str, Any]],
    qrels: dict[str, dict[int, int]],
    query_ids: list[str] | None = None,
) -> dict[str, Any]:
    query_ids = query_ids or list(qrels)
    rows = [row for row in rows if row.get("phase") == "measured"]
    canonical_rows = [row for row in rows if row["repeat"] == 1]
    if {row["queryId"] for row in canonical_rows} != set(query_ids):
        raise ValueError("canonical prediction grid is incomplete")
    per_query = []
    for row in sorted(canonical_rows, key=lambda item: item["queryId"]):
        query_id = row["queryId"]
        grades_by_id = qrels[query_id]
        ranked = row["rankedItemIds"]
        pool = row["candidatePoolIds"]
        top10_grades = [grades_by_id.get(product_id, 0) for product_id in ranked[:10]]
        ideal = dcg(sorted(grades_by_id.values(), reverse=True)[:10])
        relevant = {product_id for product_id, grade in grades_by_id.items() if grade >= 2}
        judged_top10 = sum(product_id in grades_by_id for product_id in ranked[:10])
        per_query.append({
            "queryId": query_id,
            "pooledNdcgAt10": dcg(top10_grades) / ideal if ideal else 0.0,
            "judgedGradeAtLeast2HitAt3": float(
                any(grades_by_id.get(product_id, 0) >= 2 for product_id in ranked[:3])
            ),
            "judgedRelevantPoolRecallAt50": (
                len(relevant.intersection(pool[:50])) / len(relevant) if relevant else 0.0
            ),
            "judgedRelevantFinalRecallAt20": (
                len(relevant.intersection(ranked[:20])) / len(relevant) if relevant else 0.0
            ),
            "judgedCoverageAt10": judged_top10 / max(1, min(10, len(ranked))),
        })
    durations = [float(row["durationMs"]) for row in rows]
    mismatches = 0
    for query_id in query_ids:
        query_rows = [row for row in rows if row["queryId"] == query_id]
        signatures = {(tuple(row["candidatePoolIds"]), tuple(row["rankedItemIds"])) for row in query_rows}
        mismatches += int(len(signatures) != 1)
    hard_denominator = sum(int(row["top3HardConstraintDenominator"]) for row in canonical_rows)
    hard_failures = sum(int(row["top3HardViolationCount"]) for row in canonical_rows)
    vector_active = sum(row["vectorChannelStatus"] == "active" for row in canonical_rows)
    return {
        "queryCount": len(per_query),
        "callCount": len(rows),
        "failedCallCount": sum(not row["ok"] for row in rows),
        "repeatRankingMismatchQueryCount": mismatches,
        "metrics": {
            key: round(statistics.fmean(item[key] for item in per_query), 6)
            for key in (
                "pooledNdcgAt10",
                "judgedGradeAtLeast2HitAt3",
                "judgedRelevantPoolRecallAt50",
                "judgedRelevantFinalRecallAt20",
                "judgedCoverageAt10",
            )
        },
        "hardConstraint": {
            "top3Denominator": hard_denominator,
            "top3ViolationCount": hard_failures,
            "top3ViolationRate": round(hard_failures / hard_denominator, 6) if hard_denominator else None,
        },
        "latencyMs": {
            "p50": round(percentile(durations, 0.50), 3),
            "p95": round(percentile(durations, 0.95), 3),
            "mean": round(statistics.fmean(durations), 3),
        },
        "vectorActiveCanonicalQueries": vector_active,
        "perQuery": per_query,
    }


def validate_trace_grid(traces: list[dict[str, Any]], query_ids: list[str], repeats: int) -> None:
    allowed_phases = {"warmup", "measured"}
    if any(row.get("phase") not in allowed_phases for row in traces):
        raise ValueError("unexpected trace phase")
    measured = [row for row in traces if row["phase"] == "measured"]
    measured_keys = [(row["mode"], row["repeat"], row["queryId"]) for row in measured]
    expected_keys = [
        (mode, repeat, query_id)
        for repeat in range(1, repeats + 1)
        for mode in MODES
        for query_id in query_ids
    ]
    if sorted(measured_keys) != sorted(expected_keys) or len(measured_keys) != len(set(measured_keys)):
        raise ValueError("measured trace grid mismatch")
    warmups = [row for row in traces if row["phase"] == "warmup"]
    warmup_keys = [(row["mode"], row["repeat"], row["queryId"]) for row in warmups]
    if warmup_keys != [("bm25", 0, query_ids[0]), ("hybrid", 0, query_ids[0])]:
        raise ValueError("warmup trace grid mismatch")


def decide(
    scores: dict[str, dict[str, Any]],
    *,
    prior_ce: dict[str, Any],
    prior_broad: dict[str, Any],
) -> dict[str, Any]:
    bm25 = scores["bm25"]
    hybrid = scores["hybrid"]
    quality_delta = hybrid["metrics"]["pooledNdcgAt10"] - bm25["metrics"]["pooledNdcgAt10"]
    recall_delta = (
        hybrid["metrics"]["judgedRelevantPoolRecallAt50"]
        - bm25["metrics"]["judgedRelevantPoolRecallAt50"]
    )
    top3_hit_delta = (
        hybrid["metrics"]["judgedGradeAtLeast2HitAt3"]
        - bm25["metrics"]["judgedGradeAtLeast2HitAt3"]
    )
    engineering_checks = {
        "allCallsSucceeded": all(value["failedCallCount"] == 0 for value in scores.values()),
        "repeatStable": all(value["repeatRankingMismatchQueryCount"] == 0 for value in scores.values()),
        "hardSafety": all(value["hardConstraint"]["top3ViolationCount"] == 0 for value in scores.values()),
        "hybridVectorActive": hybrid["vectorActiveCanonicalQueries"] == hybrid["queryCount"],
    }
    shadow_nomination_checks = {
        "pooledNdcgNoCatastrophicDrop": quality_delta >= -0.10,
        "judgedPoolRecallNoCatastrophicDrop": recall_delta >= -0.10,
        "judgedTop3HitRemainsAtLeastHalf": hybrid["metrics"]["judgedGradeAtLeast2HitAt3"] >= 0.50,
    }
    hybrid_shadow_candidate = (
        all(engineering_checks.values())
        and prior_broad["g1"]["rrfMrrAdvantageCiExcludesZero"]
        and all(shadow_nomination_checks.values())
    )
    blockers = [
        "five_query_nonsealed_lane_is_too_small_for_default_selection",
        "pooled_judgments_are_incomplete_and_descriptive_metrics_are_not_acceptance_gates",
        "phase_a_human_process_gate_is_not_accepted",
        "no_new_independent_blind_live_shadow_evidence",
        "selection_thresholds_were_not_externally_preregistered",
    ]
    if top3_hit_delta < 0:
        blockers.append("judged_top3_hit_regression_observed")
    return {
        "status": "HOLD_TARGET_ARCHITECTURE_EVIDENCE_REPAIR_REQUIRED",
        "selectedCandidateGeneration": (
            "hybrid_rrf_bm25_dense_structured_next_shadow_candidate"
            if hybrid_shadow_candidate
            else "bm25_structured_current_safe_default"
        ),
        "authoritativePostRecall": "java_facts_hard_gate_then_deterministic_rule_rerank",
        "crossEncoder": (
            "disabled_no_stable_gain"
            if not prior_ce["stableImprovement"]
            else "hold_for_latency_and_safety_review"
        ),
        "degradedFallback": "bm25_plus_structured_requirements",
        "onlineDefaultSwitch": "HOLD_NO_CHANGE_AUTHORIZED",
        "engineeringChecks": engineering_checks,
        "shadowNominationChecks": {
            **shadow_nomination_checks,
            "authority": "non_authorizing_guardrails_not_acceptance_thresholds",
        },
        "evidenceBlockers": blockers,
        "descriptiveDeltas": {
            "pooledNdcgAt10": round(quality_delta, 6),
            "judgedRelevantPoolRecallAt50": round(recall_delta, 6),
            "judgedGradeAtLeast2HitAt3": round(top3_hit_delta, 6),
        },
        "claimBoundary": (
            "Hybrid is the next shadow candidate, not a proven universal winner. "
            "Descriptive pooled scores do not authorize an online or repository-wide default change."
        ),
    }


async def run_predictions(projections: list[dict[str, Any]], *, repeats: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    original_mode = settings.product_retrieval_mode
    original_backend = settings.product_vector_backend
    original_reranker = settings.product_title_reranker_enabled
    try:
        settings.product_vector_backend = "local"
        settings.product_title_reranker_enabled = False

        async def execute(mode: str, repeat: int, query: dict[str, Any], *, phase: str) -> None:
            settings.product_retrieval_mode = mode
            trace = await search_products_tool(
                query=query["query"],
                category=query["category"],
                requirements=query["requirements"],
                limit=20,
            )
            detail = trace.detail if isinstance(trace.detail, dict) else {}
            candidates = detail.get("candidates", []) if trace.ok else []
            hard_checks = [
                check
                for candidate in candidates[:3]
                for check in candidate.get("checks", [])
                if check.get("priority") == "hard"
            ]
            channels = detail.get("retrievalTrace", {}).get("channels", {})
            vector_status = channels.get("localVector", channels.get("qdrant", {})).get("status", "disabled")
            rows.append({
                "schemaVersion": "product-retrieval-architecture-trace-v2",
                "phase": phase,
                "mode": mode,
                "repeat": repeat,
                "queryId": query["queryId"],
                "ok": bool(trace.ok),
                "durationMs": float(trace.duration_ms or 0.0),
                "failureCode": None if trace.ok else detail.get("code"),
                "failureMessage": None if trace.ok else detail.get("message"),
                "candidatePoolIds": [int(value) for value in detail.get("candidatePoolIds", [])],
                "rankedItemIds": [int(value) for value in detail.get("rankedItemIds", [])],
                "fusion": detail.get("retrievalTrace", {}).get("fusion"),
                "channelStatus": channels,
                "vectorChannelStatus": vector_status,
                "degraded": detail.get("retrievalTrace", {}).get("degraded", []),
                "top3HardConstraintDenominator": len(hard_checks),
                "top3HardViolationCount": sum(check.get("status") == "fail" for check in hard_checks),
                "top3HardUnknownCount": sum(check.get("status") in {"unknown", "conflict"} for check in hard_checks),
                "titleReranker": detail.get("retrievalTrace", {}).get("titleReranker", {}),
            })

        await execute("bm25", 0, projections[0], phase="warmup")
        settings.product_retrieval_mode = "hybrid"
        readiness_started = time.perf_counter()
        warmed_products = await asyncio.wait_for(
            warm_local_product_vector_cache(),
            timeout=max(float(settings.product_vector_timeout_seconds), 30.0),
        )
        readiness_ms = round((time.perf_counter() - readiness_started) * 1000, 3)
        await execute("hybrid", 0, projections[0], phase="warmup")
        rows[-1]["startupVectorPrecomputeDurationMs"] = readiness_ms
        rows[-1]["startupVectorPrecomputeProductCount"] = warmed_products
        if rows[-1]["vectorChannelStatus"] != "active":
            raise RuntimeError("hybrid readiness query did not activate local vector channel")
        for repeat in range(1, repeats + 1):
            for mode in MODES:
                for query in projections:
                    await execute(mode, repeat, query, phase="measured")
    finally:
        settings.product_retrieval_mode = original_mode
        settings.product_vector_backend = original_backend
        settings.product_title_reranker_enabled = original_reranker
    return rows


def prior_ce_evidence() -> dict[str, Any]:
    score_path = ROOT / "data/derived/ecommerce/kuaisearch_multicategory_retrieval_g1_score_20260824_r1/score.json"
    score = json.loads(score_path.read_text(encoding="utf-8"))
    rrf = score["strategies"]["rrf_bm25_dense"]
    ce = score["strategies"]["rrf_bm25_dense_ce"]
    matches = [
        row for row in score["pairedBootstrapCI"]["gradeAtLeast2"]
        if row["leftStrategy"] == "rrf_bm25_dense"
        and row["rightStrategy"] == "rrf_bm25_dense_ce"
        and row["metric"] == "mrr"
    ]
    if len(matches) != 1:
        raise ValueError("frozen RRF versus CE comparison missing")
    ci = matches[0]["ci95"]
    return {
        "source": score_path.relative_to(ROOT).as_posix(),
        "sourceSha256": sha256(score_path),
        "stableImprovement": bool(ci[0] > 0 or ci[1] < 0),
        "mrrDeltaCeMinusRrf": round(
            ce["overall"]["gradeAtLeast2"]["mrr"] - rrf["overall"]["gradeAtLeast2"]["mrr"], 6
        ),
        "mrrDeltaRrfMinusCeCi95": ci,
        "latencyMultiplierVsRrf": round(ce["latency"]["p95Ms"] / rrf["latency"]["p95Ms"], 6),
        "rrfP95Ms": rrf["latency"]["p95Ms"],
        "ceP95Ms": ce["latency"]["p95Ms"],
    }


def prior_broad_evidence() -> dict[str, Any]:
    g1_path = ROOT / "data/derived/ecommerce/kuaisearch_multicategory_retrieval_g1_score_20260824_r1/score.json"
    phase_path = ROOT / "data/derived/ecommerce/kuaisearch_g2_phase_a_retrieval_analysis_20260824_r1/report.json"
    g1 = json.loads(g1_path.read_text(encoding="utf-8"))
    phase = json.loads(phase_path.read_text(encoding="utf-8"))
    matches = [
        row for row in g1["pairedBootstrapCI"]["gradeAtLeast2"]
        if row["leftStrategy"] == "bm25_fields"
        and row["rightStrategy"] == "rrf_bm25_dense"
        and row["metric"] == "mrr"
    ]
    if len(matches) != 1:
        raise ValueError("frozen broad BM25 versus RRF comparison missing")
    ci = matches[0]["ci95"]
    phase_metrics = phase["metricsByK"]["3"]
    return {
        "g1": {
            "source": g1_path.relative_to(ROOT).as_posix(),
            "sourceSha256": sha256(g1_path),
            "queryCount": g1["counts"]["queries"],
            "labelBoundary": g1["labelBoundary"],
            "bm25FieldsMrr": g1["strategies"]["bm25_fields"]["overall"]["gradeAtLeast2"]["mrr"],
            "rrfMrr": g1["strategies"]["rrf_bm25_dense"]["overall"]["gradeAtLeast2"]["mrr"],
            "bm25MinusRrfMrrCi95": ci,
            "rrfMrrAdvantageCiExcludesZero": ci[1] < 0,
        },
        "phaseA": {
            "source": phase_path.relative_to(ROOT).as_posix(),
            "sourceSha256": sha256(phase_path),
            "queryCount": phase["queryCount"],
            "qrelCount": phase["qrelCount"],
            "pooledNdcgAt3": {
                "bm25": phase_metrics["bm25_fields"]["ndcgLowerBound"],
                "rrf": phase_metrics["rrf_bm25_dense"]["ndcgLowerBound"],
                "sourceFieldWasNamedLowerBound": True,
                "interpretation": "descriptive pooled metric only; not a mathematical lower bound",
            },
            "descriptiveLeader": phase["descriptiveLeader"],
            "processDecision": phase["decision"],
            "winnerClaimAllowed": False,
        },
    }


def source_manifest() -> dict[str, Any]:
    return {path.relative_to(ROOT).as_posix(): artifact_pin(path) for path in SUT_SOURCE_PATHS}


def model_manifest() -> dict[str, Any]:
    cache_dir = Path(settings.rag_model_cache_dir).resolve()
    files = sorted(path for path in cache_dir.rglob("*") if path.is_file())
    if not files:
        raise ValueError(f"embedding model cache is empty: {cache_dir}")
    return {
        "modelName": settings.rag_embedding_model,
        "cacheDir": cache_dir.relative_to(ROOT).as_posix(),
        "files": {path.relative_to(cache_dir).as_posix(): artifact_pin(path) for path in files},
    }


def package_manifest() -> dict[str, str]:
    return {name: importlib.metadata.version(name) for name in PACKAGE_NAMES}


def docker_identity(container: str) -> dict[str, Any]:
    raw = subprocess.check_output(["docker", "inspect", container], text=True, encoding="utf-8")
    value = json.loads(raw)[0]
    state = value.get("State", {})
    config = value.get("Config", {})
    if state.get("Running") is not True:
        raise ValueError(f"backend container is not running: {container}")
    return {
        "containerName": container,
        "containerId": value["Id"],
        "containerImageId": value["Image"],
        "configuredImage": config.get("Image"),
        "created": value.get("Created"),
        "startedAt": state.get("StartedAt"),
        "restartCount": value.get("RestartCount"),
        "ports": value.get("NetworkSettings", {}).get("Ports", {}),
    }


async def fetch_elasticsearch_snapshot(elasticsearch_url: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        root_response, index_response, alias_response = await asyncio.gather(
            client.get(elasticsearch_url.rstrip("/")),
            client.get(
                f"{elasticsearch_url.rstrip('/')}/_cat/indices",
                params={"format": "json", "h": "health,status,index,uuid,pri,rep,docs.count,docs.deleted,store.size"},
            ),
            client.get(f"{elasticsearch_url.rstrip('/')}/_alias"),
        )
    for response in (root_response, index_response, alias_response):
        response.raise_for_status()
    indices = sorted(index_response.json(), key=lambda row: row.get("index", ""))
    return {
        "node": root_response.json(),
        "indices": indices,
        "aliases": alias_response.json(),
    }


async def fetch_backend_channel_snapshot(
    backend_url: str,
    projections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        for query in projections:
            retrieval_query, expanded_terms = expand_product_query(query["query"])
            response = await client.get(
                f"{backend_url.rstrip('/')}/api/products/retrieval",
                params={"query": retrieval_query, "category": query["category"], "limit": 50},
            )
            response.raise_for_status()
            data = response.json().get("data", {})
            products = data.get("products", [])
            rows.append({
                "queryId": query["queryId"],
                "retrievalQuery": retrieval_query,
                "expandedTerms": expanded_terms,
                "channel": data.get("channel"),
                "productIds": [int(product["id"]) for product in products],
            })
    if any(row["channel"] != "elasticsearch" for row in rows):
        raise ValueError("backend retrieval channel was not Elasticsearch")
    return rows


async def fetch_catalog(backend_url: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"{backend_url.rstrip('/')}/api/products",
            params={"category": "手机", "limit": 1500},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("success") is not True or not isinstance(payload.get("data"), list):
        raise ValueError("catalog endpoint returned an invalid envelope")
    rows = sorted(payload["data"], key=lambda row: int(row["id"]))
    if len(rows) != 252:
        raise ValueError(f"expected frozen 252-product catalog, got {len(rows)}")
    return rows


def validate_source_and_model_provenance(provenance: dict[str, Any]) -> None:
    if provenance["sources"] != source_manifest():
        raise ValueError("SUT source pin mismatch")
    if provenance["packages"] != package_manifest():
        raise ValueError("runtime package pin mismatch")
    if provenance["model"] != model_manifest():
        raise ValueError("embedding model file pin mismatch")


def validate_run(output_dir: Path) -> None:
    paths = {name: output_dir / name for name in (
        "report.json", "trace.jsonl", "receipt.json", "catalog_snapshot.json",
        "provenance.json", "model_manifest.json", "elasticsearch_snapshot.json",
        "backend_channel_snapshot.json",
    )}
    report = json.loads(paths["report.json"].read_text(encoding="utf-8"))
    traces = read_jsonl(paths["trace.jsonl"])
    receipt = json.loads(paths["receipt.json"].read_text(encoding="utf-8"))
    provenance = json.loads(paths["provenance.json"].read_text(encoding="utf-8"))
    for name in (
        "report.json", "trace.jsonl", "catalog_snapshot.json", "provenance.json",
        "model_manifest.json", "elasticsearch_snapshot.json", "backend_channel_snapshot.json",
    ):
        rows = len(traces) if name == "trace.jsonl" else None
        if receipt["artifacts"][name] != artifact_pin(paths[name], rows=rows):
            raise ValueError(f"{name} artifact binding mismatch")
    if json.loads(paths["model_manifest.json"].read_text(encoding="utf-8")) != provenance["model"]:
        raise ValueError("model manifest artifact mismatch")
    catalog = json.loads(paths["catalog_snapshot.json"].read_text(encoding="utf-8"))
    catalog_content_hash = hashlib.sha256(canonical(catalog).encode("utf-8")).hexdigest()
    if provenance["catalog"]["beforeContentSha256"] != catalog_content_hash:
        raise ValueError("catalog content binding mismatch")
    if provenance["catalog"]["beforeContentSha256"] != provenance["catalog"]["afterContentSha256"]:
        raise ValueError("catalog drift recorded in authoritative run")
    if provenance["elasticsearch"]["snapshotArtifactSha256"] != sha256(paths["elasticsearch_snapshot.json"]):
        raise ValueError("Elasticsearch snapshot binding mismatch")
    if provenance["backendChannel"]["beforeArtifactSha256"] != sha256(paths["backend_channel_snapshot.json"]):
        raise ValueError("backend channel snapshot binding mismatch")
    if provenance["backendChannel"]["beforeContentSha256"] != provenance["backendChannel"]["afterContentSha256"]:
        raise ValueError("backend retrieval channel drift recorded in authoritative run")
    validate_source_and_model_provenance(provenance)
    queries, bundle_manifest = load_public_queries()
    if report["inputs"]["bundleManifestSha256"] != sha256(BUNDLE_MANIFEST_PATH):
        raise ValueError("bundle manifest pin mismatch")
    query_ids = [row["queryId"] for row in queries]
    validate_trace_grid(traces, query_ids, int(report["design"]["repeatsPerQueryPerArm"]))
    qrels = load_evaluator_qrels(query_ids, bundle_manifest)
    expected_scores = {
        mode: score_mode([row for row in traces if row["mode"] == mode], qrels, query_ids)
        for mode in MODES
    }
    expected_ce = prior_ce_evidence()
    expected_broad = prior_broad_evidence()
    expected_decision = decide(expected_scores, prior_ce=expected_ce, prior_broad=expected_broad)
    if report["scores"] != expected_scores:
        raise ValueError("score replay mismatch")
    if report["priorCrossEncoderEvidence"] != expected_ce or report["priorRetrievalEvidence"] != expected_broad:
        raise ValueError("prior evidence replay mismatch")
    if report["decision"] != expected_decision or receipt["decision"] != expected_decision:
        raise ValueError("decision replay mismatch")


async def materialize(
    output_dir: Path,
    *,
    backend_url: str,
    backend_container: str = BACKEND_CONTAINER,
    elasticsearch_url: str = ELASTICSEARCH_URL,
    elasticsearch_container: str = ELASTICSEARCH_CONTAINER,
    repeats: int = REPEATS,
) -> Path:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError(f"refusing to overwrite non-empty run directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    projections, bundle_manifest = load_public_queries()
    query_ids = [row["queryId"] for row in projections]
    catalog_before = await fetch_catalog(backend_url)
    catalog_path = output_dir / "catalog_snapshot.json"
    catalog_path.write_text(canonical(catalog_before) + "\n", encoding="utf-8")
    model = model_manifest()
    model_path = output_dir / "model_manifest.json"
    model_path.write_text(canonical(model) + "\n", encoding="utf-8")
    elasticsearch = await fetch_elasticsearch_snapshot(elasticsearch_url)
    elasticsearch_path = output_dir / "elasticsearch_snapshot.json"
    elasticsearch_path.write_text(canonical(elasticsearch) + "\n", encoding="utf-8")
    backend_channel_before = await fetch_backend_channel_snapshot(backend_url, projections)
    backend_channel_path = output_dir / "backend_channel_snapshot.json"
    backend_channel_path.write_text(canonical(backend_channel_before) + "\n", encoding="utf-8")
    provenance: dict[str, Any] = {
        "schemaVersion": "product-retrieval-runtime-provenance-v1",
        "backend": docker_identity(backend_container),
        "elasticsearch": {
            "container": docker_identity(elasticsearch_container),
            "url": elasticsearch_url.rstrip("/"),
            "snapshotArtifactSha256": sha256(elasticsearch_path),
        },
        "backendChannel": {
            "beforeArtifactSha256": sha256(backend_channel_path),
            "beforeContentSha256": hashlib.sha256(canonical(backend_channel_before).encode("utf-8")).hexdigest(),
            "afterContentSha256": None,
            "drift": None,
        },
        "catalog": {
            "count": len(catalog_before),
            "beforeContentSha256": hashlib.sha256(canonical(catalog_before).encode("utf-8")).hexdigest(),
            "afterContentSha256": None,
            "drift": None,
        },
        "sources": source_manifest(),
        "model": model,
        "packages": package_manifest(),
        "runtimeConfig": {
            "backendUrl": backend_url.rstrip("/"),
            "embeddingModel": settings.rag_embedding_model,
            "embeddingCacheDir": str(Path(settings.rag_model_cache_dir).resolve()),
            "vectorBackend": "local",
            "vectorTimeoutSeconds": settings.product_vector_timeout_seconds,
            "titleRerankerEnabled": False,
            "titleRerankerCandidateLimit": settings.product_title_reranker_candidate_limit,
            "titleRerankerTimeoutSeconds": settings.product_title_reranker_timeout_seconds,
            "syntheticPricePolicy": settings.used_phone_synthetic_price_policy,
            "productCollectionName": settings.product_collection_name,
            "modes": list(MODES),
        },
    }

    original_url = settings.backend_base_url
    settings.backend_base_url = backend_url.rstrip("/")
    try:
        traces = await run_predictions(projections, repeats=repeats)
    finally:
        settings.backend_base_url = original_url

    catalog_after = await fetch_catalog(backend_url)
    backend_channel_after = await fetch_backend_channel_snapshot(backend_url, projections)
    after_hash = hashlib.sha256(canonical(catalog_after).encode("utf-8")).hexdigest()
    provenance["catalog"]["afterContentSha256"] = after_hash
    provenance["catalog"]["drift"] = after_hash != provenance["catalog"]["beforeContentSha256"]
    if provenance["catalog"]["drift"]:
        raise ValueError("catalog changed during matched experiment")
    channel_after_hash = hashlib.sha256(canonical(backend_channel_after).encode("utf-8")).hexdigest()
    provenance["backendChannel"]["afterContentSha256"] = channel_after_hash
    provenance["backendChannel"]["drift"] = (
        channel_after_hash != provenance["backendChannel"]["beforeContentSha256"]
    )
    if provenance["backendChannel"]["drift"]:
        raise ValueError("backend Elasticsearch channel changed during matched experiment")
    provenance_path = output_dir / "provenance.json"
    provenance_path.write_text(canonical(provenance) + "\n", encoding="utf-8")

    qrels = load_evaluator_qrels(query_ids, bundle_manifest)
    scores = {
        mode: score_mode([row for row in traces if row["mode"] == mode], qrels, query_ids)
        for mode in MODES
    }
    ce = prior_ce_evidence()
    broad = prior_broad_evidence()
    decision = decide(scores, prior_ce=ce, prior_broad=broad)
    report = {
        "schemaVersion": "product-retrieval-architecture-experiment-v2",
        "status": decision["status"],
        "experimentIdentity": output_dir.name,
        "design": {
            "singleVariable": "candidate_generation_mode",
            "arms": {
                "bm25": "BM25 plus structured exact-fact channel",
                "hybrid": "BM25 plus local Dense title recall, RRF fusion, and structured exact-fact channel",
            },
            "fixed": [
                "same evaluator-owned five-query public bundle",
                "same hashed 252-product Java catalog snapshot",
                "same authoritative fact resolution",
                "same deterministic hard gate and rule reranker",
                "same output depth 20",
                "title LLM reranker disabled",
            ],
            "repeatsPerQueryPerArm": repeats,
            "warmupPolicy": (
                "BM25 query warmup; production-aligned local-vector precompute with 30-second ceiling; "
                "hybrid readiness query must report active before measured traffic"
            ),
            "modelCalls": 0,
            "tokens": {"prompt": 0, "completion": 0},
        },
        "scores": scores,
        "warmup": [
            {"mode": row["mode"], "ok": row["ok"], "durationMs": row["durationMs"], "vectorChannelStatus": row["vectorChannelStatus"], "failureCode": row["failureCode"]}
            for row in traces if row["phase"] == "warmup"
        ],
        "priorCrossEncoderEvidence": ce,
        "priorRetrievalEvidence": broad,
        "decision": decision,
        "labelBoundary": {
            "qrelsOpenedAfterPredictions": True,
            "rankingProcessReadOnlyPublicBundle": True,
            "sealedRowsAbsentFromRankingInputs": True,
            "unknownOrUnjudged": "zero gain in pooled descriptive metrics only; never stored or interpreted as a negative qrel",
            "metricAuthority": "descriptive_only_not_acceptance_gates",
        },
        "defaultConfigurationAudit": {
            "settingsFileDefault": "bm25",
            "dockerComposeEnvironmentDefault": "rrf_aliases_to_hybrid",
            "usedPhoneDemoExplicitMode": "hybrid_local",
            "conclusion": "defaults are profile-specific; no repository-wide default claim",
        },
        "inputs": {
            "publicQueries": artifact_pin(PUBLIC_QUERY_PATH, rows=len(projections)),
            "evaluatorQrels": artifact_pin(EVALUATOR_QREL_PATH, rows=sum(len(v) for v in qrels.values())),
            "bundleManifestSha256": sha256(BUNDLE_MANIFEST_PATH),
            "provenanceSha256": sha256(provenance_path),
        },
    }
    trace_path = output_dir / "trace.jsonl"
    trace_path.write_text("".join(canonical(row) + "\n" for row in traces), encoding="utf-8")
    report_path = output_dir / "report.json"
    report_path.write_text(canonical(report) + "\n", encoding="utf-8")
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        head = "UNKNOWN"
    receipt = {
        "schemaVersion": "product-retrieval-architecture-receipt-v2",
        "status": report["status"],
        "experimentIdentity": report["experimentIdentity"],
        "gitHead": head,
        "dirtyWorktreeBoundBySourceHashes": True,
        "python": sys.version,
        "platform": platform.platform(),
        "artifacts": {
            "report.json": artifact_pin(report_path),
            "trace.jsonl": artifact_pin(trace_path, rows=len(traces)),
            "catalog_snapshot.json": artifact_pin(catalog_path),
            "provenance.json": artifact_pin(provenance_path),
            "model_manifest.json": artifact_pin(model_path),
            "elasticsearch_snapshot.json": artifact_pin(elasticsearch_path),
            "backend_channel_snapshot.json": artifact_pin(backend_channel_path),
        },
        "decision": decision,
    }
    (output_dir / "receipt.json").write_text(canonical(receipt) + "\n", encoding="utf-8")
    validate_run(output_dir)
    return output_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--backend-url", default="http://127.0.0.1:18082")
    parser.add_argument("--backend-container", default=BACKEND_CONTAINER)
    parser.add_argument("--elasticsearch-url", default=ELASTICSEARCH_URL)
    parser.add_argument("--elasticsearch-container", default=ELASTICSEARCH_CONTAINER)
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--verify-run", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.verify_run is not None:
        validate_run(args.verify_run)
        print(canonical({"verified": str(args.verify_run.resolve()), "status": "PASS"}))
        return 0
    if args.repeats < 2 or args.repeats > 10:
        raise SystemExit("--repeats must be in [2, 10]")
    output = asyncio.run(materialize(
        args.output_dir,
        backend_url=args.backend_url,
        backend_container=args.backend_container,
        elasticsearch_url=args.elasticsearch_url,
        elasticsearch_container=args.elasticsearch_container,
        repeats=args.repeats,
    ))
    report = json.loads((output / "report.json").read_text(encoding="utf-8"))
    print(canonical({"outputDir": str(output.resolve()), "status": report["status"], "decision": report["decision"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
