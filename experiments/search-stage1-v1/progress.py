"""Read artifact-backed progress, without inferring completion from processes."""
import json
from pathlib import Path
from stage1 import dump,log

ROOT=Path("D:/agent-datasets/search-stage1-v1")

def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else None

def refresh():
    corpus=read(ROOT/"normalization-report.json")
    final=read(ROOT/"evaluation/final-report.json")
    complete=bool(final and final.get("status")=="COMPLETE")
    labels=all((ROOT/"qrels/final-v1"/f"{source}-{split}"/"manifest.json").exists()
               for source in ("kuaisearch","multicpr") for split in ("dev","test"))
    state={"phase":"COMPLETE" if complete else "SEARCH_STAGE1_RUNNING","normalization_complete":bool(corpus),"splits_complete":(ROOT/"split-report.json").exists(),
           "labels_complete":labels,"training_complete":(ROOT/"training/run-lora-v1/training-complete.json").exists(),
           "evaluation_complete":complete,"cloud_spend_cny":0,"sources":{}}
    for source in ("kuaisearch","multicpr"):
        dense=read(ROOT/"indexes"/source/"dense.progress.json")
        lex=read(ROOT/"indexes"/source/"lexical.manifest.json")
        state["sources"][source]={"documents":next(r["count"] for r in corpus["documents"] if r["source"]==source) if corpus else None,
            "dense_documents_done":dense["done"] if dense else 0,"dense_complete":(ROOT/"indexes"/source/"dense.manifest.json").exists(),
            "lexical_complete":bool(lex),"dev_pool_complete":(ROOT/"pools"/f"{source}-dev.json").exists(),
            "test_pool_complete":(ROOT/"pools"/f"{source}-test.json").exists()}
    state["completed_epochs"]=[n for n in (1,2,3) if (ROOT/"training/run-lora-v1"/f"epoch-{n}/complete.json").exists()]
    collection=read(ROOT/"labeling/collection.json")
    state["completed_label_packets"]=len(collection["imported"]) if collection else 0
    state["model_training_method"]="LoRA r16 alpha32 query/value plus classifier; local CUDA"
    from datetime import datetime
    state["updated_at"]=datetime.now().isoformat(timespec="seconds")
    dump(ROOT/"status.json",state)
    log("execution_progress",**state)

if __name__=="__main__":refresh()
