"""One-time builder for the frozen Context Provider V4 package."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from agent.evaluation.context_compiler_provider_paired_v1_20260901_v1 import (
    runner as base,
)
from agent.evaluation.context_compiler_provider_paired_v1_20260901_v4 import (
    runner as v4,
)


ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_SCENARIOS = ROOT / "agent/evaluation/context_compiler_provider_paired_v1_20260901_v3/scenarios.jsonl"
A2_SOURCE = ROOT / "agent/evaluation/reference_context_real_ui_v2_20260901_v1/browser-run-sanitized.jsonl"
PREREGISTRATION = PACKAGE_DIR / "preregistration.md"
SCENARIOS = PACKAGE_DIR / "scenarios.jsonl"
LINEAGE = PACKAGE_DIR / "data-lineage.json"
MANIFEST = PACKAGE_DIR / "manifest.json"
A2_REPORT_HASH = "63d371229b2f56eada9ae84f887ef5351d582f1328dd1a0c957da8e1c206d5c8"


def _none_reference_state(payload: dict[str, Any]) -> dict[str, Any]:
    return v4.bind_reference_state({
        "schemaVersion": "context-provider-reference-state-v1",
        "sourceKind": "NONE",
        "taskId": payload["taskId"],
        "contextTaskRevision": payload["baseContextRevision"],
        "referenceTaskRevision": None,
        "historySourceTurn": None,
        "candidateScopeId": None,
        "candidateScopeSourceRevision": None,
        "recentReference": None,
        "presentationMode": None,
        "presentationIds": [],
        "focusedProductId": None,
        "comparedProductIds": [],
    })


def _legacy_reference_state(case: dict[str, Any]) -> dict[str, Any]:
    payload = case["contextPayload"]
    expected = case.get("expectedRecentReference")
    if expected is None:
        return _none_reference_state(payload)
    candidates = [
        item
        for item in payload.get("historySummaries", [])
        if item.get("role") == "user"
        and type(item.get("atTurn")) is int
        and item["atTurn"] > 0
        and item.get("summary") == expected
    ]
    if not candidates:
        raise RuntimeError(f"legacy case lacks exact reference source: {case['caseId']}")
    source = max(candidates, key=lambda item: item["atTurn"])
    return v4.bind_reference_state({
        "schemaVersion": "context-provider-reference-state-v1",
        "sourceKind": "SESSION_HISTORY",
        "taskId": payload["taskId"],
        "contextTaskRevision": payload["baseContextRevision"],
        "referenceTaskRevision": payload["baseContextRevision"],
        "historySourceTurn": source["atTurn"],
        "candidateScopeId": None,
        "candidateScopeSourceRevision": None,
        "recentReference": expected,
        "presentationMode": None,
        "presentationIds": [],
        "focusedProductId": None,
        "comparedProductIds": [],
    })


def _legacy_cases() -> list[dict[str, Any]]:
    rows = base.load_jsonl(SOURCE_SCENARIOS)
    result: list[dict[str, Any]] = []
    for row in rows:
        case = copy.deepcopy(row)
        case["schemaVersion"] = "context-compiler-provider-case-v2"
        case["lineageKind"] = "LEGACY_V3"
        case["contextPayload"]["referenceContextState"] = _legacy_reference_state(case)
        v4.validate_reference_state(case)
        result.append(case)
    return result


def _a2_live_cases() -> list[dict[str, Any]]:
    rows = base.load_jsonl(A2_SOURCE)
    if len(rows) != 4 or [row["turn"] for row in rows] != [1, 2, 3, 4]:
        raise RuntimeError("A2 sanitized source must contain the frozen four-turn chain")
    messages: list[str] = []
    first_visible = list(rows[0]["visibleProductIds"])
    task_id = "task-a2-reference-live-sanitized"
    scope_id = "scope-a2-reference-live-sanitized"
    result: list[dict[str, Any]] = []
    for row in rows:
        turn = int(row["turn"])
        message = row["message"]
        request_reference = row.get("requestReference") or {}
        resolver = row.get("resolver") or {}
        has_reference = request_reference.get("present") is True
        presentation_ids = list(resolver.get("presentationIds") or [])
        focused_raw = request_reference.get("focusedProductId")
        focused_id = int(focused_raw) if focused_raw is not None else None
        compared_ids = list(resolver.get("comparedProductIds") or [])
        payload = {
            "runId": f"run-A2-LIVE-T{turn}",
            "taskId": task_id,
            "baseContextRevision": int(row["taskRevision"]),
            "goal": message,
            "confirmedFacts": [{
                "key": "scenarioTags",
                "value": ["reference_context", "real_ui_sanitized", row.get("answerClass") or "recommendation"],
            }],
            "hardConstraints": [],
            "softPreferences": [],
            "unknowns": [],
            "pendingQuestions": [],
            "shoppingGuideState": {"category": "phone"},
            "candidateScopeState": {
                "scopeId": scope_id,
                "taskId": task_id,
                "sourceRevision": 9,
                "status": "active",
                "rankedItemIds": first_visible,
                "visibleProductIds": first_visible,
            },
            "historySummaries": [
                {
                    "role": "user",
                    "summary": prior,
                    "atTurn": index,
                    "kind": "recent_verbatim",
                    "sourceTurns": [index],
                }
                for index, prior in enumerate(messages, start=1)
            ],
            "allowedTools": ["get_product_details"],
            "evidenceRefs": [f"reference-context-real-ui-v2:{A2_REPORT_HASH}"],
        }
        if has_reference:
            state = v4.bind_reference_state({
                "schemaVersion": "context-provider-reference-state-v1",
                "sourceKind": "SIGNED_UI_RECEIPT",
                "taskId": task_id,
                "contextTaskRevision": payload["baseContextRevision"],
                "referenceTaskRevision": int(resolver["taskRevision"]),
                "historySourceTurn": None,
                "candidateScopeId": scope_id,
                "candidateScopeSourceRevision": int(resolver["scopeSourceRevision"]),
                "recentReference": messages[-1] if messages else None,
                "presentationMode": request_reference["presentationMode"],
                "presentationIds": presentation_ids,
                "focusedProductId": focused_id,
                "comparedProductIds": compared_ids,
            })
        else:
            state = _none_reference_state(payload)
        payload["referenceContextState"] = state
        case = {
            "schemaVersion": "context-compiler-provider-case-v2",
            "caseId": f"A2-REAL-UI:T{turn}",
            "scenarioId": "A2-REAL-UI",
            "turnId": f"T{turn}",
            "query": message,
            "referenceQuery": has_reference,
            "expectedRecentReference": state["recentReference"],
            "lineageKind": "A2_REAL_UI_SANITIZED",
            "lineage": {
                "sourcePath": str(A2_SOURCE.relative_to(ROOT)).replace("\\", "/"),
                "sourceSha256": base.file_hash(A2_SOURCE),
                "requestId": row["requestId"],
                "turn": turn,
            },
            "contextPayload": payload,
        }
        v4.validate_reference_state(case)
        result.append(case)
        messages.append(message)
    return result


def main() -> int:
    for target in (SCENARIOS, LINEAGE, MANIFEST):
        if target.exists():
            raise RuntimeError(f"frozen V4 file already exists: {target.name}")
    legacy_cases = _legacy_cases()
    live_cases = _a2_live_cases()
    cases = [*legacy_cases, *live_cases]
    SCENARIOS.write_text(
        "".join(base.canonical_json(case) + "\n" for case in cases),
        encoding="utf-8",
        newline="\n",
    )
    lineage = {
        "schemaVersion": "context-compiler-provider-lineage-v4",
        "legacy": {
            "sourcePath": str(SOURCE_SCENARIOS.relative_to(ROOT)).replace("\\", "/"),
            "sourceSha256": base.file_hash(SOURCE_SCENARIOS),
            "caseCount": len(legacy_cases),
            "caseIdsSha256": base.sha256_json([case["caseId"] for case in legacy_cases]),
            "transformation": "add identical typed referenceContextState; preserve every legacy case",
        },
        "a2RealUi": {
            "sourcePath": str(A2_SOURCE.relative_to(ROOT)).replace("\\", "/"),
            "sourceSha256": base.file_hash(A2_SOURCE),
            "caseCount": len(live_cases),
            "rawSessionOrTaskIdsIncluded": False,
            "receiptHandlesIncluded": False,
        },
        "sealedDataUsed": False,
    }
    LINEAGE.write_text(
        json.dumps(lineage, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schemaVersion": "context-compiler-provider-manifest-v4",
        "experimentId": "context-compiler-provider-paired-v1-20260901-v4",
        "attemptId": "context-compiler-provider-paired-v1-20260901-v4-attempt001",
        "status": "FROZEN_BEFORE_EXECUTION",
        "createdAt": "2026-09-01T00:00:00Z",
        "priorEvidence": {
            "v3ExperimentId": "context-compiler-provider-paired-v1-20260901-v3",
            "v3AttemptId": "context-compiler-provider-paired-v1-20260901-v3-attempt001",
            "v3Verdict": "HOLD_CONTEXT_PROVIDER_PAIR",
            "v3FailureScope": "11 exact-fidelity failures, all confined to recentReference",
        },
        "researchQuestion": (
            "Does query-focused ContextCompiler reduce observed provider input and total tokens "
            "without degrading exact bounded context fidelity after both arms receive the same "
            "server-resolved typed reference state?"
        ),
        "independentVariable": {
            "control": "CTX1a preserve history",
            "treatment": "CTX1b deterministic query-focused old-history selection",
            "onlyDifference": "history_policy",
        },
        "dataset": {
            "path": str(SCENARIOS.relative_to(ROOT)).replace("\\", "/"),
            "sha256": base.file_hash(SCENARIOS),
            "lineagePath": str(LINEAGE.relative_to(ROOT)).replace("\\", "/"),
            "lineageSha256": base.file_hash(LINEAGE),
            "legacySourcePath": str(SOURCE_SCENARIOS.relative_to(ROOT)).replace("\\", "/"),
            "legacySourceSha256": base.file_hash(SOURCE_SCENARIOS),
            "legacyTurnCaseCount": len(legacy_cases),
            "legacyCaseIdsSha256": base.sha256_json([case["caseId"] for case in legacy_cases]),
            "a2LiveSourcePath": str(A2_SOURCE.relative_to(ROOT)).replace("\\", "/"),
            "a2LiveSourceSha256": base.file_hash(A2_SOURCE),
            "a2LiveTurnCaseCount": len(live_cases),
            "scenarioCount": 25,
            "turnCaseCount": len(cases),
            "publicOnly": True,
            "sealedDataUsed": False,
        },
        "sourceFreeze": {
            str((PACKAGE_DIR / "runner.py").relative_to(ROOT)).replace("\\", "/"): base.file_hash(PACKAGE_DIR / "runner.py"),
            str((PACKAGE_DIR / "build_package.py").relative_to(ROOT)).replace("\\", "/"): base.file_hash(PACKAGE_DIR / "build_package.py"),
            str(PREREGISTRATION.relative_to(ROOT)).replace("\\", "/"): base.file_hash(PREREGISTRATION),
            "agent/evaluation/context_compiler_provider_paired_v1_20260901_v1/runner.py": base.file_hash(ROOT / "agent/evaluation/context_compiler_provider_paired_v1_20260901_v1/runner.py"),
            "agent/evaluation/context_compiler_provider_paired_v1_20260901_v3/runner.py": base.file_hash(ROOT / "agent/evaluation/context_compiler_provider_paired_v1_20260901_v3/runner.py"),
            "agent/app/context_compiler_v1.py": base.file_hash(ROOT / "agent/app/context_compiler_v1.py"),
            "agent/app/reference_context.py": base.file_hash(ROOT / "agent/app/reference_context.py"),
        },
        "provider": {
            "name": "deepseek",
            "model": "deepseek-v4-flash",
            "temperature": 0,
            "timeoutSeconds": 30,
            "maxTokens": 320,
            "maxRetries": 0,
            "toolRequestThinkingMode": "disabled",
        },
        "compatibilitySmoke": {
            "requiredBeforeAttempt": True,
            "scored": False,
            "callCount": 1,
            "path": "agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/compatibility-smoke001/smoke.json",
            "mustPassExactFidelity": True,
        },
        "randomSeed": 20260901,
        "expectedProviderCalls": len(cases) * 2,
        "gates": {
            "protectedFieldEquality": f"{len(cases)}/{len(cases)}",
            "referenceBindingValidation": f"{len(cases)}/{len(cases)}",
            "compileReplayRecovery": f"{len(cases) * 2}/{len(cases) * 2}",
            "failClosedMutations": "8/8",
            "providerCallsSucceeded": f"{len(cases) * 2}/{len(cases) * 2}",
            "providerUsageObserved": f"{len(cases) * 2}/{len(cases) * 2}",
            "exactContextFidelityPerArm": f"{len(cases)}/{len(cases)}",
            "eachOutputFieldExactPerArm": f"{len(cases)}/{len(cases)}",
            "treatmentPromptTokenReductionMinimum": 0.03,
            "treatmentTotalTokenReductionMinimum": 0.02,
            "treatmentP95LatencyMaximumRatio": 1.20,
        },
        "stopAndRetryRules": {
            "automaticRetries": 0,
            "formalAttemptCount": 1,
            "failedAttemptOverwrite": False,
            "repairRequiresNewVersion": True,
            "priorAttemptsPreserved": True,
        },
        "productionDefaultsChanged": False,
        "claimBoundary": (
            "Measures provider-observed token usage, latency, typed-reference fidelity, deterministic "
            "compile replay and evaluation-contract fail-closed behavior. It does not measure ecommerce "
            "task success, recommendation quality, production ContextPack wiring, process recovery, "
            "production readiness or production-default authority."
        ),
    }
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({
        "status": "FROZEN",
        "scenarioCount": manifest["dataset"]["scenarioCount"],
        "turnCaseCount": len(cases),
        "expectedProviderCalls": manifest["expectedProviderCalls"],
        "manifestSha256": base.file_hash(MANIFEST),
        "scenariosSha256": base.file_hash(SCENARIOS),
        "lineageSha256": base.file_hash(LINEAGE),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

