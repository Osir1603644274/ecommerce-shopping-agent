"""Generate the frozen, bounded development retrieval grid. Never reads qrels."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from retrieval_runtime import (
    ASSET_AUDIT, CHANNELS, DEPTH, OLD_CODE, OLD_ROOT, WEIGHTS, RetrievalRuntime,
    fingerprint, model_binding, read_json, rerank_same_candidates,
    require_test_authorization, sha, weighted_rrf, write_once,
)

QUERIES = Path("D:/agent-datasets/search-stage1-dev-revision-v5/frozen/queries.jsonl")
QUERY_SHA256 = "462beddd9fe013710f98c6c9d8a6d83df621660852c7adbe3c6a94b10f78588c"
OUTPUT = Path("D:/agent-datasets/search-closure-v1/development-grid")


def load_development_queries(path=QUERIES, *, expected_sha256=QUERY_SHA256):
    if sha(path) != expected_sha256: raise ValueError("Only the frozen 40 development queries may enter this CLI")
    values = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        row = json.loads(line)
        qid = row["query_id"]
        source = "kuaisearch" if qid.startswith("s1-ku-dev-") else "multicpr" if qid.startswith("s1-mu-dev-") else None
        if source is None or not isinstance(row["query"], str) or not row["query"].strip(): raise ValueError("Invalid dev query/source")
        values.append({"query_id": qid, "query": row["query"], "source": source})
    if len(values) != 40 or len({q["query_id"] for q in values}) != 40:
        raise ValueError("Expected exactly 40 unique dev queries")
    if any(sum(q["source"] == source for q in values) != 20 for source in ("kuaisearch", "multicpr")):
        raise ValueError("Expected 20 queries per source")
    return sorted(values, key=lambda q: (q["source"], q["query_id"]))


def configured_models(root, new_models=None):
    models = {"base": Path(read_json(root / "models/manifest.json")["reranker"]["path"])}
    models.update({f"epoch{epoch}": root / "training/run-lora-v1" / f"epoch-{epoch}" for epoch in (1, 2, 3)})
    if new_models is not None:
        extra = read_json(new_models)
        if set(extra) != {"new_epoch1", "new_epoch2", "new_epoch3"}:
            raise ValueError("New model config must provide exactly new_epoch1/new_epoch2/new_epoch3 paths")
        for key, value in extra.items():
            if not isinstance(value, str) or not Path(value).is_absolute(): raise ValueError("New model paths must be explicit absolute paths")
            models[key] = Path(value)
    return models


def verify_cached(path, expected_binding):
    value = read_json(path)
    if value.get("binding") != expected_binding: raise ValueError(f"Cached inputs/model differ: {path}")
    if fingerprint(value["result"]) != value.get("result_sha256"):
        raise ValueError(f"Cached output content differs: {path}")
    return value["result"]


def save_cached(path, binding, result):
    write_once(path, {"binding": binding, "result": result, "result_sha256": fingerprint(result)})


def source_recalls(runtime, out, source, queries, grid_sha256, *, allow_inference):
    """Commit channels and nondeterministic timings together, then derive caches.

    A crash during per-query publication is recovered from the committed batch,
    even when the remaining query IDs no longer equal the original batch ID.
    """
    if not queries: return {}
    official = {query["query_id"]: query for query in queries}
    batch_directory = out / "recall" / "batches"

    def distribute(path, batch_queries):
        expected = {"grid_sha256": grid_sha256, "source": source, "queries": batch_queries}
        if (not batch_queries or len({q["query_id"] for q in batch_queries}) != len(batch_queries)
                or any(official.get(q["query_id"]) != q for q in batch_queries)):
            raise ValueError("Committed batch contains a foreign or duplicated query")
        batch = verify_cached(path, expected)
        if (not {"channels", "timings"} <= set(batch) or set(batch) - {"channels", "timings", "import_provenance"}
                or set(batch["channels"]) != {q["query_id"] for q in batch_queries}):
            raise ValueError("Committed batch channel membership differs")
        batch_sha = sha(path)
        for query in batch_queries:
            result = {"channels": batch["channels"][query["query_id"]],
                      "batch_receipt": str(path), "batch_sha256": batch_sha}
            save_cached(out / "recall" / source / (query["query_id"] + ".json"),
                        {"grid_sha256": grid_sha256, "query": query}, result)

    # Recover every committed batch before choosing pending work. This covers
    # both the receipt-before-first-cache and partially-distributed windows.
    for path in sorted(batch_directory.glob(source + "-*.json")):
        batch_binding = read_json(path).get("binding", {})
        distribute(path, batch_binding.get("queries", []))
    result, pending = {}, []
    for query in queries:
        path = out / "recall" / source / (query["query_id"] + ".json")
        if path.exists():
            cached = verify_cached(path, {"grid_sha256": grid_sha256, "query": query})
            if sha(cached["batch_receipt"]) != cached["batch_sha256"]:
                raise ValueError("Committed recall batch changed")
            result[query["query_id"]] = cached
        else:
            pending.append(query)
    if pending:
        if not allow_inference: raise ValueError("Score phase requires committed recall for every requested query")
        batch = runtime.retrieve_batch(source, pending)
        batch_id = fingerprint([q["query_id"] for q in pending])[:20]
        batch_path = batch_directory / (source + "-" + batch_id + ".json")
        # One atomic publication is the commit point. There is no earlier
        # immutable timings-only artifact that can conflict with a retry.
        save_cached(batch_path, {"grid_sha256": grid_sha256, "source": source, "queries": pending},
                    {"channels": batch["channels"], "timings": batch["timings"]})
        distribute(batch_path, pending)
        for query in pending:
            path = out / "recall" / source / (query["query_id"] + ".json")
            result[query["query_id"]] = verify_cached(path, {"grid_sha256": grid_sha256, "query": query})
    return result


def make_pools(channels):
    pools = {name: weighted_rrf(channels, weights) for name, weights in WEIGHTS.items()}
    union = sorted({row["document_id"] for pool in pools.values() for row in pool})
    return pools, union


def ranking_row(method, query, records):
    ranking = [row["document_id"] for row in records]
    return {"method": method, "query_id": query["query_id"], "source": query["source"],
            "ranking": ranking, "ranking_sha256": fingerprint(ranking)}


def import_completed_grid(source_dir, out, binding, queries, runtime):
    """Validate every old receipt before copying compatible results, never infer."""
    source_dir = Path(source_dir).resolve()
    if source_dir == out or source_dir.is_relative_to(out) or out.is_relative_to(source_dir):
        raise ValueError("Reuse grid and destination must be disjoint")
    complete_path = source_dir / "COMPLETE.json"
    complete = read_json(complete_path)
    old_binding = read_json(source_dir / "binding.json")
    if (complete.get("status") != "COMPLETE" or complete["binding_sha256"] != sha(source_dir / "binding.json")
            or complete["rankings_sha256"] != sha(source_dir / "rankings.jsonl")
            or complete["scores_sha256"] != sha(source_dir / "scores.jsonl")):
        raise ValueError("Reuse grid is not a verified completed run")
    # Model sets may grow; every other input, algorithm and actual code byte
    # must match. An old interrupted/different-code run cannot be imported.
    if {k: v for k, v in old_binding.items() if k != "models"} != {k: v for k, v in binding.items() if k != "models"}:
        raise ValueError("Reuse grid query/corpus/profile/text/code binding differs")
    common_models = sorted(set(old_binding["models"]) & set(binding["models"]))
    if not common_models or any(old_binding["models"][name] != binding["models"][name] for name in common_models):
        raise ValueError("Reuse grid model/input inference binding differs")
    if read_json(source_dir / "asset-binding.json") != runtime.asset:
        raise ValueError("Reuse grid asset binding differs from current verified assets")
    indexed = {}
    for item in complete["files"]:
        path = Path(item["path"]).resolve()
        if not path.is_relative_to(source_dir) or path in indexed or sha(path) != item["sha256"]:
            raise ValueError("Reuse grid file manifest was altered or escaped its root")
        indexed[path] = item["sha256"]
    old_sha, new_sha = fingerprint(old_binding), fingerprint(binding)
    provenance = {"source_grid": str(source_dir), "source_complete_sha256": sha(complete_path),
                  "source_binding_sha256": sha(source_dir / "binding.json")}
    prepared = []
    batches = {}
    imported_scores = 0
    official_queries = {query["query_id"]: query for query in queries}
    for query in queries:
        qid = query["query_id"]
        old_query_binding = {"grid_sha256": old_sha, "query": query}
        recall_path = source_dir / "recall" / query["source"] / (qid + ".json")
        pool_path = source_dir / "pools" / (qid + ".json")
        if recall_path.resolve() not in indexed or pool_path.resolve() not in indexed:
            raise ValueError("Reuse manifest omits a query recall/pool")
        recall = verify_cached(recall_path, old_query_binding)
        pool = verify_cached(pool_path, old_query_binding)
        batch_path = Path(recall["batch_receipt"]).resolve()
        if batch_path not in indexed or sha(batch_path) != recall["batch_sha256"]:
            raise ValueError("Reuse recall batch is not completely bound")
        raw_batch = read_json(batch_path)
        batch_queries = raw_batch["binding"]["queries"]
        if (len({row["query_id"] for row in batch_queries}) != len(batch_queries)
                or any(official_queries.get(row["query_id"]) != row or row["source"] != query["source"] for row in batch_queries)):
            raise ValueError("Reuse batch contains a foreign query/source")
        expected_batch_binding = {"grid_sha256": old_sha, "source": query["source"], "queries": batch_queries}
        batch = verify_cached(batch_path, expected_batch_binding)
        if query not in batch_queries or recall["channels"] != batch["channels"][qid]:
            raise ValueError("Reuse query channels differ from committed batch")
        if batch_path not in batches:
            clean_batch = {"channels": batch["channels"], "timings": batch["timings"],
                           "import_provenance": {**provenance, "source_path": str(batch_path), "source_sha256": sha(batch_path)}}
            target = out / "recall" / "batches" / batch_path.name
            batches[batch_path] = (target, {"grid_sha256": new_sha, "source": query["source"], "queries": batch_queries}, clean_batch)
        expected_pools, union = make_pools(recall["channels"])
        texts = runtime.document_texts(query["source"], union)
        expected_pool = {"query": query, "pools": expected_pools,
                         "pool_sha256": {name: fingerprint(value) for name, value in expected_pools.items()},
                         "union": union, "union_sha256": fingerprint(union),
                         "input_text_sha256": {did: hashlib.sha256(texts[did].encode()).hexdigest() for did in union}}
        if {k: v for k, v in pool.items() if k != "import_provenance"} != expected_pool:
            raise ValueError("Reuse pool or actual catalog text differs")
        prepared.append((out / "pools" / (qid + ".json"), {"grid_sha256": new_sha, "query": query},
                         {**expected_pool, "import_provenance": {**provenance, "source_path": str(pool_path), "source_sha256": sha(pool_path)}}))
        pairs = [[query["query"], texts[did]] for did in union]
        # Check every source score file, even models not requested for import.
        for name, old_model in old_binding["models"].items():
            path = source_dir / "scores" / name / (qid + ".json")
            if path.resolve() not in indexed: raise ValueError("Reuse manifest omits a model/query score")
            expected = {"grid_sha256": old_sha, "query": query, "model": old_model,
                        "document_ids": union, "input_sha256": fingerprint(pairs)}
            scored = verify_cached(path, expected)
            if (len(scored["scores"]) != len(union) or {row["document_id"] for row in scored["scores"]} != set(union)
                    or scored["timings"]["model_binding"] != old_model):
                raise ValueError("Reuse score candidate/model set differs")
            if name in common_models:
                prepared.append((out / "scores" / name / (qid + ".json"), {**expected, "grid_sha256": new_sha},
                                 {"scores": scored["scores"], "timings": scored["timings"],
                                  "import_provenance": {**provenance, "source_path": str(path), "source_sha256": sha(path)}}))
                imported_scores += 1
    # No target import is published until every old file, query, model and
    # actual catalog text has passed the checks above.
    for path, expected, result in [*batches.values(), *prepared]:
        save_cached(path, expected, result)
    import_record = {**provenance, "target_grid_sha256": new_sha, "models": common_models,
                     "query_count": len(queries), "score_files_imported": imported_scores,
                     "recall_batches_imported": len(batches), "inference_performed": False}
    write_once(out / "imports" / (provenance["source_complete_sha256"] + ".json"), import_record)
    return import_record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("retrieve", "score", "all"), default="all")
    parser.add_argument("--source", choices=("both", "kuaisearch", "multicpr"), default="both")
    parser.add_argument("--split", choices=("dev", "test"), default="dev")
    parser.add_argument("--root", type=Path, default=OLD_ROOT)
    parser.add_argument("--asset-audit", type=Path, default=ASSET_AUDIT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--new-models", type=Path)
    parser.add_argument("--reuse-grid", type=Path, help="Verify and import compatible completed recall/pools/model scores; never imports an interrupted run")
    parser.add_argument("--dry-run", action="store_true", help="Bind/validate inputs and models only; no GPU or output writes")
    args = parser.parse_args(argv)
    if args.split != "dev": require_test_authorization()
    root, out = args.root.resolve(), args.output.resolve()
    if out == root or out.is_relative_to(root) or root.is_relative_to(out):
        raise ValueError("New outputs must be disjoint from the historical data root")
    queries = load_development_queries()
    models = configured_models(root, args.new_models)
    bindings = {name: model_binding(path) for name, path in models.items()}
    code = {str(path): sha(path) for path in [Path(__file__).resolve(), Path(__file__).with_name("retrieval_runtime.py"),
                                          OLD_CODE / "retrieve.py", OLD_CODE / "integrity.py", OLD_CODE / "stage1.py"]}
    binding = {"version": "search-closure-dev-grid-v1", "queries_sha256": sha(QUERIES),
               "root": str(root), "asset_audit_sha256": sha(args.asset_audit), "code": code,
               "models": bindings, "depth": DEPTH, "rrf_k": 60, "weights": WEIGHTS,
               "text": "catalog.documents.text", "old_dev_scores_reused": False,
               "test_access": False, "labels_read": False}
    # Round-trip tuples to their immutable JSON representation for all resumes.
    binding = json.loads(json.dumps(binding))
    if args.dry_run:
        print(json.dumps({"status": "DRY_RUN_INPUTS_BOUND", "queries": len(queries), "models": list(models),
                          "binding_sha256": fingerprint(binding), "gpu_called": False}, ensure_ascii=False))
        return 0
    write_once(out / "binding.json", binding)
    selected = [q for q in queries if args.source == "both" or q["source"] == args.source]
    runtime = RetrievalRuntime(root, asset_audit=args.asset_audit,
                               progress=lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
    try:
        write_once(out / "asset-binding.json", runtime.asset)
        if args.reuse_grid is not None:
            print(json.dumps({"stage": "grid_import", **import_completed_grid(args.reuse_grid, out, binding, queries, runtime)}, ensure_ascii=False))
        current_binding = fingerprint(binding)
        all_recalls = {}
        for source in ("kuaisearch", "multicpr"):
            source_queries = [q for q in selected if q["source"] == source]
            all_recalls.update(source_recalls(runtime, out, source, source_queries, current_binding,
                                             allow_inference=args.phase != "score"))
        pools_by_query, texts_by_query, unions = {}, {}, {}
        for query in selected:
            qid = query["query_id"]
            pools, union = make_pools(all_recalls[qid]["channels"])
            texts = runtime.document_texts(query["source"], union)
            record = {"query": query, "pools": pools, "pool_sha256": {name: fingerprint(pool) for name, pool in pools.items()},
                      "union": union, "union_sha256": fingerprint(union),
                      "input_text_sha256": {did: hashlib.sha256(texts[did].encode()).hexdigest() for did in union}}
            pool_path = out / "pools" / (qid + ".json")
            pool_binding = {"grid_sha256": current_binding, "query": query}
            if pool_path.exists():
                cached_pool = verify_cached(pool_path, pool_binding)
                if {k: v for k, v in cached_pool.items() if k != "import_provenance"} != record:
                    raise ValueError("Imported/cached pool differs from current channels/text")
            else:
                save_cached(pool_path, pool_binding, record)
            pools_by_query[qid], texts_by_query[qid], unions[qid] = pools, texts, union
        if args.phase in {"score", "all"}:
            for model_name, model_path in models.items():
                for query in selected:
                    qid = query["query_id"]; union = unions[qid]; texts = texts_by_query[qid]
                    pairs = [[query["query"], texts[did]] for did in union]
                    expected = {"grid_sha256": current_binding, "query": query,
                                "model": bindings[model_name], "document_ids": union,
                                "input_sha256": fingerprint(pairs)}
                    path = out / "scores" / model_name / (qid + ".json")
                    if path.exists(): verify_cached(path, expected); continue
                    scores, timing = runtime.score_pairs(model_path, pairs)
                    if timing["model_binding"] != bindings[model_name]: raise ValueError("Model binding changed during scoring")
                    values = [{"document_id": did, "score": score} for did, score in zip(union, scores)]
                    save_cached(path, expected, {"scores": values, "timings": timing})
                    print(json.dumps({"stage": "ce_scored", "model": model_name, "query_id": qid, "pairs": len(pairs)}), flush=True)
        # A subset/recall-only command does not announce full-grid completion.
        expected_files = [out / "scores" / model / (q["query_id"] + ".json") for model in models for q in queries]
        if args.phase == "retrieve" or not all(path.exists() for path in expected_files):
            print(json.dumps({"status": "PARTIAL_PHASE_COMPLETE", "phase": args.phase, "source": args.source})); return 0
        rankings, scores_export, manifests = [], [], []
        for query in queries:
            qid = query["query_id"]
            recall_path = out / "recall" / query["source"] / (qid + ".json")
            recall = verify_cached(recall_path, {"grid_sha256": current_binding, "query": query})
            if sha(recall["batch_receipt"]) != recall["batch_sha256"]: raise ValueError("Recall batch receipt changed")
            pool_path = out / "pools" / (qid + ".json")
            pool_record = verify_cached(pool_path, {"grid_sha256": current_binding, "query": query})
            for channel in CHANNELS: rankings.append(ranking_row(channel, query, recall["channels"][channel]))
            for profile, pool in pool_record["pools"].items():
                rankings.append(ranking_row(profile + "/none", query, pool))
            texts = runtime.document_texts(query["source"], pool_record["union"])
            pairs = [[query["query"], texts[did]] for did in pool_record["union"]]
            for model_name in models:
                score_path = out / "scores" / model_name / (qid + ".json")
                expected = {"grid_sha256": current_binding, "query": query, "model": bindings[model_name],
                            "document_ids": pool_record["union"], "input_sha256": fingerprint(pairs)}
                scored = verify_cached(score_path, expected)
                score_map = {row["document_id"]: row["score"] for row in scored["scores"]}
                if len(score_map) != len(pool_record["union"]) or set(score_map) != set(pool_record["union"]):
                    raise ValueError("CE score set differs from union")
                for row in scored["scores"]:
                    scores_export.append({"model": model_name, "query_id": qid, "source": query["source"], **row})
                for profile, pool in pool_record["pools"].items():
                    rankings.append(ranking_row(profile + "/" + model_name, query, rerank_same_candidates(pool, score_map)))
                manifests.append({"path": str(score_path), "sha256": sha(score_path)})
            manifests.extend({"path": str(path), "sha256": sha(path)} for path in (recall_path, pool_path, Path(recall["batch_receipt"])))
        manifests.extend({"path": str(path), "sha256": sha(path)} for path in sorted((out / "imports").glob("*.json")))
        manifests = sorted({row["path"]: row for row in manifests}.values(), key=lambda row: row["path"])
        rankings_sha = write_once(out / "rankings.jsonl", rankings, jsonl=True)
        scores_sha = write_once(out / "scores.jsonl", scores_export, jsonl=True)
        receipt = {"status": "COMPLETE", "binding_sha256": sha(out / "binding.json"),
                   "rankings_sha256": rankings_sha, "scores_sha256": scores_sha, "files": manifests,
                   "query_count": len(queries), "methods": sorted({r["method"] for r in rankings}),
                   "asset_validation_seconds_this_process": runtime.asset_validation_seconds,
                   "test_access": False, "labels_read": False, "quality_metrics_computed": False,
                   "latency_notice": "Source batch wall timings are not online p95. first_call_process does not flush OS page cache."}
        if (out / "COMPLETE.json").exists():
            previous = read_json(out / "COMPLETE.json")
            # Verification duration is a current process observation, not a new output.
            receipt["asset_validation_seconds_this_process"] = previous["asset_validation_seconds_this_process"]
        write_once(out / "COMPLETE.json", receipt)
        print(json.dumps({"status": "COMPLETE", "queries": len(queries), "methods": len(receipt["methods"]), "output": str(out)}))
        return 0
    finally:
        runtime.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
