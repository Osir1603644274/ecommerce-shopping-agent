"""Private action-gate scorer for the ReAct V0 shadow pilot.

This scorer intentionally evaluates only the first bounded control decision.
State retention, retrieval quality, answer quality, durability and fault
recovery remain separate gates even though their expectations coexist in the
larger behavior dataset.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ASSET_ROOT = (
    Path(__file__).resolve().parent
    / "assets"
    / "used_phone_harness_behavior_v1_20260825"
)
PRIVATE_EXPECTATIONS = ASSET_ROOT / "private" / "expectations.jsonl"

Target = tuple[str, str | None]


_ACTION_TARGETS: dict[str, set[Target]] = {
    "SEARCH": {("CALL_TOOL", "search_products")},
    "FILTER_SCOPE_THEN_SEARCH_IF_NEEDED": {
        ("CALL_TOOL", "rerank_products_in_scope"),
        ("CALL_TOOL", "search_products"),
    },
    "COMPARE_VISIBLE": {("CALL_TOOL", "compare_products")},
    "REPLAY_OR_RETRY_COMPARE": {
        ("CALL_TOOL", "compare_products"),
        ("ANSWER", None),
    },
    "RERANK_OR_SEARCH": {
        ("CALL_TOOL", "rerank_products_in_scope"),
        ("CALL_TOOL", "search_products"),
    },
    "RERANK_SCOPE": {("CALL_TOOL", "rerank_products_in_scope")},
    "FILTER_SCOPE": {("CALL_TOOL", "rerank_products_in_scope")},
    "SEARCH_IF_SCOPE_INSUFFICIENT": {("CALL_TOOL", "search_products")},
    "REVISE_AND_SEARCH": {("CALL_TOOL", "search_products")},
    "ANSWER_FROM_STATE": {("ANSWER", None)},
    "ANSWER_FROM_SCOPE": {("ANSWER", None)},
    "ANSWER_EVIDENCE_BOUNDARY": {("ANSWER", None)},
    "CLARIFY": {("ASK_CLARIFICATION", None)},
    "CLARIFY_IF_ZERO": {("ASK_CLARIFICATION", None)},
    "CLARIFY_CATEGORY_SUPPORT": {("ASK_CLARIFICATION", None)},
    "CLARIFY_STALE_REFERENCE": {("ASK_CLARIFICATION", None)},
    "SAFE_STOP": {("NEEDS_REVIEW", None)},
    "REVISE_PLAN": {
        ("CALL_TOOL", "search_products"),
        ("CALL_TOOL", "rerank_products_in_scope"),
        ("NEEDS_REVIEW", None),
    },
    "RETRY_ONCE": {
        ("CALL_TOOL", "search_products"),
        ("NEEDS_REVIEW", None),
    },
    "RESUME_FROM_RECEIPT": {
        ("ANSWER", None),
        ("CALL_TOOL", "search_products"),
    },
    "RESUME_AND_SEARCH": {("CALL_TOOL", "search_products")},
}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _expected_targets(actions: list[str]) -> set[Target]:
    result: set[Target] = set()
    for action in actions:
        result.update(_ACTION_TARGETS.get(action, set()))
    return result


def _actual_target(selected: dict[str, Any]) -> Target:
    kind = selected.get("kind")
    tool_name = selected.get("toolName") if kind == "CALL_TOOL" else None
    return str(kind) if kind is not None else "", tool_name


def score_receipts(receipts_path: Path) -> dict[str, Any]:
    receipts = _read_jsonl(receipts_path)
    oracles = {
        row["scenarioId"]: row for row in _read_jsonl(PRIVATE_EXPECTATIONS)
    }
    oracle_turns = {
        (scenario_id, turn["turnId"]): turn
        for scenario_id, row in oracles.items()
        for turn in row["turnExpectations"]
    }
    evaluated: list[dict[str, Any]] = []
    excluded = Counter()
    status_counts = Counter()
    source_counts = Counter()
    decision_durations: list[float] = []
    view_tokens: list[int] = []
    by_provenance: dict[str, list[bool]] = defaultdict(list)
    authoritative_agreements: list[bool] = []

    for receipt in receipts:
        key = receipt.get("scenarioId"), receipt.get("turnId")
        oracle = oracle_turns.get(key)
        if oracle is None:
            excluded["oracle_missing"] += 1
            continue
        if receipt.get("executionTier") != "live_439":
            excluded["non_live_439_tier"] += 1
            continue
        if receipt.get("status") != "ok":
            excluded["runner_error"] += 1
            continue
        expected_runtime = receipt.get("expectedRuntime")
        entered_runtime = receipt.get("enteredRuntime")
        if entered_runtime is None:
            excluded["runtime_not_entered"] += 1
            continue
        if receipt.get("runtimeMatches") is not True:
            excluded["runtime_mismatch"] += 1
            continue
        if entered_runtime != expected_runtime:
            excluded["wrong_runtime"] += 1
            continue
        expected = _expected_targets(oracle.get("acceptableActions") or [])
        if not expected:
            excluded["unsupported_oracle_action"] += 1
            continue

        selected = receipt.get("selectedAction") or {}
        selected_status = str(selected.get("status") or "missing")
        status_counts[selected_status] += 1
        source_counts[str(selected.get("decisionSource") or "unknown")] += 1
        actual = _actual_target(selected)
        action_match = actual in expected

        required_tools = set(oracle.get("requiredTools") or [])
        forbidden_tools = set(oracle.get("forbiddenTools") or [])
        actual_tool = actual[1] if actual[0] == "CALL_TOOL" else None
        required_tool_match = not required_tools or actual_tool in required_tools
        forbidden_tool_match = actual_tool not in forbidden_tools

        forbidden_targets = _expected_targets(oracle.get("forbiddenActions") or [])
        forbidden_action_match = actual not in forbidden_targets
        authoritative = _actual_target(receipt.get("authoritativeAction") or {})
        agrees_with_authoritative = actual == authoritative
        authoritative_agreements.append(agrees_with_authoritative)
        passed = (
            action_match
            and required_tool_match
            and forbidden_tool_match
            and forbidden_action_match
        )
        evaluated.append({
            "scenarioId": key[0],
            "turnId": key[1],
            "expected": [list(item) for item in sorted(expected)],
            "actual": list(actual),
            "selectedStatus": selected_status,
            "pass": passed,
            "actionMatch": action_match,
            "requiredToolMatch": required_tool_match,
            "forbiddenToolMatch": forbidden_tool_match,
            "forbiddenActionMatch": forbidden_action_match,
            "authoritative": list(authoritative),
            "agreesWithAuthoritative": agrees_with_authoritative,
        })
        by_provenance[str(receipt.get("provenanceKind"))].append(passed)
        duration = selected.get("durationMs")
        if isinstance(duration, (int, float)):
            decision_durations.append(float(duration))
        tokens = selected.get("viewTokenCount")
        if type(tokens) is int:
            view_tokens.append(tokens)

    passed_count = sum(int(item["pass"]) for item in evaluated)
    evaluated_count = len(evaluated)
    report = {
        "schemaVersion": "used-phone-react-v0-action-score-v1",
        "status": "EVALUATED_PARTIAL_ACTION_GATE",
        "scope": "first_decision_live_439_only",
        "evaluatedTurns": evaluated_count,
        "passedTurns": passed_count,
        "actionGateAccuracy": (
            round(passed_count / evaluated_count, 6)
            if evaluated_count else None
        ),
        "selectedStatusCounts": dict(status_counts),
        "decisionSourceCounts": dict(source_counts),
        "excludedCounts": dict(excluded),
        "byProvenance": {
            key: {
                "evaluated": len(values),
                "passed": sum(map(int, values)),
                "accuracy": round(sum(map(int, values)) / len(values), 6),
            }
            for key, values in sorted(by_provenance.items())
            if values
        },
        "decisionDurationMsMean": (
            round(statistics.fmean(decision_durations), 2)
            if decision_durations else None
        ),
        "decisionViewTokensMean": (
            round(statistics.fmean(view_tokens), 2) if view_tokens else None
        ),
        "authoritativeAgreement": (
            round(sum(map(int, authoritative_agreements)) / len(authoritative_agreements), 6)
            if authoritative_agreements else None
        ),
        "failures": [item for item in evaluated if not item["pass"]],
        "claimBoundary": {
            "scoresTaskState": False,
            "scoresRetrievalQuality": False,
            "scoresAnswerQuality": False,
            "scoresDurability": False,
            "scoresFirstBoundedDecision": True,
        },
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = score_receipts(args.receipts)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite score: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
