"""Validate file bytes at reuse boundaries, not just completion flags/revisions."""
import json
from pathlib import Path

from stage1 import digest,dump


def checked_file(path,expected,expected_bytes=None):
    path=Path(path)
    if not expected or not path.is_file():raise ValueError(f"Missing bound artifact: {path}")
    if expected_bytes is not None and path.stat().st_size!=expected_bytes:raise ValueError(f"Artifact size drift: {path}")
    actual=digest(path)
    if actual!=expected:raise ValueError(f"Artifact hash drift: {path}")
    return actual


def verify_model(info):
    directory=Path(info["path"]).resolve()
    if not {"config.json","model.safetensors"} <= {r["name"] for r in info["files"]}:
        raise ValueError("Incomplete pinned model receipt")
    for row in info["files"]:
        path=(directory/row["name"]).resolve()
        if not path.is_relative_to(directory):raise ValueError("Model artifact path escapes its snapshot")
        checked_file(path,row["sha256"],row["bytes"])


def verify_model_path(path):
    path=Path(path).resolve()
    manifest=path.parent/"manifest.json"
    if not manifest.exists():raise ValueError("Pinned model manifest missing")
    entries=json.loads(manifest.read_text(encoding="utf-8-sig"))
    matches=[v for v in entries.values() if Path(v["path"]).resolve()==path]
    if len(matches)!=1:raise ValueError("Model path is not uniquely bound to a pinned receipt")
    verify_model(matches[0])
    return matches[0]


def verify_checkpoint(path,config_sha=None,require_inference=True):
    path=Path(path)
    receipt=json.loads((path/"complete.json").read_text(encoding="utf-8-sig"))
    checked_file(path/"adapter_model.safetensors",receipt["model_sha256"])
    checked_file(path/"optimizer.pt",receipt["optimizer_sha256"])
    if config_sha and receipt["config_sha256"]!=config_sha:raise ValueError("Checkpoint configuration drift")
    if require_inference:
        state=json.loads((path/"inference-state.json").read_text(encoding="utf-8-sig"))
        for item in state["files"]:checked_file(path/item["name"],item["sha256"],item["bytes"])
        if state["training_complete_sha256"]!=digest(path/"complete.json"):raise ValueError("Inference/training receipt drift")
    return receipt


def inference_binding(path):
    path=Path(path)
    if (path/"adapter_config.json").exists():
        verify_checkpoint(path)
        base=json.loads((path/"adapter_config.json").read_text(encoding="utf-8-sig"))["base_model_name_or_path"]
        info=verify_model_path(base)
        snapshot=digest(path/"inference-state.json")
    else:
        info=verify_model_path(path)
        snapshot=None
    return {"base_model":info,"adapter_state_sha256":snapshot,"precision":"fp16 merged","max_length":256,"microbatch":16}


def verify_score_state(directory,model_path,score_name="scores.jsonl"):
    directory=Path(directory)
    state=json.loads((directory/"inference-receipt.json").read_text(encoding="utf-8-sig"))
    checked_file(directory/score_name,state["scores_sha256"])
    if state["inference"]!=inference_binding(model_path):raise ValueError("Score inference state drift")


def verify_qrels(directory):
    directory=Path(directory)
    receipt=json.loads((directory/"manifest.json").read_text(encoding="utf-8-sig"))
    checked_file(directory/"qrels.jsonl",receipt["qrels_sha256"])
    checked_file(directory/"qrels.tsv",receipt["numeric_qrels_sha256"])
    if receipt.get("parents"):
        for parent in receipt["parents"]:
            checked_file(parent["path"],parent["sha256"])
            verify_qrels(Path(parent["path"]).parent)
    else:
        checked_file(directory/"author-receipts.jsonl",receipt.get("author_receipts_sha256"))
        root=directory.parents[2]
        authors=[json.loads(line) for line in (directory/"author-receipts.jsonl").read_text(encoding="utf-8-sig").splitlines()]
        if not authors or len({a["thread_id"] for a in authors})!=len(authors):raise ValueError("Missing independent authors")
        for author in authors:
            if not author.get("judge_model_metadata",{}).get("model") or author["judge_model_metadata"].get("source_thread_id")!=author["thread_id"]:raise ValueError("Judge model/identity missing")
            packet=root/"labeling/packets"/author["packet"]
            checked_file(packet/"judgments.jsonl",author["judgments_sha256"])
            checked_file(packet/"completion.json",author["completion_sha256"])
            manifest=json.loads((packet/"manifest.json").read_text(encoding="utf-8-sig"))
            checked_file(packet/"packet.jsonl",manifest["packet_sha256"])
            checked_file(packet/"RUBRIC.md",manifest["rubric_sha256"])
    return receipt


def seal_dense(directory,state):
    """Recover the final seal after a last-batch checkpoint, without inference."""
    import numpy as np
    directory=Path(directory)
    binding=state["binding"]
    if state["done"]!=binding["documents"]:raise ValueError("Cannot seal an incomplete embedding index")
    path=directory/"embeddings.npy"
    vectors=np.load(path,mmap_mode="r")
    if vectors.shape!=(binding["documents"],binding["dimension"]) or vectors.dtype!=np.float16:
        raise ValueError("Embedding shape/dtype drift")
    manifest_path=directory/"dense.manifest.json"
    if manifest_path.exists():
        manifest=json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if manifest["binding"]!=binding or not manifest["complete"] or manifest["done"]!=state["done"]:
            raise ValueError("Dense completion binding drift")
        checked_file(path,manifest["vectors_sha256"],manifest["vector_bytes"])
    else:
        # A completed batch is durable before its progress commit. Check stored
        # representation sanity before recovering a missing final hash manifest.
        sample=vectors[np.linspace(0,len(vectors)-1,min(1000,len(vectors)),dtype=int)].astype(np.float32)
        if not np.isfinite(sample).all() or np.max(np.abs(np.linalg.norm(sample,axis=1)-1))>=.002:
            raise ValueError("Completed embedding checkpoint contains invalid vectors")
        manifest={**state,"complete":True,"vectors_sha256":digest(path),"vector_bytes":path.stat().st_size}
        dump(manifest_path,manifest)
    dump(directory/"dense.progress.json",{**state,"complete":True})
    return manifest


def seal_lexical(root,source,count):
    directory=Path(root)/"indexes"/source
    manifest_path=directory/"lexical.manifest.json"
    expected={"source":source,"documents":count,"normalization_sha256":digest(Path(root)/"normalization-report.json"),
        "words":"SQLite FTS5 BM25 over jieba 0.42.1 cut_for_search HMM=False","chars":"SQLite FTS5 BM25 over normalized character bigrams",
        "text_fields":"same normalized document text for lexical, char, dense and reranker"}
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8-sig"))!=expected:raise ValueError("Lexical manifest binding drift")
    else:dump(manifest_path,expected)
    seal_path=directory/"lexical.seal.json"
    if seal_path.exists():
        seal=json.loads(seal_path.read_text(encoding="utf-8-sig"))
        checked_file(manifest_path,seal["manifest_sha256"])
        checked_file(directory/"lexical.sqlite",seal["index_sha256"])
    else:
        # Existing pre-seal runs have an independently recorded full-file audit.
        audit_path=Path(root)/"data-audit.json"
        if audit_path.exists():
            prior=json.loads(audit_path.read_text(encoding="utf-8-sig"))["indices"].get(source,{}).get("lexical")
            if isinstance(prior,dict) and prior.get("status")=="PASS":
                checked_file(manifest_path,prior["manifest_sha256"])
                checked_file(directory/"lexical.sqlite",prior["artifact_sha256"])
        seal={"manifest_sha256":digest(manifest_path),"index_sha256":digest(directory/"lexical.sqlite")}
        dump(seal_path,seal)
    return seal
