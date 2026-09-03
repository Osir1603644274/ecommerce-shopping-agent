"""Build and score Track A M0 v2 with paired NDCG@5 and NDCG@10 audits."""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import Any

from agent.scripts.score_track_a_m0_frozen_release import (
    EXPECTED_ARTIFACTS,
    KUAISEARCH_REVISION,
    METRICS,
    _artifact,
    _verify_artifact,
    canonical_sha256,
    evaluate,
    read_jsonl,
    sha256_path,
)


BOOTSTRAP_METRICS = ("NDCG@5", "NDCG@10")
BOOTSTRAP_ITERATIONS = 10000
BOOTSTRAP_SEED = 20260810


def validate_bootstrap_contract(value: dict[str, Any] | None) -> None:
    if set(value or {}) != set(BOOTSTRAP_METRICS):
        raise ValueError("release must freeze NDCG@5 and NDCG@10 paired bootstrap")
    for metric in BOOTSTRAP_METRICS:
        contract = value[metric]
        if contract.get("metric") != metric or contract.get("comparison") != "RRF-vs-BM25":
            raise ValueError(f"bootstrap contract mismatch: {metric}")
        if contract.get("iterations") != BOOTSTRAP_ITERATIONS or contract.get("seed") != BOOTSTRAP_SEED:
            raise ValueError(f"bootstrap configuration mismatch: {metric}")
        interval = contract.get("bootstrap95CI")
        if not isinstance(interval, list) or len(interval) != 2 or not interval[0] < 0 < interval[1]:
            raise ValueError(f"stable-improvement claim boundary violated: {metric}")


def paired_bootstrap(
    a: dict[str, dict[str, float]],
    b: dict[str, dict[str, float]],
    *,
    metric: str,
    iterations: int,
    seed: int,
) -> dict[str, Any]:
    if metric not in BOOTSTRAP_METRICS:
        raise ValueError(f"unsupported paired bootstrap metric: {metric}")
    randomizer = random.Random(seed)
    queries = sorted(a)
    deltas = [a[query][metric] - b[query][metric] for query in queries]
    observed = sum(deltas) / len(deltas)
    samples = [
        sum(deltas[randomizer.randrange(len(deltas))] for _ in deltas) / len(deltas)
        for _ in range(iterations)
    ]
    samples.sort()
    nonzero = [value for value in deltas if value]
    extreme = sum(
        abs(sum(value * (1 if randomizer.random() < 0.5 else -1) for value in nonzero) / len(deltas))
        >= abs(observed)
        for _ in range(iterations)
    )
    return {
        "absoluteDelta": observed,
        "bootstrap95CI": [samples[int(0.025 * iterations)], samples[int(0.975 * iterations) - 1]],
        "pairedSignFlipP": (extreme + 1) / (iterations + 1),
        "iterations": iterations,
        "seed": seed,
    }


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
    report_path = benchmark_dir / EXPECTED_ARTIFACTS["sourceMetrics"][0]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    expected_metrics = {
        system: {metric: report["systems"][system]["overall"][metric] for metric in METRICS}
        for system in ("BM25", "Dense", "RRF")
    }
    source_comparisons = report["pairedComparisons"]["RRF-vs-BM25"]
    for metric in BOOTSTRAP_METRICS:
        if source_comparisons[metric]["iterations"] != BOOTSTRAP_ITERATIONS or source_comparisons[metric]["seed"] != BOOTSTRAP_SEED:
            raise ValueError(f"source bootstrap configuration changed: {metric}")
    release = {
        "schemaVersion": "track-a-m0-frozen-run-release-v2",
        "releaseId": "track-a-m0-frozen-run-v2",
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
        "pairedBootstrap": {
            metric: {
                "comparison": "RRF-vs-BM25",
                "metric": metric,
                "iterations": source_comparisons[metric]["iterations"],
                "seed": source_comparisons[metric]["seed"],
                "absoluteDelta": source_comparisons[metric]["absoluteDelta"],
                "bootstrap95CI": source_comparisons[metric]["bootstrap95CI"],
                "pairedSignFlipP": source_comparisons[metric]["pairedSignFlipP"],
            }
            for metric in BOOTSTRAP_METRICS
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
    validate_bootstrap_contract(release["pairedBootstrap"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(release, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return release


def score_release(repo_root: Path, manifest_path: Path, output: Path | None = None) -> dict[str, Any]:
    release = json.loads(manifest_path.read_text(encoding="utf-8"))
    if release.get("releaseContentSha256") != canonical_sha256(release):
        raise ValueError("release manifest integrity mismatch")
    if release.get("claimPolicy") != "NO_STABLE_IMPROVEMENT_CLAIM":
        raise ValueError("release claim policy changed")
    frozen = release.get("pairedBootstrap")
    validate_bootstrap_contract(frozen)
    paths = {name: _verify_artifact(repo_root, artifact) for name, artifact in release["artifacts"].items()}
    queries = [row["intentGroupId"] for row in read_jsonl(paths["queries"])]
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
    recomputed: dict[str, dict[str, Any]] = {}
    for metric in BOOTSTRAP_METRICS:
        contract = frozen[metric]
        recomputed[metric] = paired_bootstrap(
            per_query["RRF"], per_query["BM25"], metric=metric,
            iterations=contract["iterations"], seed=contract["seed"],
        )
        for key in ("absoluteDelta", "pairedSignFlipP"):
            if not math.isclose(recomputed[metric][key], contract[key], rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"bootstrap mismatch: {metric}/{key}")
        for actual, expected_value in zip(recomputed[metric]["bootstrap95CI"], contract["bootstrap95CI"]):
            if not math.isclose(actual, expected_value, rel_tol=0.0, abs_tol=1e-12):
                raise ValueError(f"bootstrap CI mismatch: {metric}")
    audit = {
        "schemaVersion": "track-a-m0-frozen-run-score-audit-v2",
        "status": "PASS_FROZEN_SCORING_REPRODUCED",
        "releaseManifest": {"path": manifest_path.as_posix(), "sha256": sha256_path(manifest_path)},
        "queryCount": len(queries),
        "qrelRows": len(qrels),
        "metrics": metrics,
        "pairedBootstrap": recomputed,
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
    result = build_release(args.repo_root, args.benchmark_dir, args.output) if args.command == "build" else score_release(args.repo_root, args.manifest, args.output)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
