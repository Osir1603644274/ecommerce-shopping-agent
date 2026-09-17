"""Verify remaining artifact edges without changing any experiment scores."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .common import PACKAGE_DIR as ROOT, read_json, read_jsonl, sha256_file, write_json, canonical_json


EXPECTED_SUMS = {
    "datasets/manifest.json", "datasets/train.jsonl", "datasets/dev.jsonl", "datasets/test.jsonl",
    "model_source_v2.json", "frozen_config.json", "runs/training_attempt001/result.json",
    "runs/training_attempt001/adapter/adapter_config.json",
    "runs/training_attempt001/adapter/adapter_model.safetensors",
    "runs/base_attempt001/result.json", "runs/base_attempt001/rows.jsonl",
    "runs/tuned_attempt001/result.json", "runs/tuned_attempt001/rows.jsonl",
    "comparison.json", "decision.json", "FINAL_DECISION.md",
}


def main():
    output = ROOT / "audit_v2/final_binding_verification.json"
    if output.exists():
        raise FileExistsError(output)
    checks = []

    def check(name, passed):
        checks.append({"name": name, "passed": bool(passed)})

    frozen = read_json(ROOT / "frozen_config.json")
    comparison, decision = read_json(ROOT / "comparison.json"), read_json(ROOT / "decision.json")
    check("base_tuned_rows_equal_test_order", all(
        [r["exampleId"] for r in read_jsonl(ROOT / f"runs/{arm}_attempt001/rows.jsonl")]
        == [r["exampleId"] for r in read_jsonl(ROOT / "datasets/test.jsonl")]
        for arm in ("base", "tuned")))
    base = {r["exampleId"]: r for r in read_jsonl(ROOT / "runs/base_attempt001/rows.jsonl")}
    tuned = {r["exampleId"]: r for r in read_jsonl(ROOT / "runs/tuned_attempt001/rows.jsonl")}
    expected_pairs = [{
        "exampleId": key, "family": b["family"],
        "baseSemanticExact": b["semanticExact"], "tunedSemanticExact": tuned[key]["semanticExact"],
        "improved": not b["semanticExact"] and tuned[key]["semanticExact"],
        "regressed": b["semanticExact"] and not tuned[key]["semanticExact"],
        "baseContractValid": b["contractValid"], "tunedContractValid": tuned[key]["contractValid"],
    } for key, b in sorted(base.items())]
    check("every_paired_row", comparison["pairedRows"] == expected_pairs)
    check("complete_pair_identity", comparison["pairIdentity"] == {
        "status": "PASS", "modelId": frozen["model"]["id"], "modelRevision": frozen["model"]["revision"],
        "testSha256": frozen["dataset"]["test"]["sha256"], "generationConfig": frozen["generationConfig"],
        "rowCount": frozen["dataset"]["test"]["rowCount"],
    })
    accepted = all(g["status"] == "PASS" for g in comparison["gates"])
    label = "POST_TRAINING_V1_ACCEPT_OFFLINE_ADAPTER__HOLD_PRODUCTION_DEFAULT" if accepted else "POST_TRAINING_V1_HOLD"
    check("decision_full_label", decision["decision"] == label)
    check("markdown_matches_decision", f"Decision: `{label}`" in (ROOT / "FINAL_DECISION.md").read_text(encoding="utf-8"))
    check("decision_gate_count", decision["gateCount"] == len(comparison["gates"]) == 8)
    sum_rows = [line.split("  ", 1) for line in (ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()]
    check("checksum_list_exact_coverage", len(sum_rows) == len(EXPECTED_SUMS) and {name for _, name in sum_rows} == EXPECTED_SUMS)
    check("checksum_list_all_hashes", all(sha256_file(ROOT / name) == digest for digest, name in sum_rows))
    training = read_json(ROOT / "runs/training_attempt001/result.json")
    state = read_json(ROOT / "runs/training_attempt001/trainer/trainer_state.json")
    losses = [r["eval_loss"] for r in state["log_history"] if "eval_loss" in r]
    check("training_steps_epochs", state["global_step"] == 135 and state["num_train_epochs"] == 3)
    check("dev_best_selected", len(losses) == 3 and state["best_metric"] == min(losses))
    check("training_logs_bound", state["log_history"] == training["logHistory"])
    best = Path(state["best_model_checkpoint"]).resolve()
    best.relative_to((ROOT / "runs/training_attempt001/trainer").resolve())
    from safetensors import safe_open
    import torch
    with safe_open(best / "adapter_model.safetensors", framework="pt", device="cpu") as left, safe_open(ROOT / "runs/training_attempt001/adapter/adapter_model.safetensors", framework="pt", device="cpu") as right:
        check("adapter_equals_selected_checkpoint", set(left.keys()) == set(right.keys()) and all(torch.equal(left.get_tensor(k), right.get_tensor(k)) for k in left.keys()))
    config = read_json(ROOT / "runs/training_attempt001/adapter/adapter_config.json")
    check("lora_hyperparameters", config["r"] == 16 and config["lora_alpha"] == 32 and config["lora_dropout"] == .05 and config["bias"] == "none" and config["task_type"] == "CAUSAL_LM")
    check("lora_target_modules", set(config["target_modules"]) == {"q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"})
    for name in ("verification_final.json", "audit_v2/verification.json", "audit_v2/template_surface_isolation.json"):
        check("supporting_receipt:" + name, read_json(ROOT / name)["status"] == "PASS")
    result = {"schemaVersion": "posttraining-final-binding-check-v1", "observedAtUtc": datetime.now(timezone.utc).isoformat(),
              "status": "PASS" if all(c["passed"] for c in checks) else "FAIL", "checks": checks,
              "failedChecks": [c for c in checks if not c["passed"]], "changesPrimaryGate": False}
    write_json(output, result)
    print(canonical_json({"status": result["status"], "checks": len(checks), "failedChecks": result["failedChecks"], "output": str(output)}))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
