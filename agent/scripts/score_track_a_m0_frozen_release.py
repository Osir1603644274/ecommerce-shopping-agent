"""Build and independently score the minimal Track A M0 frozen-run release."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import defaultdict
from pathlib import Path
from typing import Any


KUAISEARCH_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
EXPECTED_ARTIFACTS = {
    "queries": ("final_queries_v1.jsonl", 60, "122326c19a82dafa97e692d31397d99d2a4d01b5dc5f2bc7f56d8e54fff53525"),
    "qrels": ("track_a_full_catalog_pooled_qrels_v2.jsonl", 5609, "795c1f9c59acc19aa650841c9bc58993aa772a6f48e1ca09333dfa17d37715e9"),
    "BM25": ("track_a_full_catalog_bm25_top50_run_v1.jsonl", 3000, "f27a03d65873466eb58bb2a8b508754017a097abeff1091e20be560e8be94e28"),
    "Dense": ("track_a_full_catalog_dense_top50_run_v1.jsonl", 3000, "ddc4ce82d04b9aaf6139ffdc1b2879b45cc63c64773b9c3cc76fa722aa96d03a"),
    "RRF": ("track_a_full_catalog_rrf_top50_run_v1.jsonl", 3000, "099958713fac5af79b8f8754352729db9d3842dff4b11ec4e6a229ebadbaa29f"),
    "sourceMetrics": ("track_a_full_catalog_pooled_metrics_report_v2.json", None, "55131eef33e22f390eb606386a4975d24c8f157bc85e37df1921f3af3a289671"),
    "acceptanceManifest": ("track_a_full_catalog_acceptance_manifest_v2.json", None, "16ea1d3c9579dcb6d4189d33b7c15bd9740939b321710283d862f0670754e081"),
}
METRICS = ("NDCG@5", "NDCG@10", "MRR", "Top-1", "Top-3", "Recall@20", "Recall@50", "Hit@20")


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def canonical_sha256(value: dict[str, Any]) -> str:
    payload = {key: item for key, item in value.items() if key != "releaseContentSha256"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _relative(path: Path, repo_root: Path) -> str:
    return path.resolve().relative_to(repo_root.resolve()).as_posix()


def _artifact(path: Path, repo_root: Path, rows: int | None) -> dict[str, Any]:
    value: dict[str, Any] = {"path": _relative(path, repo_root), "sha256": sha256_path(path)}
    if rows is not None:
        value["rows"] = rows
    return value


def build_release(repo_root: Path, benchmark_dir: Path, output: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for name, (filename, rows, expected_sha) in EXPECTED_ARTIFACTS.items():
        path = benchmark_dir / filename
        if not path.is_file() or sha256_path(path) != expected_sha:
            raise ValueError(f"frozen {name} artifact missing or hash changed")
        if rows is not None and len(read_jsonl(path)) != rows:
            raise ValueError(f"frozen {name} row count changed")
        artifacts[name] = _artifact(path, repo_root, rows)
    artifacts["scorer"] = _artifact(Path(__file__), repo_root, None)
    report = json.loads((benchmark_dir / EXPECTED_ARTIFACTS["sourceMetrics"][0]).read_text(encoding="utf-8"))
    expected_metrics = {name: {metric: report["systems"][name]["overall"][metric] for metric in METRICS}
                        for name in ("BM25", "Dense", "RRF")}
    comparison = report["pairedComparisons"]["RRF-vs-BM25"]["NDCG@5"]
    release = {
        "schemaVersion": "track-a-m0-frozen-run-release-v1",
        "releaseId": "track-a-m0-frozen-run-v1",
        "status": "FROZEN_SCORING_RELEASE",
        "dataset": {"id": "benchen4395/KuaiSearch", "revision": KUAISEARCH_REVISION},
        "artifacts": artifacts,
        "scoringContract": {
            "queryDenominator": 60,
            "gain": "2^grade-1",
            "discount": "log2(rank+1)",
            "mrrRelevantThreshold": "grade>0",
            "recallAndHitRelevantThreshold": "grade>=2",
            "missingQueryPolicy": "score as empty run and zero",
            "unjudgedRunDocumentPolicy": "fail; every frozen run document must be in pooled qrel",
            "expectedOverallMetrics": expected_metrics,
        },
        "bootstrap": {
            "comparison": "RRF-vs-BM25",
            "metric": "NDCG@5",
            "iterations": comparison["iterations"],
            "seed": comparison["seed"],
            "absoluteDelta": comparison["absoluteDelta"],
            "bootstrap95CI": comparison["bootstrap95CI"],
            "pairedSignFlipP": comparison["pairedSignFlipP"],
        },
        "truthBoundary": {
            "labelType": "AI-only observable catalog relevance",
            "judgedUniverse": "deterministic union of BM25, BGE Dense, and fixed RRF Top-50 runs",
            "poolPairCount": 5609,
            "outsidePool": "unknown, never implicitly grade 0",
            "recallMeaning": "pooled Recall within the judged three-run Top-50 union, not exhaustive catalog Recall",
            "notHumanGold": True,
            "notExhaustiveRecall": True,
        },
        "reproducibilityBoundary": {
            "scoringReproducible": True,
            "endToEndRetrievalReproducibleByThisReleaseAlone": False,
            "explanation": "This M0 release freezes queries, qrels, runs, and scoring; it does not package the corpus, model weights, indexes, or retrieval runtime.",
        },
        "claimPolicy": "NO_STABLE_IMPROVEMENT_CLAIM",
    }
    release["releaseContentSha256"] = canonical_sha256(release)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(release, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return release


def query_metrics(ranked: list[str], grades: dict[str, int]) -> dict[str, float]:
    ideal = sorted(grades.values(), reverse=True)

    def dcg(values: list[int]) -> float:
        return sum((2 ** grade - 1) / math.log2(index + 2) for index, grade in enumerate(values))

    def ndcg(k: int) -> float:
        denominator = dcg(ideal[:k])
        return dcg([grades.get(doc_id, 0) for doc_id in ranked[:k]]) / denominator if denominator else 0.0

    positive = {doc_id for doc_id, grade in grades.items() if grade > 0}
    relevant = {doc_id for doc_id, grade in grades.items() if grade >= 2}
    best = max(grades.values()) if grades else 0
    first = next((index + 1 for index, doc_id in enumerate(ranked) if doc_id in positive), None)
    return {
        "NDCG@5": ndcg(5),
        "NDCG@10": ndcg(10),
        "MRR": 1 / first if first else 0.0,
        "Top-1": float(any(grades.get(doc_id, -1) == best for doc_id in ranked[:1])),
        "Top-3": float(any(grades.get(doc_id, -1) == best for doc_id in ranked[:3])),
        "Recall@20": len(relevant & set(ranked[:20])) / len(relevant) if relevant else 0.0,
        "Recall@50": len(relevant & set(ranked[:50])) / len(relevant) if relevant else 0.0,
        "Hit@20": float(bool(relevant & set(ranked[:20]))),
    }


def evaluate(queries: list[str], qrels: list[dict[str, Any]], run: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    grades: dict[str, dict[str, int]] = defaultdict(dict)
    ranked: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in qrels:
        if row["docId"] in grades[row["queryId"]]:
            raise ValueError("duplicate qrel pair")
        grades[row["queryId"]][row["docId"]] = row["grade"]
    for row in run:
        ranked[row["queryId"]].append(row)
    per_query = {}
    for query_id in queries:
        rows = sorted(ranked.get(query_id, []), key=lambda item: item["rank"])
        if rows:
            if [item["rank"] for item in rows] != list(range(1, len(rows) + 1)):
                raise ValueError(f"non-contiguous rank for {query_id}")
            doc_ids = [item["docId"] for item in rows]
            if len(doc_ids) != len(set(doc_ids)):
                raise ValueError(f"duplicate run document for {query_id}")
            if any(doc_id not in grades[query_id] for doc_id in doc_ids):
                raise ValueError(f"unjudged run document for {query_id}")
        per_query[query_id] = query_metrics([item["docId"] for item in rows], grades[query_id])
    overall = {metric: sum(per_query[q][metric] for q in queries) / len(queries) for metric in METRICS}
    return overall, per_query


def paired_bootstrap(a: dict[str, dict[str, float]], b: dict[str, dict[str, float]], *, iterations: int, seed: int) -> dict[str, Any]:
    randomizer = random.Random(seed)
    queries = sorted(a)
    deltas = [a[q]["NDCG@5"] - b[q]["NDCG@5"] for q in queries]
    observed = sum(deltas) / len(deltas)
    samples = [sum(deltas[randomizer.randrange(len(deltas))] for _ in deltas) / len(deltas)
               for _ in range(iterations)]
    samples.sort()
    nonzero = [value for value in deltas if value]
    extreme = sum(
        abs(sum(value * (1 if randomizer.random() < 0.5 else -1) for value in nonzero) / len(deltas)) >= abs(observed)
        for _ in range(iterations)
    )
    return {
        "absoluteDelta": observed,
        "bootstrap95CI": [samples[int(0.025 * iterations)], samples[int(0.975 * iterations) - 1]],
        "pairedSignFlipP": (extreme + 1) / (iterations + 1),
        "iterations": iterations,
        "seed": seed,
    }


def _verify_artifact(repo_root: Path, artifact: dict[str, Any]) -> Path:
    path = repo_root / artifact["path"]
    if not path.is_file() or sha256_path(path) != artifact["sha256"]:
        raise ValueError(f"artifact hash mismatch: {artifact['path']}")
    if "rows" in artifact and len(read_jsonl(path)) != artifact["rows"]:
        raise ValueError(f"artifact row count mismatch: {artifact['path']}")
    return path


def score_release(repo_root: Path, manifest_path: Path, output: Path | None = None) -> dict[str, Any]:
    release = json.loads(manifest_path.read_text(encoding="utf-8"))
    if release.get("releaseContentSha256") != canonical_sha256(release):
        raise ValueError("release manifest integrity mismatch")
    if release.get("claimPolicy") != "NO_STABLE_IMPROVEMENT_CLAIM":
        raise ValueError("release claim policy changed")
    paths = {name: _verify_artifact(repo_root, artifact) for name, artifact in release["artifacts"].items()}
    query_rows = read_jsonl(paths["queries"])
    queries = [row["intentGroupId"] for row in query_rows]
    if len(queries) != 60 or len(set(queries)) != 60:
        raise ValueError("query contract mismatch")
    qrels = read_jsonl(paths["qrels"])
    expected = release["scoringContract"]["expectedOverallMetrics"]
    metrics: dict[str, dict[str, float]] = {}
    per_query: dict[str, dict[str, dict[str, float]]] = {}
    for system in ("BM25", "Dense", "RRF"):
        metrics[system], per_query[system] = evaluate(queries, qrels, read_jsonl(paths[system]))
        for metric in METRICS:
            if not math.isclose(metrics[system][metric], expected[system][metric], rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"metric mismatch: {system}/{metric}")
    bootstrap = paired_bootstrap(per_query["RRF"], per_query["BM25"],
                                 iterations=release["bootstrap"]["iterations"], seed=release["bootstrap"]["seed"])
    for key in ("absoluteDelta", "pairedSignFlipP"):
        if not math.isclose(bootstrap[key], release["bootstrap"][key], rel_tol=0.0, abs_tol=1e-12):
            raise ValueError(f"bootstrap mismatch: {key}")
    for actual, frozen in zip(bootstrap["bootstrap95CI"], release["bootstrap"]["bootstrap95CI"]):
        if not math.isclose(actual, frozen, rel_tol=0.0, abs_tol=1e-12):
            raise ValueError("bootstrap CI mismatch")
    audit = {
        "schemaVersion": "track-a-m0-frozen-run-score-audit-v1",
        "status": "PASS_FROZEN_SCORING_REPRODUCED",
        "releaseManifest": {"path": manifest_path.as_posix(), "sha256": sha256_path(manifest_path)},
        "queryCount": len(queries),
        "qrelRows": len(qrels),
        "metrics": metrics,
        "bootstrap": bootstrap,
        "truthBoundary": release["truthBoundary"],
        "claimPolicy": release["claimPolicy"],
    }
    if output:
        output.write_text(json.dumps(audit, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--repo-root", type=Path, default=Path("."))
    build.add_argument("--benchmark-dir", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    score = subparsers.add_parser("score")
    score.add_argument("--repo-root", type=Path, default=Path("."))
    score.add_argument("--manifest", type=Path, required=True)
    score.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = (build_release(args.repo_root, args.benchmark_dir, args.output) if args.command == "build"
              else score_release(args.repo_root, args.manifest, args.output))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
