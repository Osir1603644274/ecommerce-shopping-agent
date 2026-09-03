"""Zero-model aggregation remediation for the completed V3 trace set."""

from __future__ import annotations

import hashlib
import json
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from agent.evaluation.context_multiagent_public_pilot_v3 import (
    exact_mcnemar,
    paired_bootstrap_delta,
    percentile,
)


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "evaluation/context-multiagent-public-pilot-v3.json"
ATTEMPT = ROOT / "agent/evaluation/runs/context_multiagent_public_pilot_v3_attempt001"
OUTPUT = ROOT / "agent/evaluation/runs/context_multiagent_public_pilot_v3_attempt001_remediation001"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if OUTPUT.exists():
        raise RuntimeError("remediation output exists")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in (ATTEMPT / "traces.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    model_receipts = [json.loads(line) for line in (ATTEMPT / "model-call-receipts.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    paired: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        if row["arm"] in paired[row["scenarioId"]]:
            raise RuntimeError("duplicate arm trace")
        paired[row["scenarioId"]][row["arm"]] = row
    if len(rows) != 66 or len(paired) != 33 or any(set(pair) != {"CTX1b", "MA2"} for pair in paired.values()):
        raise RuntimeError("incomplete trace matrix")
    research = [pair for pair in paired.values() if pair["CTX1b"]["routeGold"].startswith("RESEARCH_")]
    if len(research) != 9 or len(model_receipts) != 36:
        raise RuntimeError("provider-call matrix incomplete")
    ctx_success = [bool(pair["CTX1b"]["taskSuccess"]) for pair in research]
    ma_success = [bool(pair["MA2"]["taskSuccess"]) for pair in research]
    ctx_tokens = [float(pair["CTX1b"]["inputTokens"]) for pair in research]
    ma_tokens = [float(pair["MA2"]["inputTokens"]) for pair in research]
    ctx_parent = [float(pair["CTX1b"]["parentFinalEstimatedTokens"]) for pair in research]
    ma_parent = [float(pair["MA2"]["parentFinalEstimatedTokens"]) for pair in research]
    ctx_latency = [float(pair["CTX1b"]["durationMs"]) for pair in research]
    ma_latency = [float(pair["MA2"]["durationMs"]) for pair in research]
    ctx_precision = [float(pair["CTX1b"]["finalScore"]["claimPrecision"]) for pair in research]
    ma_precision = [float(pair["MA2"]["finalScore"]["claimPrecision"]) for pair in research]
    ma_only = sum(right and not left for left, right in zip(ctx_success, ma_success))
    ctx_only = sum(left and not right for left, right in zip(ctx_success, ma_success))
    safety = sum(
        row["status"] != "ok"
        for pair in research for row in (pair["CTX1b"], pair["MA2"])
    ) + sum(pair["MA2"].get("rawChildObservationLeak") is not False for pair in research)
    communication_failures = sum(
        not isinstance(pair["MA2"].get("communication"), dict)
        or pair["MA2"]["communication"].get("requestSequence") != 1
        or pair["MA2"]["communication"].get("replySequence") != 2
        for pair in research
    )
    parent_reduction = 1 - statistics.fmean(ma_parent) / statistics.fmean(ctx_parent)
    total_ratio = statistics.fmean(ma_tokens) / statistics.fmean(ctx_tokens)
    slo = manifest["slo"]
    slo_pass = (
        percentile(ma_latency, .95) <= slo["ma1ScenarioLatencyP95Ms"]
        and parent_reduction >= slo["minimumParentContextTokenReductionRatio"]
        and total_ratio <= slo["maximumTotalInputTokenRatio"]
    )
    accepted = (
        safety == 0 and communication_failures == 0
        and sum(ma_success) >= sum(ctx_success)
        and ma_only >= 2
        and statistics.fmean(ma_precision) >= statistics.fmean(ctx_precision)
        and slo_pass
    )
    OUTPUT.mkdir(parents=True, exist_ok=False)
    started = {
        "schemaVersion": "context-multiagent-v3-remediation-started-v1",
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "modelCalls": 0,
        "sourceAttemptStatus": "FAILED_DURING_AGGREGATION_AFTER_COMPLETE_TRACE_MATRIX",
        "sourceTraceSha256": sha(ATTEMPT / "traces.jsonl"),
        "sourceModelReceiptSha256": sha(ATTEMPT / "model-call-receipts.jsonl"),
        "manifestSha256": sha(MANIFEST),
        "remediationRunnerSha256": sha(Path(__file__)),
    }
    (OUTPUT / "started.json").write_text(json.dumps(started, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    summary = {
        "schemaVersion": "context-multiagent-public-pilot-result-v3-remediation-v1",
        "status": "BOUNDED_MULTI_AGENT_V2_DEVELOPMENT_ACCEPT" if accepted else "ENGINEERING_HOLD",
        "scope": "public_development_remediation_not_untouched_confirmation_not_production_default",
        "sourceScenarioCount": len(paired),
        "researchPairCount": len(research),
        "sourceProviderCalls": len(model_receipts),
        "remediationModelCalls": 0,
        "allTaskSuccess": {"CTX1b": sum(pair["CTX1b"]["taskSuccess"] for pair in paired.values()), "MA2": sum(pair["MA2"]["taskSuccess"] for pair in paired.values())},
        "researchTaskSuccess": {"CTX1b": sum(ctx_success), "MA2": sum(ma_success), "MA2Only": ma_only, "CTX1bOnly": ctx_only},
        "evidenceClaimPrecisionMacro": {"CTX1b": statistics.fmean(ctx_precision), "MA2": statistics.fmean(ma_precision)},
        "parentFinalEstimatedTokens": {"CTX1b_P50": percentile(ctx_parent, .5), "CTX1b_P95": percentile(ctx_parent, .95), "MA2_P50": percentile(ma_parent, .5), "MA2_P95": percentile(ma_parent, .95), "MA2ReductionRatio": parent_reduction},
        "totalProviderInputTokens": {"CTX1b": sum(ctx_tokens), "MA2": sum(ma_tokens), "MA2ToCTX1bMeanRatio": total_ratio},
        "scenarioLatencyMs": {"CTX1b_P50": percentile(ctx_latency, .5), "CTX1b_P95": percentile(ctx_latency, .95), "MA2_P50": percentile(ma_latency, .5), "MA2_P95": percentile(ma_latency, .95)},
        "mcnemar": exact_mcnemar(ctx_success, ma_success),
        "bootstrap": {"taskSuccessDelta": paired_bootstrap_delta([float(v) for v in ctx_success], [float(v) for v in ma_success], seed=manifest["bootstrapSeed"], iterations=manifest["bootstrapIterations"]), "parentTokenDelta": paired_bootstrap_delta(ctx_parent, ma_parent, seed=manifest["bootstrapSeed"], iterations=manifest["bootstrapIterations"])},
        "safetyFailureCount": safety,
        "communicationFailureCount": communication_failures,
        "sloPass": slo_pass,
        "engineeringGate": {"safetyContractsPass": safety == 0, "communicationContractsPass": communication_failures == 0, "ma2NotWorseOnSuccess": sum(ma_success) >= sum(ctx_success), "ma2AtLeastTwoAdditionalSuccesses": ma_only >= 2, "evidenceClaimPrecisionNotLower": statistics.fmean(ma_precision) >= statistics.fmean(ctx_precision), "costLatencySloPass": slo_pass},
        "productionDefaultsChanged": False,
        "untouchedConfirmation": False,
    }
    summary_path = OUTPUT / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    receipt = {"status": summary["status"], "startedSha256": sha(OUTPUT / "started.json"), "summarySha256": sha(summary_path), "sourceTraceSha256": started["sourceTraceSha256"], "sourceModelReceiptSha256": started["sourceModelReceiptSha256"]}
    (OUTPUT / "receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
