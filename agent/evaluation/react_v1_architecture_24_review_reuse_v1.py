"""Carry forward blind judgments only when the reviewed dialogues are identical."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "constraintFidelity", "evidenceDiscipline", "taskProgression", "usefulness"
)
ARMS = ("fixed_v1", "react_v1")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _jsonl(path: Path) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all(isinstance(row, dict) for row in rows):
        raise ValueError(f"{path}: expected objects")
    return rows


def _receipts(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    return {(row["scenarioId"], row["turnIndex"]): row for row in _jsonl(path)}


def build_reuse_result(*, old_pack: Path, old_pair: Path, new_pair: Path) -> dict[str, Any]:
    old_result_path = old_pack / "human-reviews" / "unblinded-result.json"
    old_result = _json(old_result_path)
    new_paired_path = new_pair / "paired_result.json"
    new_paired = _json(new_paired_path)
    if not (
        old_result.get("status") == "UNBLINDED_DESCRIPTIVE_RESULT"
        and new_paired.get("status") == "ACCEPT"
        and new_paired.get("bothArmsSafetyAccept") is True
    ):
        raise ValueError("old review or new paired safety result is not accepted")
    new_keys = {
        (row["scenarioId"], row["turnIndex"])
        for row in new_paired.get("behaviorDifferenceCandidates", [])
    }
    expected = {(f"scenario-{number:03d}", 1 if number <= 2 else 2) for number in range(1, 6)}
    if new_keys != expected:
        raise ValueError("new run differences are not exactly the five reviewed adaptive items")

    old_binding = _json(old_pair / "run_binding.json")
    new_binding = _json(new_pair / "run_binding.json")
    dataset_key = "public/scenarios.jsonl"
    if old_binding["packageFilesSha256"][dataset_key] != new_binding["packageFilesSha256"][dataset_key]:
        raise ValueError("public scenario dataset changed")
    old_receipts = {
        arm: _receipts(old_pair / ("fixed" if arm == "fixed_v1" else "react") / "receipts.jsonl")
        for arm in ARMS
    }
    new_receipts = {
        arm: _receipts(new_pair / ("fixed" if arm == "fixed_v1" else "react") / "receipts.jsonl")
        for arm in ARMS
    }
    compared_turns: list[dict[str, Any]] = []
    for scenario_id, through in sorted(expected):
        for turn_index in range(1, through + 1):
            key = (scenario_id, turn_index)
            for arm in ARMS:
                if old_receipts[arm][key]["answer"] != new_receipts[arm][key]["answer"]:
                    raise ValueError(f"reviewed dialogue changed: {arm} {key}")
            compared_turns.append({"scenarioId": scenario_id, "turnIndex": turn_index})

    negative_parity: list[str] = []
    for number in range(17, 22):
        scenario_id = f"scenario-{number:03d}"
        key = (scenario_id, 1)
        fixed = new_receipts["fixed_v1"][key]
        react = new_receipts["react_v1"][key]
        if fixed["answer"] != react["answer"] or fixed["finalAction"] != react["finalAction"]:
            raise ValueError(f"scope-negative parity not restored: {scenario_id}")
        negative_parity.append(scenario_id)

    mapping = _json(old_pack / "sealed" / "mapping.json")
    mapping_by_item = {row["itemId"]: row for row in mapping["items"]}
    reviews = {
        "reviewer01": _jsonl(old_pack / "human-reviews" / "reviewer01-reviewed.jsonl"),
        "reviewer02": _jsonl(old_pack / "human-reviews" / "reviewer02-reviewed.jsonl"),
    }
    selected_items = {
        row["itemId"] for row in mapping["items"]
        if (row["source"]["scenarioId"], row["source"]["turnIndex"]) in expected
    }
    preferences = {"fixed_v1": 0, "react_v1": 0, "tie": 0, "unjudgeable": 0}
    sums = {arm: {dimension: 0 for dimension in DIMENSIONS} for arm in ARMS}
    counts = {arm: 0 for arm in ARMS}
    for reviewer, rows in reviews.items():
        for row in rows:
            if row["itemId"] not in selected_items:
                continue
            labels = mapping_by_item[row["itemId"]][reviewer]
            review = row["review"]
            preference = review["overallPreference"]
            resolved = labels[preference] if preference in {"A", "B"} else preference
            preferences[resolved] += 1
            for label in ("A", "B"):
                arm = labels[label]
                scores = review["scores"][f"candidate{label}"]
                counts[arm] += 1
                for dimension in DIMENSIONS:
                    sums[arm][dimension] += scores[dimension]
    means = {
        arm: {dimension: round(sums[arm][dimension] / counts[arm], 3) for dimension in DIMENSIONS}
        for arm in ARMS
    }
    return {
        "schemaVersion": "react-v1-architecture-24-review-reuse-v1",
        "completedAt": datetime.now(timezone.utc).isoformat(),
        "status": "ACCEPT_CONTENT_IDENTICAL_BLIND_JUDGMENT_REUSE",
        "newRunSafetyStatus": "ACCEPT",
        "reviewedDialogueEquivalence": True,
        "comparedDialogueTurns": compared_turns,
        "scopeNegativeParityRestored": negative_parity,
        "reusedHumanJudgmentCount": 10,
        "preferenceCounts": preferences,
        "scoreMeans": means,
        "effectInterpretation": "react_v1 adaptive preference signal; no fixed_v1 preference, three ties",
        "productionDecision": "HOLD_KEEP_FIXED_V1_DEFAULT",
        "inputHashes": {
            "oldUnblindedResultSha256": _sha(old_result_path),
            "oldPairResultSha256": _sha(old_pair / "paired_result.json"),
            "newPairResultSha256": _sha(new_paired_path),
            "oldBindingSha256": _sha(old_pair / "run_binding.json"),
            "newBindingSha256": _sha(new_pair / "run_binding.json"),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-pack", required=True, type=Path)
    parser.add_argument("--old-pair", required=True, type=Path)
    parser.add_argument("--new-pair", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output
    result = build_reuse_result(**{k: v for k, v in vars(args).items() if k != "output"})
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
