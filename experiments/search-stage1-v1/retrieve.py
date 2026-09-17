"""Full-corpus retrieval adapters feeding the shared, label-free PoolService."""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from retrieval_judgment_pool_core import PoolService
from stage1 import clean, digest, dump, key, log
from integrity import checked_file,verify_model,verify_model_path,verify_checkpoint,seal_lexical

QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
DEPTH = 300


def rows(path):
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def frozen_jsonl(path, values):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = "".join(json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for v in values).encode()
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"Refusing to change frozen artifact: {path}")
    else:
        with path.open("xb") as f: f.write(raw)


def readonly(path):
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def lexical_runs(root, source, queries):
    import jieba
    jieba.setLogLevel(30)
    if jieba.__version__!="0.42.1":raise ValueError("Query tokenizer version differs from the frozen lexical profile")
    jieba.initialize()
    directory = root / "indexes" / source
    if not (directory / "lexical.manifest.json").exists():
        raise ValueError("Full lexical index is not complete")
    manifest = json.loads((directory / "lexical.manifest.json").read_text(encoding="utf-8"))
    if manifest["normalization_sha256"] != digest(root / "normalization-report.json"):
        raise ValueError("Lexical corpus binding changed")
    seal_lexical(root,source,manifest["documents"])
    db = readonly(directory / "lexical.sqlite")
    catalog = readonly(root / "catalog.sqlite")
    run = {"lexical": [], "character": []}
    timings = []
    for query in queries:
        normalized = clean(query["text"]).casefold()
        words = [t for t in jieba.cut_for_search(normalized, HMM=False) if re.search(r"[\w\u3400-\u9fff]", t)]
        compact = key(normalized)
        grams = [compact[i:i+2] for i in range(len(compact)-1)] or [compact]
        for family, table, tokens in (("lexical", "words", words), ("character", "chars", grams)):
            tokens = sorted(set(t for t in tokens if t))
            expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
            started = time.perf_counter()
            found = db.execute(f"SELECT rowid,bm25({table}) AS score FROM {table} WHERE {table} MATCH ? ORDER BY score,rowid LIMIT ?", (expression, DEPTH)).fetchall() if expression else []
            elapsed = time.perf_counter()-started
            # No-match queries still have a well-defined all-zero BM25 ranking.
            # Padding is zero-score, never invented relevance or a positive label.
            used = {r[0] for r in found}
            matched = len(found)
            if len(found) < DEPTH:
                for rowid, in catalog.execute("SELECT rowid FROM documents WHERE source=? ORDER BY rowid LIMIT ?", (source, DEPTH*2)):
                    if rowid not in used:
                        found.append((rowid, 0.0))
                    if len(found) == DEPTH: break
            for rank, (rowid, score) in enumerate(found, 1):
                docid, = catalog.execute("SELECT docid FROM documents WHERE rowid=?", (rowid,)).fetchone()
                run[family].append({"query_id": query["query_id"], "document_id": docid, "rank": rank, "score": -float(score)})
            timings.append({"query_id": query["query_id"], "method": family, "search_seconds": elapsed, "matching_top_count": matched, "zero_score_padding": DEPTH-matched})
        log("lexical_query", source=source, query_id=query["query_id"])
    db.close(); catalog.close()
    return run, timings


def stable_topk(scores, ids, k):
    """Exact score-descending, numeric rowid-ascending top-k including ties."""
    import numpy as np
    if len(scores) > k:
        cutoff = np.partition(scores, len(scores)-k)[len(scores)-k]
        indices = np.flatnonzero(scores >= cutoff)
    else:
        indices = np.arange(len(scores))
    order = np.lexsort((ids[indices], -scores[indices]))[:k]
    indices = indices[order]
    return scores[indices], ids[indices]


def dense_run(root, source, queries):
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(2)
    directory = root / "indexes" / source
    manifest_path = directory / "dense.manifest.json"
    if not manifest_path.exists(): raise ValueError("Full dense index is not complete")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    binding = manifest["binding"]
    if not manifest["complete"] or binding["normalization_sha256"] != digest(root / "normalization-report.json"):
        raise ValueError("Dense corpus incomplete or changed")
    info = json.loads((root / "models/manifest.json").read_text(encoding="utf-8"))["dense"]
    if info["revision"] != binding["revision"]: raise ValueError("Dense model revision changed")
    verify_model(info)
    checked_file(directory/"embeddings.npy",manifest["vectors_sha256"],manifest["vector_bytes"])
    tokenizer = AutoTokenizer.from_pretrained(info["path"], local_files_only=True)
    model = AutoModel.from_pretrained(info["path"], local_files_only=True, torch_dtype=torch.float16).cuda().eval()
    started = time.perf_counter()
    with torch.inference_mode():
        inputs = tokenizer([QUERY_INSTRUCTION+q["text"] for q in queries], padding=True, truncation=True, max_length=binding["max_length"], return_tensors="pt").to("cuda")
        query_vectors = torch.nn.functional.normalize(model(**inputs).last_hidden_state[:,0].float(), p=2, dim=1)
    del model, tokenizer, inputs
    gc.collect(); torch.cuda.empty_cache()
    # Float32 multiplication over the stored float16 document vectors; normalize
    # after decoding to correct their storage rounding before cosine comparison.
    vectors = np.load(directory / "embeddings.npy", mmap_mode="r")
    best_scores = [np.empty(0, dtype=np.float32) for _ in queries]
    best_ids = [np.empty(0, dtype=np.int64) for _ in queries]
    with torch.inference_mode():
        for start in range(0, binding["documents"], 16384):
            block = torch.from_numpy(np.array(vectors[start:start+16384], dtype=np.float32)).cuda()
            block = torch.nn.functional.normalize(block, p=2, dim=1)
            similarities = (query_vectors @ block.T).cpu().numpy()
            ids = np.arange(start+binding["first_rowid"], start+binding["first_rowid"]+len(block), dtype=np.int64)
            for i, scores in enumerate(similarities):
                local_scores, local_ids = stable_topk(scores, ids, DEPTH)
                best_scores[i], best_ids[i] = stable_topk(np.concatenate((best_scores[i], local_scores)), np.concatenate((best_ids[i], local_ids)), DEPTH)
            if start % (16384*20) == 0: log("dense_search_progress", source=source, documents=min(start+len(block), binding["documents"]))
    torch.cuda.synchronize()
    elapsed = time.perf_counter()-started
    catalog = readonly(root / "catalog.sqlite")
    records = []
    for query, scores, ids in zip(queries, best_scores, best_ids):
        for rank, (score, rowid) in enumerate(zip(scores, ids), 1):
            docid, = catalog.execute("SELECT docid FROM documents WHERE rowid=?", (int(rowid),)).fetchone()
            records.append({"query_id": query["query_id"], "document_id": docid, "rank": rank, "score": float(score)})
    catalog.close()
    return records, {"query_count": len(queries), "batch_wall_seconds": elapsed,
                     "amortized_seconds_per_query": elapsed/len(queries), "latency_scope": "Offline full-corpus batch search; not online p95 latency",
                     "query_instruction": QUERY_INSTRUCTION, "index_manifest_sha256": digest(manifest_path)}


def retrieve(root, source):
    queries = [q for q in rows(root / "queries.selected.jsonl") if q["source"] == source]
    directory = root / "retrieval" / source
    directory.mkdir(parents=True, exist_ok=True)
    binding = {"source": source, "depth": DEPTH, "queries_sha256": digest(root / "queries.selected.jsonl"),
               "normalization_sha256": digest(root / "normalization-report.json"), "query_instruction": QUERY_INSTRUCTION,
               "dense_revision": json.loads((root / "models/manifest.json").read_text(encoding="utf-8-sig"))["dense"]["revision"]}
    binding_path = directory / "binding.json"
    if binding_path.exists() and json.loads(binding_path.read_text(encoding="utf-8")) != binding:
        raise ValueError("Retrieval resume binding changed")
    dump(binding_path, binding)
    if (directory/"lexical.complete.json").exists():
        sealed=json.loads((directory/"lexical.complete.json").read_text(encoding="utf-8"))
        for family in ("lexical","character"):checked_file(directory/f"{family}.jsonl",sealed[family])
    if (directory/"dense.complete.json").exists():
        sealed=json.loads((directory/"dense.complete.json").read_text(encoding="utf-8"))
        checked_file(directory/"dense.jsonl",sealed["run_sha256"])
    if not (directory / "lexical.complete.json").exists():
        runs, timing = lexical_runs(root, source, queries)
        for family, values in runs.items(): frozen_jsonl(directory / f"{family}.jsonl", values)
        frozen_jsonl(directory / "lexical.timings.jsonl", timing)
        dump(directory / "lexical.complete.json", {f: digest(directory/f"{f}.jsonl") for f in runs})
    if not (directory / "dense.complete.json").exists():
        values, timing = dense_run(root, source, queries)
        frozen_jsonl(directory / "dense.jsonl", values)
        dump(directory / "dense.complete.json", {"run_sha256": digest(directory / "dense.jsonl"), **timing})
    log("retrieval_complete", source=source, queries=len(queries), depth=DEPTH)


class CrossEncoder:
    def __init__(self, model_path):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        torch.set_num_threads(2)
        self.torch = torch
        adapter_config = Path(model_path) / "adapter_config.json"
        if adapter_config.exists():
            from peft import PeftModel
            base = json.loads(adapter_config.read_text(encoding="utf-8-sig"))["base_model_name_or_path"]
            verify_model_path(base)
            verify_checkpoint(model_path)
            self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
            self.model = AutoModelForSequenceClassification.from_pretrained(base, local_files_only=True, torch_dtype=torch.float16).cuda()
            self.model = PeftModel.from_pretrained(self.model, model_path, local_files_only=True).merge_and_unload().eval()
        else:
            if (Path(model_path).parent/"manifest.json").exists():verify_model_path(model_path)
            elif (Path(model_path)/"export.json").exists():
                receipt=json.loads((Path(model_path)/"export.json").read_text(encoding="utf-8-sig"))
                checked_file(Path(model_path)/"model.safetensors",receipt["model_sha256"])
                for item in receipt["files"]:checked_file(Path(model_path)/item["name"],item["sha256"],item["bytes"])
            self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)
            self.model = AutoModelForSequenceClassification.from_pretrained(model_path, local_files_only=True, torch_dtype=torch.float16).cuda().eval()

    def score(self, pairs, batch_size=16):
        values = []
        with self.torch.inference_mode():
            for start in range(0, len(pairs), batch_size):
                tokens = self.tokenizer(pairs[start:start+batch_size], padding=True, truncation=True, max_length=256, return_tensors="pt").to("cuda")
                scores = self.model(**tokens).logits.view(-1).float()
                if not self.torch.isfinite(scores).all(): raise ValueError("Non-finite cross-encoder scores")
                values.extend(scores.cpu().tolist())
        return values


def pool(root, source, split_name):
    retrieval_root=root/"retrieval"/source
    lexical_receipt=json.loads((retrieval_root/"lexical.complete.json").read_text(encoding="utf-8-sig"))
    dense_receipt=json.loads((retrieval_root/"dense.complete.json").read_text(encoding="utf-8-sig"))
    for family in ("lexical","character"):checked_file(retrieval_root/f"{family}.jsonl",lexical_receipt[family])
    checked_file(retrieval_root/"dense.jsonl",dense_receipt["run_sha256"])
    selected = [q for q in rows(root / "queries.selected.jsonl") if q["source"] == source and q["split"] == split_name]
    qids = {q["query_id"] for q in selected}
    name = f"{source}-{split_name}"
    dataset = root / "pools/inputs" / name
    dataset.mkdir(parents=True, exist_ok=True)
    models = json.loads((root / "models/manifest.json").read_text(encoding="utf-8-sig"))
    runs = {family: [r for r in rows(root / "retrieval" / source / f"{family}.jsonl") if r["query_id"] in qids] for family in ("lexical", "character", "dense")}
    document_ids = sorted({r["document_id"] for run in runs.values() for r in run})
    catalog = readonly(root / "catalog.sqlite")
    docs = []
    for docid in document_ids:
        payload, = catalog.execute("SELECT payload FROM documents WHERE docid=?", (docid,)).fetchone()
        docs.append(json.loads(payload))
    catalog.close()
    frozen_jsonl(dataset / "queries.jsonl", [{k:q[k] for k in ("query_id", "text", "variants", "structured")} for q in selected])
    frozen_jsonl(dataset / "documents.jsonl", docs)
    for family, values in runs.items(): frozen_jsonl(dataset / f"{family}.jsonl", values)
    config = {
        "schema_version": "retrieval-judgment-pool-config-v1", "execution_profile": "external_models_v1",
        "dataset": {"dataset_id": name, "revision": digest(root / "normalization-report.json"), "visibility": "public", "contains_labels": False},
        "field_mapping": {"query_id": "query_id", "query_text": "text", "query_variants": "variants", "query_structured": "structured",
            "document_id": "document_id", "document_entity_type": "entity_type", "document_source": "source", "document_text_fields": ["text"], "blind_display_fields": ["title", "brand", "categories"]},
        "retrievers": [{"retriever_id": f, "kind": "external", "family": f, "depth": DEPTH,
            "model": models["dense"]["model"] if f=="dense" else "SQLite-FTS5-BM25",
            "revision": models["dense"]["revision"] if f=="dense" else ("jieba-0.42.1-HMM-false" if f=="lexical" else "normalized-char-bigram-v1")} for f in runs],
        "fusion": {"method": "rrf", "k": 60},
        "reranker": {"kind": "external_scores", "model": models["reranker"]["model"], "revision": models["reranker"]["revision"], "pair_budget_per_query": DEPTH},
        "selection": {"raw_depth_per_retriever": DEPTH, "top_k_per_retriever": 10, "unique_quota": 10, "disagreement_quota": 10,
            "hard_negative_quota": 0, "reranker_quota": 20, "random_tail_quota": 10, "final_pool_budget_per_query": 80},
        "safety": {"output_label": "UNJUDGED", "outside_pool_is_negative": False, "read_qrels": False, "read_sealed_or_hidden": False, "production_release_allowed": False}}
    config_path = dataset / "config.json"
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8")) != config: raise ValueError("Pool config drift")
    dump(config_path, config)
    service = PoolService(root / "pools/inputs", root / "pools/runs")
    run_id = service.create_run(name)["run_id"]
    for family in runs: service.submit_retrieval_run(run_id, family, f"{family}.jsonl")
    service.prepare_retrieval(run_id)
    run_dir = root / "pools/runs" / run_id
    analysis = json.loads((run_dir / "analysis.json").read_text(encoding="utf-8"))
    ce_path = dataset / "ce.jsonl"
    from integrity import inference_binding,verify_score_state
    if ce_path.exists():verify_score_state(dataset,models["reranker"]["path"],"ce.jsonl")
    if not ce_path.exists():
        encoder = CrossEncoder(models["reranker"]["path"])
        docs_by_id = {d["document_id"]:d for d in docs}
        queries_by_id = {q["query_id"]:q for q in selected}
        ce_rows, timings = [], []
        for query in analysis["queries"]:
            qid = query["query_id"]
            candidates = query["rrf_ranking"][:DEPTH]
            pairs = [[queries_by_id[qid]["text"], docs_by_id[d["document_id"]]["text"]] for d in candidates]
            encoder.torch.cuda.synchronize(); started = time.perf_counter()
            scores = encoder.score(pairs)
            encoder.torch.cuda.synchronize()
            timings.append({"query_id": qid, "pairs": len(pairs), "seconds": time.perf_counter()-started})
            ce_rows.extend({"query_id": qid, "document_id": d["document_id"], "score": score} for d,score in zip(candidates,scores))
            log("reranker_query", cohort=name, query_id=qid, pairs=len(pairs))
        frozen_jsonl(ce_path, ce_rows)
        frozen_jsonl(dataset / "ce.timings.jsonl", timings)
        dump(dataset/"inference-receipt.json",{"inference":inference_binding(models["reranker"]["path"]),"scores_sha256":digest(ce_path),"capture":"Scoring completion"})
    service.submit_reranker_scores(run_id, "ce.jsonl")
    service.build_pool(run_id)
    verification = service.verify_run(run_id)
    dump(root / "pools" / f"{name}.json", {"run_id": run_id, "run_dir": str(run_dir), "cohort": name,
        "full_corpus_normalization_sha256": digest(root / "normalization-report.json"),
        "pool_input_scope": "Only the retrieved union is copied into PoolService; all three retrieval runs search the complete source corpus.",
        "verification": verification})
    log("pool_complete", cohort=name, run_id=run_id)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, default=Path("D:/agent-datasets/search-stage1-v1"))
    p.add_argument("command", choices=("retrieve", "pool"))
    p.add_argument("--source", required=True, choices=("kuaisearch", "multicpr"))
    p.add_argument("--split", choices=("dev", "test"))
    a = p.parse_args()
    if a.command == "retrieve": retrieve(a.root, a.source)
    else:
        if not a.split: p.error("pool requires --split")
        pool(a.root, a.source, a.split)
