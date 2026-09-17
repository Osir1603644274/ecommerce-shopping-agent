"""Fixed three-epoch native-human-label experiment; selection uses dev only."""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from pathlib import Path

from retrieve import rows
from stage1 import SEED, digest, dump, log
from integrity import verify_model,verify_checkpoint


def train(root, preflight=False):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    from peft import LoraConfig, PeftModel, TaskType, get_peft_model
    torch.set_num_threads(2)
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    random.seed(SEED)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise RuntimeError("This fixed local training profile requires CUDA BF16")
    data_path = root / "training/kuaisearch.human.train.jsonl"
    data = rows(data_path)
    info = json.loads((root / "models/manifest.json").read_text(encoding="utf-8-sig"))["reranker"]
    verify_model(info)
    settings = {"model": info["model"], "revision": info["revision"], "train_sha256": digest(data_path),
        "split_sha256": digest(root / "queries.selected.jsonl"), "seed": SEED, "epochs": 3,
        "learning_rate": 2e-5, "effective_batch": 16, "micro_batch": 16, "max_length": 256,
        "optimizer": "AdamW", "weight_decay": 0.01, "betas": [0.9,0.999], "eps": 1e-8,
        "loss": "BCEWithLogitsLoss; native grade 3 => 1, native grades 0/1/2 => 0",
        "precision": "float32 parameters with bf16 autocast", "gradient_checkpointing": False,
        "fine_tuning": "LoRA", "lora_rank": 16, "lora_alpha": 32, "lora_dropout": 0.05,
        "lora_target_modules": ["query", "value"], "modules_to_save": ["classifier"],
        "checkpoint_selection": "After fixed training schedule, development pooled nDCG only; test never selects or tunes"}
    directory = root / "training" / ("preflight-lora-m16" if preflight else "run-lora-v1")
    directory.mkdir(parents=True,exist_ok=True)
    config_path = directory / "config.json"
    if config_path.exists() and json.loads(config_path.read_text(encoding="utf-8-sig")) != settings:
        raise ValueError("Training settings or source data changed")
    dump(config_path,settings)
    completed = []
    if not preflight:
        for epoch in range(1,4):
            path=directory/f"epoch-{epoch}"
            if (path/"complete.json").exists():
                verify_checkpoint(path,digest(config_path))
                completed.append(epoch)
    if completed == [1,2,3]:
        summary=directory/"training-complete.json"
        if not summary.exists():
            last=json.loads((directory/"epoch-3/complete.json").read_text(encoding="utf-8-sig"))
            dump(summary,{"epochs":3,"global_steps":last["step"],"config_sha256":digest(config_path),
                 "selected_checkpoint":None,"selection_status":"AWAITING_DEV_SILVER_LABELS_AND_SCORING","test_read":False})
        log("training_reuse",epochs=3); return
    last_epoch=max(completed,default=0)
    if completed != list(range(1,last_epoch+1)): raise ValueError("Noncontiguous training checkpoints")
    model_path=directory/f"epoch-{last_epoch}" if last_epoch else Path(info["path"])
    tokenizer=AutoTokenizer.from_pretrained(info["path"],local_files_only=True)
    model=AutoModelForSequenceClassification.from_pretrained(info["path"],local_files_only=True).cuda()
    if last_epoch:
        model=PeftModel.from_pretrained(model,str(model_path),is_trainable=True,local_files_only=True)
    else:
        model=get_peft_model(model,LoraConfig(task_type=TaskType.SEQ_CLS,r=16,lora_alpha=32,lora_dropout=.05,
            target_modules=["query","value"],modules_to_save=["classifier"]))
    optimizer=torch.optim.AdamW(model.parameters(),lr=2e-5,weight_decay=.01,foreach=False)
    global_step=0
    if last_epoch:
        receipt=json.loads((model_path/"complete.json").read_text(encoding="utf-8-sig"))
        if digest(model_path/"adapter_model.safetensors")!=receipt["model_sha256"] or digest(model_path/"optimizer.pt")!=receipt["optimizer_sha256"] or digest(config_path)!=receipt["config_sha256"]:
            raise ValueError("Training checkpoint integrity failure")
        saved=torch.load(model_path/"optimizer.pt",map_location="cpu",weights_only=True)
        optimizer.load_state_dict(saved["optimizer"])
        torch.set_rng_state(saved["torch_rng"])
        torch.cuda.set_rng_state_all(saved["cuda_rng"])
        global_step=saved["global_step"]
        del saved
    started=time.perf_counter()
    log("training_start",preflight=preflight,examples=len(data),completed_epochs=last_epoch,config_sha256=digest(config_path))
    epochs=[1] if preflight else range(last_epoch+1,4)
    for epoch in epochs:
        order=list(range(len(data)))
        random.Random(SEED+epoch).shuffle(order)
        if preflight: order=order[:48]+sorted(range(len(data)),key=lambda i:len(data[i]["query"])+len(data[i]["text"]),reverse=True)[:16]
        model.train()
        total_loss,total_examples=0.0,0
        epoch_start=time.perf_counter()
        for begin in range(0,len(order),16):
            group=order[begin:begin+16]
            optimizer.zero_grad(set_to_none=True)
            group_loss=0.0
            for offset in range(0,len(group),16):
                batch=[data[i] for i in group[offset:offset+16]]
                inputs=tokenizer([[r["query"],r["text"]] for r in batch],padding=True,truncation=True,max_length=256,return_tensors="pt").to("cuda")
                target=torch.tensor([r["target"] for r in batch],dtype=torch.float32,device="cuda")
                with torch.autocast("cuda",dtype=torch.bfloat16):
                    logits=model(**inputs).logits.view(-1)
                    loss=torch.nn.functional.binary_cross_entropy_with_logits(logits.float(),target,reduction="sum")/len(group)
                if not torch.isfinite(loss): raise ValueError("Non-finite training loss")
                loss.backward()
                group_loss+=loss.item()
            grad_norm=torch.nn.utils.clip_grad_norm_(model.parameters(),1.0,error_if_nonfinite=True)
            optimizer.step()
            global_step+=1
            total_examples+=len(group)
            total_loss+=group_loss*len(group)
            if global_step%25==0 or preflight:
                elapsed=time.perf_counter()-epoch_start
                event={"epoch":epoch,"step":global_step,"examples":total_examples,"loss":group_loss,
                       "mean_loss":total_loss/total_examples,"grad_norm":float(grad_norm),"examples_per_second":total_examples/elapsed,
                       "peak_cuda_mb":torch.cuda.max_memory_allocated()/1024**2}
                with (directory/"steps.jsonl").open("a",encoding="utf-8") as out: out.write(json.dumps(event)+"\n")
                log("training_progress",**event)
        if preflight:
            dump(directory/"result.json",{"complete":True,"optimizer_steps":global_step,"examples":len(order),
                 "wall_seconds":time.perf_counter()-started,"mean_loss":total_loss/total_examples,
                 "peak_cuda_mb":torch.cuda.max_memory_allocated()/1024**2,"note":"Preflight is discarded; formal training starts from the pinned original model."})
            return
        checkpoint=directory/f"epoch-{epoch}"
        if checkpoint.exists(): raise ValueError("Incomplete checkpoint directory exists; preserve it before restarting the epoch")
        checkpoint.mkdir()
        model.save_pretrained(checkpoint,safe_serialization=True)
        tokenizer.save_pretrained(checkpoint)
        torch.save({"optimizer":optimizer.state_dict(),"torch_rng":torch.get_rng_state(),
                    "cuda_rng":torch.cuda.get_rng_state_all(),"global_step":global_step},checkpoint/"optimizer.pt")
        receipt_path=checkpoint/"training-receipt.pending.json"
        dump(receipt_path,{"epoch":epoch,"examples":total_examples,"step":global_step,"mean_loss":total_loss/total_examples,
             "seconds":time.perf_counter()-epoch_start,"config_sha256":digest(config_path),
             "model_sha256":digest(checkpoint/"adapter_model.safetensors"),"optimizer_sha256":digest(checkpoint/"optimizer.pt")})
        dump(checkpoint/"inference-state.json",{"capture":"Training checkpoint completion","training_complete_sha256":digest(receipt_path),
             "files":[{"name":p.name,"bytes":p.stat().st_size,"sha256":digest(p)} for p in sorted(checkpoint.iterdir()) if p.name not in ("inference-state.json","README.md","optimizer.pt","complete.json","training-receipt.pending.json")]})
        os.replace(receipt_path,checkpoint/"complete.json")
        log("epoch_complete",epoch=epoch,steps=global_step,examples=total_examples)
    dump(directory/"training-complete.json",{"epochs":3,"global_steps":global_step,"config_sha256":digest(config_path),
         "selected_checkpoint":None,"selection_status":"AWAITING_DEV_SILVER_LABELS_AND_SCORING","test_read":False})
    log("training_complete",epochs=3,selected_checkpoint=None)


if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--root",type=Path,default=Path("D:/agent-datasets/search-stage1-v1"))
    p.add_argument("--preflight",action="store_true")
    a=p.parse_args()
    train(a.root,a.preflight)
