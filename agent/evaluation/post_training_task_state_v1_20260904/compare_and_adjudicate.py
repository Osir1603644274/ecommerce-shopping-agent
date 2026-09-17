from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .common import PACKAGE_DIR, canonical_json, read_json, read_jsonl, sha256_file, write_json


def exact_two_sided_binomial_p(successes: int, failures: int) -> float:
    n = successes + failures
    if n == 0:
        return 1.0
    lower = min(successes, failures)
    tail = sum(math.comb(n, k) for k in range(lower + 1)) / (2**n)
    return min(1.0, 2.0 * tail)


def gate(name: str, passed: bool, observed: Any, threshold: Any) -> dict[str, Any]:
    return {
        "name": name,
        "status": "PASS" if passed else "FAIL",
        "observed": observed,
        "threshold": threshold,
    }


def main() -> None:
    comparison_path = PACKAGE_DIR / "comparison.json"
    decision_path = PACKAGE_DIR / "decision.json"
    final_md = PACKAGE_DIR / "FINAL_DECISION.md"
    for path in (comparison_path, decision_path, final_md):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite final artifact: {path}")

    frozen = read_json(PACKAGE_DIR / "frozen_config.json")
    base_result = read_json(PACKAGE_DIR / "runs" / "base_attempt001" / "result.json")
    tuned_result = read_json(PACKAGE_DIR / "runs" / "tuned_attempt001" / "result.json")
    training_result = read_json(PACKAGE_DIR / "runs" / "training_attempt001" / "result.json")
    base_rows = read_jsonl(PACKAGE_DIR / "runs" / "base_attempt001" / "rows.jsonl")
    tuned_rows = read_jsonl(PACKAGE_DIR / "runs" / "tuned_attempt001" / "rows.jsonl")
    base_by_id = {row["exampleId"]: row for row in base_rows}
    tuned_by_id = {row["exampleId"]: row for row in tuned_rows}
    pair_identity_pass = (
        base_result["modelId"] == tuned_result["modelId"] == frozen["model"]["id"]
        and base_result["modelRevision"] == tuned_result["modelRevision"] == frozen["model"]["revision"]
        and base_result["testSha256"] == tuned_result["testSha256"] == frozen["dataset"]["test"]["sha256"]
        and base_result["generationConfig"] == tuned_result["generationConfig"] == frozen["generationConfig"]
        and set(base_by_id) == set(tuned_by_id)
        and len(base_by_id) == frozen["dataset"]["test"]["rowCount"]
    )
    paired_rows: list[dict[str, Any]] = []
    improvements = 0
    regressions = 0
    for example_id in sorted(base_by_id):
        base = base_by_id[example_id]
        tuned = tuned_by_id[example_id]
        improved = not base["semanticExact"] and tuned["semanticExact"]
        regressed = base["semanticExact"] and not tuned["semanticExact"]
        improvements += int(improved)
        regressions += int(regressed)
        paired_rows.append({
            "exampleId": example_id,
            "family": base["family"],
            "baseSemanticExact": base["semanticExact"],
            "tunedSemanticExact": tuned["semanticExact"],
            "improved": improved,
            "regressed": regressed,
            "baseContractValid": base["contractValid"],
            "tunedContractValid": tuned["contractValid"],
        })
    p_value = exact_two_sided_binomial_p(improvements, regressions)
    base_metrics = base_result["metrics"]
    tuned_metrics = tuned_result["metrics"]
    lift = tuned_metrics["semanticExactRate"] - base_metrics["semanticExactRate"]
    gates_cfg = frozen["gates"]
    gates = [
        gate("pair_identity", pair_identity_pass, pair_identity_pass, True),
        gate(
            "training_completed",
            training_result.get("status") == "COMPLETED",
            training_result.get("status"),
            "COMPLETED",
        ),
        gate(
            "contract_valid_non_regression",
            tuned_metrics["contractValidRate"] >= base_metrics["contractValidRate"],
            tuned_metrics["contractValidRate"] - base_metrics["contractValidRate"],
            ">= 0",
        ),
        gate(
            "semantic_exact_floor",
            tuned_metrics["semanticExactRate"] >= gates_cfg["minimumSemanticExactRate"],
            tuned_metrics["semanticExactRate"],
            gates_cfg["minimumSemanticExactRate"],
        ),
        gate(
            "semantic_exact_lift",
            lift >= gates_cfg["minimumSemanticExactLift"],
            lift,
            gates_cfg["minimumSemanticExactLift"],
        ),
        gate(
            "false_ready_absolute",
            tuned_metrics["falseReadyRate"] <= gates_cfg["maximumFalseReadyRate"],
            tuned_metrics["falseReadyRate"],
            gates_cfg["maximumFalseReadyRate"],
        ),
        gate(
            "false_ready_non_regression",
            tuned_metrics["falseReadyRate"] <= base_metrics["falseReadyRate"],
            tuned_metrics["falseReadyRate"] - base_metrics["falseReadyRate"],
            "<= 0",
        ),
        gate(
            "paired_significance",
            p_value < gates_cfg["maximumMcNemarPValueExclusive"],
            p_value,
            f"< {gates_cfg['maximumMcNemarPValueExclusive']}",
        ),
    ]
    comparison = {
        "schemaVersion": "posttraining-paired-comparison-v1",
        "pairIdentity": {
            "status": "PASS" if pair_identity_pass else "FAIL",
            "modelId": base_result["modelId"],
            "modelRevision": base_result["modelRevision"],
            "testSha256": base_result["testSha256"],
            "generationConfig": base_result["generationConfig"],
            "rowCount": len(paired_rows),
        },
        "baseMetrics": base_metrics,
        "tunedMetrics": tuned_metrics,
        "semanticExactLift": lift,
        "paired": {
            "improvements": improvements,
            "regressions": regressions,
            "discordant": improvements + regressions,
            "mcnemarExactTwoSidedP": p_value,
        },
        "gates": gates,
        "pairedRows": paired_rows,
    }
    write_json(comparison_path, comparison)
    accepted = all(item["status"] == "PASS" for item in gates)
    decision_label = (
        "POST_TRAINING_V1_ACCEPT_OFFLINE_ADAPTER__HOLD_PRODUCTION_DEFAULT"
        if accepted
        else "POST_TRAINING_V1_HOLD"
    )
    decision = {
        "schemaVersion": "posttraining-bounded-decision-v1",
        "decision": decision_label,
        "offlineAdapter": "ACCEPT" if accepted else "HOLD",
        "productionDefault": "HOLD",
        "productionSwitchAuthorized": False,
        "humanParticipation": 0,
        "gatePassCount": sum(item["status"] == "PASS" for item in gates),
        "gateCount": len(gates),
        "failedGates": [item["name"] for item in gates if item["status"] != "PASS"],
        "limitations": [
            "synthetic programmatic data only",
            "no human generalization judgment",
            "no production traffic",
            "run identity and persistence architecture are outside training scope",
        ],
    }
    write_json(decision_path, decision)
    lines = [
        "# Post-Training V1 Final Decision",
        "",
        f"- Decision: `{decision_label}`",
        f"- Human participation: `0`",
        f"- Base semantic exact: `{base_metrics['semanticExactRate']:.4f}`",
        f"- Tuned semantic exact: `{tuned_metrics['semanticExactRate']:.4f}`",
        f"- Lift: `{lift:.4f}`",
        f"- Base/Tuned contract valid: `{base_metrics['contractValidRate']:.4f}` / `{tuned_metrics['contractValidRate']:.4f}`",
        f"- Base/Tuned false ready: `{base_metrics['falseReadyRate']:.4f}` / `{tuned_metrics['falseReadyRate']:.4f}`",
        f"- Paired improvements/regressions: `{improvements}` / `{regressions}`",
        f"- McNemar exact two-sided p: `{p_value:.8g}`",
        f"- Gates: `{decision['gatePassCount']}/{decision['gateCount']} PASS`",
        "",
        "## Authority boundary",
        "",
        "该结论只覆盖独立合成 held-out 上的离线 TaskState adapter。生产默认、真实流量、模型路由、V2.1 服务端投影、持久化与 run identity 均保持 HOLD；本实验不修改生产源码或配置。",
        "",
        "## Failed gates",
        "",
        *( ["- NONE"] if not decision["failedGates"] else [f"- {name}" for name in decision["failedGates"]] ),
    ]
    final_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    hash_paths = [
        PACKAGE_DIR / "datasets" / "manifest.json",
        PACKAGE_DIR / "datasets" / "train.jsonl",
        PACKAGE_DIR / "datasets" / "dev.jsonl",
        PACKAGE_DIR / "datasets" / "test.jsonl",
        PACKAGE_DIR / "model_source_v2.json",
        PACKAGE_DIR / "frozen_config.json",
        PACKAGE_DIR / "runs" / "training_attempt001" / "result.json",
        PACKAGE_DIR / "runs" / "training_attempt001" / "adapter" / "adapter_config.json",
        PACKAGE_DIR / "runs" / "training_attempt001" / "adapter" / "adapter_model.safetensors",
        PACKAGE_DIR / "runs" / "base_attempt001" / "result.json",
        PACKAGE_DIR / "runs" / "base_attempt001" / "rows.jsonl",
        PACKAGE_DIR / "runs" / "tuned_attempt001" / "result.json",
        PACKAGE_DIR / "runs" / "tuned_attempt001" / "rows.jsonl",
        comparison_path,
        decision_path,
        final_md,
    ]
    sums = [
        f"{sha256_file(path)}  {path.relative_to(PACKAGE_DIR).as_posix()}"
        for path in hash_paths
    ]
    (PACKAGE_DIR / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")
    print(canonical_json({
        "status": "COMPLETED",
        "decision": decision_label,
        "gates": f"{decision['gatePassCount']}/{decision['gateCount']}",
        "comparison": str(comparison_path),
    }))


if __name__ == "__main__":
    main()
