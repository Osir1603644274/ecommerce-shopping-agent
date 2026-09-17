from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .common import (
    DATASET_ID,
    FORBIDDEN_SOURCE_MARKERS,
    MODEL_ID,
    MODEL_REVISION,
    MODEL_WEIGHTS_SHA256,
    PACKAGE_DIR,
    REPO_DIR,
    canonical_json,
    read_json,
    read_jsonl,
    render_messages,
    sha256_file,
    sha256_text,
    validate_arguments,
    write_json,
)


def check(condition: bool, name: str, detail: Any, checks: list[dict[str, Any]]) -> None:
    checks.append({"name": name, "status": "PASS" if condition else "FAIL", "detail": detail})


def verify_dataset(checks: list[dict[str, Any]]) -> dict[str, Any]:
    manifest_path = PACKAGE_DIR / "datasets" / "manifest.json"
    manifest = read_json(manifest_path)
    check(manifest.get("datasetId") == DATASET_ID, "dataset_id", manifest.get("datasetId"), checks)

    ids_by_split: dict[str, set[str]] = {}
    prompts_by_split: dict[str, set[str]] = {}
    templates_by_split: dict[str, set[str]] = {}
    total_validated = 0
    for split in ("train", "dev", "test"):
        entry = manifest["splits"][split]
        path = PACKAGE_DIR / entry["path"]
        rows = read_jsonl(path)
        check(len(rows) == entry["rowCount"], f"{split}_row_count", len(rows), checks)
        check(sha256_file(path) == entry["sha256"], f"{split}_sha256", entry["sha256"], checks)
        ids = {row["exampleId"] for row in rows}
        prompts = set()
        templates = set()
        split_valid = True
        source_valid = True
        fingerprint_valid = True
        for row in rows:
            split_valid = split_valid and row.get("split") == split
            source = str(row.get("sourceClass", "")).casefold()
            source_valid = source == "programmatic_template_only" and not any(
                marker in source for marker in FORBIDDEN_SOURCE_MARKERS
            )
            prompt_hash = sha256_text(canonical_json(render_messages(row)))
            prompts.add(prompt_hash)
            templates.add(row["templateFamily"])
            fingerprint_valid = fingerprint_valid and (
                row["fingerprints"]["promptSha256"] == prompt_hash
                and row["fingerprints"]["targetSha256"]
                == sha256_text(canonical_json(row["targetArguments"]))
            )
            try:
                validate_arguments(row, row["targetArguments"])
            except Exception:
                split_valid = False
            total_validated += 1
        check(split_valid, f"{split}_schema_and_gold_validation", split_valid, checks)
        check(source_valid, f"{split}_source_policy", source_valid, checks)
        check(fingerprint_valid, f"{split}_fingerprints", fingerprint_valid, checks)
        check(len(ids) == len(rows), f"{split}_unique_ids", len(ids), checks)
        check(len(prompts) == len(rows), f"{split}_unique_prompts", len(prompts), checks)
        ids_by_split[split] = ids
        prompts_by_split[split] = prompts
        templates_by_split[split] = templates

    for left, right in (("train", "dev"), ("train", "test"), ("dev", "test")):
        check(not ids_by_split[left] & ids_by_split[right], f"id_isolation_{left}_{right}", 0, checks)
        check(not prompts_by_split[left] & prompts_by_split[right], f"prompt_isolation_{left}_{right}", 0, checks)
        check(not templates_by_split[left] & templates_by_split[right], f"template_isolation_{left}_{right}", 0, checks)

    for name, source in manifest["sourceHashes"].items():
        path = REPO_DIR / source["path"]
        check(path.is_file(), f"source_exists_{name}", str(path), checks)
        if path.is_file():
            check(sha256_file(path) == source["sha256"], f"source_hash_{name}", source["sha256"], checks)
    check(
        total_validated == manifest["goldProductionValidation"]["validatedRows"],
        "gold_validation_count",
        total_validated,
        checks,
    )
    return manifest


def verify_model(checks: list[dict[str, Any]]) -> dict[str, Any]:
    source_path = PACKAGE_DIR / "model_source_v2.json"
    check(source_path.is_file(), "model_source_exists", str(source_path), checks)
    if not source_path.is_file():
        return {}
    source = read_json(source_path)
    check(source.get("modelId") == MODEL_ID, "model_id", source.get("modelId"), checks)
    check(source.get("revision") == MODEL_REVISION, "model_revision", source.get("revision"), checks)
    check(source.get("weightsSha256Actual") == MODEL_WEIGHTS_SHA256, "model_weight_hash", source.get("weightsSha256Actual"), checks)
    snapshot = Path(source.get("snapshotPath", ""))
    weights = snapshot / "model.safetensors"
    check(weights.is_file(), "model_weights_exist", str(weights), checks)
    if weights.is_file():
        check(sha256_file(weights) == MODEL_WEIGHTS_SHA256, "model_weights_rehash", MODEL_WEIGHTS_SHA256, checks)
    required_model_files = (
        "README.md", "config.json", "generation_config.json",
        "merges.txt", "model.safetensors", "tokenizer.json",
        "tokenizer_config.json", "vocab.json",
    )
    for name in required_model_files:
        path = snapshot / name
        check(
            path.is_file() and path.stat().st_size > 0,
            f"model_file_nonempty_{name}",
            str(path),
            checks,
        )
    config_path = snapshot / "config.json"
    if config_path.is_file() and config_path.stat().st_size:
        config = read_json(config_path)
        check(
            config.get("model_type") == "qwen2"
            and config.get("architectures") == ["Qwen2ForCausalLM"]
            and config.get("num_hidden_layers") == 28
            and config.get("hidden_size") == 1536,
            "model_config_identity",
            {
                "model_type": config.get("model_type"),
                "architectures": config.get("architectures"),
                "num_hidden_layers": config.get("num_hidden_layers"),
                "hidden_size": config.get("hidden_size"),
            },
            checks,
        )
    return source


def verify_final(checks: list[dict[str, Any]], manifest: dict[str, Any], source: dict[str, Any]) -> None:
    required = {
        "qlora_preflight": PACKAGE_DIR / "preflight_qlora.json",
        "base_result": PACKAGE_DIR / "runs" / "base_attempt001" / "result.json",
        "base_rows": PACKAGE_DIR / "runs" / "base_attempt001" / "rows.jsonl",
        "training_result": PACKAGE_DIR / "runs" / "training_attempt001" / "result.json",
        "adapter_config": PACKAGE_DIR / "runs" / "training_attempt001" / "adapter" / "adapter_config.json",
        "adapter_weights": PACKAGE_DIR / "runs" / "training_attempt001" / "adapter" / "adapter_model.safetensors",
        "tuned_result": PACKAGE_DIR / "runs" / "tuned_attempt001" / "result.json",
        "tuned_rows": PACKAGE_DIR / "runs" / "tuned_attempt001" / "rows.jsonl",
        "comparison": PACKAGE_DIR / "comparison.json",
        "decision": PACKAGE_DIR / "decision.json",
        "final_decision": PACKAGE_DIR / "FINAL_DECISION.md",
    }
    for name, path in required.items():
        check(path.is_file() and path.stat().st_size > 0, f"final_file_{name}", str(path), checks)
    if not all(path.is_file() for path in required.values()):
        return
    base = read_json(required["base_result"])
    tuned = read_json(required["tuned_result"])
    training = read_json(required["training_result"])
    comparison = read_json(required["comparison"])
    decision = read_json(required["decision"])
    preflight = read_json(required["qlora_preflight"])
    test_hash = manifest["splits"]["test"]["sha256"]
    for arm_name, arm in (("base", base), ("tuned", tuned)):
        check(arm.get("modelId") == MODEL_ID, f"{arm_name}_model_id", arm.get("modelId"), checks)
        check(arm.get("modelRevision") == MODEL_REVISION, f"{arm_name}_model_revision", arm.get("modelRevision"), checks)
        check(arm.get("testSha256") == test_hash, f"{arm_name}_test_hash", arm.get("testSha256"), checks)
        check(arm.get("status") == "COMPLETED", f"{arm_name}_completed", arm.get("status"), checks)
    check(base.get("generationConfig") == tuned.get("generationConfig"), "paired_generation_identity", base.get("generationConfig"), checks)
    check(training.get("trainSha256") == manifest["splits"]["train"]["sha256"], "training_train_hash", training.get("trainSha256"), checks)
    check(training.get("devSha256") == manifest["splits"]["dev"]["sha256"], "training_dev_hash", training.get("devSha256"), checks)
    check(
        preflight.get("status") == "PASS"
        and preflight.get("gradientStepCompleted") is True
        and preflight.get("testRowsRead") == 0,
        "qlora_preflight",
        preflight,
        checks,
    )
    check(comparison.get("pairIdentity", {}).get("status") == "PASS", "comparison_pair_identity", comparison.get("pairIdentity"), checks)
    check(decision.get("productionDefault") == "HOLD", "production_default_hold", decision.get("productionDefault"), checks)
    check(decision.get("decision") in {
        "POST_TRAINING_V1_ACCEPT_OFFLINE_ADAPTER__HOLD_PRODUCTION_DEFAULT",
        "POST_TRAINING_V1_HOLD",
    }, "bounded_decision_label", decision.get("decision"), checks)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("dataset", "model", "final"), required=True)
    args = parser.parse_args()
    checks: list[dict[str, Any]] = []
    manifest = verify_dataset(checks)
    source: dict[str, Any] = {}
    if args.stage in {"model", "final"}:
        source = verify_model(checks)
    if args.stage == "final":
        verify_final(checks, manifest, source)
    passed = sum(check_item["status"] == "PASS" for check_item in checks)
    result = {
        "schemaVersion": "posttraining-package-verification-v1",
        "stage": args.stage,
        "status": "PASS" if passed == len(checks) else "FAIL",
        "passed": passed,
        "total": len(checks),
        "checks": checks,
    }
    output_name = (
        "verification_model_v2.json"
        if args.stage == "model"
        else f"verification_{args.stage}.json"
    )
    output = PACKAGE_DIR / output_name
    write_json(output, result)
    print(json.dumps({
        "status": result["status"],
        "stage": args.stage,
        "passed": passed,
        "total": len(checks),
        "output": str(output),
    }, ensure_ascii=False, sort_keys=True))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
