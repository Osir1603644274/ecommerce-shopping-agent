"""V4 TaskState paired gate after the common fixed_v1 action repair.

The independent variable and nine deterministic states are inherited unchanged
from V3.  This wrapper only binds a new preregistration/source freeze so the
common baseline repair cannot be misattributed to the treatment arm.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from evaluation import shopping_task_state_context_ab_v3 as _base


MODULE_PATH = Path(__file__).resolve()
ASSET_ROOT = MODULE_PATH.parent / "assets" / "shopping_task_state_context_ab_v4_20260827"
PREREGISTRATION = ASSET_ROOT / "manifest.json"
SELECTION = ASSET_ROOT / "selection.json"
BASE_RUNNER = Path(_base.__file__).resolve()
V2TaskSemanticProjectionError = _base.V2TaskSemanticProjectionError
build_gate_states = _base.build_gate_states
_state_hash = _base._state_hash


@contextmanager
def _configured_base():
    original = (_base.ASSET_ROOT, _base.PREREGISTRATION, _base.SELECTION)
    try:
        _base.ASSET_ROOT = ASSET_ROOT
        _base.PREREGISTRATION = PREREGISTRATION
        _base.SELECTION = SELECTION
        yield
    finally:
        _base.ASSET_ROOT, _base.PREREGISTRATION, _base.SELECTION = original


def _verify_freeze() -> dict[str, Any]:
    with _configured_base():
        return _base._verify_freeze()


def project_v2_task_semantics(state):
    with _configured_base():
        return _base.project_v2_task_semantics(state)


async def build_context_pack_from_v2_task_semantics(state, *, run_id: str):
    with _configured_base():
        return await _base.build_context_pack_from_v2_task_semantics(
            state,
            run_id=run_id,
        )


async def run_deterministic_safety_gate(output_dir: Path) -> dict[str, Any]:
    with _configured_base():
        result = await _base.run_deterministic_safety_gate(output_dir)
    score = result["score"]
    score.update({
        "schemaVersion": "shopping-task-state-context-safety-score-v4",
        "experimentId": "shopping-task-state-context-ab-v4-20260827",
    })
    score_path = output_dir / "score.json"
    score_path.write_text(
        json.dumps(score, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = result["manifest"]
    manifest.update({
        "schemaVersion": "shopping-task-state-context-safety-manifest-v4",
        "experimentId": score["experimentId"],
        "runner": str(MODULE_PATH),
        "runnerSha256": _base._base._sha256(MODULE_PATH),
        "baseRunner": str(BASE_RUNNER),
        "baseRunnerSha256": _base._base._sha256(BASE_RUNNER),
        "scoreSha256": _base._base._sha256(score_path),
        "productionContractRevision": "shopping-task-state-v2-source-provenance-v1",
        "commonBaselineRepair": "fixed-v1-common-action-repair-v2-20260827",
    })
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"manifest": manifest, "score": score}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run_deterministic_safety_gate(args.output_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["score"]["safetyGatePassed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
