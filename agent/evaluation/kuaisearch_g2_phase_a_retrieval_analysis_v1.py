"""Score the frozen five-strategy G1 rankings with the adjudicated G2 Phase A pool.

The primary metric is a pooled lower-bound nDCG@3. Unknown and unjudged
candidates receive zero gain but are reported separately; they are never relabeled
as negative qrels. Evaluator-private identity is used only for local joins and is
never emitted in the report.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from agent.evaluation.kuaisearch_g2_phase_a_adjudicated_qrels_v1 import validate_bundle as validate_qrels_bundle

ROOT = Path(__file__).resolve().parents[2]
QRELS_DIR = ROOT / "data/annotations/ecommerce/kuaisearch_multicategory_retrieval_g2_phase_a_adjudicated_20260824_r2"
QRELS_PATH = QRELS_DIR / "adjudicated_qrels.jsonl"
RANKING_DIR = ROOT / "data/derived/ecommerce/kuaisearch_multicategory_retrieval_g1_20260824_r1"
RANKINGS_PATH = RANKING_DIR / "rankings_top100.jsonl"
RANKING_MANIFEST_PATH = RANKING_DIR / "manifest.json"
PRIVATE_POOL_DIR = ROOT / "data/annotations/ecommerce/kuaisearch_multicategory_retrieval_g2_pool_20260824_r1/private"
PROVENANCE_PATH = PRIVATE_POOL_DIR / "provenance.jsonl"
PRIVATE_MANIFEST_PATH = PRIVATE_POOL_DIR / "manifest.json"
DEFAULT_OUTPUT_DIR = ROOT / "data/derived/ecommerce/kuaisearch_g2_phase_a_retrieval_analysis_20260824_r1"

STRATEGIES = ("bm25_fields", "bm25_title", "dense_title", "rrf_bm25_dense", "rrf_bm25_dense_ce")
PRIMARY_K = 3
K_VALUES = (3, 10, 20)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _source(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        display_path = resolved.relative_to(ROOT.resolve()).as_posix()
        path_scope = "REPOSITORY_RELATIVE"
    except ValueError:
        # Temporary test/output bundles may live outside the repository.  Keep
        # their absolute location private and label the basename explicitly so
        # it cannot be mistaken for a repository-relative provenance path.
        display_path = f"external-output/{resolved.name}"
        path_scope = "EXTERNAL_OUTPUT_BASENAME_ONLY"
    result: dict[str, Any] = {
        "path": display_path,
        "pathScope": path_scope,
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }
    if rows is not None:
        result["rows"] = rows
    return result


def _verify_inputs() -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    validate_qrels_bundle(QRELS_DIR)
    qrels = read_jsonl(QRELS_PATH)
    rankings = read_jsonl(RANKINGS_PATH)
    provenance = read_jsonl(PROVENANCE_PATH)
    ranking_manifest = json.loads(RANKING_MANIFEST_PATH.read_text(encoding="utf-8"))
    private_manifest = json.loads(PRIVATE_MANIFEST_PATH.read_text(encoding="utf-8"))

    ranking_pin = ranking_manifest["outputs"]["rankings_top100.jsonl"]
    if ranking_pin != {"bytes": RANKINGS_PATH.stat().st_size, "rows": len(rankings), "sha256": sha256(RANKINGS_PATH)}:
        raise ValueError("ranking artifact does not match its frozen manifest")
    provenance_pin = private_manifest["artifact"]
    if provenance_pin["bytes"] != PROVENANCE_PATH.stat().st_size or provenance_pin["rows"] != len(provenance) or provenance_pin["sha256"] != sha256(PROVENANCE_PATH):
        raise ValueError("private provenance does not match its frozen manifest")
    if private_manifest["inputPins"]["rankings"]["sha256"] != ranking_pin["sha256"]:
        raise ValueError("private provenance is not bound to the frozen rankings")
    if len(qrels) != 124 or len({row["queryId"] for row in qrels}) != 12:
        raise ValueError("unexpected adjudicated qrel coverage")
    return qrels, rankings, provenance, ranking_manifest


def _dcg(grades: Iterable[int]) -> float:
    return sum(((2**grade) - 1) / math.log2(index + 2) for index, grade in enumerate(grades))


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _rounded(value: float) -> float:
    return round(value, 6)


def _bootstrap_pairwise(per_query: dict[str, dict[str, float]], *, samples: int, seed: int) -> list[dict[str, Any]]:
    query_ids = sorted(per_query)
    rng = random.Random(seed)
    draws = [[rng.randrange(len(query_ids)) for _ in query_ids] for _ in range(samples)]
    pairs: list[dict[str, Any]] = []
    for left_index, left in enumerate(STRATEGIES):
        for right in STRATEGIES[left_index + 1 :]:
            deltas = [per_query[qid][left] - per_query[qid][right] for qid in query_ids]
            estimates = sorted(_mean(deltas[index] for index in draw) for draw in draws)
            low = estimates[int(0.025 * (samples - 1))]
            high = estimates[int(0.975 * (samples - 1))]
            pairs.append({
                "left": left,
                "right": right,
                "meanDelta": _rounded(_mean(deltas)),
                "ci95Low": _rounded(low),
                "ci95High": _rounded(high),
                "significant": low > 0 or high < 0,
            })
    return pairs


def analyze(*, bootstrap_samples: int = 10_000, bootstrap_seed: int = 20260824) -> dict[str, Any]:
    qrels, rankings, provenance, ranking_manifest = _verify_inputs()
    query_ids = {row["queryId"] for row in qrels}
    qrel_by_pair = {(row["queryId"], row["blindCandidateId"]): row for row in qrels}
    if len(qrel_by_pair) != len(qrels):
        raise ValueError("duplicate adjudicated qrel identity")
    provenance_by_doc = {
        (row["queryId"], row["docId"]): row["blindCandidateId"]
        for row in provenance
        if row["queryId"] in query_ids
    }
    ranking_by_pair = {
        (row["queryId"], row["strategy"]): row
        for row in rankings
        if row["queryId"] in query_ids
    }
    if len(ranking_by_pair) != len(query_ids) * len(STRATEGIES) or set(ranking_by_pair) != {(qid, strategy) for qid in query_ids for strategy in STRATEGIES}:
        raise ValueError("rankings do not exactly cover the Phase A query/strategy grid")

    ideal_grades = {
        qid: sorted(
            (int(row["relevance"]) for row in qrels if row["queryId"] == qid and row["relevance"] != "U"),
            reverse=True,
        )
        for qid in query_ids
    }
    per_k_strategy: dict[int, dict[str, list[dict[str, float]]]] = {
        k: {strategy: [] for strategy in STRATEGIES} for k in K_VALUES
    }
    primary_per_query: dict[str, dict[str, float]] = {qid: {} for qid in query_ids}

    for qid in sorted(query_ids):
        for strategy in STRATEGIES:
            ranked_docs = ranking_by_pair[(qid, strategy)]["rankedDocIds"]
            for k in K_VALUES:
                grades: list[int] = []
                decided = unknown = unjudged = violations = 0
                for doc_id in ranked_docs[:k]:
                    blind_id = provenance_by_doc.get((qid, doc_id))
                    if blind_id is None:
                        raise ValueError("top-20 ranking document is absent from evaluator-private provenance")
                    qrel = qrel_by_pair.get((qid, blind_id))
                    if qrel is None:
                        grades.append(0)
                        unjudged += 1
                    elif qrel["relevance"] == "U":
                        grades.append(0)
                        unknown += 1
                    else:
                        grade = int(qrel["relevance"])
                        grades.append(grade)
                        decided += 1
                        violations += qrel["hardConstraintConflict"] == "是"
                ideal = _dcg(ideal_grades[qid][:k])
                metrics = {
                    "ndcgLowerBound": _dcg(grades) / ideal if ideal else 0.0,
                    "decidableCoverage": decided / k,
                    "unknownCoverage": unknown / k,
                    "unjudgedCoverage": unjudged / k,
                    "hardConstraintViolationRate": violations / k,
                    "gradeAtLeast2Hit": float(any(grade >= 2 for grade in grades)),
                }
                per_k_strategy[k][strategy].append(metrics)
                if k == PRIMARY_K:
                    primary_per_query[qid][strategy] = metrics["ndcgLowerBound"]

    metrics_by_k: dict[str, Any] = {}
    for k in K_VALUES:
        metrics_by_k[str(k)] = {}
        for strategy in STRATEGIES:
            rows = per_k_strategy[k][strategy]
            metrics_by_k[str(k)][strategy] = {
                metric: _rounded(_mean(row[metric] for row in rows))
                for metric in rows[0]
            }

    pairwise = _bootstrap_pairwise(primary_per_query, samples=bootstrap_samples, seed=bootstrap_seed)
    primary = metrics_by_k[str(PRIMARY_K)]
    descriptive_leader = max(STRATEGIES, key=lambda strategy: primary[strategy]["ndcgLowerBound"])
    process_gates = {
        "exactAgreement": {"value": _rounded(82 / 124), "threshold": 0.8, "passed": False},
        "unknownRate": {"value": _rounded(33 / 124), "thresholdMaximum": 0.1, "passed": False},
        "blindedRepeatConsistency": {"value": 1.0, "threshold": 0.9, "passed": True},
        "unresolvedDisagreementRate": {"value": 0.0, "thresholdMaximum": 0.2, "passed": True},
        "weightedAgreement": {"status": "NOT_COMPUTED_DEFINITION_NOT_FROZEN"},
        "overall": "FAIL_REPAIR_OR_EXPAND",
    }
    return {
        "schemaVersion": "kuaisearch-g2-phase-a-retrieval-analysis-v1",
        "status": "PHASE_A_RETRIEVAL_DIAGNOSTIC_COMPLETE_NO_WINNER",
        "coverageScope": "SECOND_REVIEW_TEST_LANE_ONLY",
        "queryCount": len(query_ids),
        "qrelCount": len(qrels),
        "strategies": list(STRATEGIES),
        "primaryMetric": "POOLED_LOWER_BOUND_NDCG_AT_3",
        "metricPolicy": {
            "gain": "2^grade-1 for grades 0..3",
            "unknown": "zero gain in lower-bound metric; reported separately; not relabeled negative",
            "unjudged": "zero gain in lower-bound metric; reported separately; not a qrel",
            "winnerBoundary": "12-query diagnostic only; process-gate failure forbids winner declaration",
        },
        "metricsByK": metrics_by_k,
        "pairedBootstrapAt3": {"samples": bootstrap_samples, "seed": bootstrap_seed, "comparisons": pairwise},
        "descriptiveLeader": descriptive_leader,
        "processGates": process_gates,
        "decision": "NO_RETRIEVAL_WINNER_PHASE_A_PROCESS_GATES_FAILED",
        "latencyReferenceMs": {
            strategy: {
                "mean": ranking_manifest["latency"]["strategies"][strategy]["meanMs"],
                "p95": ranking_manifest["latency"]["strategies"][strategy]["p95Ms"],
            }
            for strategy in STRATEGIES
        },
        "inputs": {
            "adjudicatedQrels": _source(QRELS_PATH, rows=len(qrels)),
            "rankings": _source(RANKINGS_PATH, rows=len(rankings)),
            "rankingManifest": _source(RANKING_MANIFEST_PATH),
            "privateProvenance": _source(PROVENANCE_PATH, rows=len(provenance)),
            "privateManifest": _source(PRIVATE_MANIFEST_PATH),
        },
        "privacyBoundary": "REPORT_CONTAINS_NO_DOCID_BLIND_CANDIDATE_ID_TITLE_OR_PER_QUERY_PRIVATE_MAPPING",
    }


def _assert_public_report(report: dict[str, Any]) -> None:
    serialized = canonical(report)
    forbidden_keys = {"docId", "blindCandidateId", "title", "ranksByStrategy"}

    def walk(value: object) -> None:
        if isinstance(value, dict):
            if forbidden_keys.intersection(value):
                raise ValueError("report leaks evaluator-private identity")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(report)
    if "ksd-" in serialized:
        raise ValueError("report leaks a private document identity")


def materialize(output_dir: Path = DEFAULT_OUTPUT_DIR) -> Path:
    report = analyze()
    _assert_public_report(report)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "report.json"
    report_path.write_text(canonical(report) + "\n", encoding="utf-8")
    manifest = {
        "schemaVersion": "kuaisearch-g2-phase-a-retrieval-analysis-manifest-v1",
        "status": report["status"],
        "decision": report["decision"],
        "coverageScope": report["coverageScope"],
        "report": _source(report_path),
        "producer": _source(Path(__file__)),
    }
    manifest["canonicalDigest"] = hashlib.sha256(canonical(manifest).encode("utf-8")).hexdigest()
    (output_dir / "manifest.json").write_text(canonical(manifest) + "\n", encoding="utf-8")
    validate_bundle(output_dir)
    return output_dir


def validate_bundle(output_dir: Path = DEFAULT_OUTPUT_DIR) -> None:
    report_path = output_dir / "report.json"
    manifest_path = output_dir / "manifest.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _assert_public_report(report)
    if report != analyze():
        raise ValueError("retrieval analysis replay mismatch")
    if manifest["report"]["sha256"] != sha256(report_path) or manifest["producer"]["sha256"] != sha256(Path(__file__)):
        raise ValueError("retrieval analysis artifact binding mismatch")
    expected = dict(manifest)
    digest = expected.pop("canonicalDigest")
    if digest != hashlib.sha256(canonical(expected).encode("utf-8")).hexdigest():
        raise ValueError("retrieval analysis manifest digest mismatch")


if __name__ == "__main__":
    materialize()
