from __future__ import annotations

import json
import time
from pathlib import Path

import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from .common import PACKAGE_DIR, read_json, read_jsonl, write_json
from .train_qlora import CausalCollator, TRAINING_CONFIG, TaskStateSftDataset


def main() -> None:
    output = PACKAGE_DIR / "preflight_qlora.json"
    if output.exists():
        raise FileExistsError(f"refusing to overwrite preflight: {output}")
    source = read_json(PACKAGE_DIR / "model_source_v2.json")
    snapshot = Path(source["snapshotPath"])
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    train_rows = read_jsonl(PACKAGE_DIR / "datasets" / "train.jsonl")
    dev_rows = read_jsonl(PACKAGE_DIR / "datasets" / "dev.jsonl")
    train_dataset = TaskStateSftDataset(train_rows, tokenizer)
    dev_dataset = TaskStateSftDataset(dev_rows, tokenizer)
    max_train_tokens = max(len(item["input_ids"]) for item in train_dataset.items)
    max_dev_tokens = max(len(item["input_ids"]) for item in dev_dataset.items)

    dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=dtype,
    )
    started = time.time()
    model = AutoModelForCausalLM.from_pretrained(
        snapshot,
        local_files_only=True,
        quantization_config=quantization,
        device_map={"": 0},
        torch_dtype=dtype,
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(
        model,
        use_gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
    )
    model = get_peft_model(model, LoraConfig(
        r=TRAINING_CONFIG["loraR"],
        lora_alpha=TRAINING_CONFIG["loraAlpha"],
        lora_dropout=TRAINING_CONFIG["loraDropout"],
        target_modules=TRAINING_CONFIG["targetModules"],
        bias="none",
        task_type="CAUSAL_LM",
    ))
    model.train()
    batch = CausalCollator(tokenizer.pad_token_id)([train_dataset[0]])
    batch = {key: value.to(model.device) for key, value in batch.items()}
    result = model(**batch)
    if not torch.isfinite(result.loss):
        raise RuntimeError("preflight loss is not finite")
    result.loss.backward()
    torch.cuda.synchronize()
    trainable, total = model.get_nb_trainable_parameters()
    receipt = {
        "schemaVersion": "posttraining-qlora-preflight-v1",
        "status": "PASS",
        "trainRowsRead": len(train_rows),
        "devRowsRead": len(dev_rows),
        "testRowsRead": 0,
        "maxTrainTokens": max_train_tokens,
        "maxDevTokens": max_dev_tokens,
        "configuredMaxLength": TRAINING_CONFIG["maxLength"],
        "singleBatchLoss": float(result.loss.detach().cpu()),
        "gradientStepCompleted": True,
        "trainableParameters": trainable,
        "totalParameters": total,
        "peakGpuMemoryBytes": torch.cuda.max_memory_allocated(),
        "durationSeconds": time.time() - started,
    }
    write_json(output, receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()

