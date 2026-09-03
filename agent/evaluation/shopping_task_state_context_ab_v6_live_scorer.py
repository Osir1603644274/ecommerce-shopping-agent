"""Confirmatory V6 paired scorer with oracle-backed state safety."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from evaluation import shopping_task_state_context_ab_v5_live_scorer as _v5
from evaluation import shopping_task_state_context_ab_v2_live_scorer as _base


MODULE_PATH = Path(__file__).resolve()
EVALUATION_ROOT = MODULE_PATH.parent
PROJECT_ROOT = EVALUATION_ROOT.parent.parent
ASSET_ROOT = EVALUATION_ROOT / "assets" / "shopping_task_state_context_ab_v6_20260829"
ORACLE_SCORER = EVALUATION_ROOT / "shopping_task_state_context_ab_v6_oracle_scorer.py"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _gate_complete(gate: dict[str, Any], *, allow_not_evaluated: bool = False) -> bool:
    evaluated = gate.get("evaluated")
    passed = gate.get("passed")
    if type(evaluated) is not int or evaluated <= 0 or passed != evaluated:
        return False
    if not allow_not_evaluated and gate.get("notEvaluated", 0) != 0:
        return False
    return True


def _state_gate_complete(gate: dict[str, Any]) -> bool:
    evaluated = gate.get("evaluatedChecks")
    return type(evaluated) is int and evaluated > 0 and gate.get("passedChecks") == evaluated


def _verify_production_freeze() -> None:
    prereg = _read_json(ASSET_ROOT / "manifest.json")
    for relative, expected in prereg.get("productionSourceFreeze", {}).items():
        path = PROJECT_ROOT / relative
        actual = _base._sha256(path)
        if actual != expected:
            raise ValueError(f"production source freeze mismatch: {relative}")
    pins = prereg.get("scorerSourceFreeze") or {}
    for path, expected in (
        (ORACLE_SCORER, pins.get("oracleScorerSha256")),
        (MODULE_PATH, pins.get("pairedScorerSha256")),
    ):
        if not expected or _base._sha256(path) != expected:
            raise ValueError(f"scorer source freeze mismatch: {path.name}")


async def score_live_pair(
    *,
    control_receipts: Path,
    treatment_receipts: Path,
    control_run_manifest: Path,
    treatment_run_manifest: Path,
    control_action_score: Path,
    treatment_action_score: Path,
    control_oracle_score: Path,
    treatment_oracle_score: Path,
    control_base_url: str,
    treatment_base_url: str,
    debug_key: str,
    treatment_status_url: str,
    output_dir: Path,
) -> dict[str, Any]:
    _verify_production_freeze()
    original_asset_root = _v5.ASSET_ROOT
    try:
        _v5.ASSET_ROOT = ASSET_ROOT
        result = await _v5.score_live_pair(
            control_receipts=control_receipts,
            treatment_receipts=treatment_receipts,
            control_run_manifest=control_run_manifest,
            treatment_run_manifest=treatment_run_manifest,
            control_action_score=control_action_score,
            treatment_action_score=treatment_action_score,
            control_base_url=control_base_url,
            treatment_base_url=treatment_base_url,
            debug_key=debug_key,
            treatment_status_url=treatment_status_url,
            output_dir=output_dir,
        )
    finally:
        _v5.ASSET_ROOT = original_asset_root

    control_oracle = _read_json(control_oracle_score)
    treatment_oracle = _read_json(treatment_oracle_score)
    oracle_reports = {"control": control_oracle, "treatment": treatment_oracle}
    oracle_safe = all(
        not report.get("failures")
        and _gate_complete(report.get("actionGate") or {}, allow_not_evaluated=True)
        and _gate_complete(report.get("relationGate") or {}, allow_not_evaluated=True)
        and _state_gate_complete(report.get("stateGate") or {})
        for report in oracle_reports.values()
    )

    report = result["score"]
    pair_count = report.get("pairedTurnCount")
    strict_safe = bool(
        pair_count == 65
        and report.get("normalizedCandidateScopeEqualTurns") == 65
        and report.get("toolNamesEqualTurns") == 65
        and report.get("noModelCallFailures") is True
        and report.get("fullEligibleActionGatePassed") is True
        and (report.get("treatmentProjection") or {}).get("projectionFailures") == 0
        and (report.get("treatmentProjection") or {}).get("projectionCount", 0) > 0
        and oracle_safe
    )
    visible = int(report.get("userVisibleDifferenceTurns") or 0)
    report.update({
        "schemaVersion": "shopping-task-state-context-live-score-v6",
        "experimentId": "shopping-task-state-context-ab-v6-20260829",
        "semanticStateExactPairing": report.get("semanticStateEqualTurns") == 65,
        "semanticStateExactPairingIsGate": False,
        "semanticStateGate": "private_oracle_state_checks_plus_session_inventory",
        "oracleSafetyPassed": oracle_safe,
        "oracleGates": {
            arm: {
                "actionGate": value.get("actionGate"),
                "relationGate": value.get("relationGate"),
                "stateGate": value.get("stateGate"),
            }
            for arm, value in oracle_reports.items()
        },
        "liveSafetyPassed": strict_safe,
        "verdict": (
            "READY_FOR_BLIND_REVIEW"
            if strict_safe and visible > 0
            else "HOLD_NO_USER_VISIBLE_DIFFERENCE"
            if strict_safe
            else "HOLD_PRODUCTION_ALIGNED_SAFETY_NOT_PASSED"
        ),
        "humanReviewPacketGenerated": False,
        "nextGate": (
            "GENERATE_RANDOM_MIRRORED_BLIND_PACKET_AND_STOP"
            if strict_safe and visible > 0
            else "STOP_NO_HUMAN_PACKET"
        ),
    })

    action_status = {
        arm: {
            (str(item["scenarioId"]), str(item["turnId"])): item["actionStatus"]
            for item in oracle_reports[arm].get("checks") or []
        }
        for arm in ("control", "treatment")
    }
    trace_path = output_dir / "paired-trace-metrics.jsonl"
    rows = _base._read_jsonl(trace_path)
    for row in rows:
        key = (str(row["scenarioId"]), str(row["turnId"]))
        row["controlActionStatus"] = action_status["control"].get(key, "NOT_EVALUATED")
        row["treatmentActionStatus"] = action_status["treatment"].get(key, "NOT_EVALUATED")
        row.pop("controlActionPass", None)
        row.pop("treatmentActionPass", None)
        row.pop("treatmentRegression", None)
    trace_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )
    score_path = output_dir / "score.json"
    score_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = result["manifest"]
    manifest.update({
        "schemaVersion": "shopping-task-state-context-live-manifest-v6",
        "experimentId": report["experimentId"],
        "preregistrationSha256": _base._sha256(ASSET_ROOT / "manifest.json"),
        "selectionSha256": _base._sha256(ASSET_ROOT / "selection.json"),
        "liveScorer": str(MODULE_PATH),
        "liveScorerSha256": _base._sha256(MODULE_PATH),
        "oracleScorerSha256": _base._sha256(ORACLE_SCORER),
        "controlOracleScoreSha256": _base._sha256(control_oracle_score),
        "treatmentOracleScoreSha256": _base._sha256(treatment_oracle_score),
        "pairedTraceMetricsSha256": _base._sha256(trace_path),
        "scoreSha256": _base._sha256(score_path),
        "semanticStateExactPairingIsGate": False,
    })
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"manifest": manifest, "score": report}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-receipts", type=Path, required=True)
    parser.add_argument("--treatment-receipts", type=Path, required=True)
    parser.add_argument("--control-run-manifest", type=Path, required=True)
    parser.add_argument("--treatment-run-manifest", type=Path, required=True)
    parser.add_argument("--control-action-score", type=Path, required=True)
    parser.add_argument("--treatment-action-score", type=Path, required=True)
    parser.add_argument("--control-oracle-score", type=Path, required=True)
    parser.add_argument("--treatment-oracle-score", type=Path, required=True)
    parser.add_argument("--control-base-url", required=True)
    parser.add_argument("--treatment-base-url", required=True)
    parser.add_argument("--treatment-status-url", required=True)
    parser.add_argument("--debug-key", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(score_live_pair(**vars(args)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["score"]["verdict"].startswith("HOLD_PRODUCTION_ALIGNED"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
