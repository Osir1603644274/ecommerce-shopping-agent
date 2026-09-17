"""Gate-authorized, unjudged training pools from the two verified full catalogs.

This entry point never reads test query text, labels candidates, or trains a model.
Only the caller runs actual retrieval/CE; tests inject an in-memory runtime.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys

from retrieval_runtime import (
    ASSET_AUDIT, BLOCK_SIZE, CHANNELS, DEPTH, OLD_CODE, OLD_ROOT, SOURCES,
    WEIGHTS, RetrievalRuntime, fingerprint, model_binding, query_key, read_json,
    rerank_same_candidates, sha, weighted_rrf, write_once,
)
from run_dev_grid import save_cached, source_recalls, verify_cached

HERE = Path(__file__).resolve().parent
DATA = Path("D:/agent-datasets/search-closure-v1")
RUBRIC = Path("D:/agent-datasets/search-stage1-dev-revision-v6/policy/RUBRIC.md")
RUBRIC_SHA256 = "404b2d8111ddf4373a9f1bc107690a60141ff103ed02ef4a6df475567eb2ad67"
CONTRACT_STATUS = "TRAINING_QUERY_CONTRACTS_FROZEN_BEFORE_CANDIDATES"
POOL_SIZE = 40
TOP = 10


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checked_file(path, expected):
    path = Path(path).resolve()
    require(isinstance(expected, str) and len(expected) == 64 and sha(path) == expected,
            f"Input hash differs: {path}")
    return {"path": str(path), "sha256": expected}


def json_rows(path):
    return [json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
            for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def authorize_inputs(args):
    """Verify positive gate first; do not parse selected queries until all seals pass.

    Selection VALIDATION supplies the prior isolation audit. Held-out files are
    opened only as binary streams by sha(), never parsed or used for retrieval.
    """
    gate_ref = checked_file(args.gate, args.gate_sha256)
    gate = read_json(args.gate)
    require(gate.get("status") == "TRAINING_TRIGGERED" and gate.get("triggered") is True,
            "Training gate has not triggered: selected queries/candidates remain unopened")
    require(gate.get("threshold_query_count") == 5 and gate.get("selected_error_query_count", 0) >= 5
            and gate.get("selected_method") and gate.get("main_query_count") == 33
            and gate.get("diagnostic_queries_counted") is False
            and gate.get("test_labels_or_scores_read") is False,
            "Training gate does not implement the frozen dev-error policy")
    require(isinstance(gate.get("code"), dict) and gate["code"] and gate.get("inputs"),
            "Gate lacks code/input evidence")
    gate_evidence = [checked_file(row["path"], row["sha256"])
                     for row in [*gate["code"].values(), *gate["inputs"]]]

    seal_path = Path(args.selection_seal).resolve()
    selection_ref = checked_file(seal_path, args.selection_seal_sha256)
    seal = read_json(seal_path)
    selection_dir = seal_path.parent
    require(seal.get("status") == "SEALED_QUERY_SPLITS_ONLY"
            and seal.get("queries") == {"train": 200, "test": 80}
            and seal.get("source_counts") == {s: {"train": 100, "test": 40} for s in SOURCES},
            "Selection is not the sealed 200-train/80-test split")
    selection_files = {}
    for filename, key in (("FROZEN.json", "frozen_sha256"), ("MANIFEST.json", "manifest_sha256"),
                          ("VALIDATION.json", "validation_sha256")):
        selection_files[filename] = checked_file(selection_dir / filename, seal[key])
    frozen = read_json(selection_dir / "FROZEN.json")
    manifest = read_json(selection_dir / "MANIFEST.json")
    validation = read_json(selection_dir / "VALIDATION.json")
    counts = {s + ":" + split: n for s in SOURCES for split, n in (("train", 100), ("test", 40))}
    require(frozen.get("status") == "FROZEN_QUERY_SPLITS_ONLY" and frozen.get("counts") == counts
            and frozen.get("native_bindings_reverified") is True
            and frozen.get("test_retrieval_or_labels_executed") is False,
            "Frozen selection membership or native verification differs")
    require(validation.get("status") == "PASS_FROZEN_QUERY_ONLY_AUDIT"
            and validation.get("frozen_manifest_sha256") == seal["frozen_sha256"]
            and validation.get("cross_split_exact_or_threshold_violations") == 0
            and validation.get("root_review_binding_verified") is True
            and validation.get("test_product_retrieval_labels_scores_executed") is False,
            "Selection isolation audit is not valid")
    require(manifest.get("status") == "QUERY_SELECTION_PACKAGE_MANIFEST", "Invalid selection manifest")
    for split in ("train", "test"):
        path = selection_dir / "frozen" / (split + ".queries.jsonl")
        require(Path(seal[split + "_queries_path"]).resolve() == path.resolve(), "Selected query path differs")
        selection_files["frozen/" + path.name] = checked_file(path, frozen["files"][path.name])
    for filename, ref in selection_files.items():
        if filename == "MANIFEST.json":
            continue
        item = manifest["files"].get(filename, {})
        require(item.get("sha256") == ref["sha256"] and item.get("bytes") == Path(ref["path"]).stat().st_size,
                "Selection manifest does not bind frozen query/isolation files")

    freeze_ref = checked_file(args.query_contracts_freeze, args.query_contracts_freeze_sha256)
    freeze = read_json(args.query_contracts_freeze)
    selected_train_sha = frozen["files"]["train.queries.jsonl"]
    require(freeze.get("status") == CONTRACT_STATUS and freeze.get("query_count") == 200
            and freeze.get("selected_train_sha256") == selected_train_sha
            and freeze.get("rubric_sha256") == RUBRIC_SHA256
            and Path(freeze["query_contracts_path"]).resolve() == Path(args.query_contracts).resolve(),
            "Query contracts are not frozen against this selected training split/rubric")
    contracts_ref = checked_file(args.query_contracts, freeze["query_contracts_sha256"])
    rubric_ref = checked_file(args.rubric, RUBRIC_SHA256)
    # Text access starts only here, after a positive gate and every frozen hash.
    training = json_rows(selection_dir / "frozen/train.queries.jsonl")
    contracts = json_rows(args.query_contracts)
    by_contract = {row["query_id"]: row for row in contracts}
    require(len(contracts) == 200 and len(by_contract) == 200, "Expected 200 unique frozen query contracts")
    queries, seen, keys = [], set(), set()
    for row in training:
        qid, source, text = row.get("query_id"), row.get("source"), row.get("text")
        require(source in SOURCES and isinstance(qid, str) and qid.startswith(f"closure-{source[:2]}-train-")
                and qid not in seen and row.get("split") == "train" and row.get("native_split") == "train"
                and row.get("native_origin", {}).get("native_split") == "train", "Invalid native training query identity")
        require(isinstance(text, str) and text.strip() and row.get("query_key") == query_key(text)
                and query_key(text) not in keys, "Changed/duplicated selected query text")
        contract = by_contract.get(qid, {})
        attrs = contract.get("required_attribute_keys")
        require(contract.get("source") == source and contract.get("split") == "train" and contract.get("query") == text
                and contract.get("query_text_sha256") == hashlib.sha256(text.encode("utf-8")).hexdigest()
                and contract.get("no_added_requirements") is True and contract.get("intent_policy") == "score_product_relevance"
                and isinstance(attrs, list) and attrs and attrs[0] == "本体" and all(isinstance(v, str) and v for v in attrs)
                and len(attrs) == len(set(attrs)), "Frozen query contract differs from selected original text")
        seen.add(qid); keys.add(query_key(text))
        queries.append({"query_id": qid, "query": text, "source": source})
    require(len(queries) == 200 and Counter(q["source"] for q in queries) == {s: 100 for s in SOURCES}
            and seen == set(by_contract), "Training query membership/count differs")
    proof = {"gate": gate_ref, "gate_evidence": gate_evidence, "selection": selection_ref,
             "selection_files": selection_files, "query_contracts_freeze": freeze_ref,
             "query_contracts": contracts_ref, "selected_train_sha256": selected_train_sha,
             "rubric": rubric_ref, "test_access": "binary_hash_only_using_preexisting_isolation_receipt",
             "test_query_text_parsed": False, "labels_generated": False}
    return sorted(queries, key=lambda q: (q["source"], q["query_id"])), proof


def code_binding():
    paths = [HERE / name for name in ("run_training_pool.py", "run_dev_grid.py", "retrieval_runtime.py")]
    paths += [OLD_CODE / name for name in ("retrieve.py", "integrity.py", "stage1.py", "model_ops.py")]
    return {str(path.resolve()): sha(path) for path in paths}


def validate_channels(channels, source):
    require(set(channels) == set(CHANNELS), "Expected all three real retrieval channels")
    for records in channels.values():
        require(len(records) == DEPTH and len({r["document_id"] for r in records}) == DEPTH,
                "Each channel must contain 300 unique full-catalog documents")
        for rank, row in enumerate(records, 1):
            require(row["rank"] == rank and row["document_id"].startswith(source + ":")
                    and math.isfinite(row["score"]), "Invalid retrieval rank/source/score")


def choose_candidates(channels, ce_pool, score_map):
    require(set(score_map) == {row["document_id"] for row in ce_pool}, "Base CE must score exactly w111 RRF300")
    ce = rerank_same_candidates(ce_pool, score_map)
    memberships = {}
    for method, records in [*((name, channels[name]) for name in CHANNELS), ("base_ce", ce)]:
        for row in records[:TOP]:
            memberships.setdefault(row["document_id"], []).append(method)
    selected = set(memberships)
    require(len(selected) <= POOL_SIZE, "Four Top10 unions cannot exceed 40")
    for row in ce_pool:
        if len(selected) == POOL_SIZE:
            break
        selected.add(row["document_id"])
    require(len(selected) == POOL_SIZE, "Cannot fill 40 unique training candidates")
    # RRF300 is the CE input. Ordering the final union uses all channel IDs so
    # a raw-channel Top10 outside that CE pool is retained, never discarded.
    all_rrf = weighted_rrf(channels, WEIGHTS["w111"], depth=3 * DEPTH)
    chosen = [row for row in all_rrf if row["document_id"] in selected]
    require(len(chosen) == POOL_SIZE, "Training candidate identity lost in union ordering")
    return [{"document_id": row["document_id"], "candidate_rank": rank,
             "rrf_rank": row["rank"], "rrf_score": row["score"],
             "selection_routes": memberships.get(row["document_id"], ["rrf_fill"])}
            for rank, row in enumerate(chosen, 1)], ce


def actual_documents(catalog, source, ids):
    """Only genuine catalog.payload fields enter a public annotation document."""
    require(ids and len(ids) == len(set(ids)), "Invalid catalog document membership")
    marks = ",".join("?" for _ in ids)
    rows = catalog.execute(f"SELECT rowid,docid,source,text,payload,source_line FROM documents WHERE docid IN ({marks})", ids)
    result = {}
    for rowid, did, native_source, text, raw_payload, source_line in rows:
        payload = json.loads(raw_payload)
        require(native_source == source and did.startswith(source + ":")
                and isinstance(text, str) and text.strip(), "Candidate is outside the requested full catalog")
        require(payload.get("document_id", did) == did and payload.get("source", source) == source
                and payload.get("text", text) == text, "Catalog payload identity/text disagrees with catalog columns")
        document = {"title": payload.get("title", ""), "brand": payload.get("brand", ""),
                    "categories": payload.get("categories", []), "seller_name": payload.get("seller_name", "")}
        require(isinstance(document["title"], str) and document["title"].strip()
                and isinstance(document["brand"], str) and isinstance(document["seller_name"], str)
                and isinstance(document["categories"], list)
                and all(isinstance(value, str) for value in document["categories"]), "Invalid genuine public document fields")
        result[did] = {"catalog_text": text, "document": document,
                       "catalog_provenance": {"catalog_rowid": rowid, "source_line": source_line,
                                              "payload_sha256": hashlib.sha256(raw_payload.encode("utf-8")).hexdigest(),
                                              "catalog_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                              "seller_name_present_in_payload": "seller_name" in payload}}
    require(set(result) == set(ids), "Missing actual full-catalog candidate")
    return result


def process_query(runtime, out, query, recall, binding_sha, base_binding, *, cache_only=False):
    def publish(path,expected,value):
        if cache_only:
            require(verify_cached(path,expected)==value,'Cached result differs from CPU replay')
        else:save_cached(path,expected,value)
    qid, source = query["query_id"], query["source"]
    channels = recall["channels"]
    validate_channels(channels, source)
    batch = read_json(recall["batch_receipt"])
    require(sha(recall["batch_receipt"]) == recall["batch_sha256"]
            and batch["result"]["channels"][qid] == channels, "Recall batch/query content changed")
    lexical_timings = batch["result"]["timings"]["lexical"][qid]
    for channel in ("bm25", "character"):
        timing = lexical_timings[channel]
        require(type(timing["matched"]) is int and 0 <= timing["matched"] <= DEPTH
                and timing["zero_score_padding"] == DEPTH - timing["matched"], "Lexical padding provenance differs")
    ce_pool = weighted_rrf(channels, WEIGHTS["w111"])
    ids = [row["document_id"] for row in ce_pool]
    require(len(ids) == DEPTH, "Base CE requires the complete w111 RRF300 pool")
    texts = runtime.document_texts(source, ids)
    pairs = [[query["query"], texts[did]] for did in ids]
    query_binding = {"pool_binding_sha256": binding_sha, "query": query}
    pool_path = out / "pools" / (qid + ".json")
    publish(pool_path, query_binding,
                {"rrf300": ce_pool, "rrf300_sha256": fingerprint(ce_pool),
                 "documents": [{"document_id": did, "catalog_text": texts[did]} for did in ids],
                 "ce_input_sha256": fingerprint(pairs), "recall_batch_sha256": recall["batch_sha256"]})
    expected_score = {**query_binding, "model": base_binding, "document_ids": ids, "input_sha256": fingerprint(pairs)}
    score_path = out / "scores" / "base" / (qid + ".json")
    if not score_path.exists():
        require(not cache_only,'CPU verification cannot infer missing scores')
        scores, timing = runtime.score_pairs(base_binding["path"], pairs)
        require(len(scores) == len(ids) and all(math.isfinite(value) for value in scores), "Invalid CE output count/value")
        # Raw outputs and nondeterministic timings have one atomic commit point.
        publish(score_path, expected_score,
                    {"scores": [{"document_id": did, "score": value} for did, value in zip(ids, scores)], "timings": timing})
    scored = verify_cached(score_path, expected_score)
    timing = scored["timings"]
    score_map = {row["document_id"]: row["score"] for row in scored["scores"]}
    require(len(scored["scores"]) == DEPTH and len(score_map) == DEPTH
            and timing["model_binding"] == base_binding and timing["input_sha256"] == fingerprint(pairs)
            and timing["max_length"] == 256 and timing["batch_size"] == 16 and timing["pair_count"] == DEPTH,
            "CE model/inference/input receipt differs")
    candidates, ce_ranking = choose_candidates(channels, ce_pool, score_map)
    documents = actual_documents(runtime.catalog, source, [r["document_id"] for r in candidates])
    ranks = {channel: {r["document_id"]: r for r in channels[channel]} for channel in CHANNELS}
    ce_ranks = {r["document_id"]: r for r in ce_ranking}
    refs = {"recall_batch": {"path": recall["batch_receipt"], "sha256": recall["batch_sha256"]},
            "rrf300_inputs": {"path": str(pool_path), "sha256": sha(pool_path)},
            "base_ce_scores": {"path": str(score_path), "sha256": sha(score_path)}}
    rows = []
    for candidate in candidates:
        did = candidate["document_id"]
        actual = documents[did]
        require(did not in texts or actual["catalog_text"] == texts[did], "Public candidate text differs from scored text")
        routes = {}
        for channel in CHANNELS:
            value = ranks[channel].get(did)
            routes[channel] = None if value is None else {**value, "zero_score_padding":
                value["rank"] > lexical_timings[channel]["matched"] if channel != "dense" else False}
        rows.append({**query, "document_id": did, "catalog_text": actual["catalog_text"], "document": actual["document"],
                     "provenance": {**candidate, **actual["catalog_provenance"], "channels": routes,
                                    "base_ce": ce_ranks.get(did), "receipts": refs,
                                    "pool_binding_sha256": binding_sha, "labels_generated": False}})
    candidate_path = out / "candidates" / (qid + ".json")
    publish(candidate_path, query_binding, {"rows": rows, "ce_ranking": ce_ranking, "receipts": refs})
    return rows, [pool_path, score_path, candidate_path, Path(recall["batch_receipt"]),
                  out / "recall" / source / (qid + ".json")]


def verify_pool_provenance(path,expected_sha256):
    """Read-only CPU replay of every candidate from bound scores and real catalog."""
    from types import SimpleNamespace
    from retrieval_runtime import readonly,verify_asset_audit
    path=Path(path).resolve();out=path.parent
    checked_file(path,expected_sha256);receipt=read_json(path)
    require(receipt.get('status')=='TRAINING_CANDIDATE_POOL_READY_UNJUDGED'
            and receipt.get('candidate_count')==8000 and receipt.get('query_count')==200,
            'Expected complete 200-query training pool')
    checked_file(receipt['binding_path'],receipt['binding_sha256'])
    binding=read_json(receipt['binding_path']);auth=binding['authorization']
    args=SimpleNamespace(gate=auth['gate']['path'],gate_sha256=auth['gate']['sha256'],
        selection_seal=auth['selection']['path'],selection_seal_sha256=auth['selection']['sha256'],
        query_contracts=auth['query_contracts']['path'],query_contracts_freeze=auth['query_contracts_freeze']['path'],
        query_contracts_freeze_sha256=auth['query_contracts_freeze']['sha256'],rubric=auth['rubric']['path'])
    queries,authorization=authorize_inputs(args)
    require(authorization==auth and fingerprint(queries)==binding['query_rows_sha256'],'Pool authorization/query binding changed')
    for code_path,wanted in binding['code'].items():checked_file(code_path,wanted)
    for item in receipt['files']:
        require(Path(item['path']).resolve().is_relative_to(out),'Pool receipt escapes output')
        checked_file(item['path'],item['sha256'])
    checked_file(receipt['candidate_rows_path'],receipt['candidate_rows_sha256'])
    checked_file(receipt['asset_binding_path'],receipt['asset_binding_sha256'])
    require(read_json(receipt['asset_binding_path'])==binding['asset']==verify_asset_audit(), 'Pool catalog/index audit differs')
    require(model_binding(binding['base_ce']['path'])==binding['base_ce']==receipt['base_ce'],'Base CE model changed')
    require(receipt['query_contracts_sha256']==auth['query_contracts']['sha256']
            and receipt['selected_train_sha256']==auth['selected_train_sha256'],'Exported selected-query binding differs')
    class ReadOnlyRuntime:
        def __init__(self):self.catalog=readonly(Path(binding['root'])/'catalog.sqlite')
        def document_texts(self,source,ids):
            return {did:row['catalog_text'] for did,row in actual_documents(self.catalog,source,ids).items()}
        def score_pairs(self,*args):raise ValueError('CPU verifier cannot invoke a model')
    runtime=ReadOnlyRuntime();replayed=[];current=fingerprint(binding);official={q['query_id']:q for q in queries}
    try:
        for query in queries:
            qid=query['query_id'];source=query['source']
            recall=verify_cached(out/'recall'/source/(qid+'.json'),{'grid_sha256':current,'query':query})
            batch=read_json(recall['batch_receipt']);bq=batch['binding']['queries']
            require(bq and len({q['query_id'] for q in bq})==len(bq)
                    and all(official.get(q['query_id'])==q and q['source']==source for q in bq),'Recall contains foreign queries')
            verify_cached(recall['batch_receipt'],{'grid_sha256':current,'source':source,'queries':bq})
            values,_=process_query(runtime,out,query,recall,current,binding['base_ce'],cache_only=True)
            replayed.extend(values)
        require(replayed==json_rows(receipt['candidate_rows_path']),'Export differs from original-channel/CE/catalog replay')
    finally:runtime.catalog.close()
    return {'status':'CPU_POOL_PROVENANCE_PASS','query_count':len(queries),'pairs':len(replayed),
            'pool_complete_sha256':expected_sha256,'model_calls':0,'files_written':0}


def execute(runtime, out, queries, binding, progress=None):
    """Serial model work; a committed batch or CE score is never inferred twice."""
    progress = progress or (lambda _: None)
    require(len(queries) == 200 and Counter(q["source"] for q in queries) == {s: 100 for s in SOURCES},
            "Full training pool requires exactly 100 queries per source")
    current = fingerprint(binding)
    write_once(out / "binding.json", binding)
    write_once(out / "asset-binding.json", runtime.asset)
    all_recalls = {}
    # Dense source scans finish before serial base-CE scoring starts.
    for source in SOURCES:
        source_queries = [q for q in queries if q["source"] == source]
        all_recalls.update(source_recalls(runtime, out, source, source_queries, current, allow_inference=True))
    all_rows, files = [], {out / "binding.json", out / "asset-binding.json"}
    for query in queries:
        rows, paths = process_query(runtime, out, query, all_recalls[query["query_id"]], current, binding["base_ce"])
        all_rows.extend(rows); files.update(paths)
        progress({"stage": "training_query_pool_committed", "query_id": query["query_id"], "candidates": len(rows)})
    require(len(all_rows) == 8000 and len({(r["query_id"], r["document_id"]) for r in all_rows}) == 8000,
            "The complete training pool must contain 8000 unique query/document pairs")
    rows_sha = write_once(out / "candidate-rows.jsonl", all_rows, jsonl=True)
    inputs = binding["authorization"]
    report = {"status": "TRAINING_CANDIDATE_POOL_READY_UNJUDGED", "query_count": 200,
              "source_query_counts": {s: 100 for s in SOURCES}, "candidate_count": len(all_rows),
              "candidate_rows_path": str(out / "candidate-rows.jsonl"), "candidate_rows_sha256": rows_sha,
              "query_contracts_path": inputs["query_contracts"]["path"], "query_contracts_sha256": inputs["query_contracts"]["sha256"],
              "selected_train_path": inputs["selection_files"]["frozen/train.queries.jsonl"]["path"],
              "selected_train_sha256": inputs["selected_train_sha256"], "gate": inputs["gate"],
              "selection": inputs["selection"], "query_contracts_freeze": inputs["query_contracts_freeze"],
              "query_contracts_frozen_path": inputs["query_contracts_freeze"]["path"],
              "query_contracts_frozen_sha256": inputs["query_contracts_freeze"]["sha256"],
              "binding_path": str(out / "binding.json"), "binding_sha256": sha(out / "binding.json"),
              "asset_binding_path": str(out / "asset-binding.json"), "asset_binding_sha256": sha(out / "asset-binding.json"),
              "base_ce": binding["base_ce"], "files": [{"path": str(path), "sha256": sha(path)} for path in sorted(files)],
              "test_query_text_parsed": False, "test_access": inputs["test_access"],
              "labels_generated": False, "training_started": False,
              "latency_notice": "Batch wall timings are not online p95; OS page cache is uncontrolled."}
    write_once(out / "POOL_COMPLETE.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--gate-sha256", required=True)
    parser.add_argument("--selection-seal", type=Path, default=DATA / "selection/SEALED.json")
    parser.add_argument("--selection-seal-sha256", required=True)
    parser.add_argument("--query-contracts", type=Path, default=DATA / "training-preparation/query-contracts-frozen.jsonl")
    parser.add_argument("--query-contracts-freeze", type=Path, default=DATA / "training-preparation/QUERY_CONTRACTS_FROZEN.json")
    parser.add_argument("--query-contracts-freeze-sha256", required=True)
    parser.add_argument("--rubric", type=Path, default=RUBRIC)
    parser.add_argument("--root", type=Path, default=OLD_ROOT)
    parser.add_argument("--asset-audit", type=Path, default=ASSET_AUDIT)
    parser.add_argument("--output", type=Path, default=DATA / "training-pool")
    parser.add_argument("--dry-run", action="store_true", help="Verify gate and selected query/contract seals; no runtime, GPU or output")
    args = parser.parse_args(argv)
    queries, authorization = authorize_inputs(args)
    root, out = args.root.resolve(), args.output.resolve()
    for protected in (root, HERE, Path(args.selection_seal).resolve().parent, Path(args.query_contracts).resolve().parent):
        require(out != protected and not out.is_relative_to(protected) and not protected.is_relative_to(out),
                "Training-pool output must be disjoint from source, historical assets and frozen inputs")
    if args.dry_run:
        print(json.dumps({"status": "TRAINING_POOL_INPUTS_AUTHORIZED", "queries": len(queries),
                          "authorization_sha256": fingerprint(authorization), "gpu_called": False, "output_written": False}))
        return 0
    runtime = RetrievalRuntime(root, asset_audit=args.asset_audit,
                               progress=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
    try:
        base = model_binding(runtime.models["reranker"]["path"])
        binding = {"version": "search-closure-training-pool-v1", "authorization": authorization,
                   "query_rows_sha256": fingerprint(queries), "root": str(root), "code": code_binding(),
                   "asset": runtime.asset, "base_ce": base, "algorithm": {
                       "channels": list(CHANNELS), "depth": DEPTH, "rrf_k": 60, "weights": list(WEIGHTS["w111"]),
                       "dense_scan_block_size": BLOCK_SIZE, "ce_input": "w111 RRF300 catalog.documents.text",
                       "ce_max_length": 256, "ce_batch_size": 16, "top_per_channel_and_base_ce": TOP,
                       "candidate_count_per_query": POOL_SIZE, "fill": "w111 RRF300", "final_order": "w111 RRF over full channel union",
                       "lexical_zero_score_padding": "unchanged v1, retained and individually identified by rank>matched",
                       "query_rewrite": False, "labels": "UNJUDGED; no grades generated"}}
        report = execute(runtime, out, queries, binding, progress=lambda row: print(json.dumps(row), flush=True))
        print(json.dumps({"status": report["status"], "queries": 200, "candidates": 8000, "output": str(out)}))
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
