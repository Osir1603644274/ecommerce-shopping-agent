"""One-time V3 freeze for the provider compatibility repair."""

from __future__ import annotations

import json
from pathlib import Path

from agent.evaluation.context_compiler_provider_paired_v1_20260901_v1 import (
    runner as base,
)


ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_SCENARIOS = (
    ROOT
    / "agent/evaluation/context_compiler_provider_paired_v1_20260901_v2/scenarios.jsonl"
)
SCENARIOS = PACKAGE_DIR / "scenarios.jsonl"
MANIFEST = PACKAGE_DIR / "manifest.json"


def main() -> int:
    if SCENARIOS.exists() or MANIFEST.exists():
        raise RuntimeError("frozen V3 package files already exist; refusing overwrite")
    SCENARIOS.write_bytes(SOURCE_SCENARIOS.read_bytes())
    cases = base.load_jsonl(SCENARIOS)
    manifest = {
        "schemaVersion": "context-compiler-provider-manifest-v3",
        "experimentId": "context-compiler-provider-paired-v1-20260901-v3",
        "attemptId": "context-compiler-provider-paired-v1-20260901-v3-attempt001",
        "status": "FROZEN_BEFORE_EXECUTION",
        "createdAt": "2026-09-01T00:00:00Z",
        "repairFrom": {
            "experimentId": "context-compiler-provider-paired-v1-20260901-v2",
            "attemptId": "context-compiler-provider-paired-v1-20260901-v2-attempt001",
            "verdict": "HOLD_CONTEXT_PROVIDER_PAIR",
            "failureClass": "SHARED_PROVIDER_COMPATIBILITY",
            "failedCalls": 130,
        },
        "correctionReason": (
            "DeepSeek thinking mode rejects forced tool_choice. V3 disables "
            "thinking only for tool-bearing requests and preserves all scored "
            "data, prompts, model, budgets, gates and retry count."
        ),
        "researchQuestion": (
            "Does query-focused ContextCompiler reduce observed provider input "
            "tokens without degrading exact bounded context fidelity?"
        ),
        "independentVariable": {
            "control": "CTX1a preserve history",
            "treatment": "CTX1b deterministic query-focused old-history selection",
        },
        "dataset": {
            "path": str(SCENARIOS.relative_to(ROOT)).replace("\\", "/"),
            "sha256": base.file_hash(SCENARIOS),
            "sourcePath": str(SOURCE_SCENARIOS.relative_to(ROOT)).replace("\\", "/"),
            "sourceSha256": base.file_hash(SOURCE_SCENARIOS),
            "scenarioCount": 24,
            "turnCaseCount": len(cases),
            "publicOnly": True,
            "sealedDataUsed": False,
        },
        "sourceFreeze": {
            "agent/evaluation/context_compiler_provider_paired_v1_20260901_v3/runner.py": base.file_hash(PACKAGE_DIR / "runner.py"),
            "agent/evaluation/context_compiler_provider_paired_v1_20260901_v1/runner.py": base.file_hash(ROOT / "agent/evaluation/context_compiler_provider_paired_v1_20260901_v1/runner.py"),
            "agent/app/context_compiler_v1.py": base.file_hash(ROOT / "agent/app/context_compiler_v1.py"),
        },
        "provider": {
            "name": "deepseek",
            "model": "deepseek-v4-flash",
            "temperature": 0,
            "timeoutSeconds": 30,
            "maxTokens": 256,
            "maxRetries": 0,
            "toolRequestThinkingMode": "disabled",
        },
        "compatibilitySmoke": {
            "requiredBeforeAttempt": True,
            "scored": False,
            "callCount": 1,
            "mustPassExactFidelity": True,
        },
        "randomSeed": 20260901,
        "expectedProviderCalls": len(cases) * 2,
        "gates": {
            "sourceHashes": "exact",
            "protectedFieldEquality": f"{len(cases)}/{len(cases)}",
            "compileReplayRecovery": f"{len(cases) * 2}/{len(cases) * 2}",
            "providerCallsSucceeded": f"{len(cases) * 2}/{len(cases) * 2}",
            "providerUsageObserved": f"{len(cases) * 2}/{len(cases) * 2}",
            "exactContextFidelity": f"{len(cases) * 2}/{len(cases) * 2}",
            "treatmentPromptTokens": "sum strictly lower than control",
        },
        "stopAndRetryRules": {
            "automaticRetries": 0,
            "failedAttemptOverwrite": False,
            "repairRequiresNewVersionOrAttempt": True,
            "v2AttemptPreserved": True,
        },
        "claimBoundary": (
            "Measures provider-observed token usage, latency and exact context "
            "fidelity plus deterministic compile replay. It does not measure "
            "ecommerce task success, recommendation quality, checkpoint process "
            "recovery, production readiness or production-default authority."
        ),
    }
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps({
        "status": "FROZEN",
        "turnCaseCount": len(cases),
        "manifestSha256": base.file_hash(MANIFEST),
        "scenariosSha256": base.file_hash(SCENARIOS),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

