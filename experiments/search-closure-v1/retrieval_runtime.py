"""Read-only full-corpus retrieval and CE scoring for the search closure.

No public function seals or modifies a historical index. GPU work occurs only
when retrieve_batch/score_pairs is explicitly called by the controlling runner.
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata

import numpy as np

SOURCES = ("kuaisearch", "multicpr")
DEPTH = 300
BLOCK_SIZE = 16384
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："
WEIGHTS = {"w111": (1, 1, 1), "w211": (2, 1, 1), "w112": (1, 1, 2), "no_dense": (1, 1, 0)}
CHANNELS = ("bm25", "character", "dense")
OLD_ROOT = Path("D:/agent-datasets/search-stage1-v1")
OLD_CODE = Path(__file__).resolve().parents[1] / "search-stage1-v1"
ASSET_AUDIT = Path("D:/agent-datasets/search-closure-v1/assets/COMPLETE.json")


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def write_once(path, value, *, jsonl=False):
    """Single-writer, atomic publication; an existing result may only match exactly."""
    path = Path(path)
    raw = b"".join(canonical(row) + b"\n" for row in value) if jsonl else canonical(value) + b"\n"
    if path.exists():
        if path.read_bytes() != raw:
            raise ValueError(f"Frozen output differs: {path}")
        return sha(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        # The runner owns a single output directory. Recheck before publication.
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError(f"Concurrent frozen output differs: {path}")
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    return hashlib.sha256(raw).hexdigest()


def readonly(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    return connection


def clean(value):
    text = " ".join(str(value or "").split())
    return "" if text.upper() in {"UNKNOWN", "NULL", "NONE", "N/A"} else text


def query_key(text):
    text = unicodedata.normalize("NFKC", text).casefold()
    return "".join(char for char in text if not char.isspace() and not unicodedata.category(char).startswith("P"))


def query_tokens(query):
    import jieba
    if jieba.__version__ != "0.42.1": raise ValueError("Frozen lexical tokenizer requires jieba 0.42.1")
    jieba.setLogLevel(30)
    normalized = clean(query).casefold()
    words = [token for token in jieba.cut_for_search(normalized, HMM=False) if re.search(r"[\w\u3400-\u9fff]", token)]
    compact = query_key(normalized)
    grams = [compact[i:i + 2] for i in range(len(compact) - 1)] or [compact]
    return {"bm25": sorted(set(filter(None, words))), "character": sorted(set(filter(None, grams)))}


def lexical_query(index, catalog, source, query, *, depth=DEPTH):
    """Same FTS5 matching, tie order and explicit zero-score padding as v1."""
    if source not in SOURCES: raise ValueError("Unsupported source")
    started = time.perf_counter()
    tokens = query_tokens(query)
    token_seconds = time.perf_counter() - started
    result, timings = {}, {"tokenization_seconds": token_seconds}
    for channel, table in (("bm25", "words"), ("character", "chars")):
        expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens[channel])
        began = time.perf_counter()
        found = index.execute(
            f"SELECT rowid,bm25({table}) AS score FROM {table} WHERE {table} MATCH ? ORDER BY score,rowid LIMIT ?",
            (expression, depth),
        ).fetchall() if expression else []
        matched = len(found)
        used = {rowid for rowid, _ in found}
        if len(found) < depth:
            for rowid, in catalog.execute("SELECT rowid FROM documents WHERE source=? ORDER BY rowid LIMIT ?", (source, depth * 2)):
                if rowid not in used:
                    found.append((rowid, 0.0)); used.add(rowid)
                if len(found) == depth: break
        values = []
        for rank, (rowid, score) in enumerate(found, 1):
            row = catalog.execute("SELECT docid,source FROM documents WHERE rowid=?", (rowid,)).fetchone()
            if row is None or row[1] != source: raise ValueError("Lexical index row maps outside corpus source")
            values.append({"document_id": row[0], "rank": rank, "score": -float(score)})
        result[channel] = values
        timings[channel] = {"search_and_resolve_seconds": time.perf_counter() - began,
                            "matched": matched, "zero_score_padding": len(found) - matched,
                            "tokens": tokens[channel]}
    return result, timings


def stable_topk(scores, ids, k):
    scores, ids = np.asarray(scores), np.asarray(ids)
    if not np.isfinite(scores).all(): raise ValueError("Nonfinite dense similarity")
    if len(scores) != len(ids) or k < 1: raise ValueError("Invalid top-k arguments")
    selected = np.flatnonzero(scores >= np.partition(scores, len(scores) - k)[len(scores) - k]) if len(scores) > k else np.arange(len(scores))
    selected = selected[np.lexsort((ids[selected], -scores[selected]))[:k]]
    return scores[selected], ids[selected]


def merge_block_topk(best_scores, best_ids, scores, rowids, k=DEPTH):
    local_scores, local_ids = stable_topk(scores, rowids, k)
    return stable_topk(np.concatenate((best_scores, local_scores)), np.concatenate((best_ids, local_ids)), k)


def weighted_rrf(channels, weights, *, k=60, depth=DEPTH):
    if tuple(weights) not in WEIGHTS.values() or k != 60: raise ValueError("RRF profile is outside the fixed grid")
    scores = {}
    for channel, weight in zip(CHANNELS, weights):
        seen = set()
        for rank, row in enumerate(channels[channel], 1):
            did = row["document_id"]
            if did in seen or row["rank"] != rank: raise ValueError("Duplicate or unordered channel ranking")
            seen.add(did)
            if weight:
                scores[did] = scores.get(did, 0.0) + weight / (k + rank)
    return [{"document_id": did, "rank": rank, "score": score}
            for rank, (did, score) in enumerate(sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:depth], 1)]


def rerank_same_candidates(pool, score_map):
    ids = [row["document_id"] for row in pool]
    if len(ids) != len(set(ids)) or any(did not in score_map for did in ids):
        raise ValueError("CE scores do not cover the exact candidate pool")
    if any(not math.isfinite(float(score_map[did])) for did in ids): raise ValueError("Nonfinite CE score")
    return [{"document_id": did, "rank": rank, "score": float(score_map[did])}
            for rank, did in enumerate(sorted(ids, key=lambda did: (-score_map[did], did)), 1)]


def verify_asset_audit(path=ASSET_AUDIT, *, progress=None):
    path = Path(path)
    complete = read_json(path)
    files_path, replay_path = path.parent / "files.json", path.parent / "neural-sample-replay.json"
    if (complete.get("status") != "PASS" or sha(files_path) != complete["files_sha256"]
            or sha(replay_path) != complete["replay_sha256"]):
        raise ValueError("Asset audit completion chain differs")
    files = read_json(files_path)
    replay = read_json(replay_path)
    if files.get("status") != "PASS" or replay.get("status") != "PASS": raise ValueError("Asset audit failed")
    seen = set()
    for item in files["files"]:
        file = Path(item["path"]).resolve()
        if file in seen: raise ValueError("Duplicate asset audit path")
        seen.add(file)
        if file.stat().st_size != item["bytes"] or sha(file) != item["sha256"]:
            raise ValueError(f"Current asset bytes differ: {file}")
        if progress: progress({"stage": "asset_verified", "path": str(file)})
    return {"audit_sha256": sha(path), "files_sha256": sha(files_path),
            "files": {str(Path(item["path"]).resolve()): item for item in files["files"]},
            "normalization_sha256": files["normalization_sha256"], "replay_sha256": sha(replay_path)}


def old_readonly_modules():
    if str(OLD_CODE) not in sys.path: sys.path.insert(0, str(OLD_CODE))
    import integrity
    name = "_closure_stage1_retrieve_readonly"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, OLD_CODE / "retrieve.py")
        module = importlib.util.module_from_spec(spec); sys.modules[name] = module; spec.loader.exec_module(module)
    return integrity, sys.modules[name]


def model_binding(path):
    integrity, _ = old_readonly_modules()
    value = integrity.inference_binding(Path(path))
    return {"path": str(Path(path).resolve()), "inference": value, "inference_sha256": fingerprint(value)}


class RetrievalRuntime:
    def __init__(self, root=OLD_ROOT, *, asset_audit=ASSET_AUDIT, progress=None):
        self.root = Path(root).resolve()
        self.progress = progress or (lambda event: None)
        began = time.perf_counter()
        self.asset = verify_asset_audit(asset_audit, progress=self.progress)
        if str(self.root / "catalog.sqlite") not in self.asset["files"]:
            raise ValueError("Full catalog not included in verified asset audit")
        if sha(self.root / "normalization-report.json") != self.asset["normalization_sha256"]:
            raise ValueError("Catalog normalization changed")
        self.models = read_json(self.root / "models/manifest.json")
        for info in self.models.values():
            if not {"config.json", "model.safetensors"} <= {item["name"] for item in info["files"]}:
                raise ValueError("Incomplete model file manifest")
            for item in info["files"]:
                model_file = str((Path(info["path"]) / item["name"]).resolve())
                audited = self.asset["files"].get(model_file)
                if audited is None or any(audited[key] != item[key] for key in ("sha256", "bytes")):
                    raise ValueError("Model manifest no longer points to the audited model bytes")
        self.catalog = readonly(self.root / "catalog.sqlite")
        self.indexes, self.dense_manifests = {}, {}
        for source in SOURCES:
            directory = self.root / "indexes" / source
            lexical, dense = read_json(directory / "lexical.manifest.json"), read_json(directory / "dense.manifest.json")
            count, first, last = self.catalog.execute("SELECT count(*),min(rowid),max(rowid) FROM documents WHERE source=?", (source,)).fetchone()
            b = dense["binding"]
            if (not dense.get("complete") or count != dense["done"] or count != b["documents"]
                    or last - first + 1 != count or b["first_rowid"] != first
                    or b["dimension"] != 512 or b["dtype"] != "float16" or b["max_length"] != 128
                    or b["revision"] != self.models["dense"]["revision"]
                    or lexical["documents"] != count
                    or any(m["normalization_sha256"] != self.asset["normalization_sha256"] for m in (lexical, b))):
                raise ValueError("Full index/source/model mapping drift")
            for filename in ("lexical.sqlite", "embeddings.npy"):
                if str(directory / filename) not in self.asset["files"]: raise ValueError("Index missing from asset verification")
            if dense["vectors_sha256"] != self.asset["files"][str(directory / "embeddings.npy")]["sha256"]:
                raise ValueError("Vector manifest differs from audited bytes")
            self.indexes[source] = readonly(directory / "lexical.sqlite")
            self.dense_manifests[source] = dense
        self.asset_validation_seconds = time.perf_counter() - began
        self._dense_model = self._tokenizer = self._ce = self._ce_path = None
        self._ce_binding = None
        self._source_calls = {source: 0 for source in SOURCES}
        self._model_calls = {}

    def close(self):
        self.catalog.close()
        for db in self.indexes.values(): db.close()
        self._ce = self._dense_model = self._tokenizer = None
        if "torch" in sys.modules:
            import torch
            gc.collect(); torch.cuda.empty_cache()

    def document_texts(self, source, ids):
        if source not in SOURCES or len(ids) != len(set(ids)): raise ValueError("Invalid document request")
        result = {}
        for offset in range(0, len(ids), 500):
            chunk = ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in chunk)
            if not chunk: continue
            rows = self.catalog.execute(f"SELECT docid,source,text FROM documents WHERE docid IN ({placeholders})", chunk)
            for did, native_source, text in rows:
                if native_source != source: raise ValueError("Document ID belongs to another source")
                result[did] = text
        if set(result) != set(ids): raise ValueError("Missing document in full catalog")
        return result

    def _query_vectors(self, queries):
        import torch
        from transformers import AutoModel, AutoTokenizer
        if not torch.cuda.is_available(): raise RuntimeError("Full-corpus dense search requires explicit CUDA availability")
        torch.set_num_threads(2)
        if self._ce is not None: self._ce.model.to("cpu")
        gc.collect(); torch.cuda.empty_cache()
        begin = time.perf_counter()
        cache_state = "model_warm" if self._dense_model is not None else "model_cold"
        if self._dense_model is None:
            info = self.models["dense"]
            self._tokenizer = AutoTokenizer.from_pretrained(info["path"], local_files_only=True)
            self._dense_model = AutoModel.from_pretrained(info["path"], local_files_only=True, torch_dtype=torch.float16).eval()
        self._dense_model.to("cuda")
        torch.cuda.synchronize(); load_seconds = time.perf_counter() - begin
        begin = time.perf_counter()
        with torch.inference_mode():
            tokens = self._tokenizer([QUERY_INSTRUCTION + q["query"] for q in queries], padding=True, truncation=True,
                                     max_length=128, return_tensors="pt").to("cuda")
            vectors = torch.nn.functional.normalize(self._dense_model(**tokens).last_hidden_state[:, 0].float(), p=2, dim=1)
        torch.cuda.synchronize(); encode_seconds = time.perf_counter() - begin
        self._dense_model.to("cpu"); del tokens; gc.collect(); torch.cuda.empty_cache()
        return vectors, {"model_cache_state": cache_state, "dense_model_load_seconds": load_seconds,
                         "query_encode_seconds": encode_seconds}

    def retrieve_batch(self, source, queries, *, include_dense=True):
        if source not in SOURCES or not queries: raise ValueError("Source and nonempty query batch required")
        ids = [q["query_id"] for q in queries]
        if len(ids) != len(set(ids)) or any(not isinstance(q["query"], str) or not q["query"].strip() for q in queries):
            raise ValueError("Invalid query IDs/text")
        started = time.perf_counter()
        channels, lexical_timings = {}, {}
        for query in queries:
            channels[query["query_id"]], lexical_timings[query["query_id"]] = lexical_query(
                self.indexes[source], self.catalog, source, query["query"])
        if not include_dense:
            for query in queries: channels[query["query_id"]]["dense"] = []
            self._source_calls[source] += 1
            return {"source": source, "queries": queries, "channels": channels, "timings": {
                "dense_status": "disabled_by_no_dense_profile", "query_count": len(queries),
                "source_call_state": "first_call_process" if self._source_calls[source] == 1 else "repeated_call_process",
                "os_page_cache_state": "uncontrolled_not_claimed_cold", "wall_seconds": time.perf_counter() - started,
                "lexical": lexical_timings, "scope": "single_query_wall" if len(queries) == 1 else "batch_wall_not_online_p95",
            }}
        qvectors, timings = self._query_vectors(queries)
        import torch
        manifest = self.dense_manifests[source]; b = manifest["binding"]
        vectors = np.load(self.root / "indexes" / source / "embeddings.npy", mmap_mode="r")
        if vectors.shape != (b["documents"], 512) or vectors.dtype != np.float16: raise ValueError("Vector shape changed")
        best_scores = [np.empty(0, dtype=np.float32) for _ in queries]
        best_ids = [np.empty(0, dtype=np.int64) for _ in queries]
        began = time.perf_counter()
        with torch.inference_mode():
            for start in range(0, b["documents"], BLOCK_SIZE):
                block = torch.from_numpy(np.array(vectors[start:start + BLOCK_SIZE], dtype=np.float32)).cuda()
                block = torch.nn.functional.normalize(block, p=2, dim=1)
                scores = (qvectors @ block.T).cpu().numpy()
                rowids = np.arange(start + b["first_rowid"], start + b["first_rowid"] + len(block), dtype=np.int64)
                for i in range(len(queries)):
                    best_scores[i], best_ids[i] = merge_block_topk(best_scores[i], best_ids[i], scores[i], rowids)
                del block
                if start % (BLOCK_SIZE * 20) == 0:
                    self.progress({"stage": "dense_scan", "source": source, "documents": min(start + BLOCK_SIZE, b["documents"]), "total": b["documents"]})
        torch.cuda.synchronize(); timings["dense_scan_seconds"] = time.perf_counter() - began
        began = time.perf_counter()
        for i, query in enumerate(queries):
            values = []
            for rank, (score, rowid) in enumerate(zip(best_scores[i], best_ids[i]), 1):
                row = self.catalog.execute("SELECT docid,source FROM documents WHERE rowid=?", (int(rowid),)).fetchone()
                if row is None or row[1] != source: raise ValueError("Dense row mapping outside source")
                values.append({"document_id": row[0], "rank": rank, "score": float(score)})
            channels[query["query_id"]]["dense"] = values
        timings["dense_id_resolve_seconds"] = time.perf_counter() - began
        del vectors, qvectors; gc.collect(); torch.cuda.empty_cache()
        self._source_calls[source] += 1
        timings.update(source_call_state="first_call_process" if self._source_calls[source] == 1 else "repeated_call_process",
                       os_page_cache_state="uncontrolled_not_claimed_cold", query_count=len(queries),
                       wall_seconds=time.perf_counter() - started, lexical=lexical_timings,
                       scope="single_query_wall" if len(queries) == 1 else "batch_wall_not_online_p95")
        return {"source": source, "queries": queries, "channels": channels, "timings": timings}

    def score_pairs(self, model_path, pairs):
        path = str(Path(model_path).resolve())
        started = time.perf_counter()
        cache_state = "model_warm" if self._ce_path == path else "model_cold"
        if self._ce_path != path:
            self._ce = self._ce_path = None
            if self._dense_model is not None: self._dense_model.to("cpu")
            import torch
            gc.collect(); torch.cuda.empty_cache()
            self._ce_binding = model_binding(path)
            _, previous = old_readonly_modules()
            self._ce = previous.CrossEncoder(path)
            self._ce_path = path
        if self._dense_model is not None: self._dense_model.to("cpu")
        self._ce.model.to("cuda")
        load_seconds = time.perf_counter() - started
        self._ce.torch.cuda.synchronize(); began = time.perf_counter()
        scores = self._ce.score(pairs, batch_size=16)
        self._ce.torch.cuda.synchronize()
        if len(scores) != len(pairs) or any(not math.isfinite(s) for s in scores): raise ValueError("CE output count/values differ")
        return scores, {"model_cache_state": cache_state, "model_load_seconds": load_seconds,
                        "score_seconds": time.perf_counter() - began, "pair_count": len(pairs),
                        "input_sha256": fingerprint(pairs), "model_binding": self._ce_binding,
                        "max_length": 256, "batch_size": 16}

    def search_one(self, query, source, *, profile="w111", model_path=None, limit=10):
        if profile not in WEIGHTS or type(limit) is not int or not 1 <= limit <= 20: raise ValueError("Invalid Agent search profile/limit")
        query_id = "agent-" + fingerprint({"source": source, "query": query})[:20]
        recalled = self.retrieve_batch(source, [{"query_id": query_id, "query": query}], include_dense=WEIGHTS[profile][2] > 0)
        pool = weighted_rrf(recalled["channels"][query_id], WEIGHTS[profile])
        texts = self.document_texts(source, [r["document_id"] for r in pool])
        ce_timing = None
        if model_path is not None:
            scores, ce_timing = self.score_pairs(model_path, [[query, texts[row["document_id"]]] for row in pool])
            pool = rerank_same_candidates(pool, {row["document_id"]: score for row, score in zip(pool, scores)})
        hits = [{"source": source, "docid": row["document_id"], "text": texts[row["document_id"]],
                 "rank": row["rank"], "score": row["score"], "unknown": [],
                 "provenance": {"catalog": str(self.root / "catalog.sqlite"), "catalogSha256": self.asset["files"][str(self.root / "catalog.sqlite")]["sha256"],
                                "textSha256": hashlib.sha256(texts[row["document_id"]].encode()).hexdigest(), "profile": profile,
                                "retrievalDepth": DEPTH, "ceModel": str(model_path) if model_path else None}}
                for row in pool[:limit]]
        return {"hits": hits, "timings": {"recall": recalled["timings"], "ce": ce_timing}}

    def strategy_manifest(self, *, profile="w111", model_path=None):
        if profile not in WEIGHTS: raise ValueError("Unapproved Agent RRF profile")
        return {"version": "search-closure-agent-provider-v1", "dataRoot": str(self.root),
                "assetAuditSha256": self.asset["audit_sha256"], "profile": profile,
                "weights": list(WEIGHTS[profile]), "rrfK": 60, "depth": DEPTH,
                "model": model_binding(model_path) if model_path is not None else None,
                "inputText": "catalog.documents.text", "queryInstruction": QUERY_INSTRUCTION,
                "runtimeCodeSha256": sha(Path(__file__))}

    def agent_provider(self, binding, *, manifest_path, profile="w111", model_path=None, on_result=None):
        if (Path(binding.data_root).resolve() != self.root or sha(manifest_path) != binding.manifest_sha256
                or read_json(manifest_path) != self.strategy_manifest(profile=profile, model_path=model_path)):
            raise ValueError("Agent strategy/corpus/manifest binding differs")
        async def provider(request):
            if request.binding != binding: raise ValueError("Agent/runtime binding mismatch")
            # Intentional serialized execution: the controlling Agent runner owns
            # GPU scheduling. No second thread/worker or detached model is started.
            result = self.search_one(request.query, request.source, profile=profile, model_path=model_path, limit=request.limit)
            if on_result is not None: on_result(request, result)
            return {"binding": request.binding.model_dump(by_alias=True), "query": request.query,
                    "source": request.source, "hits": result["hits"]}
        return provider


def require_test_authorization(*, selection_receipt=None, model_selection_receipt=None):
    # Test support is intentionally not implemented until the runner can bind
    # the future sealed query selection AND the final development model choice.
    raise PermissionError("Test runtime is disabled; requires future verified sealed selection and final model-selection receipts")
