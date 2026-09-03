"""Versioned V3 gate for the source-provenance production-contract repair.

V2 evidence is immutable.  This adapter reuses its frozen comparison mechanics,
adds the exact detailed-source counterexample, and records both wrapper and base
runner hashes in the new run manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.domains.ecommerce.models import ShoppingGuideState, ShoppingRequirement
from app.domains.ecommerce.shopping_state_update import build_shopping_state_transition_patch
from app.task_state import TaskState
from evaluation import shopping_task_state_context_ab_v2 as _base


MODULE_PATH = Path(__file__).resolve()
ASSET_ROOT = MODULE_PATH.parent / "assets" / "shopping_task_state_context_ab_v3_20260827"
PREREGISTRATION = ASSET_ROOT / "manifest.json"
SELECTION = ASSET_ROOT / "selection.json"
BASE_RUNNER = Path(_base.__file__).resolve()
V2TaskSemanticProjectionError = _base.V2TaskSemanticProjectionError
_ORIGINAL_BUILD_GATE_STATES = _base.build_gate_states
_BASE_REQUIREMENT_TO_LEGACY = _base._v2_requirement_to_legacy


def _v3_requirement_to_legacy(requirement: Any, *, category: str) -> ShoppingRequirement:
    legacy = _BASE_REQUIREMENT_TO_LEGACY(requirement, category=category)
    return legacy.model_copy(
        update={"source": requirement.source_provenance or requirement.source}
    )


def _detailed_source_gate_state() -> TaskState:
    state = dict(_ORIGINAL_BUILD_GATE_STATES())["active_candidate_scope"].model_copy(deep=True)
    detailed_source = "inferred: 续航好映射为较高电池健康度偏好"
    inferred = {
        "key": "battery_health",
        "operator": "eq",
        "value": "90_plus",
        "unit": "enum",
        "priority": "soft",
        "source": detailed_source,
    }
    state.domain_state["shoppingGuide"]["requirements"].append(deepcopy(inferred))
    state.domain_state["candidateScope"]["requirementsSnapshot"].append(deepcopy(inferred))
    guide = ShoppingGuideState.model_validate(state.domain_state["shoppingGuide"])
    patch = build_shopping_state_transition_patch(
        state,
        guide,
        {"status": "ready"},
        constraints_changed=False,
    )
    state.domain_state["shoppingTaskStateV2"] = patch["shoppingTaskStateV2"]
    return state


def build_gate_states() -> list[tuple[str, TaskState]]:
    return [*_ORIGINAL_BUILD_GATE_STATES(), ("detailed_source_provenance", _detailed_source_gate_state())]


@contextmanager
def _configured_base():
    original = {
        "ASSET_ROOT": _base.ASSET_ROOT,
        "PREREGISTRATION": _base.PREREGISTRATION,
        "SELECTION": _base.SELECTION,
        "build_gate_states": _base.build_gate_states,
        "requirement_projection": _base._v2_requirement_to_legacy,
    }
    try:
        _base.ASSET_ROOT = ASSET_ROOT
        _base.PREREGISTRATION = PREREGISTRATION
        _base.SELECTION = SELECTION
        _base.build_gate_states = build_gate_states
        _base._v2_requirement_to_legacy = _v3_requirement_to_legacy
        yield
    finally:
        _base.ASSET_ROOT = original["ASSET_ROOT"]
        _base.PREREGISTRATION = original["PREREGISTRATION"]
        _base.SELECTION = original["SELECTION"]
        _base.build_gate_states = original["build_gate_states"]
        _base._v2_requirement_to_legacy = original["requirement_projection"]


_state_hash = _base._state_hash


def _verify_freeze() -> dict[str, Any]:
    with _configured_base():
        return _base._verify_freeze()


def project_v2_task_semantics(state: TaskState) -> TaskState:
    with _configured_base():
        return _base.project_v2_task_semantics(state)


async def build_context_pack_from_v2_task_semantics(
    state: TaskState,
    *,
    run_id: str,
) -> Any:
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
        "schemaVersion": "shopping-task-state-context-safety-score-v3",
        "experimentId": "shopping-task-state-context-ab-v3-20260827",
    })
    score_path = output_dir / "score.json"
    score_path.write_text(
        json.dumps(score, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = result["manifest"]
    manifest.update({
        "schemaVersion": "shopping-task-state-context-safety-manifest-v3",
        "experimentId": score["experimentId"],
        "runner": str(MODULE_PATH),
        "runnerSha256": _base._sha256(MODULE_PATH),
        "baseRunner": str(BASE_RUNNER),
        "baseRunnerSha256": _base._sha256(BASE_RUNNER),
        "scoreSha256": _base._sha256(score_path),
        "productionContractRevision": "shopping-task-state-v2-source-provenance-v1",
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
