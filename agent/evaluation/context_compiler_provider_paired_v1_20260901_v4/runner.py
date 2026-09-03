"""Context Provider V4: typed server-resolved reference fidelity evaluation."""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from agent.evaluation.context_compiler_provider_paired_v1_20260901_v1 import (
    runner as base,
)
from agent.evaluation.context_compiler_provider_paired_v1_20260901_v3.runner import (
    DeepSeekToolCompatibleClient,
)


ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_MANIFEST = PACKAGE_DIR / "manifest.json"
DEFAULT_OUTPUT = PACKAGE_DIR / "attempt001"
DEFAULT_SMOKE_OUTPUT = PACKAGE_DIR / "compatibility-smoke001"
ARMS = base.ARMS
PROTECTED_FIELDS = base.PROTECTED_FIELDS + ("referenceContextState",)
REFERENCE_STATE_FIELDS = (
    "recentReference",
    "referenceSource",
    "referenceTaskRevision",
    "referenceScopeSourceRevision",
    "focusedProductId",
    "comparedProductIds",
)


class ReferenceStateError(RuntimeError):
    """Fail-closed typed reference-state contract violation."""


def _state_payload_for_hash(state: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in state.items() if key != "bindingHash"}


def bind_reference_state(state: dict[str, Any]) -> dict[str, Any]:
    bound = copy.deepcopy(state)
    bound["bindingHash"] = base.sha256_json(_state_payload_for_hash(bound))
    return bound


def validate_reference_state(case: dict[str, Any]) -> dict[str, Any]:
    payload = case.get("contextPayload")
    if not isinstance(payload, dict):
        raise ReferenceStateError("context_payload_missing")
    state = payload.get("referenceContextState")
    if not isinstance(state, dict):
        raise ReferenceStateError("reference_state_missing")
    required = {
        "schemaVersion",
        "sourceKind",
        "taskId",
        "contextTaskRevision",
        "referenceTaskRevision",
        "historySourceTurn",
        "candidateScopeId",
        "candidateScopeSourceRevision",
        "recentReference",
        "presentationMode",
        "presentationIds",
        "focusedProductId",
        "comparedProductIds",
        "bindingHash",
    }
    if set(state) != required:
        raise ReferenceStateError("reference_state_shape_invalid")
    if state["schemaVersion"] != "context-provider-reference-state-v1":
        raise ReferenceStateError("reference_state_schema_invalid")
    expected_hash = base.sha256_json(_state_payload_for_hash(state))
    if state["bindingHash"] != expected_hash:
        raise ReferenceStateError("reference_state_binding_invalid")
    if state["taskId"] != payload.get("taskId"):
        raise ReferenceStateError("reference_state_cross_task")
    revision = payload.get("baseContextRevision")
    if not isinstance(revision, int) or state["contextTaskRevision"] != revision:
        raise ReferenceStateError("reference_state_revision_invalid")

    source = state["sourceKind"]
    if source not in {"NONE", "SESSION_HISTORY", "SIGNED_UI_RECEIPT"}:
        raise ReferenceStateError("reference_state_source_invalid")
    reference_revision = state["referenceTaskRevision"]
    if reference_revision is not None and (
        not isinstance(reference_revision, int)
        or reference_revision < 1
        or reference_revision > revision
    ):
        raise ReferenceStateError("reference_state_source_revision_invalid")
    presentation_ids = state["presentationIds"]
    compared_ids = state["comparedProductIds"]
    if (
        not isinstance(presentation_ids, list)
        or any(type(value) is not int or value < 1 for value in presentation_ids)
        or len(set(presentation_ids)) != len(presentation_ids)
    ):
        raise ReferenceStateError("reference_state_presentation_invalid")
    if (
        not isinstance(compared_ids, list)
        or any(type(value) is not int or value < 1 for value in compared_ids)
        or len(set(compared_ids)) != len(compared_ids)
        or any(value not in presentation_ids for value in compared_ids)
    ):
        raise ReferenceStateError("reference_state_comparison_invalid")
    focused_id = state["focusedProductId"]
    if focused_id is not None and (
        type(focused_id) is not int or focused_id not in presentation_ids
    ):
        raise ReferenceStateError("reference_state_focus_invalid")

    if source == "NONE":
        if any((
            state["referenceTaskRevision"] is not None,
            state["historySourceTurn"] is not None,
            state["candidateScopeId"] is not None,
            state["candidateScopeSourceRevision"] is not None,
            state["recentReference"] is not None,
            state["presentationMode"] is not None,
            bool(presentation_ids),
            focused_id is not None,
            bool(compared_ids),
        )):
            raise ReferenceStateError("reference_state_none_not_empty")
    elif source == "SESSION_HISTORY":
        if (
            state["referenceTaskRevision"] != revision
            or type(state["historySourceTurn"]) is not int
            or state["historySourceTurn"] < 1
            or not isinstance(state["recentReference"], str)
            or not state["recentReference"]
            or state["candidateScopeId"] is not None
            or state["candidateScopeSourceRevision"] is not None
            or state["presentationMode"] is not None
            or presentation_ids
            or focused_id is not None
            or compared_ids
        ):
            raise ReferenceStateError("reference_state_history_invalid")
    else:
        scope = payload.get("candidateScopeState")
        if not isinstance(scope, dict):
            raise ReferenceStateError("reference_state_scope_missing")
        if (
            not isinstance(reference_revision, int)
            or state["historySourceTurn"] is not None
            or state["candidateScopeId"] != scope.get("scopeId")
            or state["candidateScopeSourceRevision"] != scope.get("sourceRevision")
            or state["presentationMode"] not in {"compact", "expanded"}
            or not presentation_ids
            or any(value not in scope.get("rankedItemIds", []) for value in presentation_ids)
        ):
            raise ReferenceStateError("reference_state_signed_scope_invalid")
    return state


def compile_case(case: dict[str, Any], arm: str) -> Any:
    validate_reference_state(case)
    return base.compile_case(case, arm)


def expected_output(case: dict[str, Any]) -> dict[str, Any]:
    payload = case["contextPayload"]
    state = validate_reference_state(case)
    return {
        "currentGoal": payload["goal"],
        "candidateIds": payload["candidateScopeState"]["rankedItemIds"],
        "evidenceRefs": payload["evidenceRefs"],
        "recentReference": state["recentReference"],
        "referenceSource": state["sourceKind"],
        "referenceTaskRevision": state["referenceTaskRevision"],
        "referenceScopeSourceRevision": state["candidateScopeSourceRevision"],
        "focusedProductId": state["focusedProductId"],
        "comparedProductIds": state["comparedProductIds"],
    }


FIDELITY_TOOL = {
    "type": "function",
    "function": {
        "name": "record_context_fidelity_v4",
        "description": "Copy bounded fields exactly from the supplied context.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "currentGoal",
                "candidateIds",
                "evidenceRefs",
                *REFERENCE_STATE_FIELDS,
            ],
            "properties": {
                "currentGoal": {"type": "string"},
                "candidateIds": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "evidenceRefs": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "recentReference": {
                    "anyOf": [{"type": "string"}, {"type": "null"}],
                },
                "referenceSource": {"type": "string"},
                "referenceTaskRevision": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                },
                "referenceScopeSourceRevision": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                },
                "focusedProductId": {
                    "anyOf": [{"type": "integer"}, {"type": "null"}],
                },
                "comparedProductIds": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
        },
    },
}

SYSTEM_PROMPT = """You are a deterministic bounded-context fidelity checker.
Call record_context_fidelity_v4 exactly once. Copy currentGoal, candidateIds and
evidenceRefs exactly from goal, candidateScopeState.rankedItemIds and
evidenceRefs. Copy recentReference, referenceSource, referenceTaskRevision,
referenceScopeSourceRevision, focusedProductId and comparedProductIds exactly
from referenceContextState. That state is already resolved and authoritative;
never infer a reference from historySummaries. Do not translate, reorder, add,
omit or normalize any value."""


def _parse_tool_arguments(response: Any) -> dict[str, Any]:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise RuntimeError("provider returned no choices")
    calls = getattr(choices[0].message, "tool_calls", None) or []
    matching = [
        call
        for call in calls
        if call.function.name == "record_context_fidelity_v4"
    ]
    if len(matching) != 1:
        raise RuntimeError("provider did not call record_context_fidelity_v4 exactly once")
    return json.loads(matching[0].function.arguments)


async def _call_provider(
    client: DeepSeekToolCompatibleClient,
    *,
    model: str,
    max_tokens: int,
    case: dict[str, Any],
    arm: str,
    model_view: dict[str, Any],
) -> dict[str, Any]:
    started = time.perf_counter()
    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": base.canonical_json({
                    "caseId": case["caseId"],
                    "context": model_view,
                }),
            },
        ],
        tools=[FIDELITY_TOOL],
        tool_choice={
            "type": "function",
            "function": {"name": "record_context_fidelity_v4"},
        },
        temperature=0,
        max_tokens=max_tokens,
    )
    actual = _parse_tool_arguments(response)
    expected = expected_output(case)
    field_fidelity = {
        field: actual.get(field) == expected[field]
        for field in expected
    }
    return {
        "schemaVersion": "context-compiler-provider-trace-v4",
        "caseId": case["caseId"],
        "scenarioId": case["scenarioId"],
        "turnId": case["turnId"],
        "lineageKind": case["lineageKind"],
        "arm": arm,
        "status": "SUCCEEDED",
        "durationMs": (time.perf_counter() - started) * 1000.0,
        "usage": base._usage(response),
        "expected": expected,
        "actual": actual,
        "fieldFidelity": field_fidelity,
        "exactFidelity": all(field_fidelity.values()) and set(actual) == set(expected),
    }


def _mutation_matrix(case: dict[str, Any]) -> dict[str, bool]:
    def mutated(
        name: str,
        change: Callable[[dict[str, Any]], None],
        *,
        rebind: bool = True,
    ) -> tuple[str, bool]:
        candidate = copy.deepcopy(case)
        state = candidate["contextPayload"]["referenceContextState"]
        change(state)
        if rebind:
            state["bindingHash"] = base.sha256_json(_state_payload_for_hash(state))
        try:
            validate_reference_state(candidate)
        except ReferenceStateError:
            return name, True
        return name, False

    outside = max(case["contextPayload"]["candidateScopeState"]["rankedItemIds"]) + 1
    return dict([
        mutated("binding_tamper", lambda state: state.__setitem__("bindingHash", "0" * 64), rebind=False),
        mutated("cross_task", lambda state: state.__setitem__("taskId", "task-forged")),
        mutated("future_revision", lambda state: state.__setitem__("referenceTaskRevision", state["contextTaskRevision"] + 1)),
        mutated("wrong_scope_revision", lambda state: state.__setitem__("candidateScopeSourceRevision", state["candidateScopeSourceRevision"] + 1)),
        mutated("focus_outside_presentation", lambda state: state.__setitem__("focusedProductId", outside)),
        mutated("duplicate_presentation", lambda state: state["presentationIds"].append(state["presentationIds"][0])),
        mutated("comparison_outside_presentation", lambda state: state["comparedProductIds"].append(outside)),
        mutated("source_contract_conflict", lambda state: state.__setitem__("sourceKind", "NONE")),
    ])


def deterministic_preflight(manifest_path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "FROZEN_BEFORE_EXECUTION":
        raise RuntimeError("manifest is not frozen before execution")
    for relative, expected_hash in manifest["sourceFreeze"].items():
        actual_hash = base.file_hash(ROOT / relative)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"source hash mismatch for {relative}: "
                f"expected {expected_hash}, got {actual_hash}"
            )
    dataset_path = ROOT / manifest["dataset"]["path"]
    if base.file_hash(dataset_path) != manifest["dataset"]["sha256"]:
        raise RuntimeError("scenario hash mismatch")
    cases = base.load_jsonl(dataset_path)
    if len(cases) != manifest["dataset"]["turnCaseCount"]:
        raise RuntimeError("scenario count mismatch")
    legacy_ids = [case["caseId"] for case in cases if case["lineageKind"] == "LEGACY_V3"]
    live_cases = [case for case in cases if case["lineageKind"] == "A2_REAL_UI_SANITIZED"]
    legacy_hash = base.sha256_json(legacy_ids)

    protected_equal = 0
    recovery_equal = 0
    reference_valid = 0
    reduced_cases = 0
    estimates: dict[str, list[int]] = {arm: [] for arm in ARMS}
    for case in cases:
        validate_reference_state(case)
        reference_valid += 1
        compiled = {arm: compile_case(case, arm) for arm in ARMS}
        if all(
            compiled["CTX1a"].model_view.get(field)
            == compiled["CTX1b"].model_view.get(field)
            for field in PROTECTED_FIELDS
        ):
            protected_equal += 1
        if (
            compiled["CTX1b"].receipt.estimated_tokens
            < compiled["CTX1a"].receipt.estimated_tokens
        ):
            reduced_cases += 1
        for arm in ARMS:
            estimates[arm].append(compiled[arm].receipt.estimated_tokens)
            replay_case = json.loads(base.canonical_json(case))
            replay = compile_case(replay_case, arm)
            if (
                replay.model_view == compiled[arm].model_view
                and replay.receipt.semantic_hash == compiled[arm].receipt.semantic_hash
            ):
                recovery_equal += 1
    focus_case = next(
        case
        for case in live_cases
        if case["contextPayload"]["referenceContextState"]["focusedProductId"] is not None
        and case["contextPayload"]["referenceContextState"]["comparedProductIds"]
    )
    mutation_results = _mutation_matrix(focus_case)
    expected_count = len(cases)
    expected_recovery = expected_count * len(ARMS)
    status = "PASS" if (
        len(legacy_ids) == manifest["dataset"]["legacyTurnCaseCount"]
        and legacy_hash == manifest["dataset"]["legacyCaseIdsSha256"]
        and len(live_cases) == manifest["dataset"]["a2LiveTurnCaseCount"]
        and protected_equal == expected_count
        and recovery_equal == expected_recovery
        and reference_valid == expected_count
        and all(mutation_results.values())
        and reduced_cases > 0
    ) else "FAIL"
    return {
        "status": status,
        "turnCaseCount": expected_count,
        "legacyTurnCaseCount": len(legacy_ids),
        "a2LiveTurnCaseCount": len(live_cases),
        "protectedFieldEqualityCount": protected_equal,
        "referenceBindingValidationCount": reference_valid,
        "compileReplayRecoveryCount": recovery_equal,
        "compileReplayRecoveryExpected": expected_recovery,
        "treatmentReductionCaseCount": reduced_cases,
        "failClosedMutationResults": mutation_results,
        "failClosedMutationCount": sum(mutation_results.values()),
        "estimatedTokensP50": {
            arm: base.percentile([float(value) for value in estimates[arm]], 0.5)
            for arm in ARMS
        },
    }


def _aggregate(traces: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    rows = [row for row in traces if row["arm"] == arm]
    succeeded = [row for row in rows if row["status"] == "SUCCEEDED"]
    observed = [
        row for row in succeeded
        if all(row["usage"].get(field) is not None for field in (
            "promptTokens", "completionTokens", "totalTokens"
        ))
    ]
    latencies = [float(row["durationMs"]) for row in succeeded]
    prompt_tokens = [float(row["usage"]["promptTokens"]) for row in observed]
    completion_tokens = [float(row["usage"]["completionTokens"]) for row in observed]
    total_tokens = [float(row["usage"]["totalTokens"]) for row in observed]
    cached_tokens = [
        int(row["usage"]["cachedInputTokens"])
        for row in observed
        if row["usage"].get("cachedInputTokens") is not None
    ]
    field_counts = {
        field: sum(bool(row.get("fieldFidelity", {}).get(field)) for row in rows)
        for field in (
            "currentGoal",
            "candidateIds",
            "evidenceRefs",
            *REFERENCE_STATE_FIELDS,
        )
    }
    return {
        "caseCount": len(rows),
        "succeeded": len(succeeded),
        "failed": len(rows) - len(succeeded),
        "exactFidelity": sum(bool(row.get("exactFidelity")) for row in rows),
        "fieldFidelity": field_counts,
        "usageObserved": len(observed),
        "promptTokensSum": int(sum(prompt_tokens)),
        "promptTokensP50": base.percentile(prompt_tokens, 0.5),
        "promptTokensP95": base.percentile(prompt_tokens, 0.95),
        "completionTokensSum": int(sum(completion_tokens)),
        "totalTokensSum": int(sum(total_tokens)),
        "totalTokensP50": base.percentile(total_tokens, 0.5),
        "totalTokensP95": base.percentile(total_tokens, 0.95),
        "cachedInputTokensSum": sum(cached_tokens) if cached_tokens else None,
        "latencyP50Ms": base.percentile(latencies, 0.5),
        "latencyP95Ms": base.percentile(latencies, 0.95),
    }


async def compatibility_smoke(manifest_path: Path, output_dir: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    preflight = deterministic_preflight(manifest_path)
    if preflight["status"] != "PASS":
        raise RuntimeError("deterministic preflight failed")
    if output_dir.exists():
        raise RuntimeError("compatibility smoke output already exists")
    if not base.settings.deepseek_api_key:
        raise RuntimeError("DeepSeek API key is not configured")
    if base.settings.deepseek_model != manifest["provider"]["model"]:
        raise RuntimeError("configured model differs from frozen manifest")
    output_dir.mkdir(parents=True, exist_ok=False)
    cases = base.load_jsonl(ROOT / manifest["dataset"]["path"])
    case = next(row for row in cases if row["lineageKind"] == "A2_REAL_UI_SANITIZED" and row["turnId"] == "T4")
    compiled = compile_case(case, "CTX1b")
    client = DeepSeekToolCompatibleClient(
        api_key=base.settings.deepseek_api_key,
        base_url=base.settings.deepseek_base_url,
        timeout=float(manifest["provider"]["timeoutSeconds"]),
        max_retries=0,
    )
    started = time.perf_counter()
    error: Exception | None = None
    trace: dict[str, Any] | None = None
    try:
        trace = await _call_provider(
            client,
            model=manifest["provider"]["model"],
            max_tokens=int(manifest["provider"]["maxTokens"]),
            case=case,
            arm="CTX1b",
            model_view=compiled.model_view,
        )
    except Exception as exc:
        error = exc
    finally:
        await client.close()
    passed = error is None and bool(trace and trace.get("exactFidelity"))
    receipt = {
        "schemaVersion": "context-compiler-provider-compatibility-smoke-v4",
        "status": "PASS" if passed else "FAIL",
        "scored": False,
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "durationMs": (time.perf_counter() - started) * 1000.0,
        "thinkingDisabledOnlyForToolRequests": True,
        "maxRetries": 0,
        "caseId": case["caseId"],
        "trace": trace,
        "errorCode": type(error).__name__ if error is not None else None,
        "errorMessage": str(error)[:500] if error is not None else None,
    }
    (output_dir / "smoke.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2), flush=True)
    return 0 if passed else 1


async def execute(manifest_path: Path, output_dir: Path, attempt_id: str) -> int:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    preflight = deterministic_preflight(manifest_path)
    if preflight["status"] != "PASS":
        raise RuntimeError("deterministic preflight failed")
    if attempt_id != manifest["attemptId"]:
        raise RuntimeError("attempt id differs from frozen manifest")
    if output_dir.exists():
        raise RuntimeError("attempt output already exists; refusing overwrite")
    smoke_path = ROOT / manifest["compatibilitySmoke"]["path"]
    smoke = json.loads(smoke_path.read_text(encoding="utf-8"))
    if smoke.get("status") != "PASS" or smoke.get("scored") is not False:
        raise RuntimeError("frozen compatibility smoke is not a passing unscored run")
    if not base.settings.deepseek_api_key:
        raise RuntimeError("DeepSeek API key is not configured")
    if base.settings.deepseek_model != manifest["provider"]["model"]:
        raise RuntimeError("configured model differs from frozen manifest")

    output_dir.mkdir(parents=True, exist_ok=False)
    started_path = output_dir / "started.json"
    started = {
        "schemaVersion": "context-compiler-provider-started-v4",
        "attemptId": attempt_id,
        "status": "RUNNING",
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "runnerSha256": base.file_hash(Path(__file__)),
        "compilerSha256": base.file_hash(ROOT / "agent/app/context_compiler_v1.py"),
        "a2ReferenceResolverSha256": base.file_hash(ROOT / "agent/app/reference_context.py"),
        "automaticRetries": 0,
    }
    started_path.write_text(
        json.dumps(started, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    cases = base.load_jsonl(ROOT / manifest["dataset"]["path"])
    trace_path = output_dir / "traces.jsonl"
    rng = random.Random(manifest["randomSeed"])
    client = DeepSeekToolCompatibleClient(
        api_key=base.settings.deepseek_api_key,
        base_url=base.settings.deepseek_base_url,
        timeout=float(manifest["provider"]["timeoutSeconds"]),
        max_retries=0,
    )
    traces: list[dict[str, Any]] = []
    provider_call_ordinal = 0
    try:
        for case in cases:
            order = list(ARMS)
            rng.shuffle(order)
            for arm in order:
                provider_call_ordinal += 1
                compiled = compile_case(case, arm)
                try:
                    trace = await _call_provider(
                        client,
                        model=manifest["provider"]["model"],
                        max_tokens=int(manifest["provider"]["maxTokens"]),
                        case=case,
                        arm=arm,
                        model_view=compiled.model_view,
                    )
                except Exception as exc:
                    trace = {
                        "schemaVersion": "context-compiler-provider-trace-v4",
                        "caseId": case["caseId"],
                        "scenarioId": case["scenarioId"],
                        "turnId": case["turnId"],
                        "lineageKind": case["lineageKind"],
                        "arm": arm,
                        "status": "FAILED",
                        "errorCode": type(exc).__name__,
                        "errorMessage": str(exc)[:500],
                        "usage": {
                            "promptTokens": None,
                            "completionTokens": None,
                            "totalTokens": None,
                            "cachedInputTokens": None,
                        },
                        "fieldFidelity": {},
                        "exactFidelity": False,
                    }
                trace["providerCallOrdinal"] = provider_call_ordinal
                traces.append(trace)
                base.append_jsonl(trace_path, trace)
    finally:
        await client.close()

    aggregate = {arm: _aggregate(traces, arm) for arm in ARMS}
    case_count = len(cases)
    expected_calls = case_count * len(ARMS)
    prompt_reduction = (
        (aggregate["CTX1a"]["promptTokensSum"] - aggregate["CTX1b"]["promptTokensSum"])
        / aggregate["CTX1a"]["promptTokensSum"]
        if aggregate["CTX1a"]["promptTokensSum"] else 0.0
    )
    total_reduction = (
        (aggregate["CTX1a"]["totalTokensSum"] - aggregate["CTX1b"]["totalTokensSum"])
        / aggregate["CTX1a"]["totalTokensSum"]
        if aggregate["CTX1a"]["totalTokensSum"] else 0.0
    )
    p95_ratio = (
        aggregate["CTX1b"]["latencyP95Ms"] / aggregate["CTX1a"]["latencyP95Ms"]
        if aggregate["CTX1a"]["latencyP95Ms"] else None
    )
    all_fields_exact = all(
        count == case_count
        for arm in ARMS
        for count in aggregate[arm]["fieldFidelity"].values()
    )
    gates = {
        "deterministicPreflight": preflight["status"] == "PASS",
        "allCallsSucceeded": all(aggregate[arm]["succeeded"] == case_count for arm in ARMS),
        "allUsageObserved": all(aggregate[arm]["usageObserved"] == case_count for arm in ARMS),
        "allExactFidelity": all(aggregate[arm]["exactFidelity"] == case_count for arm in ARMS),
        "allFieldsExact": all_fields_exact,
        "treatmentPromptTokenReductionAtLeast3Pct": prompt_reduction >= 0.03,
        "treatmentTotalTokenReductionAtLeast2Pct": total_reduction >= 0.02,
        "treatmentP95LatencyWithin20Pct": p95_ratio is not None and p95_ratio <= 1.20,
        "zeroRetries": True,
    }
    accepted = all(gates.values())
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    result = {
        "schemaVersion": "context-compiler-provider-result-v4",
        "attemptId": attempt_id,
        "status": "COMPLETE",
        "verdict": "BOUNDED_CONTEXT_PROVIDER_ACCEPT" if accepted else "HOLD_CONTEXT_PROVIDER_PAIR",
        "completedAt": completed_at,
        "scope": "public_context_fidelity_plus_a2_reference_projection_not_ecommerce_task_success",
        "scenarioCount": manifest["dataset"]["scenarioCount"],
        "turnCaseCount": case_count,
        "expectedProviderCalls": expected_calls,
        "actualProviderCalls": len(traces),
        "preflight": preflight,
        "aggregate": aggregate,
        "pairedEffects": {
            "promptTokenReductionFraction": prompt_reduction,
            "totalTokenReductionFraction": total_reduction,
            "treatmentToControlP95LatencyRatio": p95_ratio,
        },
        "gates": gates,
        "productionDefaultsChanged": False,
        "taskSuccessMeasured": False,
        "resumeClaimAllowed": "bounded_provider_context_fidelity_only",
    }
    result_path = output_dir / "result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "schemaVersion": "context-compiler-provider-receipt-v4",
        "attemptId": attempt_id,
        "verdict": result["verdict"],
        "completedAt": completed_at,
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "startedSha256": base.file_hash(started_path),
        "tracesSha256": base.file_hash(trace_path),
        "resultSha256": base.file_hash(result_path),
    }
    receipt_path = output_dir / "receipt.json"
    receipt_path.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    checksum_paths = (started_path, trace_path, result_path, receipt_path)
    (output_dir / "SHA256SUMS.txt").write_text(
        "\n".join(f"{base.file_hash(path)}  {path.name}" for path in checksum_paths) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if accepted else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--attempt-id",
        default="context-compiler-provider-paired-v1-20260901-v4-attempt001",
    )
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--compatibility-smoke", action="store_true")
    args = parser.parse_args()
    manifest_path = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    if args.preflight:
        print(json.dumps(deterministic_preflight(manifest_path), ensure_ascii=False, indent=2))
        return 0
    if args.compatibility_smoke:
        if args.output_dir == DEFAULT_OUTPUT:
            output_dir = DEFAULT_SMOKE_OUTPUT
        return asyncio.run(compatibility_smoke(manifest_path, output_dir))
    return asyncio.run(execute(manifest_path, output_dir, args.attempt_id))


if __name__ == "__main__":
    raise SystemExit(main())

