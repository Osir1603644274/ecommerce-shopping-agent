from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from retrieval_judgment_pool_core import PoolError, PoolService
from retrieval_judgment_pool_core.core import read_json, read_jsonl, write_json, write_jsonl


def prepared(tmp_path):
    fixture = Path(__file__).resolve().parents[2] / "evaluation/retrieval-judgment-pool-v1-20260901/fixtures/public_tiny_v1"
    data = tmp_path / "data/public-external"
    shutil.copytree(fixture, data)
    config = read_json(data / "config.json")
    config["execution_profile"] = "external_models_v1"
    config["retrievers"] = [{"retriever_id": family, "kind": "external", "family": family,
                             "depth": 12, "model": "test-fixture-only", "revision": "test-v1"}
                            for family in ("lexical", "character", "dense")]
    config["reranker"] = {"kind": "external_scores", "model": "test-fixture-pair-model", "revision": "test-v1", "pair_budget_per_query": 8}
    write_json(data / "config.json", config)
    service = PoolService(tmp_path / "data", tmp_path / "runs")
    run_id = service.create_run("public-external")["run_id"]
    run_dir = tmp_path / "runs" / run_id
    queries = read_jsonl(run_dir / "normalized/queries.jsonl")
    docs = read_jsonl(run_dir / "normalized/documents.jsonl")
    for offset, family in enumerate(("lexical", "character", "dense")):
        rotated = docs[offset:] + docs[:offset]
        rows = [{"query_id": q["query_id"], "document_id": d["document_id"], "rank": i + 1, "score": 100.0 - i}
                for q in queries for i, d in enumerate(rotated[:12])]
        write_jsonl(data / f"{family}.jsonl", rows)
        service.submit_retrieval_run(run_id, family, f"{family}.jsonl")
    service.prepare_retrieval(run_id)
    analysis = read_json(run_dir / "analysis.json")
    scores = [{"query_id": q["query_id"], "document_id": d["document_id"], "score": float(i) + 0.12345}
              for q in analysis["queries"] for i, d in enumerate(q["rrf_ranking"][:8])]
    write_jsonl(data / "ce.jsonl", scores)
    return service, run_id, data, run_dir, scores


def test_real_profile_uses_imported_scores_and_preserves_unscored(tmp_path):
    service, run_id, data, run_dir, scores = prepared(tmp_path)
    with pytest.raises(PoolError) as missing:
        service.build_pool(run_id)
    assert missing.value.code == "EXTERNAL_RERANKER_SCORES_REQUIRED"
    service.submit_reranker_scores(run_id, "ce.jsonl")
    assert service.submit_reranker_scores(run_id, "ce.jsonl")["idempotent_reuse"]
    assert service.build_pool(run_id)["verification"]["all_candidates_unjudged"]
    actual = read_jsonl(run_dir / "reranker_scores.jsonl")
    expected = {(r["query_id"], r["document_id"]): r["score"] for r in scores}
    assert {(r["query_id"], r["document_id"]): r["score"] for r in actual if r["status"] == "SCORED"} == expected
    assert all(r["score"] is None for r in actual if r["status"] == "NOT_SCORED")
    assert service.verify_run(run_id)["blind_provenance_hidden"]


@pytest.mark.parametrize("mutation,code", [("missing", "EXTERNAL_PAIR_INCOMPLETE"), ("duplicate", "EXTERNAL_PAIR_IDENTITY_INVALID"),
                                          ("foreign", "EXTERNAL_PAIR_IDENTITY_INVALID"), ("nonfinite", "EXTERNAL_PAIR_VALUE_INVALID")])
def test_external_pair_input_failures(tmp_path, mutation, code):
    service, run_id, data, _, scores = prepared(tmp_path)
    if mutation == "missing": scores.pop()
    if mutation == "duplicate": scores.append(scores[0])
    if mutation == "foreign": scores[0]["document_id"] = "not-retrieved"
    if mutation == "nonfinite": scores[0]["score"] = float("nan")
    (data / "bad.jsonl").write_text("".join(json.dumps(r) + "\n" for r in scores), encoding="utf-8")
    with pytest.raises(PoolError) as error:
        service.submit_reranker_scores(run_id, "bad.jsonl")
    assert error.value.code == code


def test_external_pair_tamper_rejected_before_pool_build(tmp_path):
    service, run_id, _, run_dir, _ = prepared(tmp_path)
    service.submit_reranker_scores(run_id, "ce.jsonl")
    (run_dir / "external_reranker/scores.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(PoolError) as error:
        service.build_pool(run_id)
    assert error.value.code == "EXTERNAL_PAIR_DRIFT"


def test_retrieval_staging_tamper_rejected(tmp_path):
    service,run_id,_,run_dir,_=prepared(tmp_path)
    path=run_dir/"external_runs/lexical.jsonl"
    values=read_jsonl(path)
    values[0]["score"]+=.125
    write_jsonl(path,values)
    with pytest.raises(PoolError) as error:service.verify_run(run_id)
    assert error.value.code=="EXTERNAL_RUN_DRIFT"


def test_retrieval_identical_resubmit_after_analysis_and_ready(tmp_path):
    service,run_id,data,_,_=prepared(tmp_path)
    assert service.submit_retrieval_run(run_id,"lexical","lexical.jsonl")["idempotent_reuse"]
    service.submit_reranker_scores(run_id,"ce.jsonl")
    service.build_pool(run_id)
    assert service.submit_retrieval_run(run_id,"lexical","lexical.jsonl")["idempotent_reuse"]
    values=read_jsonl(data/"lexical.jsonl")
    values[0]["score"]+=.125
    write_jsonl(data/"different.jsonl",values)
    with pytest.raises(PoolError) as error:service.submit_retrieval_run(run_id,"lexical","different.jsonl")
    assert error.value.code=="EXTERNAL_RUN_DRIFT"
