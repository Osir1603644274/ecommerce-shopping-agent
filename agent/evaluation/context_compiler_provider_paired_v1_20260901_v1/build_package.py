"""One-time builder for the public frozen turn cases and preregistration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE = (
    ROOT
    / "agent/evaluation/assets/used_phone_harness_behavior_v1_20260825/public/scenarios.jsonl"
)
SCENARIOS = PACKAGE_DIR / "scenarios.jsonl"
MANIFEST = PACKAGE_DIR / "manifest.json"
REFERENCE_MARKERS = (
    "刚才", "之前", "前面", "最开始", "上一个", "上一轮",
    "那个", "那些", "这两个", "那两个", "上述", "继续", "还是",
)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def history_for(turns: list[dict[str, Any]], index: int) -> list[dict[str, Any]]:
    distractors = [
        {
            "role": "user",
            "summary": "较早对话：用户询问发票抬头和配送时间",
            "atTurn": -2,
            "kind": "older_summary",
            "sourceTurns": [1],
        },
        {
            "role": "assistant",
            "summary": "较早对话：助手介绍了售后服务入口",
            "atTurn": -1,
            "kind": "older_summary",
            "sourceTurns": [1],
        },
    ]
    natural = [
        {
            "role": "user",
            "summary": turn["text"],
            "atTurn": previous_index + 1,
            "kind": (
                "recent_verbatim"
                if previous_index >= max(0, index - 2)
                else "older_summary"
            ),
            "sourceTurns": [previous_index + 1],
        }
        for previous_index, turn in enumerate(turns[:index])
    ]
    return distractors + natural[-4:]


def main() -> int:
    if SCENARIOS.exists() or MANIFEST.exists():
        raise RuntimeError("frozen package files already exist; refusing overwrite")
    sources = [
        json.loads(line)
        for line in SOURCE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases: list[dict[str, Any]] = []
    for scenario in sources:
        turns = scenario["turns"]
        for index, turn in enumerate(turns):
            query = turn["text"]
            reference = any(marker in query.casefold() for marker in REFERENCE_MARKERS)
            revision = index + 1
            case_id = f"{scenario['scenarioId']}:{turn['turnId']}"
            cases.append({
                "schemaVersion": "context-compiler-provider-case-v1",
                "caseId": case_id,
                "scenarioId": scenario["scenarioId"],
                "turnId": turn["turnId"],
                "query": query,
                "referenceQuery": reference,
                "expectedRecentReference": (
                    turns[index - 1]["text"] if reference and index > 0 else None
                ),
                "contextPayload": {
                    "runId": f"run-{scenario['scenarioId']}-{turn['turnId']}",
                    "taskId": f"task-{scenario['scenarioId']}",
                    "baseContextRevision": revision,
                    "goal": query,
                    "confirmedFacts": [
                        {"key": "scenarioTags", "value": scenario.get("tags", [])}
                    ],
                    "hardConstraints": [],
                    "softPreferences": [],
                    "unknowns": [],
                    "pendingQuestions": [],
                    "shoppingGuideState": {"category": "phone"},
                    "candidateScopeState": {
                        "scopeId": f"scope-{scenario['scenarioId']}",
                        "taskId": f"task-{scenario['scenarioId']}",
                        "sourceRevision": revision,
                        "status": "active",
                        "rankedItemIds": [101, 102, 103],
                        "visibleProductIds": [101, 102, 103],
                    },
                    "historySummaries": history_for(turns, index),
                    "allowedTools": ["get_product_details"],
                    "evidenceRefs": ["dataset:uphb-public"],
                },
            })
    SCENARIOS.write_text(
        "".join(canonical_json(case) + "\n" for case in cases),
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schemaVersion": "context-compiler-provider-manifest-v1",
        "experimentId": "context-compiler-provider-paired-v1-20260901",
        "attemptId": "context-compiler-provider-paired-v1-20260901-attempt001",
        "status": "FROZEN_BEFORE_EXECUTION",
        "createdAt": "2026-09-01T00:00:00Z",
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
            "sha256": file_hash(SCENARIOS),
            "sourcePath": str(SOURCE.relative_to(ROOT)).replace("\\", "/"),
            "sourceSha256": file_hash(SOURCE),
            "scenarioCount": len(sources),
            "turnCaseCount": len(cases),
            "publicOnly": True,
            "sealedDataUsed": False,
        },
        "sourceFreeze": {
            "agent/evaluation/context_compiler_provider_paired_v1_20260901_v1/runner.py": file_hash(PACKAGE_DIR / "runner.py"),
            "agent/app/context_compiler_v1.py": file_hash(ROOT / "agent/app/context_compiler_v1.py"),
        },
        "provider": {
            "name": "deepseek",
            "model": "deepseek-v4-flash",
            "temperature": 0,
            "timeoutSeconds": 30,
            "maxTokens": 256,
            "maxRetries": 0,
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
    print(canonical_json({
        "status": "FROZEN",
        "scenarioCount": len(sources),
        "turnCaseCount": len(cases),
        "manifestSha256": file_hash(MANIFEST),
        "scenariosSha256": file_hash(SCENARIOS),
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

