from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

import evaluation.kuaisearch_multicategory_retrieval_baseline_v1 as baseline
from evaluation.kuaisearch_multicategory_retrieval_baseline_v1 import (
    BM25_FIELD_WEIGHTS,
    build_receipt,
    DenseIndex,
    IndexedBM25,
    RankingInputError,
    RetrievalDocument,
    RetrievalQuery,
    rank_queries,
    rrf_fuse,
    validate_g0_directory,
    validate_rank_rows,
    write_run,
)


class _FakeEmbedding:
    def embed(self, values, batch_size=256):
        for value in values:
            text = str(value)
            yield np.asarray([float(len(text)), float(text.count("鞋")), float(text.count("手机")), 1.0], dtype=np.float32)


class _StableCrossEncoder:
    model_name = "BAAI/bge-reranker-v2-m3"
    revision = "test-revision"
    device = "cpu"
    deps_dir = None
    dependencies_snapshot = {"root": "test", "fileCount": 1, "snapshotSha256": "x"}
    cache_receipt = {"path": "test", "files": {}}
    load_ms = 0.0

    def rerank(self, _query, ranking, *, candidate_limit=20):
        return list(ranking), 2.0


class _FickleCrossEncoder(_StableCrossEncoder):
    def __init__(self):
        self.calls = 0

    def rerank(self, _query, ranking, *, candidate_limit=20):
        self.calls += 1
        result = list(ranking)
        if self.calls % 2 == 0:
            result[:candidate_limit] = reversed(result[:candidate_limit])
        return result, 2.0


def _documents():
    return [
        RetrievalDocument("ksd-b", "红色跑步鞋", "轻便 运动", "A"),
        RetrievalDocument("ksd-a", "蓝色休闲鞋", "日常 运动", "B"),
        RetrievalDocument("ksd-c", "手机保护壳", "透明", "C"),
    ]


def test_rrf_requires_exactly_two_channels_and_is_stable():
    assert rrf_fuse({"bm25": ["ksd-b", "ksd-a"], "dense": ["ksd-a", "ksd-b"]}) == ["ksd-a", "ksd-b"]
    with pytest.raises(ValueError):
        rrf_fuse({"bm25": ["ksd-a"], "dense": ["ksd-b"], "other": []})


def test_indexed_bm25_supports_string_ids_and_field_control():
    index = IndexedBM25(_documents())
    fields = index.rank("跑步鞋", field_weights=BM25_FIELD_WEIGHTS)
    title = index.rank("跑步鞋", field_weights={"title": 1.0})
    assert len(fields) == 3
    assert fields[0] == "ksd-b"
    assert title[0] == "ksd-b"
    assert set(fields) == {"ksd-a", "ksd-b", "ksd-c"}


def test_dense_rank_is_deterministic_with_injected_local_model():
    docs = _documents()
    first = DenseIndex(docs, cache_dir=Path("."), model=_FakeEmbedding())
    second = DenseIndex(docs, cache_dir=Path("."), model=_FakeEmbedding())
    assert first.rank("手机", top_k=3) == second.rank("手机", top_k=3)


def test_rank_rows_use_canonical_flat_strategy_contract():
    docs = _documents()
    rows, _latency = rank_queries(
        docs,
        [RetrievalQuery("q1", "跑步鞋", "train")],
        dense=DenseIndex(docs, cache_dir=Path("."), model=_FakeEmbedding()),
        bm25=IndexedBM25(docs),
    )
    assert {row["strategy"] for row in rows} == {"bm25_fields", "bm25_title", "dense_title", "rrf_bm25_dense"}
    assert all(set(row) == {"schemaVersion", "queryId", "strategy", "rankedDocIds", "queryLatencyMs"} for row in rows)
    assert all(len(row["rankedDocIds"]) <= 100 for row in rows)


def test_strategy_latency_is_end_to_end_and_repeat_is_recorded():
    docs = _documents()
    rows, receipt = rank_queries(
        docs,
        [RetrievalQuery("q1", "跑步鞋", "train")],
        dense=DenseIndex(docs, cache_dir=Path("."), model=_FakeEmbedding()),
        bm25=IndexedBM25(docs),
        cross_encoder=_StableCrossEncoder(),
    )
    by_strategy = {row["strategy"]: row["queryLatencyMs"] for row in rows}
    assert by_strategy["rrf_bm25_dense"] >= by_strategy["bm25_fields"]
    assert by_strategy["rrf_bm25_dense"] >= by_strategy["dense_title"]
    assert by_strategy["rrf_bm25_dense_ce"] >= by_strategy["rrf_bm25_dense"]
    assert receipt["repeatVerifiedQueries"] == 1
    assert receipt["repeatMismatchCount"] == 0
    assert receipt["repeatExtraLatencyMs"]["count"] == 1


def test_unstable_cross_encoder_repeat_fails_closed():
    docs = _documents()
    with pytest.raises(RankingInputError):
        rank_queries(
            docs,
            [RetrievalQuery("q1", "跑步鞋", "train")],
            dense=DenseIndex(docs, cache_dir=Path("."), model=_FakeEmbedding()),
            bm25=IndexedBM25(docs),
            cross_encoder=_FickleCrossEncoder(),
        )


def test_cross_encoder_dependency_path_is_prepended(tmp_path: Path):
    original = list(__import__("sys").path)
    try:
        resolved = baseline.prepend_local_deps(tmp_path)
        assert resolved == str(tmp_path.resolve())
        assert __import__("sys").path[0] == resolved
    finally:
        __import__("sys").path[:] = original


def test_cli_exposes_cross_encoder_deps_argument():
    from scripts.run_kuaisearch_multicategory_retrieval_baseline_v1 import build_parser
    args = build_parser().parse_args(["--documents", "d.jsonl", "--queries", "q.jsonl", "--output-dir", "out", "--cross-encoder-deps", "deps"])
    assert args.cross_encoder_deps == Path("deps")


def test_cli_validates_g0_before_preparing_deps_and_fails_closed(tmp_path: Path):
    import inspect
    import scripts.run_kuaisearch_multicategory_retrieval_baseline_v1 as cli
    source = inspect.getsource(cli.main)
    assert source.index("validate_g0_directory(args.g0_dir)") < source.index("_prepare_cross_encoder_deps(g0[\"crossEncoderDeps\"])")
    with pytest.raises(SystemExit):
        cli._prepare_cross_encoder_deps(tmp_path / "missing-deps")


def test_cross_encoder_identity_is_available_in_receipt(monkeypatch, tmp_path: Path):
    (tmp_path / "bundle.py").write_text("x=1\n", encoding="utf-8")
    monkeypatch.setattr(baseline, "verify_cache", lambda *_args, **_kwargs: {"path": "pinned", "files": {"model.safetensors": {"sha256": "x"}}})
    encoder = baseline.CrossEncoderReranker(
        {"ksd-a": _documents()[0]},
        cache_dir=tmp_path,
        deps_dir=tmp_path,
        model=object(),
        tokenizer=object(),
        device="cpu",
    )
    assert encoder.model_name == "BAAI/bge-reranker-v2-m3"
    assert encoder.revision == baseline.CROSS_ENCODER_REVISION
    assert encoder.device == "cpu"
    assert encoder.deps_dir == str(tmp_path.resolve())


def test_dependency_snapshot_ignores_pyc_but_changes_for_source(tmp_path: Path):
    source = tmp_path / "bundle.py"
    pyc = tmp_path / "__pycache__" / "bundle.cpython-312.pyc"
    source.write_text("x=1\n", encoding="utf-8")
    pyc.parent.mkdir()
    pyc.write_bytes(b"one")
    first = baseline.freeze_dependency_snapshot(tmp_path)
    pyc.write_bytes(b"two")
    assert baseline.freeze_dependency_snapshot(tmp_path) == first
    source.write_text("x=2\n", encoding="utf-8")
    second = baseline.freeze_dependency_snapshot(tmp_path)
    assert second["snapshotSha256"] != first["snapshotSha256"]
    assert second["fileCount"] == first["fileCount"] == 1


def test_package_versions_include_runtime_huggingface_hub():
    versions = baseline.package_versions()
    assert set(versions) == {"fastembed", "onnxruntime", "torch", "transformers", "huggingface_hub"}
    assert all(versions[name] for name in versions)


def test_runtime_package_versions_use_exact_g0_keys_and_loaded_module_versions(monkeypatch):
    import sys
    class _Module:
        def __init__(self, version):
            self.__version__ = version
    names = {"fastembed": "f", "onnxruntime": "o", "torch": "t", "transformers": "x", "huggingface_hub": "h"}
    for name, version in names.items():
        monkeypatch.setitem(sys.modules, name, _Module(version))
    assert baseline.package_versions(runtime_model_stack=True) == names


def test_receipt_contains_cross_encoder_identity(monkeypatch, tmp_path: Path):
    docs = _documents()
    monkeypatch.setattr(baseline, "package_versions", lambda **_kwargs: {
        "fastembed": "0.3.6", "onnxruntime": "1.20.1", "torch": "2.5.1+cu121",
        "transformers": "5.12.1", "huggingface_hub": "1.28.0",
    })
    documents_path = tmp_path / "documents.jsonl"
    queries_path = tmp_path / "queries.jsonl"
    documents_path.write_text("{}\n", encoding="utf-8")
    queries_path.write_text("{}\n", encoding="utf-8")
    dense = DenseIndex(docs, cache_dir=Path("."), model=_FakeEmbedding())
    receipt = build_receipt(
        documents_path=documents_path,
        queries_path=queries_path,
        documents=docs,
        queries=[RetrievalQuery("q1", "鞋", "train")],
        bm25=IndexedBM25(docs),
        dense=dense,
        cross_encoder=_StableCrossEncoder(),
        query_latency={"components": {}, "strategies": {}},
        rss_before=1,
        rss_after=2,
        rss_samples=[1, 2, 3],
        code_paths=[],
    )
    assert receipt["models"]["crossEncoder"]["name"] == "BAAI/bge-reranker-v2-m3"
    assert receipt["models"]["crossEncoder"]["revision"] == "test-revision"
    assert receipt["models"]["crossEncoder"]["device"] == "cpu"
    assert receipt["memory"]["sampledPeakRssBytes"] == 3
    assert receipt["packageVersions"]["huggingface_hub"] == "1.28.0"
    assert set(receipt["packageVersions"]) == {"fastembed", "onnxruntime", "torch", "transformers", "huggingface_hub"}


def test_query_projection_rejects_extra_fields(tmp_path: Path):
    path = tmp_path / "queries.jsonl"
    path.write_text(json.dumps({"queryId": "q1", "query": "鞋", "split": "train", "extra": 1}) + "\n", encoding="utf-8")
    from evaluation.kuaisearch_multicategory_retrieval_baseline_v1 import load_queries
    with pytest.raises(RankingInputError):
        load_queries(path)


def test_document_projection_rejects_non_retrieval_fields(tmp_path: Path):
    path = tmp_path / "documents.jsonl"
    path.write_text(json.dumps({"doc_id": "ksd-a", "title": "鞋", "attr_value": "", "brand": "", "categoryKey": "x"}) + "\n", encoding="utf-8")
    from evaluation.kuaisearch_multicategory_retrieval_baseline_v1 import load_documents
    with pytest.raises(RankingInputError):
        load_documents(path)


def test_non_empty_output_refuses_overwrite(tmp_path: Path):
    output = tmp_path / "run"
    output.mkdir()
    (output / "existing").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_run(output_dir=output, rows=[], receipt={})


def test_strict_formal_rows_require_507_times_5_and_100_unique_ids():
    strategies = ("bm25_fields", "bm25_title", "dense_title", "rrf_bm25_dense", "rrf_bm25_dense_ce")
    rows = [
        {"schemaVersion": baseline.SCHEMA_VERSION, "queryId": f"q{query_number}", "strategy": strategy,
         "rankedDocIds": [f"d{doc_number}" for doc_number in range(100)], "queryLatencyMs": 1.0}
        for query_number in range(507) for strategy in strategies
    ]
    validate_rank_rows(rows)
    rows[-1]["rankedDocIds"][-1] = rows[-1]["rankedDocIds"][-2]
    with pytest.raises(RankingInputError):
        validate_rank_rows(rows)


@pytest.mark.parametrize("drift", ["artifact SHA drift", "runtime code SHA drift", "Dense ONNX SHA drift", "G0 query input pin drift"])
def test_formal_g0_validation_fails_closed_for_any_pin_drift(monkeypatch, tmp_path: Path, drift: str):
    deps = tmp_path / "deps"; dense = tmp_path / "dense"; cross = tmp_path / "cross"
    for directory in (deps, dense, cross):
        directory.mkdir()
        (directory / "pin").write_text("x", encoding="utf-8")
    g0 = tmp_path / "g0"; g0.mkdir()
    (g0 / "models.manifest.json").write_text(json.dumps({
        "dense": {"root": str(dense)}, "crossEncoder": {"root": str(cross)},
        "crossEncoderDependencies": {"root": str(deps)},
    }), encoding="utf-8")

    class _Contract:
        FINAL_CODE_PIN_BASENAMES = (
            "kuaisearch_multicategory_retrieval_contract_v1.py", "prepare_kuaisearch_multicategory_retrieval_g0_v1.py",
            "kuaisearch_multicategory_retrieval_baseline_v1.py", "run_kuaisearch_multicategory_retrieval_baseline_v1.py",
            "kuaisearch_multicategory_retrieval_scorer_v1.py", "score_kuaisearch_multicategory_retrieval_g1_v1.py",
        )

        @staticmethod
        def verify_g0_bundle(**_kwargs):
            raise ValueError(drift)

        @staticmethod
        def environment_pin():
            return {"offline": True}

    monkeypatch.setattr(baseline.importlib, "import_module", lambda name: _Contract() if name.endswith("contract_v1") else __import__(name))
    with pytest.raises(RankingInputError, match="G0 validation failed closed"):
        validate_g0_directory(g0)
