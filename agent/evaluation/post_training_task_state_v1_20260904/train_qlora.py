from __future__ import annotations

import json
import os
import platform
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import torch
from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
from torch.utils.data import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    BitsAndBytesConfig,
    Trainer,
    TrainingArguments,
)

from .common import (
    MODEL_ID,
    MODEL_REVISION,
    PACKAGE_DIR,
    canonical_json,
    read_json,
    read_jsonl,
    render_messages,
    sha256_file,
    write_json,
)


SEED = 9042026
MAX_LENGTH = 1024
TRAINING_CONFIG = {
    "method": "QLoRA",
    "quantization": "NF4",
    "doubleQuantization": True,
    "loraR": 16,
    "loraAlpha": 32,
    "loraDropout": 0.05,
    "targetModules": "all-linear",
    "epochs": 3.0,
    "learningRate": 2e-4,
    "batchSize": 1,
    "gradientAccumulationSteps": 8,
    "warmupRatio": 0.05,
    "weightDecay": 0.0,
    "maxLength": MAX_LENGTH,
    "seed": SEED,
}


class TaskStateSftDataset(Dataset):
    def __init__(self, rows: list[dict[str, Any]], tokenizer: Any):
        self.items: list[dict[str, list[int]]] = []
        for row in rows:
            messages = render_messages(row)
            target = canonical_json(row["targetArguments"])
            prompt_text = tokenizer.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=True,
            )
            full_text = tokenizer.apply_chat_template(
                [*messages, {"role": "assistant", "content": target}],
                tokenize=False,
                add_generation_prompt=False,
            )
            prompt_ids = tokenizer(prompt_text, add_special_tokens=False)["input_ids"]
            full_ids = tokenizer(full_text, add_special_tokens=False)["input_ids"]
            if full_ids[: len(prompt_ids)] != prompt_ids:
                raise ValueError(f"chat-template prefix mismatch for {row['exampleId']}")
            if len(full_ids) > MAX_LENGTH:
                raise ValueError(
                    f"encoded example exceeds max length: {row['exampleId']}={len(full_ids)}"
                )
            labels = [-100] * len(prompt_ids) + full_ids[len(prompt_ids) :]
            if all(label == -100 for label in labels):
                raise ValueError(f"no assistant labels for {row['exampleId']}")
            self.items.append({
                "input_ids": full_ids,
                "attention_mask": [1] * len(full_ids),
                "labels": labels,
            })

    def __len__(self) -> int:
        return len(self.items)

    def __getitem__(self, index: int) -> dict[str, list[int]]:
        return self.items[index]


@dataclass
class CausalCollator:
    pad_token_id: int

    def __call__(self, features: list[dict[str, list[int]]]) -> dict[str, torch.Tensor]:
        max_length = max(len(feature["input_ids"]) for feature in features)
        input_ids: list[list[int]] = []
        attention_mask: list[list[int]] = []
        labels: list[list[int]] = []
        for feature in features:
            padding = max_length - len(feature["input_ids"])
            input_ids.append(feature["input_ids"] + [self.pad_token_id] * padding)
            attention_mask.append(feature["attention_mask"] + [0] * padding)
            labels.append(feature["labels"] + [-100] * padding)
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
        }


def _versions() -> dict[str, str]:
    import accelerate
    import bitsandbytes
    import peft
    import transformers

    return {
        "python": sys.version,
        "torch": torch.__version__,
        "transformers": transformers.__version__,
        "peft": peft.__version__,
        "accelerate": accelerate.__version__,
        "bitsandbytes": bitsandbytes.__version__,
    }


def main() -> None:
    run_dir = PACKAGE_DIR / "runs" / "training_attempt001"
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing attempt: {run_dir}")
    run_dir.mkdir(parents=True)
    started = time.time()
    manifest = read_json(PACKAGE_DIR / "datasets" / "manifest.json")
    frozen = read_json(PACKAGE_DIR / "frozen_config.json")
    source = read_json(PACKAGE_DIR / "model_source_v2.json")
    train_path = PACKAGE_DIR / "datasets" / "train.jsonl"
    dev_path = PACKAGE_DIR / "datasets" / "dev.jsonl"
    if sha256_file(train_path) != manifest["splits"]["train"]["sha256"]:
        raise RuntimeError("train split changed after manifest freeze")
    if sha256_file(dev_path) != manifest["splits"]["dev"]["sha256"]:
        raise RuntimeError("dev split changed after manifest freeze")
    if frozen["trainingConfig"] != TRAINING_CONFIG:
        raise RuntimeError("training config differs from frozen_config.json")

    random.seed(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    snapshot = Path(source["snapshotPath"])
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    train_rows = read_jsonl(train_path)
    dev_rows = read_jsonl(dev_path)
    train_dataset = TaskStateSftDataset(train_rows, tokenizer)
    dev_dataset = TaskStateSftDataset(dev_rows, tokenizer)

    use_bf16 = bool(torch.cuda.is_available() and torch.cuda.is_bf16_supported())
    compute_dtype = torch.bfloat16 if use_bf16 else torch.float16
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=compute_dtype,
    )
    model = AutoModelForCausalLM.from_pretrained(
        snapshot,
        local_files_only=True,
        quantization_config=quantization,
        device_map={"": 0},
        torch_dtype=compute_dtype,
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
    trainable, total = model.get_nb_trainable_parameters()

    trainer_output = run_dir / "trainer"
    arguments = TrainingArguments(
        output_dir=str(trainer_output),
        overwrite_output_dir=False,
        num_train_epochs=TRAINING_CONFIG["epochs"],
        per_device_train_batch_size=TRAINING_CONFIG["batchSize"],
        per_device_eval_batch_size=1,
        gradient_accumulation_steps=TRAINING_CONFIG["gradientAccumulationSteps"],
        learning_rate=TRAINING_CONFIG["learningRate"],
        warmup_ratio=TRAINING_CONFIG["warmupRatio"],
        weight_decay=TRAINING_CONFIG["weightDecay"],
        lr_scheduler_type="cosine",
        optim="adamw_torch",
        logging_strategy="steps",
        logging_steps=5,
        evaluation_strategy="epoch",
        save_strategy="epoch",
        save_total_limit=1,
        load_best_model_at_end=True,
        metric_for_best_model="eval_loss",
        greater_is_better=False,
        bf16=use_bf16,
        fp16=not use_bf16,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        max_grad_norm=1.0,
        report_to=[],
        remove_unused_columns=False,
        seed=SEED,
        data_seed=SEED,
        dataloader_num_workers=0,
    )
    trainer = Trainer(
        model=model,
        args=arguments,
        train_dataset=train_dataset,
        eval_dataset=dev_dataset,
        data_collator=CausalCollator(tokenizer.pad_token_id),
        processing_class=tokenizer,
    )
    result = trainer.train()
    adapter_dir = run_dir / "adapter"
    model.save_pretrained(adapter_dir, safe_serialization=True)
    tokenizer.save_pretrained(adapter_dir)
    trainer.save_state()
    adapter_file = adapter_dir / "adapter_model.safetensors"
    receipt = {
        "schemaVersion": "posttraining-training-result-v1",
        "status": "COMPLETED",
        "modelId": MODEL_ID,
        "modelRevision": MODEL_REVISION,
        "trainSha256": sha256_file(train_path),
        "devSha256": sha256_file(dev_path),
        "frozenConfigSha256": sha256_file(PACKAGE_DIR / "frozen_config.json"),
        "trainingConfig": TRAINING_CONFIG,
        "trainRows": len(train_rows),
        "devRows": len(dev_rows),
        "trainableParameters": trainable,
        "totalParameters": total,
        "trainablePercent": 100.0 * trainable / total,
        "metrics": result.metrics,
        "logHistory": trainer.state.log_history,
        "adapterPath": str(adapter_dir),
        "adapterSha256": sha256_file(adapter_file),
        "durationSeconds": time.time() - started,
        "environment": {
            "platform": platform.platform(),
            "gpu": torch.cuda.get_device_name(0),
            "computeDtype": str(compute_dtype),
            "versions": _versions(),
        },
    }
    write_json(run_dir / "result.json", receipt)
    print(json.dumps({
        "status": receipt["status"],
        "adapter": str(adapter_dir),
        "adapterSha256": receipt["adapterSha256"],
        "trainRuntime": result.metrics.get("train_runtime"),
    }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
