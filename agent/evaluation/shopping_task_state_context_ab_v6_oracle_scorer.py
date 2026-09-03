"""V6 oracle scorer: V5 live action gate plus explicit state/relation checks.

The source 24-scenario corpus remains immutable.  This scorer adds the missing
evidence that V5 did not collect: final session task inventory, relation checks,
and executable private ``stateChecks``.  Action status is explicitly
PASS/FAIL/NOT_EVALUATED; non-live fault scenarios are never reported as passed.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import httpx

from evaluation import shopping_task_state_context_ab_v5_action_scorer as _v5
from evaluation import used_phone_harness_behavior_scorer_v1 as _action_base


PRIVATE_EXPECTATIONS = (
    Path(__file__).resolve().parent
    / "assets"
    / "used_phone_harness_behavior_v1_20260825"
    / "private"
    / "expectations.jsonl"
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _guide(receipt: dict[str, Any]) -> dict[str, Any]:
    state = receipt.get("taskState") or {}
    domain = state.get("domainState") or {}
    value = domain.get("shoppingGuide") or {}
    return value if isinstance(value, dict) else {}


def _requirements(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    guide = _guide(receipt)
    value = guide.get("requirements") or []
    result = [item for item in value if isinstance(item, dict)]
    for avoidance in guide.get("brandAvoidances") or []:
        if not isinstance(avoidance, dict):
            continue
        result.append({
            "key": "brand",
            "operator": "not_in",
            "value": avoidance.get("values") or [],
            "priority": avoidance.get("strength"),
        })
    return result


def _frozen(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _requirement_identity(item: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("key")),
        str(item.get("operator")),
        _frozen(item.get("value")),
        str(item.get("priority")),
    )


def _parse_value(text: str) -> Any:
    if text.startswith("[") and text.endswith("]"):
        body = text[1:-1]
        return [part for part in body.split(",") if part]
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


def _value_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    if isinstance(expected, list):
        return isinstance(actual, list) and sorted(map(str, actual)) == sorted(map(str, expected))
    if expected == "domestic_phone_group" and isinstance(actual, list):
        domestic = {
            "huawei", "honor", "xiaomi", "redmi", "oppo", "vivo", "iqoo",
            "oneplus", "realme", "nubia", "blackshark", "hi",
        }
        values = {str(item).casefold() for item in actual}
        return bool(values) and values.issubset(domestic)
    if isinstance(actual, list):
        return str(expected).casefold() in {str(item).casefold() for item in actual}
    return str(actual).casefold() == str(expected).casefold()


def _matching_requirement(
    requirements: list[dict[str, Any]],
    *,
    key: str,
    operator: str | None = None,
    value: Any = None,
    priority: str | None = None,
) -> bool:
    for item in requirements:
        if item.get("key") != key:
            continue
        if operator is not None and item.get("operator") != operator:
            continue
        if value is not None and not _value_equal(item.get("value"), value):
            continue
        if priority is not None and item.get("priority") != priority:
            continue
        return True
    return False


def _check_requirement_expression(check: str, requirements: list[dict[str, Any]]) -> bool | None:
    parts = check.split(":")
    if len(parts) == 2 and parts[1] == "absent":
        return not _matching_requirement(requirements, key=parts[0])
    if len(parts) >= 3 and parts[-1] == "absent":
        priority = parts[-2] if len(parts) == 5 else None
        value_index = -3 if priority is not None else -2
        return not _matching_requirement(
            requirements,
            key=parts[0],
            operator=parts[1],
            value=_parse_value(parts[value_index]),
            priority=priority,
        )
    if len(parts) == 4:
        return _matching_requirement(
            requirements,
            key=parts[0],
            operator=parts[1],
            value=_parse_value(parts[2]),
            priority=parts[3],
        )
    return None


def _session_inventory(base_url: str, session_ids: set[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    with httpx.Client(timeout=15.0) as client:
        for session_id in sorted(session_ids):
            response = client.get(f"{base_url.rstrip('/')}/agent/sessions/{session_id}/tasks")
            response.raise_for_status()
            rows = response.json()
            result[session_id] = [
                {
                    "taskId": row.get("taskId"),
                    "status": row.get("status"),
                    "category": ((row.get("domainState") or {}).get("shoppingGuide") or {}).get("category"),
                }
                for row in rows
                if isinstance(row, dict)
            ]
    return result


def _state_check(
    check: str,
    receipt: dict[str, Any],
    previous_same_task: dict[str, Any] | None,
    inventory: list[dict[str, Any]],
) -> bool:
    state = receipt.get("taskState") or {}
    guide = _guide(receipt)
    requirements = _requirements(receipt)
    parsed = _check_requirement_expression(check, requirements)
    if parsed is not None:
        return parsed
    if check.startswith("category="):
        return guide.get("category") == check.split("=", 1)[1]
    if check.startswith("use_case="):
        return check.split("=", 1)[1] in (guide.get("useCases") or [])
    if check == "status=collecting_information":
        return state.get("status") == "collecting_information"
    if check == "pending_question=cleared":
        return not (state.get("pendingQuestions") or [])
    if check == "unbound_negative_target_recorded":
        return bool(state.get("unknowns") or state.get("pendingQuestions"))
    if check == "checkpoint_written":
        trace = receipt.get("requestTrace") or {}
        return bool(receipt.get("runId")) and trace.get("agentFinalAction") == "ask_user"
    if check == "phone_task_retained_in_session":
        categories = [item.get("category") for item in inventory]
        return categories.count("phone") == 1 and categories.count("headphones") == 1
    if check in {"candidate_pool_count<=50", "ranked_ids_count<=20"}:
        domain = (state.get("domainState") or {})
        scope = domain.get("candidateScope") or {}
        key, limit = ("candidatePoolIds", 50) if check.startswith("candidate") else ("rankedItemIds", 20)
        return len(scope.get(key) or []) <= limit
    if check in {"no_duplicate_requirements", "single_price_requirement", "single_screen_requirement"}:
        if check == "no_duplicate_requirements":
            identities = [_requirement_identity(item) for item in requirements]
            return len(identities) == len(set(identities))
        key = "price_minor" if check == "single_price_requirement" else "screen_originality"
        return sum(item.get("key") == key for item in requirements) == 1
    if check in {"game_requirement:absent", "camera_requirement:absent"}:
        prefix = check.split("_", 1)[0]
        return not any(prefix in str(item.get("key", "")) for item in requirements)
    if check == "same_compared_ids":
        return previous_same_task is not None and (
            _guide(previous_same_task).get("comparedIds") or []
        ) == (guide.get("comparedIds") or [])
    if check.startswith("preserve:"):
        key = check.split(":", 1)[1]
        if previous_same_task is None:
            return False
        before = {
            _requirement_identity(item)
            for item in _requirements(previous_same_task)
            if item.get("key") == key
        }
        after = {_requirement_identity(item) for item in requirements}
        return bool(before) and before.issubset(after)
    if check in {"all_prior_requirements_preserved", "all_non_price_requirements_preserved"}:
        if previous_same_task is None:
            return False
        before = {
            _requirement_identity(item)
            for item in _requirements(previous_same_task)
            if check == "all_prior_requirements_preserved" or item.get("key") != "price_minor"
        }
        after = {_requirement_identity(item) for item in requirements}
        return before.issubset(after)
    raise ValueError(f"unsupported state check: {check}")


def score_receipts(receipts_path: Path, *, base_url: str) -> dict[str, Any]:
    raw_rows = _read_jsonl(receipts_path)
    projected_rows, projection_cases = _v5._project_receipts(raw_rows)
    oracles = {
        row["scenarioId"]: row for row in _read_jsonl(PRIVATE_EXPECTATIONS)
    }
    fault_scenarios = {
        scenario_id for scenario_id, row in oracles.items() if row.get("faultPlan")
    }
    oracle_turns = {
        (scenario_id, turn["turnId"]): turn
        for scenario_id, row in oracles.items()
        for turn in row["turnExpectations"]
    }
    inventories = _session_inventory(
        base_url,
        {str(row["sessionId"]) for row in raw_rows if row.get("sessionId")},
    )
    raw_by_key = {
        (str(row.get("scenarioId")), str(row.get("turnId"))): row for row in raw_rows
    }
    projected_by_key = {
        (str(row.get("scenarioId")), str(row.get("turnId"))): row for row in projected_rows
    }
    previous_by_task: dict[tuple[str, str], dict[str, Any]] = {}
    checks: list[dict[str, Any]] = []
    for key in raw_by_key:
        receipt = raw_by_key[key]
        projected = projected_by_key[key]
        oracle = oracle_turns[key]
        expected_targets = _action_base._expected_targets(oracle.get("acceptableActions") or [])
        action_evaluated = (
            receipt.get("executionTier") == "live_439"
            and receipt.get("status") == "ok"
            and receipt.get("runtimeMatches") is True
            and bool(expected_targets)
        )
        actual_target = _action_base._actual_target(projected.get("selectedAction") or {})
        action_pass = actual_target in expected_targets if action_evaluated else None
        expected_relation = oracle.get("relation")
        relation_evaluated = (
            receipt.get("requestKind") == "fresh_turn" and key[0] not in fault_scenarios
        )
        actual_relation = (receipt.get("taskRelation") or {}).get("relation")
        relation_pass = actual_relation == expected_relation if relation_evaluated else None
        task_id = str((receipt.get("taskState") or {}).get("taskId") or "")
        previous = previous_by_task.get((key[0], task_id))
        state_results = [
            {
                "check": check,
                "pass": _state_check(
                    check,
                    receipt,
                    previous,
                    inventories.get(str(receipt.get("sessionId")), []),
                ),
            }
            for check in (oracle.get("stateChecks") or [])
        ] if key[0] not in fault_scenarios else []
        checks.append({
            "scenarioId": key[0],
            "turnId": key[1],
            "actionStatus": (
                "PASS" if action_pass is True else "FAIL" if action_pass is False else "NOT_EVALUATED"
            ),
            "actualAction": list(actual_target),
            "relationExpected": expected_relation,
            "relationActual": actual_relation,
            "relationStatus": (
                "PASS" if relation_pass is True else "FAIL" if relation_pass is False else "NOT_EVALUATED"
            ),
            "relationPass": relation_pass,
            "stateChecks": state_results,
            "statePass": all(item["pass"] for item in state_results),
        })
        if task_id:
            previous_by_task[(key[0], task_id)] = receipt

    action_evaluated = [row for row in checks if row["actionStatus"] != "NOT_EVALUATED"]
    state_results = [item for row in checks for item in row["stateChecks"]]
    report = {
        "schemaVersion": "shopping-task-state-context-oracle-score-v6",
        "status": "COMPLETE",
        "actionGate": {
            "evaluated": len(action_evaluated),
            "passed": sum(row["actionStatus"] == "PASS" for row in action_evaluated),
            "notEvaluated": sum(row["actionStatus"] == "NOT_EVALUATED" for row in checks),
        },
        "relationGate": {
            "evaluated": sum(row["relationStatus"] != "NOT_EVALUATED" for row in checks),
            "passed": sum(row["relationStatus"] == "PASS" for row in checks),
            "notEvaluated": sum(row["relationStatus"] == "NOT_EVALUATED" for row in checks),
        },
        "stateGate": {
            "evaluatedChecks": len(state_results),
            "passedChecks": sum(item["pass"] for item in state_results),
            "turnsWithChecks": sum(bool(row["stateChecks"]) for row in checks),
            "turnsPassed": sum(row["statePass"] for row in checks if row["stateChecks"]),
        },
        "evidenceBoundaryProjectionCount": len(projection_cases),
        "sessionTaskInventories": inventories,
        "checks": checks,
        "failures": [
            row for row in checks
            if row["actionStatus"] == "FAIL" or row["relationStatus"] == "FAIL" or not row["statePass"]
        ],
        "claimBoundary": {
            "scoresLive439Action": True,
            "scoresRelationAllTurns": True,
            "scoresDeclaredPrivateStateChecks": True,
            "scoresAnswerQuality": False,
            "scoresFaultInjection": False,
        },
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = score_receipts(args.receipts, base_url=args.base_url)
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite score: {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["failures"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
