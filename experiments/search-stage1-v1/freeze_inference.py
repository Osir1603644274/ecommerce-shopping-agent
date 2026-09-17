"""Explicit post-run audit migration of earlier receipts; never claim pre-run sealing."""
import json
from pathlib import Path
from stage1 import dump,digest,log
from integrity import verify_checkpoint,verify_model,inference_binding,checked_file

ROOT=Path("D:/agent-datasets/search-stage1-v1")

def freeze():
    from transformers import AutoTokenizer
    info=json.loads((ROOT/"models/manifest.json").read_text(encoding="utf-8-sig"))["reranker"]
    verify_model(info)
    base=AutoTokenizer.from_pretrained(info["path"],local_files_only=True)
    probes=[["普通商品搜索 iPhone 15","品牌、型号、尺寸 256GB"],["孕妇控制体重的食物","食品及规格"],["测试","a "*400]]
    for epoch in (1,2,3):
        path=ROOT/"training/run-lora-v1"/f"epoch-{epoch}"
        verify_checkpoint(path,digest(path.parent/"config.json"),require_inference=False)
        config=json.loads((path/"adapter_config.json").read_text(encoding="utf-8-sig"))
        if config["r"]!=16 or config["lora_alpha"]!=32 or config["lora_dropout"]!=.05 or set(config["target_modules"])!={"query","value"} or Path(config["base_model_name_or_path"]).resolve()!=Path(info["path"]).resolve():raise ValueError("Saved adapter differs from fixed training profile")
        tokenizer=AutoTokenizer.from_pretrained(path,local_files_only=True)
        if tokenizer(probes,padding=True,truncation=True,max_length=256)!=base(probes,padding=True,truncation=True,max_length=256):raise ValueError("Saved tokenizer differs from base probes")
        state={"capture":"Post-training integrity audit; not an original pre-run fingerprint", "training_complete_sha256":digest(path/"complete.json"),
            "files":[{"name":p.name,"bytes":p.stat().st_size,"sha256":digest(p)} for p in sorted(path.iterdir()) if p.name not in ("inference-state.json","README.md","optimizer.pt","complete.json")]}
        target=path/"inference-state.json"
        if target.exists() and json.loads(target.read_text(encoding="utf-8-sig"))!=state:raise ValueError("Refusing inference snapshot drift")
        dump(target,state)
    for source in ("kuaisearch","multicpr"):
        for split in ("dev","test"):
            path=ROOT/"pools/inputs"/f"{source}-{split}"
            seal(path,info["path"],"ce.jsonl")
        for epoch in (1,2,3):
            path=ROOT/"evaluation/scores"/f"{source}-dev"/f"epoch-{epoch}"
            receipt=json.loads((path/"complete.json").read_text(encoding="utf-8-sig"))
            checked_file(path/"scores.jsonl",receipt["scores_sha256"])
            seal(path,ROOT/"training/run-lora-v1"/f"epoch-{epoch}","scores.jsonl")
    log("inference_audit_sealed",historical_pre_run_fingerprint=False)

def seal(path,model,score):
    state={"inference":inference_binding(model),"scores_sha256":digest(path/score),"capture":"Post-run audit migration; existing score bytes preserved"}
    target=path/"inference-receipt.json"
    if target.exists() and json.loads(target.read_text(encoding="utf-8-sig"))!=state:raise ValueError("Existing inference receipt drift")
    dump(target,state)

if __name__=="__main__":freeze()
