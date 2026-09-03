"""Machine-check the deterministic ReAct repair stability replay.

This is a development gate over already-seen scenarios.  It verifies runtime,
receipt, option, model-attribution, constraint and answer-boundary contracts;
it deliberately does not claim held-out generalization or answer superiority.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from pathlib import Path
from typing import Any


TARGET_SCENARIOS = ("UPRG-V1-002", "UPRG-V1-003", "UPRG-V1-006")


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    values = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if any(not isinstance(value, dict) for value in values):
        raise ValueError(f"{path}: expected JSON objects")
    return values


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _expected_turns(dataset_path: Path) -> set[tuple[str, str]]:
    expected: set[tuple[str, str]] = set()
    found: set[str] = set()
    for scenario in _read_jsonl(dataset_path):
        scenario_id = scenario.get("scenarioId")
        if scenario_id not in TARGET_SCENARIOS:
            continue
        found.add(str(scenario_id))
        turns = scenario.get("turns")
        if not isinstance(turns, list):
            raise ValueError(f"{scenario_id}: turns invalid")
        for turn in turns:
            turn_id = turn.get("turnId") if isinstance(turn, dict) else None
            if not isinstance(turn_id, str):
                raise ValueError(f"{scenario_id}: turn invalid")
            expected.add((str(scenario_id), turn_id))
    if found != set(TARGET_SCENARIOS):
        raise ValueError(f"dataset missing repair scenarios: {sorted(set(TARGET_SCENARIOS) - found)}")
    return expected


def _actions(receipt: dict[str, Any]) -> list[dict[str, Any]]:
    sequence = receipt.get("reactSequence")
    actions = sequence.get("actions") if isinstance(sequence, dict) else None
    return [item for item in actions or [] if isinstance(item, dict)]


def _route(receipt: dict[str, Any]) -> tuple[str | None, str | None]:
    task_state = receipt.get("taskState")
    domain = task_state.get("domainState") if isinstance(task_state, dict) else None
    extraction = domain.get("taskStateExtraction") if isinstance(domain, dict) else None
    if not isinstance(extraction, dict):
        return None, None
    return extraction.get("route"), extraction.get("reason")


def _price_ceiling(receipt: dict[str, Any]) -> int | None:
    task_state = receipt.get("taskState")
    domain = task_state.get("domainState") if isinstance(task_state, dict) else None
    guide = domain.get("shoppingGuide") if isinstance(domain, dict) else None
    requirements = guide.get("requirements") if isinstance(guide, dict) else None
    for item in requirements or []:
        if isinstance(item, dict) and item.get("key") == "price_minor":
            value = item.get("value")
            return int(value) if isinstance(value, (int, float)) else None
    return None


def _receipt_failures(
    key: tuple[str, str], receipt: dict[str, Any]
) -> list[str]:
    failures: list[str] = []
    if receipt.get("status") != "ok":
        failures.append("receipt_status_not_ok")
    if receipt.get("enteredRuntime") != "react_v0" or receipt.get("runtimeMatches") is not True:
        failures.append("runtime_not_react_v0")
    answer = receipt.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        failures.append("answer_missing")

    actions = _actions(receipt)
    sequence = receipt.get("reactSequence")
    outcomes = sequence.get("outcomes") if isinstance(sequence, dict) else None
    if not actions or not isinstance(outcomes, list) or len(outcomes) != len(actions):
        failures.append("action_outcome_sequence_incomplete")
    for action in actions:
        published = action.get("publishedOptionIds")
        if (
            action.get("status") != "accepted"
            or not isinstance(published, list)
            or action.get("optionId") not in published
        ):
            failures.append("published_option_contract_failed")
            break

    attribution = receipt.get("modelAttribution")
    counts = attribution.get("modelCallCounts") if isinstance(attribution, dict) else None
    if isinstance(counts, dict) and any(
        isinstance(value, int) and value > 0 for value in counts.values()
    ):
        failures.append("unexpected_model_call")

    route, reason = _route(receipt)
    kinds = [str(action.get("kind")) for action in actions]
    if key == ("UPRG-V1-002", "T2"):
        if (route, reason) != (
            "deterministic_capability_boundary",
            "unsupported_game_camera_evidence",
        ):
            failures.append("capability_boundary_route_missing")
        if kinds != ["ANSWER"]:
            failures.append("capability_boundary_action_invalid")
        if not isinstance(answer, str) or not all(
            cue in answer for cue in ("帧率", "散热", "不能", "标题")
        ):
            failures.append("capability_boundary_answer_invalid")
    elif key == ("UPRG-V1-002", "T3"):
        if route != "deterministic_validated_scope_answer" or kinds != ["ANSWER"]:
            failures.append("validated_scope_answer_invalid")
    elif key == ("UPRG-V1-003", "T4"):
        if (route, reason) != (
            "deterministic_stale_scope_clarification",
            "stale_candidate_reference",
        ):
            failures.append("stale_scope_route_missing")
        if kinds != ["ASK_CLARIFICATION"]:
            failures.append("stale_scope_action_invalid")
        if not isinstance(answer, str) or "旧筛选范围" not in answer:
            failures.append("stale_scope_answer_invalid")
    elif key == ("UPRG-V1-006", "T2"):
        if _price_ceiling(receipt) != 160_000:
            failures.append("budget_replacement_failed")
        if kinds != ["CALL_TOOL", "ANSWER"]:
            failures.append("budget_action_sequence_invalid")
        guide_result = receipt.get("guideResult")
        products = guide_result.get("products") if isinstance(guide_result, dict) else None
        for item in products or []:
            product = item.get("product") if isinstance(item, dict) else None
            price = (
                product.get("syntheticReferencePriceMinor")
                if isinstance(product, dict)
                else None
            )
            if isinstance(price, (int, float)) and price > 160_000:
                failures.append("displayed_product_over_budget")
                break
    return failures


def evaluate_repair_stability(
    *, dataset_path: Path, run_roots: list[Path]
) -> dict[str, Any]:
    expected = _expected_turns(dataset_path)
    dataset_sha = _sha256(dataset_path)
    failures: list[dict[str, Any]] = []
    run_summaries: list[dict[str, Any]] = []
    latencies: list[float] = []

    for run_index, root in enumerate(run_roots, 1):
        manifest_path = root / "run" / "manifest.json"
        receipts_path = root / "run" / "receipts.jsonl"
        manifest = _read_json(manifest_path)
        rows = _read_jsonl(receipts_path)
        mapped = {(row.get("scenarioId"), row.get("turnId")): row for row in rows}
        if len(mapped) != len(rows) or set(mapped) != expected:
            failures.append({"run": run_index, "code": "receipt_set_mismatch"})
        if (
            manifest.get("status") != "COMPLETE"
            or manifest.get("expectedRuntime") != "react_v0"
            or manifest.get("datasetSha256") != dataset_sha
            or manifest.get("errorCount") != 0
            or manifest.get("runtimeMismatchCount") != 0
        ):
            failures.append({"run": run_index, "code": "manifest_contract_failed"})
        for key in sorted(expected & set(mapped)):
            for code in _receipt_failures(key, mapped[key]):
                failures.append({
                    "run": run_index,
                    "target": f"{key[0]}:{key[1]}",
                    "requestId": mapped[key].get("requestId"),
                    "code": code,
                })
            duration = mapped[key].get("runnerDurationMs")
            if isinstance(duration, (int, float)):
                latencies.append(float(duration))
        run_summaries.append({
            "run": run_index,
            "root": str(root),
            "turnCount": len(rows),
            "receiptsSha256": _sha256(receipts_path),
            "failureCount": sum(1 for item in failures if item.get("run") == run_index),
        })

    return {
        "schemaVersion": "used-phone-react-repair-stability-gate-v1",
        "status": "ACCEPT_REPAIR_STABILITY" if not failures else "HOLD",
        "scope": "seen_development_replay_only",
        "datasetSha256": dataset_sha,
        "runCount": len(run_roots),
        "turnCount": len(expected) * len(run_roots),
        "latencyMs": {
            "mean": round(statistics.fmean(latencies), 2) if latencies else None,
            "max": round(max(latencies), 2) if latencies else None,
        },
        "runs": run_summaries,
        "failureCount": len(failures),
        "failures": failures,
        "claimBoundary": {
            "developmentReplayOnly": True,
            "provesHeldoutGeneralization": False,
            "provesReactSuperiority": False,
            "authorizesDefaultRuntimeSwitch": False,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--run-root", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError(f"refusing to overwrite output: {args.output}")
    report = evaluate_repair_stability(
        dataset_path=args.dataset,
        run_roots=args.run_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["status"] == "ACCEPT_REPAIR_STABILITY" else 1)


if __name__ == "__main__":
    main()
