"""Corrected deterministic gate for the TaskState ContextPack paired experiment.

The independent variable is the source of task semantics, not ownership of
every ecommerce support object:

* control: the production ContextPack builder reads compatibility state;
* treatment: goal, requirements and unknowns are projected from
  ``shoppingTaskStateV2``;
* shared controls: validated category/mode/comparison identity and the exact
  server-owned CandidateScope/ScopeRerankRequest already used in production.

The adapter is evaluation-only.  It does not modify production contracts,
settings, Redis, models, tools, HTTP state, or the frozen source dataset.
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
    BrandAvoidance,
    CandidateScope,
    ScopeRerankRequest,
    ShoppingGuideState,
    ShoppingRequirement,
    compiled_shopping_requirements,
)
from app.domains.ecommerce.shopping_task_state_v2 import ShoppingTaskStateV2
from app.task_state import TaskState
from evaluation.shopping_task_state_context_ab_v1 import build_gate_states


ROOT = Path(__file__).resolve().parents[2]
ASSET_ROOT = (
    Path(__file__).resolve().parent
    / "assets"
    / "shopping_task_state_context_ab_v2_20260827"
)
PREREGISTRATION = ASSET_ROOT / "manifest.json"
SELECTION = ASSET_ROOT / "selection.json"


class V2TaskSemanticProjectionError(ValueError):
    """Fail-closed projection error with a stable audit code."""

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


def _value_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _state_hash(state: TaskState) -> str:
    return _value_hash(state.model_dump(by_alias=True, mode="json"))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * percentile) - 1))
    return round(ordered[index], 3)


def _v2_requirement_to_legacy(
    requirement: Any,
    *,
    category: str,
) -> ShoppingRequirement:
    if requirement.status != "active":
        raise V2TaskSemanticProjectionError(
            "V2_NON_ACTIVE_TERMINAL_REQUIREMENT",
            "shoppingTaskStateV2.requirements must contain active terminal requirements only",
        )
    spec = SPEC_REGISTRY.get(category, {}).get(requirement.key)
    if spec is None:
        raise V2TaskSemanticProjectionError(
            "V2_REQUIREMENT_KEY_UNSUPPORTED",
            f"V2 requirement {requirement.key!r} is not valid for the shared category",
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


def _validated_shared_support(
    state: TaskState,
    *,
    snapshot: ShoppingTaskStateV2,
    guide: ShoppingGuideState,
    projected_requirements: list[ShoppingRequirement],
) -> tuple[CandidateScope | None, ScopeRerankRequest | None]:
    raw_scope = state.domain_state.get("candidateScope")
    try:
        scope = CandidateScope.model_validate(raw_scope)
    except (TypeError, ValueError):
        scope = None

    v2_scope = snapshot.candidate_scope
    if v2_scope is not None:
        if scope is None:
            raise V2TaskSemanticProjectionError(
                "SHARED_SCOPE_MISSING",
                "V2 references a candidate scope but the shared server-owned CandidateScope is absent or invalid",
            )
        if (
            scope.status != "active"
            or scope.task_id != state.task_id
            or scope.scope_id != v2_scope.scope_id
            or scope.category != v2_scope.category
            or scope.category != guide.category
            or tuple(scope.visible_product_ids or scope.ranked_item_ids)
            != tuple(v2_scope.candidate_ids)
            or list(scope.requirements_snapshot) != projected_requirements
        ):
            raise V2TaskSemanticProjectionError(
                "SHARED_SCOPE_MISMATCH",
                "V2 scope reference does not match the exact active server-owned CandidateScope",
            )
    elif scope is not None and scope.status == "active":
        raise V2TaskSemanticProjectionError(
            "V2_SCOPE_LINK_MISSING",
            "an active shared CandidateScope exists but V2 does not reference it",
        )

    raw_rerank = state.domain_state.get("scopeRerankRequest")
    try:
        rerank = ScopeRerankRequest.model_validate(raw_rerank)
    except (TypeError, ValueError):
        rerank = None
    if rerank is not None and (
        scope is None
        or scope.status != "active"
        or rerank.scope_id != scope.scope_id
        or v2_scope is None
    ):
        raise V2TaskSemanticProjectionError(
            "SHARED_RERANK_SCOPE_MISMATCH",
            "shared ScopeRerankRequest is not bound to the active V2-referenced CandidateScope",
        )
    return scope, rerank


def project_v2_task_semantics(state: TaskState) -> TaskState:
    """Return a read-only treatment state with V2 task semantics.

    Category, mode and comparison identity are frozen shared routing controls;
    CandidateScope and ScopeRerankRequest remain the same validated server-owned
    support objects in both arms.  No candidate/provenance/evidence field is
    copied into V2 or reconstructed from model text.
    """

    raw_v2 = state.domain_state.get("shoppingTaskStateV2")
    try:
        snapshot = ShoppingTaskStateV2.model_validate(raw_v2)
    except (TypeError, ValueError) as exc:
        raise V2TaskSemanticProjectionError(
            "V2_SNAPSHOT_INVALID",
            "shoppingTaskStateV2 is absent or invalid",
        ) from exc

    try:
        shared_guide = ShoppingGuideState.model_validate(
            state.domain_state.get("shoppingGuide")
        )
    except (TypeError, ValueError) as exc:
        raise V2TaskSemanticProjectionError(
            "SHARED_ROUTING_IDENTITY_INVALID",
            "the frozen category/mode/comparison routing identity is absent or invalid",
        ) from exc
    if shared_guide.category is None:
        raise V2TaskSemanticProjectionError(
            "SHARED_CATEGORY_MISSING",
            "the shared category routing identity is missing",
        )

    projected_requirements: list[ShoppingRequirement] = []
    projected_avoidances: list[BrandAvoidance] = []
    for requirement in snapshot.requirements:
        legacy = _v2_requirement_to_legacy(
            requirement,
            category=shared_guide.category,
        )
        if legacy.key == "brand" and legacy.operator in {"neq", "not_in"}:
            values = legacy.value if isinstance(legacy.value, list) else [legacy.value]
            if requirement.source != "user" or not all(
                isinstance(item, str) for item in values
            ):
                raise V2TaskSemanticProjectionError(
                    "V2_BRAND_AVOIDANCE_INVALID",
                    "brand exclusions must retain exact user-owned string values",
                )
            projected_avoidances.append(BrandAvoidance(
                values=values,
                strength=legacy.priority,
                source="user",
            ))
        else:
            projected_requirements.append(legacy)
    projected_guide = shared_guide.model_copy(
        deep=True,
        update={
            "use_cases": [] if snapshot.use_case == "shopping" else [snapshot.use_case],
            "requirements": projected_requirements,
            "brand_avoidances": projected_avoidances,
            "candidate_ids": [],
        },
    )
    try:
        projected_guide = ShoppingGuideState.model_validate(
            projected_guide.model_dump(by_alias=True, mode="json")
        )
    except ValueError as exc:
        raise V2TaskSemanticProjectionError(
            "V2_REQUIREMENTS_INCOMPATIBLE_WITH_SHARED_CATEGORY",
            "V2 requirements are not valid for the frozen shared category",
        ) from exc

    scope, rerank = _validated_shared_support(
        state,
        snapshot=snapshot,
        guide=projected_guide,
        projected_requirements=list(compiled_shopping_requirements(projected_guide)),
    )
    if scope is not None and scope.status == "active":
        projected_guide = projected_guide.model_copy(
            update={"candidate_ids": list(scope.visible_product_ids or scope.ranked_item_ids)}
        )
    if projected_guide.mode == "compare":
        if scope is None or scope.status != "active":
            raise V2TaskSemanticProjectionError(
                "SHARED_COMPARISON_SCOPE_MISSING",
                "comparison identity requires an active shared CandidateScope",
            )
        if not set(projected_guide.compared_ids).issubset(set(scope.ranked_item_ids)):
            raise V2TaskSemanticProjectionError(
                "SHARED_COMPARISON_IDS_OUT_OF_SCOPE",
                "shared comparedIds are outside the server-owned CandidateScope",
            )

    projected_domain = deepcopy(state.domain_state)
    projected_domain["shoppingGuide"] = projected_guide.model_dump(
        by_alias=True,
        mode="json",
    )
    if scope is None:
        projected_domain.pop("candidateScope", None)
    else:
        projected_domain["candidateScope"] = scope.model_dump(by_alias=True, mode="json")
    if rerank is None:
        projected_domain.pop("scopeRerankRequest", None)
    else:
        projected_domain["scopeRerankRequest"] = rerank.model_dump(
            by_alias=True,
            mode="json",
        )

    return state.model_copy(
        deep=True,
        update={
            "goal": snapshot.goal,
            "unknowns": [item.reason for item in snapshot.unknowns],
            "pending_questions": [
                item.reason for item in snapshot.unknowns if item.blocking
            ],
            "domain_state": projected_domain,
        },
    )


async def build_context_pack_from_v2_task_semantics(
    state: TaskState,
    *,
    run_id: str,
) -> Any:
    projected = project_v2_task_semantics(state)
    return await build_context_pack(
        projected,
        allowed_tools=["search_products", "compare_products", "rerank_products_in_scope"],
        history=[],
        run_id=run_id,
    )


def _verify_freeze() -> dict[str, Any]:
    prereg = json.loads(PREREGISTRATION.read_text(encoding="utf-8"))
    mismatches: list[dict[str, str]] = []
    for relative, expected in prereg["productionSourceFreeze"].items():
        actual = _sha256(ROOT / relative)
        if actual != expected:
            mismatches.append({"path": relative, "expected": expected, "actual": actual})

    source = prereg["sourceDataset"]
    source_root = ASSET_ROOT.parent / "used_phone_harness_behavior_v1_20260825"
    dataset_checks = {
        "manifest": _sha256(source_root / "manifest.json"),
        "publicScenarios": _sha256(source_root / "public" / "scenarios.jsonl"),
        "privateExpectations": _sha256(source_root / "private" / "expectations.jsonl"),
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
        "schemaVersion": "shopping-task-state-context-safety-receipt-v2",
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
        "sharedControlFields": [
            "shoppingGuide.category",
            "shoppingGuide.mode",
            "shoppingGuide.comparedIds",
            "candidateScope",
            "scopeRerankRequest",
        ],
        "treatmentSemanticFields": [
            "shoppingTaskStateV2.goal",
            "shoppingTaskStateV2.useCase",
            "shoppingTaskStateV2.requirements",
            "shoppingTaskStateV2.unknowns",
        ],
    }

    started = time.perf_counter()
    try:
        control = await build_context_pack(
            state,
            allowed_tools=["search_products", "compare_products", "rerank_products_in_scope"],
            history=[],
            run_id=f"control-{case_id}",
        )
        receipt["control"] = {
            "status": "PASS",
            "contextPackTokens": context_pack_token_count(control),
            "candidateScopeSha256": _value_hash(control.candidate_scope_state),
        }
    except Exception as exc:  # pragma: no cover
        receipt["control"] = {
            "status": "FAIL",
            "errorCode": type(exc).__name__,
            "detail": str(exc)[:1000],
        }
    receipt["control"]["durationMs"] = round(
        (time.perf_counter() - started) * 1000.0,
        3,
    )

    started = time.perf_counter()
    try:
        treatment = await build_context_pack_from_v2_task_semantics(
            state,
            run_id=f"treatment-{case_id}",
        )
        receipt["treatment"] = {
            "status": "PASS",
            "contextPackTokens": context_pack_token_count(treatment),
            "candidateScopeSha256": _value_hash(treatment.candidate_scope_state),
        }
    except V2TaskSemanticProjectionError as exc:
        receipt["treatment"] = {
            "status": "FAIL",
            "errorCode": exc.code,
            "detail": exc.detail,
        }
    except Exception as exc:  # pragma: no cover
        receipt["treatment"] = {
            "status": "FAIL",
            "errorCode": type(exc).__name__,
            "detail": str(exc)[:1000],
        }
    receipt["treatment"]["durationMs"] = round(
        (time.perf_counter() - started) * 1000.0,
        3,
    )

    receipt["sharedCandidateScopePreserved"] = (
        receipt["control"].get("candidateScopeSha256")
        == receipt["treatment"].get("candidateScopeSha256")
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
    receipts = [
        await _evaluate_state(case_id, state)
        for case_id, state in build_gate_states()
    ]
    control_passed = sum(row["control"]["status"] == "PASS" for row in receipts)
    treatment_passed = sum(row["treatment"]["status"] == "PASS" for row in receipts)
    read_only_passed = all(row["readOnlyStatePreserved"] for row in receipts)
    shared_scope_passed = all(row["sharedCandidateScopePreserved"] for row in receipts)
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
        and shared_scope_passed
    )
    score = {
        "schemaVersion": "shopping-task-state-context-safety-score-v2",
        "experimentId": "shopping-task-state-context-ab-v2-20260827",
        "verdict": (
            "ACCEPT_DETERMINISTIC_SAFETY"
            if safety_passed
            else "HOLD_CORRECTED_CONTEXT_PROJECTION_UNSAFE"
        ),
        "safetyGatePassed": safety_passed,
        "caseCount": len(receipts),
        "controlPassed": control_passed,
        "treatmentPassed": treatment_passed,
        "readOnlyStatePreserved": read_only_passed,
        "sharedCandidateScopePreserved": shared_scope_passed,
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
        "schemaVersion": "shopping-task-state-context-safety-manifest-v2",
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
        "productionContractChanged": False,
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
