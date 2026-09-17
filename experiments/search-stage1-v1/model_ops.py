"""Pinned model acquisition and resumable GPU embeddings for complete corpora."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import urllib.request
from pathlib import Path

from stage1 import digest, dump, log
from integrity import verify_model,seal_dense

MODELS = {"dense": "BAAI/bge-small-zh-v1.5", "reranker": "BAAI/bge-reranker-base"}


def download(root):
    proxy = os.environ.get("SEARCH_STAGE1_PROXY")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}) if proxy else urllib.request.ProxyHandler())
    target = root / "models"
    target.mkdir(exist_ok=True)
    manifest_path = target / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    for role, model in MODELS.items():
        meta = json.load(opener.open(f"https://huggingface.co/api/models/{model}", timeout=30))
        revision = manifest.get(role, {}).get("revision", meta["sha"])
        files = json.load(opener.open(f"https://huggingface.co/api/models/{model}/tree/{revision}?recursive=true", timeout=30))
        wanted = {"config.json", "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "vocab.txt", "sentencepiece.bpe.model", "model.safetensors"}
        chosen = [f for f in files if f.get("path") in wanted]
        if not any(f["path"] == "model.safetensors" for f in chosen):
            raise ValueError("Expected safe-tensor checkpoint unavailable; do not load pickle weights")
        model_root = target / (role + "-" + revision)
        model_root.mkdir(exist_ok=True)
        receipts = []
        for item in chosen:
            path = model_root / item["path"]
            if not path.exists():
                log("model_download", role=role, file=item["path"], bytes=item["size"])
                part = path.with_suffix(path.suffix + ".download")
                with opener.open(f"https://huggingface.co/{model}/resolve/{revision}/{item['path']}", timeout=60) as response, part.open("wb") as out:
                    for chunk in iter(lambda: response.read(1024*1024), b""):
                        out.write(chunk)
                os.replace(part, path)
            sha = digest(path)
            expected = item.get("lfs", {}).get("oid")
            if path.stat().st_size != item["size"] or expected and sha != expected:
                raise ValueError(f"Model file integrity mismatch: {role}/{item['path']}")
            receipts.append({"name": item["path"], "sha256": sha, "bytes": path.stat().st_size})
        manifest[role] = {"model": model, "revision": revision, "path": str(model_root), "files": receipts}
        dump(manifest_path, manifest)
        log("model_ready", role=role, revision=revision)


def encode(root, source, batch_size, limit):
    import numpy as np
    import torch
    from transformers import AutoModel, AutoTokenizer
    torch.set_num_threads(2)
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable; do not silently run a full corpus on CPU")
    model_info = json.loads((root / "models/manifest.json").read_text(encoding="utf-8-sig"))["dense"]
    verify_model(model_info)
    db = sqlite3.connect(f"file:{(root/'catalog.sqlite').as_posix()}?mode=ro", uri=True)
    count, first, last = db.execute("SELECT count(*),min(rowid),max(rowid) FROM documents WHERE source=?", (source,)).fetchone()
    if last-first+1 != count:
        raise ValueError("Dense rowid mapping requires a contiguous normalized source block")
    directory = root / "indexes" / source
    directory.mkdir(parents=True, exist_ok=True)
    progress_path = directory / "dense.progress.json"
    binding = {"model": model_info["model"], "revision": model_info["revision"], "documents": count,
               "first_rowid": first, "dimension": 512, "dtype": "float16", "max_length": 128,
               "normalization_sha256": digest(root / "normalization-report.json")}
    # bge-small-zh-v1.5 has 512 dimensions, verified again against the checkpoint.
    state = json.loads(progress_path.read_text(encoding="utf-8-sig")) if progress_path.exists() else {"binding": binding, "done": 0, "complete": False}
    if state["binding"] != binding:
        raise ValueError("Dense corpus/model/config binding drift")
    if state["complete"] or state["done"]==count:
        seal_dense(directory,state)
        db.close()
        log("dense_reuse", source=source, documents=count); return
    if (directory/"dense.manifest.json").exists():raise ValueError("Completed dense manifest conflicts with partial progress")
    tokenizer = AutoTokenizer.from_pretrained(model_info["path"], local_files_only=True)
    model = AutoModel.from_pretrained(model_info["path"], local_files_only=True, torch_dtype=torch.float16).to("cuda").eval()
    if model.config.hidden_size != binding["dimension"]:
        raise ValueError("Checkpoint embedding dimension differs from declared mapping")
    vector_path = directory / "embeddings.npy"
    vectors = np.lib.format.open_memmap(vector_path, mode="r+" if vector_path.exists() else "w+", dtype=np.float16, shape=(count, binding["dimension"]))
    if vectors.shape != (count, binding["dimension"]) or vectors.dtype != np.float16:
        raise ValueError("Existing vector file has a different shape or dtype")
    done = state["done"]
    started, initial = time.monotonic(), done
    cursor = db.execute("SELECT rowid,text FROM documents WHERE source=? AND rowid>=? ORDER BY rowid", (source, first+done))
    target = min(count, done+limit) if limit else count
    log("dense_start", source=source, done=done, target=target, batch=batch_size)
    with torch.inference_mode():
        while done < target:
            batch = cursor.fetchmany(min(batch_size, target-done))
            if not batch: raise ValueError("Unexpected corpus truncation")
            inputs = tokenizer([r[1] for r in batch], padding=True, truncation=True, max_length=128, return_tensors="pt").to("cuda")
            encoded = model(**inputs).last_hidden_state[:,0]
            encoded = torch.nn.functional.normalize(encoded.float(), p=2, dim=1)
            values = encoded.cpu().numpy().astype(np.float16)
            if not np.isfinite(values).all(): raise ValueError("Non-finite embeddings")
            vectors[done:done+len(batch)] = values
            done += len(batch)
            if done % (batch_size*20) == 0 or done == target:
                vectors.flush()
                elapsed = time.monotonic()-started
                state.update(done=done, complete=False, elapsed_this_run_seconds=round(elapsed,3),
                             documents_per_second=round((done-initial)/elapsed,2), peak_cuda_bytes=torch.cuda.max_memory_allocated())
                dump(progress_path,state)
                log("dense_progress", source=source, done=done, total=count, rate=state["documents_per_second"], peak_cuda_mb=round(state["peak_cuda_bytes"]/1024**2))
    del vectors
    db.close()
    if done==count:
        seal_dense(directory,state)
        log("dense_complete", source=source, documents=count)


if __name__ == "__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,default=Path("D:/agent-datasets/search-stage1-v1"))
    p.add_argument("command",choices=("download","encode"))
    p.add_argument("--source",choices=("kuaisearch","multicpr"))
    p.add_argument("--batch-size",type=int,default=128)
    p.add_argument("--limit",type=int,default=0)
    a=p.parse_args()
    if a.command=="download": download(a.root)
    else:
        if not a.source: p.error("encode needs --source")
        encode(a.root,a.source,a.batch_size,a.limit)
