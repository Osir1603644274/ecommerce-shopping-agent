"""Deterministic CTX0/CTX1a/CTX1b compiler pilot on public UPHB turns.

This runner proves migration and selector contracts only. Deterministically
injected old-history distractors are programmatic mutations, so the run has
zero independent effectiveness samples and cannot authorize a production
default switch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.context_compiler_v1 import (
    RunContextV1,
    canonical_json,
    compile_context_v1,
    context_items_from_pack,
    sha256_json,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "evaluation" / "context-paired-pilot-v1.json"
DEFAULT_OUTPUT = (
    ROOT
    / "agent"
    / "evaluation"
    / "runs"
    / "context_paired_pilot_v1_attempt001"
)
HASH_A = "a" * 64
HASH_B = "b" * 64
REFERENCE_MARKERS = (
    "刚才",
    "之前",
    "前面",
    "最开始",
    "上一个",
    "上一轮",
    "那个",
    "那些",
    "这两个",
    "那两个",
    "上述",
    "继续",
    "还是",
)
PROTECTED_FIELDS = (
    "goal",
    "confirmedFacts",
    "hardConstraints",
    "candidateScopeState",
    "evidenceRefs",
)


class Pack:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.run_id = payload["runId"]
        self.candidate_scope_state = payload.get("candidateScopeState")

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self.payload)


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def percentile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def git_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def history_for(turns: list[dict[str, Any]], turn_index: int) -> list[dict[str, Any]]:
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
    previous = turns[:turn_index]
    natural: list[dict[str, Any]] = []
    for index, turn in enumerate(previous):
        kind = "recent_verbatim" if index >= max(0, len(previous) - 2) else "older_summary"
        natural.append(
            {
                "role": "user",
                "summary": turn["text"],
                "atTurn": index + 1,
                "kind": kind,
                "sourceTurns": [index + 1],
            }
        )
    # The production ContextPack is bounded; retain two deterministic old
    # distractors and at most four natural history items.
    return distractors + natural[-4:]


def build_payload(
    scenario: dict[str, Any], turn_index: int
) -> tuple[dict[str, Any], str]:
    turn = scenario["turns"][turn_index]
    query = turn["text"]
    revision = turn_index + 1
    scope = {
        "scopeId": f"scope-{scenario['scenarioId']}",
        "taskId": f"task-{scenario['scenarioId']}",
        "sourceRevision": revision,
        "status": "active",
        "rankedItemIds": [101, 102, 103],
        "visibleProductIds": [101, 102, 103],
    }
    return (
        {
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
            "candidateScopeState": scope,
            "historySummaries": history_for(scenario["turns"], turn_index),
            "allowedTools": ["get_product_details"],
            "evidenceRefs": ["dataset:uphb-public"],
        },
        query,
    )


def stable_receipt(receipt: Any) -> dict[str, Any]:
    value = receipt.model_dump(by_alias=True, mode="json")
    value.pop("compileDurationMs", None)
    value.pop("persistenceStatus", None)
    return value


def execute(manifest_path: Path, output_dir: Path, attempt_id: str) -> int:
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("status") != "FROZEN_BEFORE_EXECUTION":
        raise RuntimeError("paired pilot manifest is not frozen")
    for relative, expected in manifest["sourceFiles"].items():
        actual = file_hash(ROOT / relative)
        if actual != expected:
            raise RuntimeError(
                f"source hash mismatch for {relative}: expected {expected}, got {actual}"
            )

    dataset_path = ROOT / manifest["dataset"]["path"]
    if file_hash(dataset_path) != manifest["dataset"]["sha256"]:
        raise RuntimeError("public UPHB dataset hash mismatch")
    scenarios = [
        json.loads(line)
        for line in dataset_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(scenarios) != manifest["dataset"]["scenarioCount"]:
        raise RuntimeError("public UPHB scenario count mismatch")

    cases: list[dict[str, Any]] = []
    durations: dict[str, list[float]] = {"CTX1a": [], "CTX1b": []}
    tokens: dict[str, list[float]] = {"CTX1a": [], "CTX1b": []}
    model_bytes: dict[str, list[float]] = {"CTX1a": [], "CTX1b": []}
    for scenario in scenarios:
        for turn_index, turn in enumerate(scenario["turns"]):
            payload, query = build_payload(scenario, turn_index)
            pack = Pack(payload)
            candidate_scope = payload["candidateScopeState"]
            run = RunContextV1(
                runId=payload["runId"],
                parentRunId=None,
                handoffId=None,
                tenantId="tenant-public-pilot",
                ownerId="owner-public-pilot",
                sessionId=f"session-{scenario['scenarioId']}",
                recipientType="SELF",
                recipientId="owner-public-pilot",
                taskId=payload["taskId"],
                taskRevision=payload["baseContextRevision"],
                agentRole="SHOPPING_AGENT",
                phase="SHOPPING_PLANNER",
                modelCallOrdinal=0,
                candidateScopeId=candidate_scope["scopeId"],
                candidateScopeSourceRevision=candidate_scope["sourceRevision"],
                candidateScopeHash=sha256_json(candidate_scope),
                deadlineAt=datetime.now(timezone.utc) + timedelta(hours=1),
                compilerVersion="context-compiler-v1",
                policyVersion="context-policy-v1",
                capabilityGrantId="grant-public-pilot",
                capabilityGrantHash=HASH_A,
                sensitivity="SERVER_ONLY",
            )
            items = context_items_from_pack(pack, run)
            ctx1a = compile_context_v1(
                run,
                items,
                budget_tokens=20_000,
                tool_schema_hash=HASH_A,
                model_config_hash=HASH_B,
                history_policy="preserve",
                query=query,
            )
            ctx1b = compile_context_v1(
                run,
                items,
                budget_tokens=20_000,
                tool_schema_hash=HASH_A,
                model_config_hash=HASH_B,
                history_policy="query_focused",
                query=query,
            )
            ctx0 = {key: value for key, value in payload.items() if key != "runId"}
            protected_equal = all(
                ctx1a.model_view.get(field) == ctx1b.model_view.get(field)
                for field in PROTECTED_FIELDS
            )
            reference_query = any(marker in query.casefold() for marker in REFERENCE_MARKERS)
            history_equal = (
                ctx1a.model_view.get("historySummaries", [])
                == ctx1b.model_view.get("historySummaries", [])
            )
            byte_delta = (
                ctx1b.receipt.model_view_bytes - ctx1a.receipt.model_view_bytes
            )
            token_delta = (
                ctx1b.receipt.estimated_tokens - ctx1a.receipt.estimated_tokens
            )
            checks = {
                "ctx0Ctx1aSemanticEqual": ctx0 == ctx1a.model_view,
                "protectedFieldsEqual": protected_equal,
                "candidateScopeEqual": (
                    ctx1a.model_view.get("candidateScopeState")
                    == ctx1b.model_view.get("candidateScopeState")
                ),
                "ctx1bNeverLarger": byte_delta <= 0 and token_delta <= 0,
                "referenceHistoryPreserved": (not reference_query) or history_equal,
            }
            input_payload = {
                "scenarioId": scenario["scenarioId"],
                "turnId": turn["turnId"],
                "query": query,
                "contextPayload": payload,
                "datasetHash": manifest["dataset"]["sha256"],
            }
            receipt_payload = {
                "ctx1a": stable_receipt(ctx1a.receipt),
                "ctx1b": stable_receipt(ctx1b.receipt),
            }
            cases.append(
                {
                    "caseId": f"{scenario['scenarioId']}:{turn['turnId']}",
                    "sourceClusterId": scenario["scenarioId"],
                    "inputHash": sha256_json(input_payload),
                    "referenceQuery": reference_query,
                    "historyItemCountCtx1a": len(
                        ctx1a.model_view.get("historySummaries", [])
                    ),
                    "historyItemCountCtx1b": len(
                        ctx1b.model_view.get("historySummaries", [])
                    ),
                    "modelViewBytesCtx1a": ctx1a.receipt.model_view_bytes,
                    "modelViewBytesCtx1b": ctx1b.receipt.model_view_bytes,
                    "estimatedTokensCtx1a": ctx1a.receipt.estimated_tokens,
                    "estimatedTokensCtx1b": ctx1b.receipt.estimated_tokens,
                    "byteDeltaCtx1bMinusCtx1a": byte_delta,
                    "tokenDeltaCtx1bMinusCtx1a": token_delta,
                    "checks": checks,
                    "actualOutcome": "PASS" if all(checks.values()) else "FAIL",
                    "receipts": receipt_payload,
                    "receiptHash": sha256_json(receipt_payload),
                    "modelCalls": 0,
                }
            )
            durations["CTX1a"].append(ctx1a.receipt.compile_duration_ms)
            durations["CTX1b"].append(ctx1b.receipt.compile_duration_ms)
            tokens["CTX1a"].append(float(ctx1a.receipt.estimated_tokens))
            tokens["CTX1b"].append(float(ctx1b.receipt.estimated_tokens))
            model_bytes["CTX1a"].append(float(ctx1a.receipt.model_view_bytes))
            model_bytes["CTX1b"].append(float(ctx1b.receipt.model_view_bytes))

    failures = [case for case in cases if case["actualOutcome"] != "PASS"]
    non_reference_reductions = [
        case
        for case in cases
        if not case["referenceQuery"] and case["byteDeltaCtx1bMinusCtx1a"] < 0
    ]
    context_contract_accept = not failures and bool(non_reference_reductions)
    status = "BOUNDED_CONTEXT_ACCEPT" if context_contract_accept else "CONTEXT_HOLD"
    summary = {
        "schemaVersion": "context-paired-pilot-result-v1",
        "attemptId": attempt_id,
        "status": status,
        "completedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "gitHead": git_head(),
        "manifestPath": str(manifest_path.relative_to(ROOT)).replace("\\", "/"),
        "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "runnerPath": str(Path(__file__).relative_to(ROOT)).replace("\\", "/"),
        "runnerSha256": file_hash(Path(__file__)),
        "sourceClusterCount": len(scenarios),
        "turnCaseCount": len(cases),
        "passedTurnCaseCount": len(cases) - len(failures),
        "failedTurnCaseCount": len(failures),
        "referenceQueryCaseCount": sum(1 for case in cases if case["referenceQuery"]),
        "nonReferenceReductionCaseCount": len(non_reference_reductions),
        "ctx0Ctx1aExactEqualityCount": sum(
            1 for case in cases if case["checks"]["ctx0Ctx1aSemanticEqual"]
        ),
        "protectedFieldEqualityCount": sum(
            1 for case in cases if case["checks"]["protectedFieldsEqual"]
        ),
        "aggregate": {
            arm: {
                "compileLatencyP50Ms": percentile(durations[arm], 0.50),
                "compileLatencyP95Ms": percentile(durations[arm], 0.95),
                "estimatedTokensP50": percentile(tokens[arm], 0.50),
                "estimatedTokensP95": percentile(tokens[arm], 0.95),
                "modelViewBytesP50": percentile(model_bytes[arm], 0.50),
                "modelViewBytesP95": percentile(model_bytes[arm], 0.95),
            }
            for arm in ("CTX1a", "CTX1b")
        },
        "modelCallCounts": {
            "taskManager": 0,
            "taskStateExtraction": 0,
            "shoppingPolicyDecision": 0,
            "researchPolicyDecision": 0,
            "finalAnswer": 0,
        },
        "tokenStatus": "ESTIMATED",
        "ctx0Ctx1aDecision": (
            "CTX_MIGRATION_ACCEPT" if context_contract_accept else "CTX_MIGRATION_HOLD"
        ),
        "ctx1aCtx1bDecision": (
            "CTX_SELECTOR_CONTRACT_ACCEPT"
            if context_contract_accept
            else "CTX_SELECTOR_HOLD"
        ),
        "effectivenessDecision": "HOLD_NOT_MEASURED",
        "independentEffectivenessSampleCount": 0,
        "syntheticHistoryMutationUsed": True,
        "sealedDataUsed": False,
        "productionDefaultsChanged": False,
    }
    stable_summary = {key: value for key, value in summary.items() if key != "completedAt"}
    # Runtime latency is descriptive and excluded from the stable decision hash.
    stable_summary["aggregate"] = {
        arm: {
            key: value
            for key, value in metrics.items()
            if not key.startswith("compileLatency")
        }
        for arm, metrics in summary["aggregate"].items()
    }
    summary["resultHash"] = sha256_json(stable_summary)

    output_dir.mkdir(parents=True, exist_ok=False)
    cases_path = output_dir / "cases.jsonl"
    cases_path.write_text(
        "".join(canonical_json(case) + "\n" for case in cases),
        encoding="utf-8",
        newline="\n",
    )
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "schemaVersion": "context-paired-pilot-receipt-v1",
        "attemptId": attempt_id,
        "decision": status,
        "resultHash": summary["resultHash"],
        "casesSha256": file_hash(cases_path),
        "summarySha256": file_hash(summary_path),
    }
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if context_contract_accept else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--attempt-id", default="context-paired-pilot-v1-attempt001")
    args = parser.parse_args()
    manifest = args.manifest if args.manifest.is_absolute() else ROOT / args.manifest
    output = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    return execute(manifest, output, args.attempt_id)


if __name__ == "__main__":
    raise SystemExit(main())
