from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest
import evaluation.kuaisearch_multicategory_retrieval_scorer_v1 as scorer_module

from evaluation.kuaisearch_multicategory_retrieval_scorer_v1 import (
    RANKING_SCHEMA_VERSION,
    RANKING_STRATEGIES,
    ScoringInputError,
    build_blind_pool,
    load_rankings,
    load_source_qrels,
    paired_bootstrap_ci,
    score_rankings,
    _validate_ranking_manifest,
    sha256_file,
    write_blind_pool,
)


def _ranking_rows() -> list[dict]:
    rows = []
    for strategy in RANKING_STRATEGIES:
        for query_id, docs in (("q1", ["d1", "d2"]), ("q2", ["d2", "d1"])):
            rows.append({"schemaVersion": "rank-v1", "queryId": query_id, "strategy": strategy, "rankedDocIds": docs, "queryLatencyMs": 1.0})
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _formal_rows() -> list[dict]:
    rows = []
    qids = [f"q{i:03d}" for i in range(507)]
    for strategy in RANKING_STRATEGIES:
        for query_id in qids:
            docs = [f"{query_id}-d{i:03d}" for i in range(100)]
            rows.append({"schemaVersion": RANKING_SCHEMA_VERSION, "queryId": query_id, "strategy": strategy, "rankedDocIds": docs, "queryLatencyMs": 1.0})
    return rows


def test_canonical_rankings_allow_optional_legacy_split_but_reject_duplicates(tmp_path: Path):
    path = tmp_path / "rankings_top100.jsonl"
    rows = _ranking_rows()
    rows[0]["split"] = "train"
    _write_jsonl(path, rows)
    assert len(load_rankings(path, strict=False)) == 10
    rows.append(dict(rows[0]))
    _write_jsonl(path, rows)
    with pytest.raises(ScoringInputError):
        load_rankings(path, strict=False)


def test_formal_ranking_contract_rejects_wrong_schema_short_rows_and_ce_drift(tmp_path: Path):
    rows = _formal_rows()
    path = tmp_path / "formal.jsonl"
    _write_jsonl(path, rows)
    assert len(load_rankings(path)) == 2535
    rows[0]["schemaVersion"] = "wrong-v1"
    _write_jsonl(path, rows)
    with pytest.raises(ScoringInputError):
        load_rankings(path)
    rows = _formal_rows()
    rows[0]["rankedDocIds"] = rows[0]["rankedDocIds"][:-1]
    _write_jsonl(path, rows)
    with pytest.raises(ScoringInputError):
        load_rankings(path)
    rows = _formal_rows()
    rrf = next(row for row in rows if row["strategy"] == "rrf_bm25_dense" and row["queryId"] == "q000")
    ce = next(row for row in rows if row["strategy"] == "rrf_bm25_dense_ce" and row["queryId"] == "q000")
    ce["rankedDocIds"][20] = "expanded-candidate"
    _write_jsonl(path, rows)
    with pytest.raises(ScoringInputError):
        load_rankings(path)
    ce["rankedDocIds"][20] = rrf["rankedDocIds"][20]
    ce["rankedDocIds"][0] = "expanded-candidate"
    _write_jsonl(path, rows)
    with pytest.raises(ScoringInputError):
        load_rankings(path)


def test_ranking_manifest_is_fail_closed_and_binds_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    rankings_path = tmp_path / "formal.jsonl"
    _write_jsonl(rankings_path, _formal_rows())
    g0_dir = tmp_path / "g0"
    g0_dir.mkdir()
    g0_queries = g0_dir / "queries.jsonl"
    g0_documents = g0_dir / "documents.jsonl"
    g0_queries.write_text("queries", encoding="utf-8")
    g0_documents.write_text("documents", encoding="utf-8")
    dense_root, cross_root, deps_root = g0_dir / "dense", g0_dir / "cross", g0_dir / "deps"
    dense_files = [{"path": "dense.bin", "sha256": "1" * 64, "bytes": 1}]
    cross_files = [{"path": "cross.bin", "sha256": "2" * 64, "bytes": 1}]
    deps_files = [{"path": "deps.bin", "sha256": "3" * 64, "bytes": 1}]
    g0_models = {"dense": {"root": str(dense_root), "files": dense_files, "fileCount": 1, "snapshotSha256": "4" * 64}, "crossEncoder": {"root": str(cross_root), "files": cross_files, "fileCount": 1, "snapshotSha256": "5" * 64}, "crossEncoderDependencies": {"root": str(deps_root), "files": deps_files, "fileCount": 1, "snapshotSha256": "6" * 64}}
    systems = {"bm25": {"fieldWeights": {"title": 1.0, "attributeText": 0.45, "brand": 0.25}}, "dense": {"model": "BAAI/bge-small-zh-v1.5", "input": "title"}, "rrf": {"k": 60}, "crossEncoder": {"model": "BAAI/bge-reranker-v2-m3", "revision": "rev", "candidateLimit": 20, "reordersOnly": True}}
    g0_prereg = {"systems": systems, "indexIdentity": "index", "topK": 100, "seed": 0, "environment": {"packages": {"fastembed": "1", "onnxruntime": "2"}, "hardware": {}}, "memoryMeasurement": {"unit": "process RSS sampled", "isTruePeak": False, "points": ["before_build"]}}
    g0_inputs = {"documents": {"sha256": "7" * 64}, "breadthManifest": {"sha256": "8" * 64}}
    (g0_dir / "models.manifest.json").write_text(json.dumps(g0_models), encoding="utf-8")
    (g0_dir / "preregistration.json").write_text(json.dumps(g0_prereg), encoding="utf-8")
    (g0_dir / "inputs.manifest.json").write_text(json.dumps(g0_inputs), encoding="utf-8")
    binding = {"g0Dir": str(g0_dir.resolve()), "manifestSha256": "a" * 64, "inputsManifestSha256": "b" * 64, "modelsManifestSha256": "c" * 64, "preregistrationSha256": "d" * 64, "codePins": {name: {"sha256": "e" * 64, "bytes": 1} for name in ("run_kuaisearch_multicategory_retrieval_baseline_v1.py", "kuaisearch_multicategory_retrieval_baseline_v1.py", "a.py", "b.py", "c.py", "d.py")}}
    monkeypatch.setattr(scorer_module, "_validate_g0_binding", lambda _path, _binding: {"g0Binding": binding})
    manifest = {
        "schemaVersion": RANKING_SCHEMA_VERSION,
        "outputs": {"rankings_top100.jsonl": {"rows": 2535, "bytes": rankings_path.stat().st_size, "sha256": sha256_file(rankings_path)}},
        "inputs": {"queries": {"path": str(g0_queries), "rows": 507, "sha256": sha256_file(g0_queries)}, "documents": {"path": str(g0_documents), "rows": 46079, "sha256": sha256_file(g0_documents)}},
        "models": {"dense": {"name": "BAAI/bge-small-zh-v1.5", "cache": {"path": str(dense_root), "files": dense_files}}, "crossEncoder": {"name": "BAAI/bge-reranker-v2-m3", "revision": "rev", "cache": {"path": str(cross_root), "files": cross_files}, "dependenciesPath": str(deps_root), "dependenciesSnapshot": {"root": str(deps_root), "fileCount": 1, "snapshotSha256": "6" * 64}}},
        "indexIdentity": "index",
        "contract": {"topK": 100, "rrfK": 60, "bm25Fields": {"title": 1.0, "attributeText": 0.45, "brand": 0.25}, "denseTextField": "title", "crossEncoderCandidateLimit": 20, "crossEncoderDoesNotExpandCandidates": True, "randomSeed": 0},
        "packageVersions": {"fastembed": "1", "onnxruntime": "2"}, "hardware": {}, "codeSha256": {str((g0_dir / "runner" / "run_kuaisearch_multicategory_retrieval_baseline_v1.py").resolve()): "e" * 64, str((g0_dir / "baseline" / "kuaisearch_multicategory_retrieval_baseline_v1.py").resolve()): "e" * 64}, "memory": {"unit": "process RSS sampled", "isTruePeak": False, "samplingPoints": ["before_build"]}, "datasetPins": {"documentsSha256": "7" * 64, "breadthManifestSha256": "8" * 64}, "buildMs": {"bm25": 1.0, "dense": 2.0, "crossEncoderLoad": 3.0},
        "latency": {"coldBuildMs": 6.0, "components": {}, "strategies": {}, "crossEncoderRepeatVerification": {"repeatVerifiedQueries": 507, "repeatMismatchCount": 0, "repeatExtraLatencyMs": {}}},
        "g0Binding": binding,
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _validate_ranking_manifest(manifest_path, rankings_path, g0_dir=g0_dir)["schemaVersion"] == RANKING_SCHEMA_VERSION
    for section, key, bad in (("models", "revision", "bad-revision"), ("models", "snapshotSha256", "bad-deps"), ("contract", "randomSeed", 9)):
        if section == "models":
            if key == "revision":
                original = manifest["models"]["crossEncoder"][key]
                manifest["models"]["crossEncoder"][key] = bad
            else:
                original = manifest["models"]["crossEncoder"]["dependenciesSnapshot"][key]
                manifest["models"]["crossEncoder"]["dependenciesSnapshot"][key] = bad
        else:
            original = manifest[section][key]
            manifest[section][key] = bad
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ScoringInputError):
            _validate_ranking_manifest(manifest_path, rankings_path, g0_dir=g0_dir)
        if section == "models":
            if key == "revision":
                manifest["models"]["crossEncoder"][key] = original
            else:
                manifest["models"]["crossEncoder"]["dependenciesSnapshot"][key] = original
        else:
            manifest[section][key] = original
    original_index = manifest["indexIdentity"]
    manifest["indexIdentity"] = "bad-index"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ScoringInputError):
        _validate_ranking_manifest(manifest_path, rankings_path, g0_dir=g0_dir)
    manifest["indexIdentity"] = original_index
    original_weight = manifest["contract"]["bm25Fields"]["title"]
    manifest["contract"]["bm25Fields"]["title"] = 9.0
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ScoringInputError):
        _validate_ranking_manifest(manifest_path, rankings_path, g0_dir=g0_dir)
    manifest["contract"]["bm25Fields"]["title"] = original_weight
    for section, key, bad in (("packageVersions", "extra", "drift"), ("hardware", "cpu", "drift"), ("memory", "unit", "wrong"), ("datasetPins", "documentsSha256", "bad"), ("codeSha256", str((g0_dir / "baseline" / "kuaisearch_multicategory_retrieval_baseline_v1.py").resolve()), "bad"), ("buildMs", "bm25", -1.0), ("latency", "coldBuildMs", 7.0)):
        original = manifest[section].get(key)
        manifest[section][key] = bad
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ScoringInputError):
            _validate_ranking_manifest(manifest_path, rankings_path, g0_dir=g0_dir)
        if original is None:
            manifest[section].pop(key)
        else:
            manifest[section][key] = original
    manifest.pop("latency")
    manifest["repeatVerification"] = {"queries": 507, "mismatchCount": 0}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ScoringInputError):
        _validate_ranking_manifest(manifest_path, rankings_path, g0_dir=g0_dir)
    manifest.pop("repeatVerification")
    manifest["outputs"]["rankings_top100.jsonl"]["bytes"] += 1
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ScoringInputError):
        _validate_ranking_manifest(manifest_path, rankings_path, g0_dir=g0_dir)
def test_score_reports_sparse_grade_bands_and_human_pair_subset(tmp_path: Path):
    ranking_path = tmp_path / "rankings.jsonl"
    _write_jsonl(ranking_path, _ranking_rows())
    qrel_path = tmp_path / "qrels.jsonl"
    _write_jsonl(qrel_path, [
        {"queryId": "q1", "docId": "d1", "sourceRelevance": 2, "split": "train", "categoryKey": "c1", "labelScope": "SPARSE_SOURCE_LABEL_NOT_EXHAUSTIVE_GOLD"},
        {"queryId": "q2", "docId": "d1", "sourceRelevance": 0, "split": "test", "categoryKey": "c2", "labelScope": "SPARSE_SOURCE_LABEL_NOT_EXHAUSTIVE_GOLD"},
    ])
    category_path = tmp_path / "categories.jsonl"
    _write_jsonl(category_path, [{"categoryKey": "c1"}, {"categoryKey": "c2"}])
    labels_path = tmp_path / "labels.jsonl"
    _write_jsonl(labels_path, [{"sampleKind": "QUERY_LABEL", "queryId": "q1", "docId": "d1", "sourceRelevance": 2}])
    report = score_rankings(rankings_path=ranking_path, source_qrels_path=qrel_path, categories_path=category_path, human_sample_labels_path=labels_path, bootstrap_samples=20, strict_rankings=False)
    assert report["counts"] == {"queries": 2, "gradePositive": 1, "gradeAtLeast2": 1, "gradeZero": 1, "humanReviewedQueryPairs": 1, "categories": 2}
    metrics = report["strategies"]["bm25_fields"]["overall"]
    assert metrics["gradePositive"]["hitAt1"]["hits"] == 1
    assert metrics["gradeZeroKnownNegativeExposure"]["at1"]["exposed"] == 0
    assert len(report["pairedBootstrapCI"]["gradePositive"]) == 10 * 7


def test_same_query_id_replacement_label_is_rejected(tmp_path: Path):
    path = tmp_path / "qrels.jsonl"
    rows = [
        {"queryId": "q1", "docId": "d1", "sourceRelevance": 1, "split": "train", "categoryKey": "c1", "labelScope": "SPARSE_SOURCE_LABEL_NOT_EXHAUSTIVE_GOLD"},
        {"queryId": "q1", "docId": "d2", "sourceRelevance": 3, "split": "train", "categoryKey": "c1", "labelScope": "SPARSE_SOURCE_LABEL_NOT_EXHAUSTIVE_GOLD"},
    ]
    _write_jsonl(path, rows)
    with pytest.raises(ScoringInputError):
        load_source_qrels(path)


def test_blind_pool_physically_separates_public_and_private_provenance(tmp_path: Path):
    qrels = {"q1": {"docId": "d-source"}, "q2": {"docId": "d-other"}}
    queries = {"q1": {"query": "红鞋"}, "q2": {"query": "蓝鞋"}}
    documents = {doc: {"title": doc, "attr_value": "属性", "brand": "品牌", "seller_name": "商家"} for doc in ("d1", "d2", "d-source", "d-other")}
    secret = b"a" * 32
    pool = build_blind_pool(_ranking_rows(), qrels, blind_secret=secret, depth=1, queries=queries, documents=documents)
    assert pool["publicReviewRows"] and pool["privateProvenance"]
    assert all("docId" not in row and "strategies" not in row and "blindContributionToken" not in row for row in pool["publicReviewRows"])
    assert all(set(row) == {"schemaVersion", "queryId", "query", "blindCandidateId", "poolPosition", "title", "attr_value", "brand", "seller_name"} for row in pool["publicReviewRows"])
    assert all(not any(token in row for token in ("bm25", "dense", "sourcePair", "sourceRelevance", "grade", "category", "categoryKey")) for row in pool["publicReviewRows"])
    assert any(row["sourcePairIncluded"] for row in pool["privateProvenance"])
    output = tmp_path / "pool"
    artifacts = write_blind_pool(output, pool, salt_sha256=hashlib.sha256(secret).hexdigest())
    assert (output / "public" / "review_rows.jsonl").exists()
    assert (output / "private" / "provenance.jsonl").exists()
    assert artifacts["public"]["rows"] == artifacts["private"]["rows"]
    with pytest.raises(FileExistsError):
        write_blind_pool(output, pool, salt_sha256=hashlib.sha256(secret).hexdigest())


def test_blind_order_uses_secret_hmac_not_strategy_rank_and_changes_with_secret():
    qrels = {"q1": {"docId": "d1"}, "q2": {"docId": "d2"}}
    queries = {"q1": {"query": "红鞋"}, "q2": {"query": "蓝鞋"}}
    documents = {doc: {"title": doc, "attr_value": "属性", "brand": "品牌", "seller_name": "商家"} for doc in ("d1", "d2", "d3", "d4")}
    rows = _ranking_rows()
    first = build_blind_pool(rows, qrels, blind_secret=b"a" * 32, depth=2, queries=queries, documents=documents)
    shuffled = [dict(row, rankedDocIds=list(reversed(row["rankedDocIds"]))) for row in rows]
    second = build_blind_pool(shuffled, qrels, blind_secret=b"a" * 32, depth=2, queries=queries, documents=documents)
    assert [(row["queryId"], row["blindCandidateId"]) for row in first["publicReviewRows"]] == [(row["queryId"], row["blindCandidateId"]) for row in second["publicReviewRows"]]
    third = build_blind_pool(rows, qrels, blind_secret=b"b" * 32, depth=2, queries=queries, documents=documents)
    assert [row["blindCandidateId"] for row in first["publicReviewRows"]] != [row["blindCandidateId"] for row in third["publicReviewRows"]]
    public_text = json.dumps(first["publicReviewRows"])
    assert all(token not in public_text for token in ("bm25_fields", "bm25_title", "dense_title", "rrf_bm25_dense", "docId", "sourcePairIncluded", "ranksByStrategy", "saltSha256"))


def test_paired_bootstrap_is_deterministic_and_uses_only_allowed_metrics():
    records = [{"queryId": "q1", "docId": "d1", "grade": 2}, {"queryId": "q2", "docId": "d2", "grade": 2}]
    rankings = {"a": {"q1": ["d1"], "q2": ["x"]}, "b": {"q1": ["x"], "q2": ["d2"]}}
    first = paired_bootstrap_ci(records, rankings, samples=25, seed=7)
    assert first == paired_bootstrap_ci(records, rankings, samples=25, seed=7)
    assert {row["metric"] for row in first} == {"hitAt1", "hitAt5", "hitAt10", "hitAt20", "hitAt50", "hitAt100", "mrr"}
