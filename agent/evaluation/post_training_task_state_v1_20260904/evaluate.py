from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from .common import (
    MODEL_ID,
    MODEL_REVISION,
    PACKAGE_DIR,
    canonical_json,
    parse_json_object,
    read_json,
    read_jsonl,
    render_messages,
    requirement_lanes,
    score_rows,
    semantic_effect,
    sha256_file,
    validate_arguments,
    write_json,
)


GENERATION_CONFIG = {
    "doSample": False,
    "numBeams": 1,
    "maxNewTokens": 320,
    "repetitionPenalty": 1.0,
}


def _load_model(snapshot: Path, arm: str):
    use_bf16 = bool(torch.cuda.is_available() and torch.cuda.is_bf16_supported())
    dtype = torch.bfloat16 if use_bf16 else torch.float16
    quantization = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True,
        bnb_4bit_compute_dtype=dtype,
    )
    model = AutoModelForCausalLM.from_pretrained(
        snapshot,
        local_files_only=True,
        quantization_config=quantization,
        device_map={"": 0},
        torch_dtype=dtype,
    )
    model.generation_config.temperature = None
    model.generation_config.top_p = None
    model.generation_config.top_k = None
    adapter_hash = None
    if arm == "tuned":
        training = read_json(PACKAGE_DIR / "runs" / "training_attempt001" / "result.json")
        adapter = PACKAGE_DIR / "runs" / "training_attempt001" / "adapter"
        adapter_file = adapter / "adapter_model.safetensors"
        adapter_hash = sha256_file(adapter_file)
        if adapter_hash != training["adapterSha256"]:
            raise RuntimeError("adapter hash changed after training")
        model = PeftModel.from_pretrained(model, adapter, is_trainable=False)
    model.eval()
    return model, adapter_hash


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--arm", choices=("base", "tuned"), required=True)
    args = parser.parse_args()
    attempt = f"{args.arm}_attempt001"
    run_dir = PACKAGE_DIR / "runs" / attempt
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing attempt: {run_dir}")
    run_dir.mkdir(parents=True)

    manifest = read_json(PACKAGE_DIR / "datasets" / "manifest.json")
    frozen = read_json(PACKAGE_DIR / "frozen_config.json")
    if frozen["generationConfig"] != GENERATION_CONFIG:
        raise RuntimeError("generation config differs from frozen_config.json")
    source = read_json(PACKAGE_DIR / "model_source_v2.json")
    snapshot = Path(source["snapshotPath"])
    test_path = PACKAGE_DIR / "datasets" / "test.jsonl"
    test_hash = sha256_file(test_path)
    if test_hash != manifest["splits"]["test"]["sha256"]:
        raise RuntimeError("test split changed after manifest freeze")
    tokenizer = AutoTokenizer.from_pretrained(snapshot, local_files_only=True)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"
    model, adapter_hash = _load_model(snapshot, args.arm)
    rows = read_jsonl(test_path)
    scored: list[dict[str, Any]] = []
    rows_path = run_dir / "rows.jsonl"
    started = time.time()
    with rows_path.open("w", encoding="utf-8", newline="\n") as output:
        for index, record in enumerate(rows, start=1):
            prompt = tokenizer.apply_chat_template(
                render_messages(record),
                tokenize=False,
                add_generation_prompt=True,
            )
            encoded = tokenizer(
                prompt,
                return_tensors="pt",
                add_special_tokens=False,
                truncation=True,
                max_length=1024,
            ).to(model.device)
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            row_started = time.perf_counter()
            with torch.inference_mode():
                generated = model.generate(
                    **encoded,
                    do_sample=False,
                    num_beams=GENERATION_CONFIG["numBeams"],
                    max_new_tokens=GENERATION_CONFIG["maxNewTokens"],
                    repetition_penalty=GENERATION_CONFIG["repetitionPenalty"],
                    pad_token_id=tokenizer.pad_token_id,
                    eos_token_id=tokenizer.eos_token_id,
                )
            if torch.cuda.is_available():
                torch.cuda.synchronize()
            latency_ms = (time.perf_counter() - row_started) * 1000.0
            new_tokens = generated[0, encoded["input_ids"].shape[-1] :]
            raw = tokenizer.decode(new_tokens, skip_special_tokens=True)
            prediction, strict_json, parse_error = parse_json_object(raw)
            gold_payload, _ = validate_arguments(record, record["targetArguments"])
            gold_effect = semantic_effect(record, gold_payload)
            predicted_effect = None
            validation_error = None
            error_code = parse_error
            if prediction is not None:
                try:
                    predicted_payload, _ = validate_arguments(record, prediction)
                    predicted_effect = semantic_effect(record, predicted_payload)
                except Exception as exc:
                    validation_error = str(exc)
                    error_code = str(getattr(exc, "code", "production_validation_error"))
            gold_guide = gold_effect.get("shoppingGuide")
            predicted_guide = (
                predicted_effect.get("shoppingGuide")
                if isinstance(predicted_effect, dict)
                else None
            )
            gold_lanes = requirement_lanes(gold_guide)
            predicted_lanes = requirement_lanes(predicted_guide)
            target_status = gold_effect["status"]
            predicted_status = predicted_effect.get("status") if predicted_effect else None
            row = {
                "schemaVersion": "posttraining-evaluation-row-v1",
                "arm": args.arm,
                "index": index,
                "exampleId": record["exampleId"],
                "family": record["family"],
                "templateFamily": record["templateFamily"],
                "latencyMs": latency_ms,
                "inputTokens": int(encoded["input_ids"].shape[-1]),
                "outputTokens": int(new_tokens.shape[-1]),
                "rawOutput": raw,
                "prediction": prediction,
                "parsedJson": prediction is not None,
                "strictJson": strict_json,
                "contractValid": predicted_effect is not None,
                "semanticExact": predicted_effect == gold_effect,
                "statusExact": predicted_status == target_status,
                "guideExact": predicted_guide == gold_guide,
                "falseReady": target_status == "collecting_information" and predicted_status in {"ready", "executing"},
                "falseClarify": target_status in {"ready", "executing"} and predicted_status == "collecting_information",
                "goldRequirementLaneCount": len(gold_lanes),
                "predictedRequirementLaneCount": len(predicted_lanes),
                "matchedRequirementLaneCount": len(gold_lanes & predicted_lanes),
                "errorCode": error_code,
                "validationError": validation_error,
            }
            scored.append(row)
            output.write(canonical_json(row) + "\n")
            output.flush()
            print(canonical_json({
                "arm": args.arm,
                "progress": f"{index}/{len(rows)}",
                "semanticExact": row["semanticExact"],
                "contractValid": row["contractValid"],
            }), flush=True)

    result = {
        "schemaVersion": "posttraining-evaluation-result-v1",
        "status": "COMPLETED",
        "arm": args.arm,
        "modelId": MODEL_ID,
        "modelRevision": MODEL_REVISION,
        "adapterSha256": adapter_hash,
        "testSha256": test_hash,
        "frozenConfigSha256": sha256_file(PACKAGE_DIR / "frozen_config.json"),
        "generationConfig": GENERATION_CONFIG,
        "metrics": score_rows(scored),
        "rowsSha256": sha256_file(rows_path),
        "durationSeconds": time.time() - started,
        "peakGpuMemoryBytes": (
            torch.cuda.max_memory_allocated() if torch.cuda.is_available() else None
        ),
    }
    write_json(run_dir / "result.json", result)
    print(canonical_json({
        "status": result["status"],
        "arm": args.arm,
        "metrics": result["metrics"],
        "result": str(run_dir / "result.json"),
    }))


if __name__ == "__main__":
    main()
