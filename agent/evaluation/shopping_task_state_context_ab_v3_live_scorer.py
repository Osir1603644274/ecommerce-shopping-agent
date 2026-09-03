"""Versioned live scorer for the V3 source-provenance repair experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from evaluation import shopping_task_state_context_ab_v2_live_scorer as _base


MODULE_PATH = Path(__file__).resolve()
EVALUATION_ROOT = MODULE_PATH.parent
ASSET_ROOT = EVALUATION_ROOT / "assets" / "shopping_task_state_context_ab_v3_20260827"
TREATMENT_SERVER = EVALUATION_ROOT / "shopping_task_state_context_ab_v3_server.py"
BASE_SCORER = Path(_base.__file__).resolve()
BASE_SERVER = EVALUATION_ROOT / "shopping_task_state_context_ab_v2_server.py"

_action = _base._action
_normalized_scope = _base._normalized_scope
_percentile = _base._percentile
_semantic_state = _base._semantic_state


async def score_live_pair(**kwargs: Any) -> dict[str, Any]:
    _base.ASSET_ROOT = ASSET_ROOT
    _base.TREATMENT_SERVER = TREATMENT_SERVER
    result = await _base.score_live_pair(**kwargs)
    output_dir = Path(kwargs["output_dir"])
    report = result["score"]
    report.update({
        "schemaVersion": "shopping-task-state-context-live-score-v3",
        "experimentId": "shopping-task-state-context-ab-v3-20260827",
        "confirmedProjectionFailureCause": (
            "V3 sourceProvenance preservation did not prevent every fail-closed projection error."
            if report["treatmentProjectionFailureTurns"]
            else None
        ),
        "nextGate": (
            "GENERATE_ANONYMOUS_MIRRORED_AB_PACKET"
            if report["verdict"] == "READY_FOR_BLIND_REVIEW"
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
        "schemaVersion": "shopping-task-state-context-live-manifest-v3",
        "experimentId": report["experimentId"],
        "liveScorer": str(MODULE_PATH),
        "liveScorerSha256": _base._sha256(MODULE_PATH),
        "baseLiveScorer": str(BASE_SCORER),
        "baseLiveScorerSha256": _base._sha256(BASE_SCORER),
        "treatmentServer": str(TREATMENT_SERVER),
        "treatmentServerSha256": _base._sha256(TREATMENT_SERVER),
        "baseTreatmentServer": str(BASE_SERVER),
        "baseTreatmentServerSha256": _base._sha256(BASE_SERVER),
        "scoreSha256": _base._sha256(score_path),
        "productionContractChangedDuringRun": False,
        "productionContractRevision": "shopping-task-state-v2-source-provenance-v1",
    })
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"manifest": manifest, "score": report}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-receipts", type=Path, required=True)
    parser.add_argument("--treatment-receipts", type=Path, required=True)
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


if __name__ == "__main__":
    main()
