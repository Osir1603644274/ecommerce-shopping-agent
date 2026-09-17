"""Export exactly the selected FP16 merged inference profile, then check replay."""
import json
from pathlib import Path

from retrieve import CrossEncoder,rows
from stage1 import digest,dump,log
from integrity import checked_file,inference_binding

ROOT=Path("D:/agent-datasets/search-stage1-v1")

def export():
    import torch
    selected=json.loads((ROOT/"evaluation/selected-checkpoint.json").read_text(encoding="utf-8"))
    inference=inference_binding(selected["model_path"])
    checked_file(Path(selected["model_path"])/"adapter_model.safetensors",selected["model_sha256"])
    destination=ROOT/"export/selected-reranker"
    if (destination/"export.json").exists():
        receipt=json.loads((destination/"export.json").read_text(encoding="utf-8"))
        if receipt["selected_checkpoint_sha256"]!=digest(ROOT/"evaluation/selected-checkpoint.json") or receipt["model_sha256"]!=digest(destination/"model.safetensors"):
            raise ValueError("Export binding drift")
        if receipt["inference"]!=inference:raise ValueError("Export inference binding drift")
        for item in receipt["files"]:checked_file(destination/item["name"],item["sha256"],item["bytes"])
        log("export_reuse");return
    if destination.exists():raise ValueError("Incomplete export exists; preserve before retrying")
    encoder=CrossEncoder(selected["model_path"])
    # Verification uses development examples only; never test examples for model changes.
    query_rows=rows(ROOT/"pools/inputs/multicpr-dev/queries.jsonl")[:4]
    doc_rows=rows(ROOT/"pools/inputs/multicpr-dev/documents.jsonl")[:4]
    pairs=[[q["text"],d["text"]] for q,d in zip(query_rows,doc_rows)]
    before=encoder.score(pairs)
    destination.mkdir(parents=True)
    encoder.model.save_pretrained(destination,safe_serialization=True)
    encoder.tokenizer.save_pretrained(destination)
    del encoder
    torch.cuda.empty_cache()
    restored=CrossEncoder(str(destination))
    after=restored.score(pairs)
    if max(abs(a-b) for a,b in zip(before,after))>1e-5:
        raise ValueError("Exported model scores do not replay the selected profile")
    dump(destination/"export.json",{"selected_checkpoint_sha256":digest(ROOT/"evaluation/selected-checkpoint.json"),
         "inference":inference,"files":[{"name":p.name,"bytes":p.stat().st_size,"sha256":digest(p)} for p in sorted(destination.iterdir()) if p.name!="export.json"],
         "adapter_sha256":selected["model_sha256"],"model_sha256":digest(destination/"model.safetensors"),
         "precision":"FP16; same merged inference profile as benchmark scorer","replay_before":before,"replay_after":after,
         "replay_max_absolute_error":max(abs(a-b) for a,b in zip(before,after)),"production_switch_authorized":False})
    log("model_exported",directory=str(destination))

if __name__=="__main__":export()
