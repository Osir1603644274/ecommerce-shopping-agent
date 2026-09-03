"""Production-aligned deterministic gate for the TaskState semantic-source pair.

V5 keeps the corrected V3 projection mechanics but binds them to the current
production source snapshot and the complete 24-scenario/65-turn corpus.  The
live phase is separately required to run both arms under ``react_v1``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.domains.ecommerce.models import ShoppingGuideState
from app.domains.ecommerce.shopping_state_update import build_shopping_state_transition_patch
from evaluation import shopping_task_state_context_ab_v3 as _base


MODULE_PATH = Path(__file__).resolve()
ASSET_ROOT = MODULE_PATH.parent / "assets" / "shopping_task_state_context_ab_v5_20260829"
PREREGISTRATION = ASSET_ROOT / "manifest.json"
SELECTION = ASSET_ROOT / "selection.json"
BASE_RUNNER = Path(_base.__file__).resolve()
V2TaskSemanticProjectionError = _base.V2TaskSemanticProjectionError
_state_hash = _base._state_hash
_BASE_BUILD_GATE_STATES = _base.build_gate_states


def _brand_include_exclude_state():
    state = dict(_BASE_BUILD_GATE_STATES())["initial_search"].model_copy(deep=True)
    raw_guide = dict(state.domain_state["shoppingGuide"])
    raw_guide["requirements"] = [
        *raw_guide.get("requirements", []),
        {
            "key": "brand",
            "operator": "in",
            "value": ["huawei", "honor", "xiaomi"],
            "unit": "text",
            "priority": "hard",
            "source": "user",
        },
    ]
    raw_guide["brandAvoidances"] = [
        {"values": ["apple"], "strength": "hard", "source": "user"}
    ]
    guide = ShoppingGuideState.model_validate(raw_guide)
    state.domain_state["shoppingGuide"] = guide.model_dump(
        by_alias=True,
        mode="json",
    )
    patch = build_shopping_state_transition_patch(
        state,
        guide,
        {"status": "ready"},
        constraints_changed=True,
    )
    state.domain_state["shoppingTaskStateV2"] = patch["shoppingTaskStateV2"]
    return state


def build_gate_states():
    return [
        *_BASE_BUILD_GATE_STATES(),
        ("brand_include_and_exclude_coexist", _brand_include_exclude_state()),
    ]


@contextmanager
def _configured_base():
    original = (
        _base.ASSET_ROOT,
        _base.PREREGISTRATION,
        _base.SELECTION,
        _base.build_gate_states,
    )
    try:
        _base.ASSET_ROOT = ASSET_ROOT
        _base.PREREGISTRATION = PREREGISTRATION
        _base.SELECTION = SELECTION
        _base.build_gate_states = build_gate_states
        yield
    finally:
        (
            _base.ASSET_ROOT,
            _base.PREREGISTRATION,
            _base.SELECTION,
            _base.build_gate_states,
        ) = original


def _verify_freeze() -> dict[str, Any]:
    with _configured_base():
        return _base._verify_freeze()


def project_v2_task_semantics(state):
    with _configured_base():
        return _base.project_v2_task_semantics(state)


async def build_context_pack_from_v2_task_semantics(state, *, run_id: str):
    with _configured_base():
        return await _base.build_context_pack_from_v2_task_semantics(state, run_id=run_id)


async def run_deterministic_safety_gate(output_dir: Path) -> dict[str, Any]:
    with _configured_base():
        result = await _base.run_deterministic_safety_gate(output_dir)
    score = result["score"]
    score.update({
        "schemaVersion": "shopping-task-state-context-safety-score-v5",
        "experimentId": "shopping-task-state-context-ab-v5-20260829",
        "productionAlignedRuntimeRequiredForNextGate": "react_v1",
    })
    score_path = output_dir / "score.json"
    score_path.write_text(
        json.dumps(score, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = result["manifest"]
    manifest.update({
        "schemaVersion": "shopping-task-state-context-safety-manifest-v5",
        "experimentId": score["experimentId"],
        "runner": str(MODULE_PATH),
        "runnerSha256": _base._base._sha256(MODULE_PATH),
        "baseRunner": str(BASE_RUNNER),
        "baseRunnerSha256": _base._base._sha256(BASE_RUNNER),
        "scoreSha256": _base._base._sha256(score_path),
        "productionContractRevision": "shopping-task-state-v2-source-provenance-v1",
        "liveRuntimeRequired": "react_v1",
        "liveScenarioCountRequired": 24,
        "liveTurnCountRequired": 65,
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
