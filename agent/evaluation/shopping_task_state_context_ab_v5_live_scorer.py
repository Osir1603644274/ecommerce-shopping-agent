"""Strict production-aligned live scorer for the V5 TaskState pair."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from evaluation import shopping_task_state_context_ab_v2_live_scorer as _base


MODULE_PATH = Path(__file__).resolve()
EVALUATION_ROOT = MODULE_PATH.parent
ASSET_ROOT = EVALUATION_ROOT / "assets" / "shopping_task_state_context_ab_v5_20260829"
TREATMENT_SERVER = EVALUATION_ROOT / "shopping_task_state_context_ab_v5_server.py"
HTTP_RUNNER = EVALUATION_ROOT / "shopping_task_state_context_ab_v5_runner.py"
ACTION_SCORER = EVALUATION_ROOT / "shopping_task_state_context_ab_v5_action_scorer.py"


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _require_run_manifest(path: Path, receipts: Path, arm: str) -> dict[str, Any]:
    value = _read_json(path)
    if (
        value.get("experimentId") != "shopping-task-state-context-ab-v5-20260829"
        or value.get("arm") != arm
        or value.get("runtime") != "react_v1"
        or value.get("scenarioCount") != 24
        or value.get("turnCount") != 65
        or value.get("status") != "ACCEPT"
        or value.get("receiptsSha256") != _base._sha256(receipts)
        or value.get("privateOracleReadByRunner") is not False
    ):
        raise ValueError(f"{arm} run manifest is not an accepted V5 full run")
    return value


async def score_live_pair(
    *,
    control_receipts: Path,
    treatment_receipts: Path,
    control_run_manifest: Path,
    treatment_run_manifest: Path,
    control_action_score: Path,
    treatment_action_score: Path,
    control_base_url: str,
    treatment_base_url: str,
    debug_key: str,
    treatment_status_url: str,
    output_dir: Path,
) -> dict[str, Any]:
    control_run = _require_run_manifest(control_run_manifest, control_receipts, "control")
    treatment_run = _require_run_manifest(
        treatment_run_manifest, treatment_receipts, "treatment"
    )
    original_asset_root = _base.ASSET_ROOT
    original_treatment_server = _base.TREATMENT_SERVER
    original_http_runner = _base.HTTP_RUNNER
    original_action_scorer = _base.ACTION_SCORER
    try:
        _base.ASSET_ROOT = ASSET_ROOT
        _base.TREATMENT_SERVER = TREATMENT_SERVER
        _base.HTTP_RUNNER = HTTP_RUNNER
        _base.ACTION_SCORER = ACTION_SCORER
        result = await _base.score_live_pair(
            control_receipts=control_receipts,
            treatment_receipts=treatment_receipts,
            control_action_score=control_action_score,
            treatment_action_score=treatment_action_score,
            control_base_url=control_base_url,
            treatment_base_url=treatment_base_url,
            debug_key=debug_key,
            treatment_status_url=treatment_status_url,
            output_dir=output_dir,
        )
    finally:
        _base.ASSET_ROOT = original_asset_root
        _base.TREATMENT_SERVER = original_treatment_server
        _base.HTTP_RUNNER = original_http_runner
        _base.ACTION_SCORER = original_action_scorer

    report = result["score"]
    pair_count = report.get("pairedTurnCount")
    exact_state = report.get("semanticStateEqualTurns") == pair_count == 65
    exact_scope = report.get("normalizedCandidateScopeEqualTurns") == pair_count == 65
    exact_tools = report.get("toolNamesEqualTurns") == pair_count == 65
    no_model_failures = not any(control_run.get("modelCallFailures", {}).values()) and not any(
        treatment_run.get("modelCallFailures", {}).values()
    )
    full_action_gate = (
        report.get("controlActionGate", {}).get("evaluated", 0) > 0
        and report.get("controlActionGate", {}).get("evaluated")
        == report.get("controlActionGate", {}).get("passed")
        and report.get("treatmentActionGate", {}).get("evaluated")
        == report.get("treatmentActionGate", {}).get("passed")
    )
    strict_safe = bool(
        report.get("liveSafetyPassed")
        and exact_state
        and exact_scope
        and exact_tools
        and no_model_failures
        and full_action_gate
        and control_run.get("status") == treatment_run.get("status") == "ACCEPT"
    )
    visible_differences = int(report.get("userVisibleDifferenceTurns") or 0)
    report.update({
        "schemaVersion": "shopping-task-state-context-live-score-v5",
        "experimentId": "shopping-task-state-context-ab-v5-20260829",
        "productionAlignedRuntime": "react_v1",
        "fullCorpus": {"scenarioCount": 24, "turnCount": 65},
        "semanticStateExactPairing": exact_state,
        "candidateScopeExactPairing": exact_scope,
        "toolSequenceExactPairing": exact_tools,
        "noModelCallFailures": no_model_failures,
        "fullEligibleActionGatePassed": full_action_gate,
        "liveSafetyPassed": strict_safe,
        "verdict": (
            "READY_FOR_BLIND_REVIEW"
            if strict_safe and visible_differences > 0
            else "HOLD_NO_USER_VISIBLE_DIFFERENCE"
            if strict_safe
            else "HOLD_PRODUCTION_ALIGNED_SAFETY_NOT_PASSED"
        ),
        "humanReviewPacketGenerated": False,
        "nextGate": (
            "GENERATE_RANDOM_MIRRORED_BLIND_PACKET_AND_STOP"
            if strict_safe and visible_differences > 0
            else "STOP_NO_HUMAN_PACKET"
        ),
    })
    score_path = output_dir / "score.json"
    score_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = result["manifest"]
    manifest.update({
        "schemaVersion": "shopping-task-state-context-live-manifest-v5",
        "experimentId": report["experimentId"],
        "controlRunManifestSha256": _base._sha256(control_run_manifest),
        "treatmentRunManifestSha256": _base._sha256(treatment_run_manifest),
        "liveScorer": str(MODULE_PATH),
        "liveScorerSha256": _base._sha256(MODULE_PATH),
        "scoreSha256": _base._sha256(score_path),
        "runtimeBothArms": "react_v1",
        "scenarioCount": 24,
        "turnCount": 65,
        "productionDefaultChanged": False,
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
    parser.add_argument("--control-base-url", required=True)
    parser.add_argument("--treatment-base-url", required=True)
    parser.add_argument("--treatment-status-url", required=True)
    parser.add_argument("--debug-key", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(score_live_pair(**vars(args)))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["score"]["verdict"].startswith("HOLD_PRODUCTION_ALIGNED_SAFETY"):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
