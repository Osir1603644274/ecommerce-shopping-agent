"""Synthetic-only gate, full-count export, catalog and crash recovery checks."""
from argparse import Namespace
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

import run_dev_grid as grid
import run_training_pool as pool
from retrieval_runtime import CHANNELS, SOURCES, fingerprint, query_key, read_json, sha, weighted_rrf, write_once


def raw_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return sha(path)


def synthetic_inputs(tmp_path, monkeypatch):
    selection = tmp_path / "selection"
    training, contracts = [], []
    for source in SOURCES:
        for ordinal in range(100):
            qid = f"closure-{source[:2]}-train-{ordinal:03}"
            query = f"fixture {source} original query {ordinal}"
            training.append({"query_id": qid, "source": source, "split": "train", "native_split": "train",
                             "native_origin": {"native_split": "train"}, "text": query, "query_key": query_key(query)})
            contracts.append({"query_id": qid, "query": query, "source": source, "split": "train",
                              "required_attribute_keys": ["本体"], "no_added_requirements": True,
                              "intent_policy": "score_product_relevance",
                              "query_text_sha256": hashlib.sha256(query.encode()).hexdigest()})
    train_path = selection / "frozen/train.queries.jsonl"
    test_path = selection / "frozen/test.queries.jsonl"
    write_once(train_path, training, jsonl=True)
    # Deliberately invalid JSON: the runner must hash these bytes, never parse.
    test_path.write_bytes(b"held-out sentinel: MUST ONLY BE HASHED\xff\xfe")
    counts = {s + ":" + split: n for s in SOURCES for split, n in (("train", 100), ("test", 40))}
    frozen = {"status": "FROZEN_QUERY_SPLITS_ONLY", "counts": counts, "native_bindings_reverified": True,
              "test_retrieval_or_labels_executed": False,
              "files": {train_path.name: sha(train_path), test_path.name: sha(test_path)}}
    frozen_sha = raw_json(selection / "FROZEN.json", frozen)
    validation = {"status": "PASS_FROZEN_QUERY_ONLY_AUDIT", "frozen_manifest_sha256": frozen_sha,
                  "cross_split_exact_or_threshold_violations": 0, "root_review_binding_verified": True,
                  "test_product_retrieval_labels_scores_executed": False}
    validation_sha = raw_json(selection / "VALIDATION.json", validation)
    manifest = {"status": "QUERY_SELECTION_PACKAGE_MANIFEST", "files": {
        n: {"sha256": sha(selection / n), "bytes": (selection / n).stat().st_size}
        for n in ("FROZEN.json", "VALIDATION.json", "frozen/train.queries.jsonl", "frozen/test.queries.jsonl")}}
    manifest_sha = raw_json(selection / "MANIFEST.json", manifest)
    seal = {"status": "SEALED_QUERY_SPLITS_ONLY", "queries": {"train": 200, "test": 80},
            "source_counts": {s: {"train": 100, "test": 40} for s in SOURCES},
            "frozen_sha256": frozen_sha, "manifest_sha256": manifest_sha, "validation_sha256": validation_sha,
            "train_queries_path": str(train_path), "test_queries_path": str(test_path)}
    seal_path = selection / "SEALED.json"
    seal_sha = raw_json(seal_path, seal)
    contract_path = tmp_path / "preparation/query-contracts-frozen.jsonl"
    contracts_sha = write_once(contract_path, contracts, jsonl=True)
    rubric = tmp_path / "rubric.md"
    rubric.write_text("fixture rubric", encoding="utf-8")
    monkeypatch.setattr(pool, "RUBRIC_SHA256", sha(rubric))
    freeze_path = tmp_path / "preparation/QUERY_CONTRACTS_FROZEN.json"
    freeze = {"status": pool.CONTRACT_STATUS, "query_count": 200, "selected_train_sha256": sha(train_path),
              "rubric_sha256": sha(rubric), "query_contracts_path": str(contract_path), "query_contracts_sha256": contracts_sha}
    freeze_sha = raw_json(freeze_path, freeze)
    evidence = tmp_path / "gate-evidence.txt"
    evidence.write_text("fixture input and source code", encoding="utf-8")
    gate_path = tmp_path / "gate.json"
    gate = {"status": "TRAINING_TRIGGERED", "triggered": True, "threshold_query_count": 5,
            "selected_error_query_count": 5, "selected_method": "fixture_epoch_1", "main_query_count": 33,
            "diagnostic_queries_counted": False, "test_labels_or_scores_read": False,
            "code": {"fixture": {"path": str(evidence), "sha256": sha(evidence)}},
            "inputs": [{"path": str(evidence), "sha256": sha(evidence)}]}
    gate_sha = raw_json(gate_path, gate)
    return Namespace(gate=gate_path, gate_sha256=gate_sha, selection_seal=seal_path,
                     selection_seal_sha256=seal_sha, query_contracts=contract_path,
                     query_contracts_freeze=freeze_path, query_contracts_freeze_sha256=freeze_sha,
                     rubric=rubric), training, contracts


@pytest.mark.parametrize("failure", ["gate_hash", "negative_gate", "insufficient_errors", "changed_gate_evidence"])
def test_gate_rejects_before_selection_or_query_text(tmp_path, monkeypatch, failure):
    args, _, _ = synthetic_inputs(tmp_path, monkeypatch)
    if failure == "gate_hash":
        args.gate_sha256 = "0" * 64
    elif failure == "changed_gate_evidence":
        (tmp_path / "gate-evidence.txt").write_text("tampered", encoding="utf-8")
    else:
        gate = read_json(args.gate)
        if failure == "negative_gate":
            gate.update(status="TRAINING_NOT_TRIGGERED", triggered=False)
        else:
            gate["selected_error_query_count"] = 4
        args.gate_sha256 = raw_json(args.gate, gate)
    # Deleting a later metadata entry proves failure ordering, not only a mock.
    args.selection_seal.unlink()
    monkeypatch.setattr(pool, "json_rows", lambda _: pytest.fail("Gate allowed query text access"))
    with pytest.raises(ValueError, match="hash differs|gate has not triggered|dev-error policy"):
        pool.authorize_inputs(args)


@pytest.mark.parametrize("failure", ["seal_hash", "train_bytes", "test_bytes", "freeze_hash", "freeze_train_binding", "contract_bytes"])
def test_seals_fail_before_selected_query_parse(tmp_path, monkeypatch, failure):
    args, _, _ = synthetic_inputs(tmp_path, monkeypatch)
    if failure == "seal_hash": args.selection_seal_sha256 = "0" * 64
    elif failure in {"train_bytes", "test_bytes"}:
        (args.selection_seal.parent / "frozen" / (failure.split("_")[0] + ".queries.jsonl")).write_bytes(b"changed")
    elif failure == "freeze_hash": args.query_contracts_freeze_sha256 = "0" * 64
    elif failure == "freeze_train_binding":
        freeze = read_json(args.query_contracts_freeze)
        freeze["selected_train_sha256"] = "0" * 64
        args.query_contracts_freeze_sha256 = raw_json(args.query_contracts_freeze, freeze)
    elif failure == "contract_bytes": args.query_contracts.write_bytes(b"changed")
    monkeypatch.setattr(pool, "json_rows", lambda _: pytest.fail("Unverified seals allowed query text access"))
    with pytest.raises(ValueError, match="hash differs|not frozen"):
        pool.authorize_inputs(args)


def test_authorization_parses_only_train_and_contracts_and_retains_original_query(tmp_path, monkeypatch):
    args, training, _ = synthetic_inputs(tmp_path, monkeypatch)
    original, seen = pool.json_rows, []
    def track(path):
        seen.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(pool, "json_rows", track)
    queries, proof = pool.authorize_inputs(args)
    assert seen == ["train.queries.jsonl", "query-contracts-frozen.jsonl"]
    assert {q["query_id"]: q["query"] for q in queries} == {q["query_id"]: q["text"] for q in training}
    assert proof["test_query_text_parsed"] is False


def test_rebound_but_changed_contract_query_rejected(tmp_path, monkeypatch):
    args, _, contracts = synthetic_inputs(tmp_path, monkeypatch)
    contracts[0]["query"] += " inferred intent"
    args.query_contracts.write_text("\n".join(json.dumps(row) for row in contracts), encoding="utf-8")
    freeze = read_json(args.query_contracts_freeze)
    freeze["query_contracts_sha256"] = sha(args.query_contracts)
    args.query_contracts_freeze_sha256 = raw_json(args.query_contracts_freeze, freeze)
    with pytest.raises(ValueError, match="original text"):
        pool.authorize_inputs(args)


def channels_for(source):
    # Deliberate disjoint Top10 from every recall channel, with a long overlap.
    ranges = ([*range(10), *range(30, 320)], [*range(10, 20), *range(30, 320)],
              [*range(20, 30), *range(30, 320)])
    return {name: [{"document_id": f"{source}:{i:04}", "rank": rank, "score": -float(rank)}
                   for rank, i in enumerate(ids, 1)] for name, ids in zip(CHANNELS, ranges)}


class FakeRuntime:
    def __init__(self):
        self.asset = {"fixture": True, "not_actual_full_corpus": True}
        self.base = {"path": "fixture-base-only", "inference": {"synthetic": True}, "inference_sha256": "fixture"}
        self.catalog = sqlite3.connect(":memory:")
        self.catalog.execute("CREATE TABLE documents(docid TEXT,source TEXT,text TEXT,payload TEXT,source_line INTEGER)")
        for source in SOURCES:
            for i in range(320):
                did = f"{source}:{i:04}"
                text = f"actual fixture catalog.text {source} {i}"
                payload = {"document_id": did, "source": source, "text": text, "title": f"raw title {i}",
                           "brand": "", "categories": [], "price": 99, "native_id": i}
                if i == 31: payload["seller_name"] = "genuine fixture seller"
                self.catalog.execute("INSERT INTO documents VALUES(?,?,?,?,?)", (did, source, text, json.dumps(payload), i))
        self.recalls = []; self.scores = []

    def retrieve_batch(self, source, queries):
        self.recalls.append((source, len(queries)))
        timings = {"lexical": {q["query_id"]: {channel: {"matched": 295, "zero_score_padding": 5}
                                              for channel in ("bm25", "character")} for q in queries},
                   "wall_seconds": float(len(self.recalls)), "fixture": True}
        return {"channels": {q["query_id"]: channels_for(source) for q in queries}, "timings": timings}

    def document_texts(self, source, ids):
        values = self.catalog.execute("SELECT docid,text FROM documents WHERE source=?", (source,))
        return {did: text for did, text in values if did in set(ids)}

    def score_pairs(self, model_path, pairs):
        assert model_path == self.base["path"]
        assert len(pairs) == 300
        self.scores.append(fingerprint(pairs))
        # Negative logits are valid; the last ten RRF candidates win.
        scores = [float(i - len(pairs)) for i in range(len(pairs))]
        return scores, {"model_binding": self.base, "input_sha256": fingerprint(pairs),
                        "max_length": 256, "batch_size": 16, "pair_count": len(pairs),
                        "score_seconds": float(len(self.scores)), "fixture": True}


def test_four_top10_union_and_fill_raw_negative_ce(tmp_path):
    channels = channels_for("kuaisearch")
    ce_pool = weighted_rrf(channels, (1, 1, 1))
    scores = {r["document_id"]: -float(300 - i) for i, r in enumerate(ce_pool)}
    candidates, ce = pool.choose_candidates(channels, ce_pool, scores)
    expected = {r["document_id"] for name in CHANNELS for r in channels[name][:10]} | {r["document_id"] for r in ce[:10]}
    assert len(expected) == 40
    assert {r["document_id"] for r in candidates} == expected
    assert ce[0]["score"] < 0
    # Identical channels exercise RRF fill after a much smaller union.
    same = {name: channels["bm25"] for name in CHANNELS}
    ce_pool = weighted_rrf(same, (1, 1, 1))
    candidates, _ = pool.choose_candidates(same, ce_pool, {r["document_id"]: -r["rank"] for r in ce_pool})
    assert len(candidates) == 40 and sum(r["selection_routes"] == ["rrf_fill"] for r in candidates) == 30


def test_raw_channel_top10_outside_ce_pool_is_never_discarded():
    channels = channels_for("kuaisearch")
    ce_pool = weighted_rrf(channels, (1, 1, 1))
    protected = channels["bm25"][0]["document_id"]
    # Pure selection helper checks this boundary even though production derives
    # the CE pool itself. A raw Top10 must never be silently cut by CE membership.
    ce_pool = [row for row in ce_pool if row["document_id"] != protected]
    selected, _ = pool.choose_candidates(channels, ce_pool, {r["document_id"]: -r["rank"] for r in ce_pool})
    assert protected in {r["document_id"] for r in selected}
    assert len(selected) == 40


def test_catalog_public_document_sanitization_and_missing_seller():
    runtime = FakeRuntime()
    values = pool.actual_documents(runtime.catalog, "kuaisearch", ["kuaisearch:0030", "kuaisearch:0031"])
    assert values["kuaisearch:0030"]["document"] == {"title": "raw title 30", "brand": "", "categories": [], "seller_name": ""}
    assert values["kuaisearch:0031"]["document"]["seller_name"] == "genuine fixture seller"
    assert values["kuaisearch:0030"]["catalog_text"] == "actual fixture catalog.text kuaisearch 30"
    assert values["kuaisearch:0030"]["catalog_provenance"]["seller_name_present_in_payload"] is False
    with pytest.raises(ValueError, match="outside"):
        pool.actual_documents(runtime.catalog, "multicpr", ["kuaisearch:0030"])
    runtime.catalog.execute("UPDATE documents SET text='tampered' WHERE docid='kuaisearch:0030'")
    with pytest.raises(ValueError, match="identity/text"):
        pool.actual_documents(runtime.catalog, "kuaisearch", ["kuaisearch:0030"])


def single_query_receipt(tmp_path):
    runtime = FakeRuntime()
    query = {"query_id": "closure-ku-train-fixture", "source": "kuaisearch", "query": "original ambiguous query"}
    recall = grid.source_recalls(runtime, tmp_path, "kuaisearch", [query], "binding", allow_inference=True)[query["query_id"]]
    return runtime, query, recall


def test_score_commit_before_candidate_failure_resumes_without_ce(tmp_path, monkeypatch):
    runtime, query, recall = single_query_receipt(tmp_path)
    actual = pool.actual_documents
    monkeypatch.setattr(pool, "actual_documents", lambda *args: (_ for _ in ()).throw(InterruptedError("after score commit")))
    with pytest.raises(InterruptedError):
        pool.process_query(runtime, tmp_path, query, recall, "binding", runtime.base)
    assert len(runtime.scores) == 1
    monkeypatch.setattr(pool, "actual_documents", actual)
    rows, _ = pool.process_query(runtime, tmp_path, query, recall, "binding", runtime.base)
    assert len(rows) == 40 and len(runtime.scores) == 1
    assert all(row["query"] == query["query"] for row in rows)

def test_cpu_replay_never_writes_or_invokes_model(tmp_path,monkeypatch):
    runtime,query,recall=single_query_receipt(tmp_path)
    expected,_=pool.process_query(runtime,tmp_path,query,recall,'binding',runtime.base)
    monkeypatch.setattr(pool,'save_cached',lambda *a:pytest.fail('CPU replay wrote a file'))
    monkeypatch.setattr(runtime,'score_pairs',lambda *a:pytest.fail('CPU replay called model'))
    actual,_=pool.process_query(runtime,tmp_path,query,recall,'binding',runtime.base,cache_only=True)
    assert actual==expected
    (tmp_path/'candidates'/(query['query_id']+'.json')).unlink()
    with pytest.raises(FileNotFoundError):pool.process_query(runtime,tmp_path,query,recall,'binding',runtime.base,cache_only=True)

def test_cpu_replay_rejects_rehashed_candidate_forgery(tmp_path):
    runtime,query,recall=single_query_receipt(tmp_path)
    pool.process_query(runtime,tmp_path,query,recall,'binding',runtime.base)
    path=tmp_path/'candidates'/(query['query_id']+'.json')
    value=read_json(path);value['result']['rows'][0]['document']['title']='fabricated title'
    value['result_sha256']=fingerprint(value['result']);raw_json(path,value)
    with pytest.raises(ValueError,match='CPU replay'):
        pool.process_query(runtime,tmp_path,query,recall,'binding',runtime.base,cache_only=True)


def test_failure_before_score_commit_allows_new_timing_retry(tmp_path, monkeypatch):
    runtime, query, recall = single_query_receipt(tmp_path)
    original = pool.save_cached
    def interrupt(path, binding, result):
        if "scores" in Path(path).parts: raise InterruptedError("before atomic score commit")
        original(path, binding, result)
    monkeypatch.setattr(pool, "save_cached", interrupt)
    with pytest.raises(InterruptedError):
        pool.process_query(runtime, tmp_path, query, recall, "binding", runtime.base)
    monkeypatch.setattr(pool, "save_cached", original)
    rows, _ = pool.process_query(runtime, tmp_path, query, recall, "binding", runtime.base)
    assert len(rows) == 40 and len(runtime.scores) == 2


def test_tampered_score_and_model_binding_rejected_without_rescoring(tmp_path):
    runtime, query, recall = single_query_receipt(tmp_path)
    pool.process_query(runtime, tmp_path, query, recall, "binding", runtime.base)
    with pytest.raises(ValueError, match="inputs/model differ"):
        pool.process_query(runtime, tmp_path, query, recall, "binding", {**runtime.base, "inference_sha256": "changed"})
    score_path = tmp_path / "scores/base" / (query["query_id"] + ".json")
    value = read_json(score_path); value["result"]["scores"][0]["score"] += 1
    raw_json(score_path, value)
    with pytest.raises(ValueError, match="output content differs"):
        pool.process_query(runtime, tmp_path, query, recall, "binding", runtime.base)
    assert len(runtime.scores) == 1


def test_full_200_fixture_batch_commit_crash_and_resume_without_old_inference(tmp_path, monkeypatch):
    args, _, _ = synthetic_inputs(tmp_path, monkeypatch)
    queries, authorization = pool.authorize_inputs(args)
    runtime = FakeRuntime()
    binding = {"authorization": authorization, "base_ce": runtime.base, "asset": runtime.asset, "fixture_only": True}
    out = tmp_path / "training-pool"
    save = grid.save_cached
    crashed = False
    def crash_after_batch(path, expected, result):
        nonlocal crashed
        if "batches" not in Path(path).parts and not crashed:
            crashed = True
            raise InterruptedError("committed channels+timings, before first query cache")
        return save(path, expected, result)
    monkeypatch.setattr(grid, "save_cached", crash_after_batch)
    with pytest.raises(InterruptedError):
        pool.execute(runtime, out, queries, binding)
    assert runtime.recalls == [("kuaisearch", 100)] and runtime.scores == []
    assert len(list((out / "recall/batches").glob("*.json"))) == 1
    assert not (out / "POOL_COMPLETE.json").exists()
    monkeypatch.setattr(grid, "save_cached", save)
    receipt = pool.execute(runtime, out, queries, binding)
    assert runtime.recalls == [("kuaisearch", 100), ("multicpr", 100)]
    assert len(runtime.scores) == 200
    assert receipt["candidate_count"] == 8000 and receipt["query_count"] == 200
    assert receipt["candidate_rows_sha256"] == sha(out / "candidate-rows.jsonl")
    assert receipt["query_contracts_sha256"] == sha(args.query_contracts)
    exported = pool.json_rows(out / "candidate-rows.jsonl")
    assert len(exported) == 8000 and all(set(row["document"]) == {"title", "brand", "categories", "seller_name"} for row in exported)
    assert all("grade" not in row and "price" not in row["document"] for row in exported)
    assert all(row["document_id"].startswith(row["source"] + ":") for row in exported)
    first_complete_sha = sha(out / "POOL_COMPLETE.json")
    # Existing model scores/recalls are immutable; a complete resume computes none.
    monkeypatch.setattr(runtime, "retrieve_batch", lambda *args: pytest.fail("Resume repeated recall"))
    monkeypatch.setattr(runtime, "score_pairs", lambda *args: pytest.fail("Resume repeated CE"))
    pool.execute(runtime, out, queries, binding)
    assert sha(out / "POOL_COMPLETE.json") == first_complete_sha


def test_partial_query_set_cannot_claim_complete(tmp_path):
    runtime = FakeRuntime()
    with pytest.raises(ValueError, match="100 queries per source"):
        pool.execute(runtime, tmp_path, [], {"base_ce": runtime.base})
    assert runtime.recalls == [] and runtime.scores == []
    assert not (tmp_path / "POOL_COMPLETE.json").exists()


def test_dry_run_never_constructs_runtime_or_writes_pool(tmp_path, monkeypatch, capsys):
    args, _, _ = synthetic_inputs(tmp_path, monkeypatch)
    out = tmp_path / "dry-run-output"
    monkeypatch.setattr(pool, "RetrievalRuntime", lambda *a, **k: pytest.fail("Dry run initialized actual runtime"))
    result = pool.main(["--gate", str(args.gate), "--gate-sha256", args.gate_sha256,
                        "--selection-seal", str(args.selection_seal), "--selection-seal-sha256", args.selection_seal_sha256,
                        "--query-contracts", str(args.query_contracts), "--query-contracts-freeze", str(args.query_contracts_freeze),
                        "--query-contracts-freeze-sha256", args.query_contracts_freeze_sha256,
                        "--rubric", str(args.rubric), "--output", str(out), "--dry-run"])
    assert result == 0 and not out.exists()
    assert json.loads(capsys.readouterr().out)["gpu_called"] is False
