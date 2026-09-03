"""V6 deterministic safety gate over the unchanged V5 projection cases."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from evaluation import shopping_task_state_context_ab_v5 as _v5


MODULE_PATH = Path(__file__).resolve()
ASSET_ROOT = MODULE_PATH.parent / "assets" / "shopping_task_state_context_ab_v6_20260829"


async def run_deterministic_safety_gate(output_dir: Path):
    original = (_v5.ASSET_ROOT, _v5.PREREGISTRATION, _v5.SELECTION)
    try:
        _v5.ASSET_ROOT = ASSET_ROOT
        _v5.PREREGISTRATION = ASSET_ROOT / "manifest.json"
        _v5.SELECTION = ASSET_ROOT / "selection.json"
        result = await _v5.run_deterministic_safety_gate(output_dir)
    finally:
        _v5.ASSET_ROOT, _v5.PREREGISTRATION, _v5.SELECTION = original
    score = result["score"]
    score.update({
        "schemaVersion": "shopping-task-state-context-safety-score-v6",
        "experimentId": "shopping-task-state-context-ab-v6-20260829",
    })
    score_path = output_dir / "score.json"
    score_path.write_text(
        json.dumps(score, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = result["manifest"]
    manifest.update({
        "schemaVersion": "shopping-task-state-context-safety-manifest-v6",
        "experimentId": score["experimentId"],
        "runner": str(MODULE_PATH),
        "runnerSha256": _v5._base._base._sha256(MODULE_PATH),
        "scoreSha256": _v5._base._base._sha256(score_path),
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
