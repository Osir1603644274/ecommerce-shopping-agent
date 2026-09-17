"""Replay a recorded validator boundary on owned Redis, with zero model calls."""
import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
from unittest.mock import patch

from agent.app import task_state, validator
from agent.app.settings import settings
from agent.app.domains.ecommerce.shopping_state_authority import select_shopping_state_authority
from agent.evaluation.real_user_multiturn_ab_executor_20260902_v2 import lane_runtime as catalog
from .artifacts import HERE, file_sha, write_new
from .private_redis import private_redis


async def run(output, proposed_fix=False):
    output.mkdir(parents=True, exist_ok=False)
    source = HERE / "long_dev2_A002/state_transitions.jsonl"
    recorded = None
    with source.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["turn"] == 11:
                recorded = row
    if recorded is None or recorded["phase"] != "harness_ready_for_validation":
        raise ValueError("expected_recorded_validation_boundary_missing")
    state = task_state.TaskState.model_validate(recorded["state"])
    write_new(output / "source.json", {"path": str(source), "sha256": file_sha(source),
        "turn": 11, "phase": recorded["phase"], "recordedState": recorded["state"],
        "note": "Previously generated SUT state, not a gold state. Isolated regression only, never a comparative episode."})
    async with private_redis(output / "redis") as store:
        with ExitStack() as stack:
            stack.enter_context(patch.object(task_state, "_client", store))
            stack.enter_context(patch.object(settings, "context_history_v1_enabled", True))
            stack.enter_context(patch.object(settings, "used_phone_synthetic_price_policy", "budget_and_ranking"))
            stack.enter_context(patch.object(settings, "used_phone_synthetic_price_dir", str(catalog.CATALOG_DIR)))
            if proposed_fix:
                original_refresh = validator.refresh_shopping_state_v2_after_validation
                def proposed_refresh(current, domain_patch):
                    if "candidateScope" in domain_patch and "shoppingGuide" in domain_patch:
                        domain_patch["shoppingGuide"] = {**domain_patch["shoppingGuide"], "comparedIds": [], "mode": "recommend"}
                    return original_refresh(current, domain_patch)
                stack.enter_context(patch.object(validator, "refresh_shopping_state_v2_after_validation", proposed_refresh))
            await store.set(task_state._state_key(state.task_id), state.model_dump_json(by_alias=True))
            try:
                result, updated = await validator.run_validator_phase(state)
                selection = select_shopping_state_authority(domain_state=updated.domain_state,
                    task_id=updated.task_id, task_revision=updated.revision, goal=updated.goal,
                    unknowns=updated.unknowns, pending_questions=updated.pending_questions, mode=settings.shopping_state_authority)
                success = result.outcome == "passed" and not updated.domain_state["shoppingGuide"]["comparedIds"]
                value = {"status": "RECORDED_SCOPE_REFRESH_PASS" if success else "RECORDED_SCOPE_REFRESH_HOLD",
                    "validator": result.model_dump(mode="json", by_alias=True),
                    "postState": updated.model_dump(mode="json", by_alias=True), "authoritySource": selection.source}
            except Exception as exc:
                value = {"status": "RECORDED_SCOPE_REFRESH_REPRODUCED_FAILURE", "type": type(exc).__name__, "error": str(exc)}
            value.update({"modelCalls": 0, "formalExperimentalAcceptance": False, "proposedFixRuntimePatch": proposed_fix})
            write_new(output / "result.json", value)
            print(json.dumps({key: value[key] for key in ("status", "modelCalls")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--proposed-fix", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.output, args.proposed_fix))
