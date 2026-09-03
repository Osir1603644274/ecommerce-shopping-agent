"""Read-only artifact-chain validator for public pilot V2 attempt002."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "evaluation/context-multiagent-public-pilot-v2.json"
RUN = ROOT / "agent/evaluation/runs/context_multiagent_public_pilot_v2_attempt002"
BLIND = RUN / "blind-review-v1"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def json_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def main() -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    summary = json.loads((RUN / "summary.json").read_text(encoding="utf-8"))
    attempt = json.loads((RUN / "attempt.json").read_text(encoding="utf-8"))
    traces = load_jsonl(RUN / "traces.jsonl")
    model_receipts = load_jsonl(RUN / "model-call-receipts.jsonl")
    context_receipts = load_jsonl(RUN / "context-receipts.jsonl")
    checks: dict[str, bool] = {}

    checks["manifestFrozen"] = manifest["status"] == "FROZEN_BEFORE_EXECUTION"
    checks["manifestSourceHashesMatch"] = all(
        file_hash(ROOT / path) == expected
        for path, expected in manifest["sourceFiles"].items()
    )
    checks["datasetHashMatches"] = file_hash(ROOT / manifest["dataset"]["path"]) == manifest["dataset"]["sha256"]
    checks["catalogHashMatches"] = file_hash(ROOT / manifest["catalog"]["path"]) == manifest["catalog"]["sha256"]
    checks["attemptCompleted"] = attempt["status"] == "COMPLETED"
    checks["attemptManifestHashMatches"] = attempt["manifestSha256"] == file_hash(MANIFEST)
    checks["attemptTraceHashMatches"] = attempt["tracesSha256"] == file_hash(RUN / "traces.jsonl")
    checks["attemptModelReceiptHashMatches"] = attempt["modelCallReceiptsSha256"] == file_hash(RUN / "model-call-receipts.jsonl")
    checks["attemptContextReceiptHashMatches"] = attempt["contextReceiptsSha256"] == file_hash(RUN / "context-receipts.jsonl")
    checks["attemptSummaryHashMatches"] = attempt["summarySha256"] == file_hash(RUN / "summary.json")
    stable_summary = {key: value for key, value in summary.items() if key not in {"completedAt", "resultHash"}}
    checks["resultHashRecomputes"] = summary["resultHash"] == json_hash(stable_summary)
    checks["attemptResultHashMatches"] = attempt["resultHash"] == summary["resultHash"]

    by_scenario: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in traces:
        by_scenario[row["scenarioId"]][row["arm"]] = row
    checks["traceCount66"] = len(traces) == 66
    checks["scenarioCount33"] = len(by_scenario) == 33
    checks["eachScenarioHasExactlyTwoArms"] = all(set(pair) == {"CTX1b", "MA1"} for pair in by_scenario.values())
    research_pairs = [pair for pair in by_scenario.values() if pair["CTX1b"]["routeGold"].startswith("RESEARCH_")]
    checks["researchPairCount9"] = len(research_pairs) == 9
    checks["allResearchArmsExecuted"] = all(pair[arm]["status"] == "ok" for pair in research_pairs for arm in ("CTX1b", "MA1"))
    checks["sharedPairIdentityExact"] = all(
        all(
            pair["CTX1b"][field] == pair["MA1"][field]
            for field in (
                "candidateScopeHash",
                "candidateIds",
                "investigationSetId",
                "investigationSetBindingHash",
                "evidenceGapKeys",
                "deadlineAt",
            )
        )
        for pair in research_pairs
    )
    checks["candidateBoundedUniqueOrdered"] = all(
        0 < len(pair[arm]["candidateIds"]) <= 8
        and len(pair[arm]["candidateIds"]) == len(set(pair[arm]["candidateIds"]))
        for pair in research_pairs
        for arm in ("CTX1b", "MA1")
    )
    checks["fixedCallBudgetsObserved"] = all(
        pair[arm]["toolCalls"] == 1 and pair[arm]["modelCalls"] == 2
        for pair in research_pairs
        for arm in ("CTX1b", "MA1")
    )
    checks["rawChildObservationLeakZero"] = all(pair["MA1"]["rawChildObservationLeak"] is False for pair in research_pairs)
    checks["executionFailureCountZero"] = summary["diagnostics"]["armExecutionFailureCount"] == 0
    checks["contractSafetyFailureCountZero"] = summary["diagnostics"]["contractSafetyFailureCount"] == 0

    model_ids = [row["modelCallId"] for row in model_receipts]
    purpose_counts = Counter(row["callPurpose"] for row in model_receipts)
    checks["modelReceiptCount36"] = len(model_receipts) == 36
    checks["modelCallIdsUnique"] = len(model_ids) == len(set(model_ids))
    checks["modelCallsAllSucceeded"] = all(row["status"] == "SUCCEEDED" and row["retryOrdinal"] == 0 for row in model_receipts)
    checks["modelTokensObserved"] = all(row["tokenStatus"] == "OBSERVED" for row in model_receipts)
    checks["callLayersSeparated"] = purpose_counts == {
        "research_policy_decision": 9,
        "shopping_policy_decision": 9,
        "final_answer": 18,
    }
    checks["contextReceiptCount54"] = len(context_receipts) == 54
    checks["contextReceiptIdsUnique"] = len({row["receiptId"] for row in context_receipts}) == len(context_receipts)

    reviewer01 = load_jsonl(BLIND / "reviewer01.jsonl")
    reviewer02 = load_jsonl(BLIND / "reviewer02.jsonl")
    sealed = json.loads((BLIND / "SEALED_DO_NOT_SHARE.json").read_text(encoding="utf-8"))
    public_text = (BLIND / "reviewer01.jsonl").read_text(encoding="utf-8") + (BLIND / "reviewer02.jsonl").read_text(encoding="utf-8")
    checks["blindPackagesHave9Items"] = len(reviewer01) == len(reviewer02) == len(sealed["items"]) == 9
    checks["blindPackagesAreExactMirrors"] = all(
        left["candidateA"] == right["candidateB"]
        and left["candidateB"] == right["candidateA"]
        and left["itemId"] == right["itemId"]
        for left, right in zip(reviewer01, reviewer02)
    )
    checks["publicBlindPackagesDoNotLeakArmIdentity"] = not any(
        marker in public_text for marker in ("CTX1b", "MA1", "scenarioId", "UPHB", "UPRG")
    )

    report = {
        "schemaVersion": "context-multiagent-public-pilot-validation-v2",
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "checkCount": len(checks),
        "passedCount": sum(checks.values()),
        "purposeCounts": dict(purpose_counts),
        "summaryResultHash": summary["resultHash"],
        "blindPackageReceiptHash": json.loads((BLIND / "receipt.json").read_text(encoding="utf-8"))["receiptHash"],
    }
    report["reportHash"] = json_hash(report)
    (RUN / "validation-v2.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
