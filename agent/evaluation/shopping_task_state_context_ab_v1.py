"""Deterministic safety gate for the ShoppingTaskStateV2 ContextPack A/B.

The control path calls the production ContextPack builder unchanged.  The
treatment adapter is deliberately strict and reads shopping semantics only
from ``shoppingTaskStateV2``.  Missing authoritative fields are reported as a
gate failure; the adapter never fills them from legacy compatibility fields.

No model, tool, Redis, HTTP, or private oracle is used by this module.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.context_pack import build_context_pack, context_pack_token_count
from app.domains.ecommerce.models import (
    SPEC_REGISTRY,
    CandidateScope,
    ShoppingGuideState,
    ShoppingRequirement,
)
from app.domains.ecommerce.shopping_state_update import (
    build_shopping_state_transition_patch,
    clear_stale_guide_references,
    refresh_shopping_state_v2_after_validation,
)
from app.domains.ecommerce.shopping_task_state_v2 import ShoppingTaskStateV2
from app.task_state import TaskState


ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = (
    Path(__file__).resolve().parent
    / "assets"
    / "shopping_task_state_context_ab_v1_20260826"
)
PREREGISTRATION = ASSET_ROOT / "manifest.json"
SELECTION = ASSET_ROOT / "selection.json"


class V2ContextProjectionError(ValueError):
    """Fail-closed treatment projection error with a stable audit code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 3)


def _state_hash(state: TaskState) -> str:
    return hashlib.sha256(
        _canonical_bytes(state.model_dump(by_alias=True, mode="json"))
    ).hexdigest()


def _v2_requirement_to_legacy(
    requirement: Any,
    *,
    category: str,
) -> ShoppingRequirement:
    spec = SPEC_REGISTRY.get(category, {}).get(requirement.key)
    if spec is None:
        raise V2ContextProjectionError(
            "V2_REQUIREMENT_KEY_UNSUPPORTED",
            f"V2 requirement {requirement.key!r} is not valid for {category!r}",
        )
    _value_type, unit, _operators = spec
    value = list(requirement.value) if isinstance(requirement.value, tuple) else requirement.value
    return ShoppingRequirement(
        key=requirement.key,
        operator=requirement.operator,
        value=value,
        unit=unit,
        priority=requirement.priority,
        source=requirement.source,
    )


def project_v2_context_domain(state: TaskState) -> dict[str, Any]:
    """Project a legacy-shaped domain context from V2 alone or fail closed.

    The production ContextPack contract currently validates ``ShoppingGuideState``
    and the full server-owned ``CandidateScope``.  This adapter refuses to read
    category, mode, comparison IDs, evidence status, or scope provenance from
    the compatibility fields because doing so would invalidate the single-
    variable experiment.
    """

    raw = state.domain_state.get("shoppingTaskStateV2")
    try:
        snapshot = ShoppingTaskStateV2.model_validate(raw)
    except (TypeError, ValueError) as exc:
        raise V2ContextProjectionError(
            "V2_SNAPSHOT_INVALID",
            "shoppingTaskStateV2 is absent or invalid",
        ) from exc

    scope = snapshot.candidate_scope
    if scope is None:
        raise V2ContextProjectionError(
            "V2_CATEGORY_UNAVAILABLE",
            "ShoppingTaskStateV2 has no category outside candidateScope; an initial, zero-result, or invalidated-scope turn cannot build ShoppingGuideState.",
        )

    if snapshot.current_action.kind == "answer":
        raise V2ContextProjectionError(
            "V2_MODE_UNAVAILABLE_AFTER_VALIDATION",
            "currentAction=answer does not distinguish recommend from compare.",
        )

    # CandidateScopeV2 intentionally has only identity/category/visible IDs/source
    # turn. ContextPack requires full pool/ranking/evidence/plan/step provenance.
    raise V2ContextProjectionError(
        "V2_CANDIDATE_SCOPE_INSUFFICIENT",
        "CandidateScopeV2 lacks candidatePoolIds, rankedItemIds, sourcePlanId, sourceStepId, requirementsSnapshot, evidenceRefs, status and invalidation metadata required by ContextPack.",
    )


async def build_context_pack_from_v2(
    state: TaskState,
    *,
    run_id: str,
) -> Any:
    """Read-only V2 treatment adapter used only by this evaluation gate."""

    projected_domain = project_v2_context_domain(state)
    projected_state = state.model_copy(
        deep=True,
        update={"domain_state": projected_domain},
    )
    return await build_context_pack(
        projected_state,
        allowed_tools=["search_products", "compare_products", "rerank_products_in_scope"],
        history=[],
        run_id=run_id,
    )


def _requirements(price_minor: int = 220_000) -> list[dict[str, Any]]:
    return [
        {
            "key": "os",
            "operator": "eq",
            "value": "ios",
            "unit": "enum",
            "priority": "hard",
            "source": "user",
        },
        {
            "key": "price_minor",
            "operator": "lte",
            "value": price_minor,
            "unit": "CNY_MINOR",
            "priority": "hard",
            "source": "user",
        },
    ]


def _candidate_scope(requirements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scopeId": "scope-context-ab",
        "taskId": "task-context-ab",
        "sourceRevision": 6,
        "sourcePlanId": "plan-context-ab",
        "sourceStepId": "step-context-ab",
        "category": "phone",
        "candidatePoolIds": [101, 102, 103],
        "rankedItemIds": [101, 102, 103],
        "visibleProductIds": [101, 102],
        "requirementsSnapshot": requirements,
        "brandAvoidancesSnapshot": [],
        "evidenceRefs": ["evidence:context-ab"],
        "createdAt": "2026-08-26T00:00:00+00:00",
        "status": "active",
        "invalidationReason": None,
    }


def _base_state(*, with_scope: bool = False, mode: str = "recommend") -> TaskState:
    requirements = _requirements()
    guide = {
        "mode": mode,
        "category": "phone",
        "useCases": ["student"],
        "requirements": requirements,
        "brandAvoidances": [],
        "candidateIds": [101, 102] if with_scope else [],
        "comparedIds": [101, 102] if mode == "compare" else [],
        "evidenceStatus": "complete" if with_scope else "missing",
    }
    domain_state: dict[str, Any] = {"shoppingGuide": guide}
    if with_scope:
        domain_state["candidateScope"] = _candidate_scope(requirements)
    return TaskState(
        taskId="task-context-ab",
        taskType="ecommerce_guide",
        status="ready",
        revision=7,
        goal="预算2200以内，只看iOS",
        unknowns=[],
        pendingQuestions=[],
        domainState=domain_state,
        createdAt="2026-08-26T00:00:00+00:00",
        updatedAt="2026-08-26T00:00:00+00:00",
    )


def _with_transition(
    state: TaskState,
    guide: ShoppingGuideState,
    *,
    payload: dict[str, Any] | None = None,
) -> TaskState:
    normalized, changed = clear_stale_guide_references(state, guide)
    transition = build_shopping_state_transition_patch(
        state,
        normalized,
        payload or {"status": "ready", "goal": state.goal},
        constraints_changed=changed,
    )
    domain = deepcopy(state.domain_state)
    domain["shoppingGuide"] = normalized.model_dump(by_alias=True, mode="json")
    domain.update(transition)
    return state.model_copy(deep=True, update={"domain_state": domain})


def build_gate_states() -> list[tuple[str, TaskState]]:
    """Create production-shaped state classes without model/tool/storage calls."""

    initial = _base_state(with_scope=False)
    initial = _with_transition(
        initial,
        ShoppingGuideState.model_validate(initial.domain_state["shoppingGuide"]),
    )

    scoped = _base_state(with_scope=True)
    scoped = _with_transition(
        scoped,
        ShoppingGuideState.model_validate(scoped.domain_state["shoppingGuide"]),
    )

    changed_guide = ShoppingGuideState.model_validate({
        **scoped.domain_state["shoppingGuide"],
        "requirements": _requirements(160_000),
    })
    budget_override = _with_transition(
        scoped,
        changed_guide,
        payload={"status": "ready", "goal": "预算改成1600，其他不变"},
    )

    compared = _base_state(with_scope=True, mode="compare")
    compared = _with_transition(
        compared,
        ShoppingGuideState.model_validate(compared.domain_state["shoppingGuide"]),
    )

    validated_domain = deepcopy(scoped.domain_state)
    validated_domain["validationResult"] = {"outcome": "passed"}
    validated_state = scoped.model_copy(deep=True, update={"domain_state": validated_domain})
    refreshed = refresh_shopping_state_v2_after_validation(
        validated_state,
        {"validationResult": {"outcome": "passed"}},
    )
    assert refreshed is not None
    validated_state.domain_state["shoppingTaskStateV2"] = refreshed

    zero_result = initial.model_copy(deep=True)
    zero_result.domain_state["validationResult"] = {
        "outcome": "failed",
        "reason": "zero_results",
    }

    evidence_insufficient = initial.model_copy(
        deep=True,
        update={
            "status": "collecting_information",
            "unknowns": ["商品证据不足"],
            "pending_questions": ["是否允许放宽条件？"],
        },
    )
    evidence_insufficient = _with_transition(
        evidence_insufficient,
        ShoppingGuideState.model_validate(
            evidence_insufficient.domain_state["shoppingGuide"]
        ),
        payload={
            "status": "collecting_information",
            "goal": evidence_insufficient.goal,
        },
    )

    negative_control = _base_state(with_scope=False)
    negative_control.domain_state["shoppingGuide"]["requirements"] = []
    negative_control = _with_transition(
        negative_control,
        ShoppingGuideState.model_validate(negative_control.domain_state["shoppingGuide"]),
    )

    return [
        ("initial_search", initial),
        ("budget_override_scope_invalidated", budget_override),
        ("active_candidate_scope", scoped),
        ("comparison_intent", compared),
        ("validated_answer", validated_state),
        ("zero_result", zero_result),
        ("evidence_insufficient", evidence_insufficient),
        ("negative_control", negative_control),
    ]


def _verify_freeze() -> dict[str, Any]:
    prereg = json.loads(PREREGISTRATION.read_text(encoding="utf-8"))
    mismatches: list[dict[str, str]] = []
    for relative, expected in prereg["productionSourceFreeze"].items():
        path = ROOT / relative
        actual = _sha256(path)
        if actual != expected:
            mismatches.append({"path": relative, "expected": expected, "actual": actual})

    source = prereg["sourceDataset"]
    dataset_checks = {
        "manifest": _sha256(
            ASSET_ROOT.parent / "used_phone_harness_behavior_v1_20260825" / "manifest.json"
        ),
        "publicScenarios": _sha256(
            ASSET_ROOT.parent
            / "used_phone_harness_behavior_v1_20260825"
            / "public"
            / "scenarios.jsonl"
        ),
        "privateExpectations": _sha256(
            ASSET_ROOT.parent
            / "used_phone_harness_behavior_v1_20260825"
            / "private"
            / "expectations.jsonl"
        ),
    }
    expected_dataset = {
        "manifest": source["manifestSha256"],
        "publicScenarios": source["publicScenariosSha256"],
        "privateExpectations": source["privateExpectationsSha256"],
    }
    for name, actual in dataset_checks.items():
        if actual != expected_dataset[name]:
            mismatches.append({
                "path": name,
                "expected": expected_dataset[name],
                "actual": actual,
            })
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "mismatches": mismatches,
        "datasetSha256": dataset_checks,
    }


async def _evaluate_state(case_id: str, state: TaskState) -> dict[str, Any]:
    before = _state_hash(state)
    receipt: dict[str, Any] = {
        "schemaVersion": "shopping-task-state-context-safety-receipt-v1",
        "caseId": case_id,
        "stateSha256": before,
        "modelCalls": {
            "task_manager": 0,
            "task_state": 0,
            "react_decision": 0,
            "final_answer": 0,
        },
        "modelTokens": {"input": 0, "output": 0, "status": "NOT_CALLED"},
        "toolCalls": 0,
    }

    started = time.perf_counter()
    try:
        control = await build_context_pack(
            state,
            allowed_tools=[
                "search_products",
                "compare_products",
                "rerank_products_in_scope",
            ],
            history=[],
            run_id=f"control-{case_id}",
        )
        receipt["control"] = {
            "status": "PASS",
            "contextPackTokens": context_pack_token_count(control),
        }
    except Exception as exc:  # pragma: no cover - recorded as experiment evidence
        receipt["control"] = {
            "status": "FAIL",
            "errorCode": type(exc).__name__,
            "detail": str(exc)[:1000],
        }
    receipt["control"]["durationMs"] = round(
        (time.perf_counter() - started) * 1000.0, 3
    )

    started = time.perf_counter()
    try:
        treatment = await build_context_pack_from_v2(
            state,
            run_id=f"treatment-{case_id}",
        )
        receipt["treatment"] = {
            "status": "PASS",
            "contextPackTokens": context_pack_token_count(treatment),
        }
    except V2ContextProjectionError as exc:
        receipt["treatment"] = {
            "status": "FAIL",
            "errorCode": exc.code,
            "detail": exc.detail,
        }
    except Exception as exc:  # pragma: no cover - recorded as experiment evidence
        receipt["treatment"] = {
            "status": "FAIL",
            "errorCode": type(exc).__name__,
            "detail": str(exc)[:1000],
        }
    receipt["treatment"]["durationMs"] = round(
        (time.perf_counter() - started) * 1000.0, 3
    )

    after = _state_hash(state)
    receipt["readOnlyStatePreserved"] = before == after
    receipt["stateSha256After"] = after
    return receipt


async def run_deterministic_safety_gate(output_dir: Path) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    freeze = _verify_freeze()
    states = build_gate_states()
    receipts = [await _evaluate_state(case_id, state) for case_id, state in states]
    control_passed = sum(row["control"]["status"] == "PASS" for row in receipts)
    treatment_passed = sum(row["treatment"]["status"] == "PASS" for row in receipts)
    read_only_passed = all(row["readOnlyStatePreserved"] for row in receipts)
    error_counts: dict[str, int] = {}
    for row in receipts:
        if row["treatment"]["status"] != "PASS":
            code = row["treatment"]["errorCode"]
            error_counts[code] = error_counts.get(code, 0) + 1

    control_durations = [row["control"]["durationMs"] for row in receipts]
    treatment_durations = [row["treatment"]["durationMs"] for row in receipts]
    safety_passed = (
        freeze["status"] == "PASS"
        and control_passed == len(receipts)
        and treatment_passed == len(receipts)
        and read_only_passed
    )
    verdict = "ACCEPT_DETERMINISTIC_SAFETY" if safety_passed else "HOLD_V2_CONTEXT_SCHEMA_INSUFFICIENT"
    score = {
        "schemaVersion": "shopping-task-state-context-safety-score-v1",
        "experimentId": "shopping-task-state-context-ab-v1-20260826",
        "verdict": verdict,
        "safetyGatePassed": safety_passed,
        "caseCount": len(receipts),
        "controlPassed": control_passed,
        "treatmentPassed": treatment_passed,
        "readOnlyStatePreserved": read_only_passed,
        "freezeStatus": freeze["status"],
        "treatmentErrorCounts": error_counts,
        "latencyMs": {
            "control": {
                "p50": _percentile(control_durations, 0.50),
                "p95": _percentile(control_durations, 0.95),
                "mean": round(statistics.fmean(control_durations), 3),
            },
            "treatment": {
                "p50": _percentile(treatment_durations, 0.50),
                "p95": _percentile(treatment_durations, 0.95),
                "mean": round(statistics.fmean(treatment_durations), 3),
            },
        },
        "callAttribution": {
            "task_manager": 0,
            "task_state": 0,
            "react_decision": 0,
            "final_answer": 0,
            "toolCalls": 0,
            "inputTokens": 0,
            "outputTokens": 0,
            "status": "DETERMINISTIC_GATE_NO_MODEL_OR_TOOL",
        },
        "nextGate": (
            "LIVE_PAIRED_RUN"
            if safety_passed
            else "BLOCKED_BY_DETERMINISTIC_SAFETY; NO_LIVE_RUN_OR_HUMAN_PACKET"
        ),
    }

    receipts_path = output_dir / "receipts.jsonl"
    receipts_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in receipts),
        encoding="utf-8",
        newline="\n",
    )
    score_path = output_dir / "score.json"
    score_path.write_text(
        json.dumps(score, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    module_path = Path(__file__).resolve()
    run_manifest = {
        "schemaVersion": "shopping-task-state-context-safety-manifest-v1",
        "experimentId": score["experimentId"],
        "status": "COMPLETE",
        "startedAndFinishedAt": datetime.now(timezone.utc).isoformat(),
        "preregistration": str(PREREGISTRATION),
        "preregistrationSha256": _sha256(PREREGISTRATION),
        "selection": str(SELECTION),
        "selectionSha256": _sha256(SELECTION),
        "runner": str(module_path),
        "runnerSha256": _sha256(module_path),
        "freeze": freeze,
        "receiptSha256": _sha256(receipts_path),
        "scoreSha256": _sha256(score_path),
        "fixedRuntimeChanged": False,
        "reactLiveChanged": False,
        "modelCalled": False,
        "toolCalled": False,
        "livePairedRunExecuted": False,
        "humanReviewPacketGenerated": False,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(run_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return {"manifest": run_manifest, "score": score}


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
