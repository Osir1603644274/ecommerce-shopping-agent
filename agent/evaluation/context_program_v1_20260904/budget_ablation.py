"""Deterministic P5 budget and removable-component ablation.

Inputs are the completed P4 control dialogues and their post-turn semantic state.
This phase tests compiler mechanics only; P4 HOLD prevents quality promotion.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from agent.app.context_compiler_v1 import (
    ContextBudgetExceeded,
    RunContextV1,
    compile_context_v1,
    context_items_from_pack,
)


HERE = Path(__file__).resolve().parent
P4 = HERE / "p4/attempt002"
BUDGETS = (4000, 2000, 1000, 500, 250)
POLICIES = ("preserve", "query_focused")
COMPONENTS = (
    "ALL",
    "NO_RAW_HISTORY",
    "NO_SOFT_PREFERENCE",
    "NO_BACKGROUND",
    "PROTECTED_ONLY",
)
PROTECTED = {
    "TASK_FACT", "HARD_CONSTRAINT", "FRESH_EVIDENCE", "WORKING_SET",
    "REFERENCE_CONTEXT", "RESEARCH_REPORT",
}
HASH_A = "a" * 64
HASH_B = "b" * 64


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def append_jsonl(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(canonical(value) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


class Pack:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.run_id = payload["runId"]
        self.candidate_scope_state = payload.get("candidateScopeState")

    def model_dump(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self.payload)


def make_run(conversation_id: str, revision: int) -> RunContextV1:
    return RunContextV1.model_validate({
        "runId": f"p5-{conversation_id}",
        "parentRunId": None,
        "handoffId": None,
        "tenantId": "tenant-local",
        "ownerId": "context-program",
        "sessionId": f"p5-{conversation_id}",
        "recipientType": "SELF",
        "recipientId": "context-program",
        "taskId": f"p5-{conversation_id}",
        "taskRevision": revision,
        "agentRole": "SHOPPING_AGENT",
        "phase": "SHOPPING_FINAL_ANSWER",
        "modelCallOrdinal": 0,
        "candidateScopeId": None,
        "candidateScopeSourceRevision": None,
        "candidateScopeHash": None,
        "deadlineAt": datetime.now(timezone.utc) + timedelta(hours=1),
        "compilerVersion": "context-compiler-v1",
        "policyVersion": "context-policy-v1",
        "capabilityGrantId": f"grant-{conversation_id}",
        "capabilityGrantHash": HASH_B,
        "sensitivity": "SERVER_ONLY",
    })


def build_items() -> dict[str, dict[str, Any]]:
    outputs = load_jsonl(P4 / "paired_outputs.jsonl")
    private = {
        row["executionOrdinal"]: row["semanticState"]
        for row in load_jsonl(P4 / "private_turn_receipts.jsonl")
    }
    final_control: dict[str, dict[str, Any]] = {}
    for row in outputs:
        if row["arm"] != "RAW_FULL_CONTROL":
            continue
        previous = final_control.get(row["conversationId"])
        if previous is None or row["semanticTurn"] > previous["semanticTurn"]:
            final_control[row["conversationId"]] = row
    cases: dict[str, dict[str, Any]] = {}
    for conversation_id, row in sorted(final_control.items()):
        state = private[row["executionOrdinal"]]
        guide = state.get("shoppingGuide") or {}
        requirements = list(guide.get("requirements") or [])
        history = row["dialogue"][:-2]
        summaries = []
        for index, message in enumerate(history):
            summaries.append({
                "role": message["role"],
                "summary": message["content"],
                "kind": "recent_verbatim" if index >= len(history) - 2 else "older_summary",
                "sourceTurns": [index // 2 + 1],
            })
        payload = {
            "runId": f"legacy-{conversation_id}",
            "taskId": f"p5-{conversation_id}",
            "baseContextRevision": int(row["postStateRevision"]),
            "goal": row["dialogue"][-2]["content"],
            "confirmedFacts": [{"key": "category", "value": "phone"}],
            "hardConstraints": [item for item in requirements if item.get("priority") == "hard"],
            "softPreferences": [
                item for item in requirements if item.get("priority") != "hard"
            ] + [{"useCase": item} for item in guide.get("useCases") or []],
            "historySummaries": summaries,
            "candidateScopeState": {
                "category": guide.get("category"),
                "candidateIds": guide.get("candidateIds") or [],
                "comparedIds": guide.get("comparedIds") or [],
                "evidenceStatus": guide.get("evidenceStatus"),
            },
            "evidenceRefs": [f"product:{item}:catalog" for item in guide.get("candidateIds") or []],
        }
        run = make_run(conversation_id, int(row["postStateRevision"]))
        items = context_items_from_pack(Pack(payload), run)
        cases[conversation_id] = {
            "run": run,
            "items": items,
            "query": payload["goal"],
            "rawHistoryMessages": len(history),
        }
    return cases


def variant(items: list[Any], name: str) -> list[Any]:
    if name == "ALL":
        return list(items)
    if name == "NO_RAW_HISTORY":
        return [item for item in items if item.item_type != "RAW_HISTORY"]
    if name == "NO_SOFT_PREFERENCE":
        return [item for item in items if item.item_type != "SOFT_PREFERENCE"]
    if name == "NO_BACKGROUND":
        return [item for item in items if item.item_type != "BACKGROUND"]
    if name == "PROTECTED_ONLY":
        return [item for item in items if item.item_type in PROTECTED]
    raise ValueError(name)


def compile_once(run: Any, items: list[Any], budget: int, policy: str, query: str) -> dict[str, Any]:
    protected_ids = {item.item_id for item in items if item.item_type in PROTECTED}
    protected_tokens = sum(item.estimated_tokens for item in items if item.item_type in PROTECTED)
    try:
        compiled = compile_context_v1(
            run,
            items,
            budget_tokens=budget,
            tool_schema_hash=HASH_A,
            model_config_hash=HASH_B,
            query=query,
            history_policy=policy,
        )
    except ContextBudgetExceeded as exc:
        return {
            "status": "BUDGET_EXCEEDED",
            "reason": str(exc),
            "protectedTokens": protected_tokens,
            "expectedBecauseProtectedExceedsBudget": protected_tokens > budget,
        }
    selected_ids = {item.item_id for item in compiled.receipt.selected_items}
    return {
        "status": "COMPILED",
        "semanticHash": compiled.receipt.semantic_hash,
        "modelViewBytes": compiled.receipt.model_view_bytes,
        "estimatedTokens": compiled.receipt.estimated_tokens,
        "selectedItemIds": sorted(selected_ids),
        "rejected": sorted(
            (item.item_id, item.reason) for item in compiled.receipt.rejected_items
        ),
        "protectedTokens": protected_tokens,
        "allProtectedRetained": protected_ids <= selected_ids,
    }


def execute(output: Path) -> dict[str, Any]:
    if output.exists():
        raise RuntimeError(f"refusing to overwrite {output}")
    output.mkdir(parents=True, exist_ok=False)
    cases = build_items()
    started = {
        "schemaVersion": "context-program-p5-started-v1",
        "startedAt": utc_now(),
        "kind": "DETERMINISTIC_OFFLINE_DIAGNOSTIC",
        "p4ResultSha256": sha_file(P4 / "result.json"),
        "p4Decision": json.loads((P4 / "result.json").read_text(encoding="utf-8"))["strictDecision"],
        "runnerSha256": sha_file(Path(__file__)),
        "budgets": BUDGETS,
        "policies": POLICIES,
        "components": COMPONENTS,
    }
    write_json(output / "started.json", started)
    rows_path = output / "ablation_rows.jsonl"
    rows: list[dict[str, Any]] = []
    for conversation_id, case in cases.items():
        for component in COMPONENTS:
            selected = variant(case["items"], component)
            for policy in POLICIES:
                for budget in BUDGETS:
                    first = compile_once(case["run"], selected, budget, policy, case["query"])
                    second = compile_once(case["run"], selected, budget, policy, case["query"])
                    deterministic_fields = (
                        "status", "reason", "semanticHash", "modelViewBytes",
                        "estimatedTokens", "selectedItemIds", "rejected",
                        "protectedTokens", "allProtectedRetained",
                        "expectedBecauseProtectedExceedsBudget",
                    )
                    deterministic = all(first.get(key) == second.get(key) for key in deterministic_fields)
                    row = {
                        "conversationId": conversation_id,
                        "componentVariant": component,
                        "historyPolicy": policy,
                        "budgetTokens": budget,
                        "rawHistoryMessages": case["rawHistoryMessages"],
                        "deterministicRerunExact": deterministic,
                        **first,
                    }
                    append_jsonl(rows_path, row)
                    rows.append(row)
    compiled = [row for row in rows if row["status"] == "COMPILED"]
    exceeded = [row for row in rows if row["status"] == "BUDGET_EXCEEDED"]
    unexpected_exceeded = [
        row for row in exceeded if not row["expectedBecauseProtectedExceedsBudget"]
    ]
    gates = {
        "all400RerunsDeterministic": len(rows) == 400 and all(row["deterministicRerunExact"] for row in rows),
        "allCompiledVariantsRetainProtectedItems": all(row["allProtectedRetained"] for row in compiled),
        "allBudgetExceededAreProtectedOverflow": not unexpected_exceeded,
        "current4000BudgetCompilesAllFullCases": sum(
            row["status"] == "COMPILED"
            and row["componentVariant"] == "ALL"
            and row["historyPolicy"] == "preserve"
            and row["budgetTokens"] == 4000
            for row in rows
        ) == 8,
    }
    current = [
        row for row in rows
        if row["componentVariant"] == "ALL"
        and row["historyPolicy"] == "preserve"
        and row["budgetTokens"] == 4000
    ]
    result = {
        "schemaVersion": "context-program-p5-result-v1",
        "status": "COMPLETE",
        "completedAt": utc_now(),
        "strictDecision": (
            "BOUNDED_OFFLINE_BUDGET_MECHANICS_ACCEPT"
            if all(gates.values()) else "HOLD_OFFLINE_BUDGET_MECHANICS"
        ),
        "rows": len(rows),
        "compiledRows": len(compiled),
        "budgetExceededRows": len(exceeded),
        "statusByBudget": {
            str(budget): dict(Counter(row["status"] for row in rows if row["budgetTokens"] == budget))
            for budget in BUDGETS
        },
        "gates": gates,
        "currentBudget4000": {
            "caseCount": len(current),
            "estimatedTokens": [row.get("estimatedTokens") for row in current],
            "modelViewBytes": [row.get("modelViewBytes") for row in current],
        },
        "claimBoundary": {
            "mechanicsOnly": True,
            "qualityOrLatencyClaimAllowed": False,
            "p4HoldStillBinding": True,
            "productionDefaultMayChange": False,
        },
    }
    write_json(output / "result.json", result)
    receipt = {
        "schemaVersion": "context-program-p5-receipt-v1",
        "rowCount": len(rows),
        "rowsSha256": sha_file(rows_path),
        "resultSha256": sha_file(output / "result.json"),
        "automaticRetries": 0,
    }
    write_json(output / "receipt.json", receipt)
    witnesses = (output / "started.json", rows_path, output / "result.json", output / "receipt.json")
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha_file(path)}  {path.name}\n" for path in witnesses),
        encoding="utf-8",
        newline="\n",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(execute(args.output.resolve()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
