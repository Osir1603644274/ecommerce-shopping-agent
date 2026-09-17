"""Read-only verification of frozen study inputs and outputs; writes new audit receipts.

Supplementary scores never select a checkpoint or change primary acceptance gates.
This is an independent verification path, not an independent human review.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from typing import Any

from .common import (
    PACKAGE_DIR as ROOT, REPO_DIR, SYSTEM_PROMPT, canonical_json,
    parse_json_object, read_json, read_jsonl, render_messages,
    requirement_lanes, score_rows, semantic_effect, sha256_file, sha256_text,
    validate_arguments, write_json,
)

AUDIT = ROOT / "audit_v2"
EXPECTED_FREEZE = "7daeeffbd014cfee77bb191a4ca9c1a85947ac5c17c5645e4f833151b1697dce"


def strict_object(raw: str) -> dict | None:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(value)

    try:
        value = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject)
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def normalize_raw(value: Any) -> Any:
    """Only drop empty containers and ignore array ordering; no contract repair."""
    if isinstance(value, dict):
        return {k: normalize_raw(v) for k, v in sorted(value.items())
                if v not in ([], {})}
    if isinstance(value, list):
        return sorted((normalize_raw(v) for v in value), key=canonical_json)
    return value


def audit(stage: str) -> dict:
    target = AUDIT / ("pre_eval_receipt.json" if stage == "pre-eval" else "verification.json")
    if target.exists():
        raise FileExistsError(f"refusing overwrite: {target}")
    checks: list[dict] = []

    def check(name, passed, detail=None):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    frozen = read_json(ROOT / "frozen_config.json")
    check("frozen_config_hash", sha256_file(ROOT / "frozen_config.json") == EXPECTED_FREEZE)
    for name, digest in frozen["scriptHashes"].items():
        check("script:" + name, sha256_file(ROOT / name) == digest)
    for key, path in [("manifestSha256", "datasets/manifest.json"),
                      ("modelSourceSha256", "model_source_v2.json"),
                      ("qloraPreflightSha256", "preflight_qlora.json")]:
        check(key, sha256_file(ROOT / path) == frozen[key])
    manifest = read_json(ROOT / "datasets/manifest.json")
    for name, item in manifest["sourceHashes"].items():
        check("source:" + name, sha256_file(REPO_DIR / item["path"]) == item["sha256"])
    for split, item in frozen["dataset"].items():
        check("split_hash:" + split,
              sha256_file(ROOT / f"datasets/{split}.jsonl") == item["sha256"])
    check("system_prompt", sha256_text(SYSTEM_PROMPT) == manifest["prompt"]["systemPromptSha256"])
    source = read_json(ROOT / "model_source_v2.json")
    snapshot = Path(source["snapshotPath"])
    for name, item in source["files"].items():
        path = snapshot / name
        check("model_file:" + name,
              path.stat().st_size == item["size"] and sha256_file(path) == item["sha256"])
        if name.endswith(".json"):
            check("model_json:" + name, isinstance(read_json(path), dict))
    check("model_revision", source["revision"] == source["modelApi"]["sha"] == frozen["model"]["revision"])
    extra: dict[str, Any] = {}
    if stage == "pre-eval":
        for arm in ("base", "tuned"):
            check("evaluation_not_started:" + arm, not (ROOT / f"runs/{arm}_attempt001").exists())
        extra["supplementaryAnalysisPlan"] = [
            "Recompute every row from raw model output using the frozen production contract.",
            "Strict JSON additionally rejects duplicate keys and nonfinite JSON constants.",
            "Raw argument exact ignores empty containers and ordering only; no server repair.",
            "Corrected guide exact requires contract-valid output.",
            "Report raw false-ready including invalid/rejected output and conditional denominator.",
            "Report paired exact effect by semantic family and disclose shared templates.",
            "Recompute all frozen gates without changing thresholds or selecting another checkpoint.",
            "Check adapter tensors, hash edges, complete-case retention and all eleven fallacies.",
        ]
        extra["timingBoundary"] = (
            "This receipt uses actual UTC clock before both eval arms; training already underway. "
            "Original manifest/freeze declared timestamps are constants, not trustworthy execution times."
        )
    else:
        prereg = read_json(AUDIT / "pre_eval_receipt.json")
        check("pre_eval_pass", prereg["status"] == "PASS")
        check("audit_code_unchanged", prereg["auditScriptSha256"] == sha256_file(Path(__file__)))
        datasets = {s: read_jsonl(ROOT / f"datasets/{s}.jsonl") for s in frozen["dataset"]}
        identity_sets = {}
        for split, records in datasets.items():
            check("split_count:" + split, len(records) == frozen["dataset"][split]["rowCount"])
            for record in records:
                prefix = "data:" + record["exampleId"]
                check(prefix + ":source", record["sourceClass"] == "programmatic_template_only")
                check(prefix + ":split", record["split"] == split)
                check(prefix + ":tool", record["targetTool"] == "update_task_state")
                fp = record["fingerprints"]
                check(prefix + ":prompt", fp["promptSha256"] == sha256_text(canonical_json(render_messages(record))))
                check(prefix + ":target", fp["targetSha256"] == sha256_text(canonical_json(record["targetArguments"])))
                validate_arguments(record, record["targetArguments"])
            identity_sets[split] = {
                "id": {r["exampleId"] for r in records},
                "prompt": {r["fingerprints"]["promptSha256"] for r in records},
                "message": {r["userMessage"] for r in records},
            }
            for key in ("id", "prompt"):
                check(f"within_split_unique:{split}:{key}", len(identity_sets[split][key]) == len(records))
            extra.setdefault("withinSplitRepeatedMessages", {})[split] = len(records) - len(identity_sets[split]["message"])
        for a, b in combinations(datasets, 2):
            for key in ("id", "prompt"):
                overlap = identity_sets[a][key] & identity_sets[b][key]
                check(f"isolation:{a}:{b}:{key}", not overlap, len(overlap))
            extra.setdefault("crossSplitMessageOverlap", {})[a + ":" + b] = len(identity_sets[a]["message"] & identity_sets[b]["message"])
        training = read_json(ROOT / "runs/training_attempt001/result.json")
        check("training_status", training["status"] == "COMPLETED")
        check("training_freeze", training["frozenConfigSha256"] == EXPECTED_FREEZE)
        check("training_config", training["trainingConfig"] == frozen["trainingConfig"])
        for split in ("train", "dev"):
            check("training_data:" + split, training[split + "Sha256"] == frozen["dataset"][split]["sha256"])
        adapter = ROOT / "runs/training_attempt001/adapter/adapter_model.safetensors"
        check("adapter_hash", sha256_file(adapter) == training["adapterSha256"])
        from safetensors import safe_open
        import torch
        with safe_open(adapter, framework="pt", device="cpu") as tensors:
            keys = list(tensors.keys())
            check("adapter_nonempty", bool(keys))
            b_nonzero = 0
            for key in keys:
                tensor = tensors.get_tensor(key)
                check("adapter_finite:" + key, torch.isfinite(tensor).all().item())
                b_nonzero += int("lora_B" in key and torch.count_nonzero(tensor).item() > 0)
            check("adapter_trained_nonzero_B", b_nonzero > 0, b_nonzero)
        test = datasets["test"]
        arm_rows, arm_results, supplements = {}, {}, {}
        for arm in ("base", "tuned"):
            directory = ROOT / f"runs/{arm}_attempt001"
            rows = read_jsonl(directory / "rows.jsonl")
            result = read_json(directory / "result.json")
            arm_rows[arm], arm_results[arm] = rows, result
            check(arm + ":complete", result["status"] == "COMPLETED")
            check(arm + ":rows_hash", result["rowsSha256"] == sha256_file(directory / "rows.jsonl"))
            check(arm + ":freeze", result["frozenConfigSha256"] == EXPECTED_FREEZE)
            check(arm + ":model", result["modelId"] == frozen["model"]["id"] and result["modelRevision"] == frozen["model"]["revision"])
            check(arm + ":test", result["testSha256"] == frozen["dataset"]["test"]["sha256"])
            check(arm + ":generation", result["generationConfig"] == frozen["generationConfig"])
            check(arm + ":adapter", result["adapterSha256"] == (training["adapterSha256"] if arm == "tuned" else None))
            check(arm + ":all_rows_ordered", [r["exampleId"] for r in rows] == [r["exampleId"] for r in test])
            counts = Counter()
            for index, (record, row) in enumerate(zip(test, rows), 1):
                prediction, strict, parse_error = parse_json_object(row["rawOutput"])
                gold, _ = validate_arguments(record, record["targetArguments"])
                gold_effect = semantic_effect(record, gold)
                effect, error, validation_error = None, parse_error, None
                if prediction is not None:
                    try:
                        payload, _ = validate_arguments(record, prediction)
                        effect = semantic_effect(record, payload)
                    except Exception as exc:
                        error = str(getattr(exc, "code", "production_validation_error"))
                        validation_error = str(exc)
                pred_guide = effect.get("shoppingGuide") if effect else None
                gold_guide = gold_effect["shoppingGuide"]
                pred_status = effect["status"] if effect else None
                gold_status = gold_effect["status"]
                pred_lanes, gold_lanes = requirement_lanes(pred_guide), requirement_lanes(gold_guide)
                expected = {
                    "arm": arm, "index": index, "family": record["family"],
                    "templateFamily": record["templateFamily"], "prediction": prediction,
                    "parsedJson": prediction is not None, "strictJson": strict,
                    "contractValid": effect is not None, "semanticExact": effect == gold_effect,
                    "statusExact": pred_status == gold_status, "guideExact": pred_guide == gold_guide,
                    "falseReady": gold_status == "collecting_information" and pred_status in {"ready", "executing"},
                    "falseClarify": gold_status in {"ready", "executing"} and pred_status == "collecting_information",
                    "goldRequirementLaneCount": len(gold_lanes), "predictedRequirementLaneCount": len(pred_lanes),
                    "matchedRequirementLaneCount": len(gold_lanes & pred_lanes),
                    "errorCode": error, "validationError": validation_error,
                }
                differences = [k for k, v in expected.items() if row[k] != v]
                check(f"{arm}:raw_rescore:{record['exampleId']}", not differences, differences)
                counts["strictRFCObject"] += strict_object(row["rawOutput"]) is not None
                counts["rawArgumentExact"] += prediction is not None and normalize_raw(prediction) == normalize_raw(record["targetArguments"])
                counts["contractValidGuideExact"] += effect is not None and pred_guide == gold_guide
                raw_ready = (prediction or {}).get("status") in ("ready", "executing")
                is_blocking = gold_status == "collecting_information"
                counts["blockingGoldCount"] += is_blocking
                counts["rawFalseReady"] += is_blocking and raw_ready
                counts["rejectedRawFalseReady"] += is_blocking and raw_ready and effect is None
            check(arm + ":metrics_recomputed", score_rows(rows) == result["metrics"])
            supplements[arm] = {"counts": dict(counts), "denominator": len(test),
                "rawFalseReadyConditionalRate": counts["rawFalseReady"] / max(1, counts["blockingGoldCount"])}
        base, tuned = arm_rows["base"], arm_rows["tuned"]
        improved = sum(not b["semanticExact"] and t["semanticExact"] for b, t in zip(base, tuned))
        regressed = sum(b["semanticExact"] and not t["semanticExact"] for b, t in zip(base, tuned))
        n = improved + regressed
        p = min(1., 2 * sum(math.comb(n, k) for k in range(min(improved, regressed) + 1)) / 2 ** n) if n else 1.
        bm, tm = arm_results["base"]["metrics"], arm_results["tuned"]["metrics"]
        lift = tm["semanticExactRate"] - bm["semanticExactRate"]
        g = frozen["gates"]
        gates = {
            "pair_identity": all(c["passed"] for c in checks if c["name"].split(":")[0] in {"base", "tuned"}),
            "training_completed": training["status"] == "COMPLETED",
            "contract_valid_non_regression": tm["contractValidRate"] >= bm["contractValidRate"],
            "semantic_exact_floor": tm["semanticExactRate"] >= g["minimumSemanticExactRate"],
            "semantic_exact_lift": lift >= g["minimumSemanticExactLift"],
            "false_ready_absolute": tm["falseReadyRate"] <= g["maximumFalseReadyRate"],
            "false_ready_non_regression": tm["falseReadyRate"] <= bm["falseReadyRate"],
            "paired_significance": p < g["maximumMcNemarPValueExclusive"],
        }
        comparison, decision = read_json(ROOT / "comparison.json"), read_json(ROOT / "decision.json")
        check("comparison_counts", comparison["paired"] == {"improvements": improved, "regressions": regressed, "discordant": n, "mcnemarExactTwoSidedP": p})
        check("comparison_lift", comparison["semanticExactLift"] == lift)
        check("comparison_metrics", comparison["baseMetrics"] == bm and comparison["tunedMetrics"] == tm)
        check("primary_gates", {v["name"]: v["status"] == "PASS" for v in comparison["gates"]} == gates)
        accepted = all(gates.values())
        check("decision_recomputed", decision["offlineAdapter"] == ("ACCEPT" if accepted else "HOLD") and decision["gatePassCount"] == sum(gates.values()) and decision["failedGates"] == [k for k, v in gates.items() if not v])
        check("production_hold", decision["productionDefault"] == "HOLD" and decision["productionSwitchAuthorized"] is False)
        for line in (ROOT / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
            digest, name = line.split("  ", 1)
            check("package_checksum:" + name, sha256_file(ROOT / name) == digest)
        family_effects = {}
        for family in sorted({r["family"] for r in test}):
            pairs = [(b, t) for b, t in zip(base, tuned) if b["family"] == family]
            family_effects[family] = {"n": len(pairs), "baseExact": sum(b["semanticExact"] for b, _ in pairs),
                "tunedExact": sum(t["semanticExact"] for _, t in pairs)}
        extra.update(supplementaryMetrics=supplements, familyEffects=family_effects,
                     primaryDecision=decision["decision"], mcnemarP=p, semanticExactLift=lift,
                     statisticalConfidence="CAUTION", trainingReproducibility="NOT_RERUN")
    result = {"schemaVersion": "posttraining-completion-audit-v2", "stage": stage,
              "observedAtUtc": datetime.now(timezone.utc).isoformat(),
              "status": "PASS" if all(c["passed"] for c in checks) else "FAIL",
              "auditScriptSha256": sha256_file(Path(__file__)),
              "frozenConfigSha256": sha256_file(ROOT / "frozen_config.json"),
              "checkCount": len(checks), "failedChecks": [c for c in checks if not c["passed"]],
              "checks": checks, **extra}
    write_json(target, result)
    print(canonical_json({"status": result["status"], "checks": len(checks), "failedChecks": result["failedChecks"], "path": str(target)}))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["pre-eval", "final"], required=True)
    raise SystemExit(0 if audit(parser.parse_args().stage)["status"] == "PASS" else 1)
