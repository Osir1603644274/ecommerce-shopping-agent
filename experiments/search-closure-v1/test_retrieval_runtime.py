"""Synthetic CPU/SQLite tests; no historical queries, labels or GPU are read."""
from pathlib import Path
import sqlite3

import numpy as np
import pytest

from retrieval_runtime import (
    WEIGHTS, fingerprint, lexical_query, merge_block_topk, readonly,
    rerank_same_candidates, require_test_authorization, stable_topk, weighted_rrf, write_once,
)
from run_dev_grid import make_pools, ranking_row, save_cached, verify_cached


def channels():
    ids = {"bm25": ["kuaisearch:1", "kuaisearch:2", "kuaisearch:3"],
           "character": ["kuaisearch:2", "kuaisearch:3", "kuaisearch:1"],
           "dense": ["kuaisearch:3", "kuaisearch:2", "kuaisearch:1"]}
    return {name: [{"document_id": did, "rank": rank, "score": 1 / rank} for rank, did in enumerate(values, 1)]
            for name, values in ids.items()}


def test_block_merge_matches_full_cosine_and_numeric_ties():
    vectors = np.array([[1, 0], [0, 1], [1, 1], [-1, 0], [1, 0]], dtype=np.float16)
    query = np.array([1, 0], dtype=np.float32)
    decoded = vectors.astype(np.float32); decoded /= np.linalg.norm(decoded, axis=1, keepdims=True)
    scores = decoded @ query
    expected = stable_topk(scores, np.arange(10, 15), 3)
    best_scores, best_ids = np.empty(0, np.float32), np.empty(0, np.int64)
    for start in range(0, len(vectors), 2):
        best_scores, best_ids = merge_block_topk(best_scores, best_ids, scores[start:start + 2], np.arange(start + 10, min(start + 12, 15)), 3)
    assert list(best_ids) == [10, 14, 12]
    np.testing.assert_array_equal(best_ids, expected[1]); np.testing.assert_array_equal(best_scores, expected[0])


def test_rrf_profiles_use_declared_weights_and_same_candidate_ce():
    original = channels()
    pools, union = make_pools(original)
    assert set(pools) == set(WEIGHTS) and len(union) == 3
    expected = 2 / 61 + 1 / 63 + 1 / 63
    result = weighted_rrf(original, (2, 1, 1))
    assert next(row["score"] for row in result if row["document_id"] == "kuaisearch:1") == pytest.approx(expected)
    logits = {"kuaisearch:1": -4, "kuaisearch:2": -1, "kuaisearch:3": -1}
    reranked = rerank_same_candidates(result, logits)
    assert [row["document_id"] for row in reranked] == ["kuaisearch:2", "kuaisearch:3", "kuaisearch:1"]
    assert {row["document_id"] for row in reranked} == set(union)


def test_no_dense_does_not_import_dense_only_candidates():
    data = channels(); data["dense"] = [{"document_id": "kuaisearch:999", "rank": 1, "score": 1}]
    assert "kuaisearch:999" not in {row["document_id"] for row in weighted_rrf(data, (1, 1, 0))}


@pytest.mark.parametrize("kind", ["duplicate", "rank", "nan", "missing"])
def test_bad_rankings_or_missing_ce_scores_fail(kind):
    data = channels()
    with pytest.raises(ValueError):
        if kind == "duplicate":
            data["bm25"][1]["document_id"] = data["bm25"][0]["document_id"]; weighted_rrf(data, (1, 1, 1))
        elif kind == "rank":
            data["dense"][0]["rank"] = 4; weighted_rrf(data, (1, 1, 1))
        elif kind == "nan": stable_topk([float("nan")], [1], 1)
        else: rerank_same_candidates(data["bm25"], {"kuaisearch:1": 1.0})


def test_lexical_sql_fixture_padding_and_source_identity(tmp_path):
    cp, ip = tmp_path / "catalog.sqlite", tmp_path / "lexical.sqlite"
    db = sqlite3.connect(cp)
    db.execute("CREATE TABLE documents (docid TEXT,source TEXT,text TEXT)")
    db.executemany("INSERT INTO documents VALUES(?,?,?)", [("kuaisearch:1", "kuaisearch", "alpha"),
                                                          ("kuaisearch:2", "kuaisearch", "beta"),
                                                          ("kuaisearch:3", "kuaisearch", "gamma"),
                                                          ("multicpr:1", "multicpr", "alpha")])
    db.commit(); db.close()
    db = sqlite3.connect(ip)
    for table in ("words", "chars"):
        db.execute(f"CREATE VIRTUAL TABLE {table} USING fts5(text)")
        db.executemany(f"INSERT INTO {table}(rowid,text) VALUES(?,?)", [(1, "alpha al lp ph ha"), (2, "beta"), (3, "gamma")])
    db.commit(); db.close()
    catalog, index = readonly(cp), readonly(ip)
    result, timing = lexical_query(index, catalog, "kuaisearch", "alpha", depth=3)
    assert [row["document_id"] for row in result["bm25"]] == ["kuaisearch:1", "kuaisearch:2", "kuaisearch:3"]
    assert timing["bm25"]["matched"] == 1 and timing["bm25"]["zero_score_padding"] == 2
    assert result["bm25"][1]["score"] == 0
    with pytest.raises(sqlite3.OperationalError): catalog.execute("DELETE FROM documents")
    with pytest.raises(ValueError): lexical_query(index, catalog, "multicpr", "alpha", depth=1)
    catalog.close(); index.close()


def test_frozen_resume_binds_input_model_and_detects_output_edit(tmp_path):
    path = tmp_path / "score.json"
    binding = {"input_sha256": "one", "model_sha256": "model"}
    save_cached(path, binding, {"score": -2.0})
    assert verify_cached(path, binding) == {"score": -2.0}
    before = path.read_bytes(); save_cached(path, binding, {"score": -2.0}); assert before == path.read_bytes()
    with pytest.raises(ValueError): verify_cached(path, {**binding, "model_sha256": "other"})
    with pytest.raises(ValueError): write_once(path, {"different": True})
    path.write_text(path.read_text().replace('"score":-2.0', '"score":-3.0'))
    with pytest.raises(ValueError): verify_cached(path, binding)


def test_rankings_match_evaluator_contract():
    query = {"query_id": "fixture", "source": "kuaisearch"}
    row = ranking_row("w111/base", query, channels()["bm25"])
    assert row["ranking_sha256"] == fingerprint(row["ranking"])
    assert "results" not in row and row["method"] == "w111/base"


def test_no_dense_single_query_does_not_request_neural_recall():
    from retrieval_runtime import RetrievalRuntime
    from types import SimpleNamespace
    seen = []
    class PartialRuntime(RetrievalRuntime):
        def __init__(self):
            self.root = Path("/synthetic")
            self.asset = {"files": {str(self.root / "catalog.sqlite"): {"sha256": "fixture"}}}
        def retrieve_batch(self, source, queries, *, include_dense=True):
            seen.append(include_dense)
            values = channels(); values["dense"] = []
            return {"channels": {queries[0]["query_id"]: values}, "timings": {"fixture": True}}
        def document_texts(self, source, ids): return {did: "fixture" for did in ids}
    result = PartialRuntime().search_one("fixture", "kuaisearch", profile="no_dense")
    assert seen == [False] and result["timings"]["ce"] is None
    assert result["hits"][0]["docid"].startswith("kuaisearch:")


def test_test_split_rejects_before_any_query_or_model_access(monkeypatch):
    import run_dev_grid
    monkeypatch.setattr(run_dev_grid, "load_development_queries", lambda: pytest.fail("must not read query files"))
    with pytest.raises(PermissionError): run_dev_grid.main(["--split", "test"])
    with pytest.raises(PermissionError): require_test_authorization(selection_receipt={}, model_selection_receipt={})


@pytest.mark.parametrize("reuse_change", [None, "file", "query", "model"])
def test_cli_scores_union_once_per_model_query_and_resumes_without_inference(tmp_path, monkeypatch, reuse_change):
    import run_dev_grid as grid
    root, out = tmp_path / "historical", tmp_path / "grid"
    root.mkdir()
    queries = [{"query_id": f"s1-{prefix}-dev-{i:03}", "query": f"fixture {prefix} {i}", "source": source}
               for prefix, source in (("ku", "kuaisearch"), ("mu", "multicpr")) for i in range(20)]
    query_file = tmp_path / "queries.jsonl"; write_once(query_file, queries, jsonl=True)
    audit = tmp_path / "audit.json"; write_once(audit, {"fixture": True})
    models = {name: tmp_path / name for name in ("base", "epoch1", "epoch2", "epoch3")}
    calls = {"recall": 0, "score": 0}

    def binding(path): return {"path": str(path), "inference_sha256": fingerprint(str(path))}

    class FakeRuntime:
        def __init__(self, *args, **kwargs): self.asset = {"fixture": True}; self.asset_validation_seconds = 1.0
        def close(self): pass
        def retrieve_batch(self, source, batch):
            calls["recall"] += 1
            rankings = channels()
            if source == "multicpr":
                rankings = {key: [{**row, "document_id": row["document_id"].replace("kuaisearch", "multicpr")} for row in values]
                            for key, values in rankings.items()}
            return {"channels": {q["query_id"]: rankings for q in batch}, "timings": {"fixture": True}}
        def document_texts(self, source, ids): return {did: "fixture text " + did for did in ids}
        def score_pairs(self, path, pairs):
            calls["score"] += 1
            assert len(pairs) == 3  # Four profiles share one scored union.
            return [-3.0, -2.0, -1.0], {"model_binding": binding(path)}

    monkeypatch.setattr(grid, "QUERIES", query_file)
    monkeypatch.setattr(grid, "load_development_queries", lambda: queries)
    monkeypatch.setattr(grid, "configured_models", lambda *args: models)
    monkeypatch.setattr(grid, "model_binding", binding)
    monkeypatch.setattr(grid, "RetrievalRuntime", FakeRuntime)
    command = ["--root", str(root), "--output", str(out), "--asset-audit", str(audit)]
    assert grid.main(command) == 0
    assert calls == {"recall": 2, "score": 160}
    old = (out / "COMPLETE.json").read_bytes()
    assert grid.main(command) == 0
    assert calls == {"recall": 2, "score": 160}
    assert (out / "COMPLETE.json").read_bytes() == old
    assert len((out / "rankings.jsonl").read_text().splitlines()) == 40 * 23
    # Extend the completed four-model grid with three models. All old
    # recall/model computation must be imported rather than run again.
    models.update({f"new_epoch{i}": tmp_path / f"new_epoch{i}" for i in (1, 2, 3)})
    target = tmp_path / "with-new-models"
    extended = ["--root", str(root), "--output", str(target), "--asset-audit", str(audit), "--reuse-grid", str(out)]
    if reuse_change == "file":
        path = out / "scores" / "base" / (queries[0]["query_id"] + ".json")
        path.write_bytes(path.read_bytes().replace(b'"score":-3.0', b'"score":-9.0'))
    elif reuse_change in {"query", "model"}:
        old_binding = grid.read_json(out / "binding.json")
        if reuse_change == "query": old_binding["queries_sha256"] = "changed-query-input"
        else: old_binding["models"]["base"]["inference_sha256"] = "changed-model"
        # Even an internally re-signed complete receipt cannot authorize
        # different source inputs or an incompatible current model.
        (out / "binding.json").write_text(__import__("json").dumps(old_binding))
        complete = grid.read_json(out / "COMPLETE.json")
        complete["binding_sha256"] = grid.sha(out / "binding.json")
        (out / "COMPLETE.json").write_text(__import__("json").dumps(complete))
    if reuse_change is not None:
        with pytest.raises(ValueError): grid.main(extended)
        assert calls == {"recall": 2, "score": 160}
        assert not (target / "recall").exists() and not (target / "scores").exists()
    else:
        assert grid.main(extended) == 0
        assert calls == {"recall": 2, "score": 280}
        receipt = grid.read_json(next((target / "imports").glob("*.json")))
        assert receipt["score_files_imported"] == 160 and receipt["recall_batches_imported"] == 2
        assert receipt["inference_performed"] is False
        assert grid.main(extended) == 0
        assert calls == {"recall": 2, "score": 280}


@pytest.mark.parametrize("crash_window", ["before_batch_commit", "after_batch_commit", "after_first_query_cache"])
def test_real_recall_publication_interrupt_windows_resume(tmp_path, monkeypatch, crash_window):
    import run_dev_grid as grid
    queries = [{"query_id": f"fixture-{i}", "query": f"fixture {i}", "source": "kuaisearch"} for i in range(3)]
    calls = []
    class Runtime:
        def retrieve_batch(self, source, batch):
            calls.append([q["query_id"] for q in batch])
            return {"channels": {q["query_id"]: channels() for q in batch},
                    "timings": {"wall_seconds": 0.123 * len(calls)}}
    original = grid.save_cached
    interrupted = False
    def crash_once(path, binding, value):
        nonlocal interrupted
        is_batch = path.parent.name == "batches"
        is_first_query = path.parent.name == "kuaisearch" and path.name == "fixture-0.json"
        if not interrupted and crash_window == "before_batch_commit" and is_batch:
            interrupted = True
            raise KeyboardInterrupt("injected before atomic batch publication")
        original(path, binding, value)
        if not interrupted and ((crash_window == "after_batch_commit" and is_batch)
                                or (crash_window == "after_first_query_cache" and is_first_query)):
            interrupted = True
            raise KeyboardInterrupt("injected after committed publication")
    monkeypatch.setattr(grid, "save_cached", crash_once)
    with pytest.raises(KeyboardInterrupt):
        grid.source_recalls(Runtime(), tmp_path, "kuaisearch", queries, "grid-binding", allow_inference=True)
    assert interrupted and len(calls) == 1
    committed = list((tmp_path / "recall" / "batches").glob("*.json"))
    prior_bytes = {str(path): path.read_bytes() for path in committed}
    monkeypatch.setattr(grid, "save_cached", original)
    recovered = grid.source_recalls(Runtime(), tmp_path, "kuaisearch", queries, "grid-binding",
                                    allow_inference=crash_window == "before_batch_commit")
    assert set(recovered) == {q["query_id"] for q in queries}
    assert len(calls) == (2 if crash_window == "before_batch_commit" else 1)
    assert all(Path(path).read_bytes() == raw for path, raw in prior_bytes.items())
    again = grid.source_recalls(Runtime(), tmp_path, "kuaisearch", queries, "grid-binding", allow_inference=False)
    assert again == recovered
