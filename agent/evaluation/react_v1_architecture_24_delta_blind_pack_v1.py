"""Build a minimal re-review packet for changed dialogues after a repair."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from react_v1_architecture_24_blind_pack_v1 import (
    _blind_row,
    _dialogue,
    _public_scenarios,
    _read_json,
    _receipt_map,
    _sha256,
    _write_json,
    _write_jsonl,
)


def build_delta_pack(
    *, old_pair: Path, new_pair: Path, old_pack: Path,
    dataset: Path, output_dir: Path, seed: str,
) -> dict[str, Any]:
    paired = _read_json(new_pair / "paired_result.json")
    if not (
        paired.get("status") == "ACCEPT"
        and paired.get("blindPackEligible") is True
        and paired.get("bothArmsSafetyAccept") is True
        and paired.get("coverage") == "full_suite"
    ):
        raise ValueError("new paired run is not eligible")
    public = _public_scenarios(dataset)
    old = {
        "fixed_v1": _receipt_map(old_pair / "fixed" / "receipts.jsonl"),
        "react_v1": _receipt_map(old_pair / "react" / "receipts.jsonl"),
    }
    new = {
        "fixed_v1": _receipt_map(new_pair / "fixed" / "receipts.jsonl"),
        "react_v1": _receipt_map(new_pair / "react" / "receipts.jsonl"),
    }
    candidate_keys = [
        (row["scenarioId"], row["turnIndex"])
        for row in paired.get("behaviorDifferenceCandidates", [])
    ]
    changed: list[tuple[str, int]] = []
    reusable: list[tuple[str, int]] = []
    for scenario_id, turn_index in candidate_keys:
        turns = public[scenario_id]
        old_dialogues = {
            arm: _dialogue(
                scenario_id=scenario_id, through=turn_index,
                turns=turns, receipts=old[arm],
            ) for arm in old
        }
        new_dialogues = {
            arm: _dialogue(
                scenario_id=scenario_id, through=turn_index,
                turns=turns, receipts=new[arm],
            ) for arm in new
        }
        (changed if old_dialogues != new_dialogues else reusable).append(
            (scenario_id, turn_index)
        )
    expected_changed = {("scenario-004", 2), ("scenario-005", 2)}
    expected_reusable = {
        ("scenario-001", 1), ("scenario-002", 1), ("scenario-003", 2)
    }
    if set(changed) != expected_changed or set(reusable) != expected_reusable:
        raise ValueError("observed delta set does not match the independently reviewed set")

    reviewer_one: list[dict[str, Any]] = []
    reviewer_two: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    for ordinal, (scenario_id, turn_index) in enumerate(changed, 1):
        turns = public[scenario_id]
        fixed_dialogue = _dialogue(
            scenario_id=scenario_id, through=turn_index,
            turns=turns, receipts=new["fixed_v1"],
        )
        react_dialogue = _dialogue(
            scenario_id=scenario_id, through=turn_index,
            turns=turns, receipts=new["react_v1"],
        )
        item_id = f"delta-{ordinal:02d}"
        swap = hashlib.sha256(f"{seed}:{item_id}".encode()).digest()[0] & 1
        if swap:
            first = (react_dialogue, fixed_dialogue)
            labels = {"A": "react_v1", "B": "fixed_v1"}
        else:
            first = (fixed_dialogue, react_dialogue)
            labels = {"A": "fixed_v1", "B": "react_v1"}
        reviewer_one.append(_blind_row(item_id=item_id, dialogue_a=first[0], dialogue_b=first[1]))
        reviewer_two.append(_blind_row(item_id=item_id, dialogue_a=first[1], dialogue_b=first[0]))
        mappings.append({
            "itemId": item_id,
            "source": {"scenarioId": scenario_id, "turnIndex": turn_index},
            "reviewer01": labels,
            "reviewer02": {"A": labels["B"], "B": labels["A"]},
        })

    one_path = output_dir / "reviewer01-delta.jsonl"
    two_path = output_dir / "reviewer02-delta.jsonl"
    sealed_path = output_dir / "sealed" / "mapping.json"
    instructions_path = output_dir / "HUMAN_DELTA_REVIEW_INSTRUCTIONS.md"
    _write_jsonl(one_path, reviewer_one)
    _write_jsonl(two_path, reviewer_two)
    _write_json(sealed_path, {
        "schemaVersion": "react-v1-architecture-24-delta-blind-mapping-v1",
        "seed": seed,
        "items": mappings,
    })
    instructions_path.write_text(
        "# 两项增量盲审\n\n"
        "两名原审稿人分别只读取自己的 delta 文件，评分前不得读取 sealed/。\n"
        "只需评审 2 项；逐项填写四个 1-5 分维度、A/B/tie/unjudgeable 与简短理由。\n",
        encoding="utf-8", newline="\n",
    )
    manifest = {
        "schemaVersion": "react-v1-architecture-24-delta-blind-manifest-v1",
        "status": "AWAITING_TWO_HUMAN_DELTA_BLIND_REVIEWS",
        "itemCountPerReviewer": 2,
        "mirroredAssignment": True,
        "minimumReviewers": 2,
        "reviewerMustNotReadSealedDirectory": True,
        "strictlyReusablePriorItemCount": 3,
        "removedEqualArmItemCount": 5,
        "outputs": {
            "reviewer01Sha256": _sha256(one_path),
            "reviewer02Sha256": _sha256(two_path),
            "sealedMappingSha256": _sha256(sealed_path),
            "instructionsSha256": _sha256(instructions_path),
            "seedSha256": hashlib.sha256(seed.encode()).hexdigest(),
        },
        "inputs": {
            "oldPairedResultSha256": _sha256(old_pair / "paired_result.json"),
            "newPairedResultSha256": _sha256(new_pair / "paired_result.json"),
            "oldUnblindedResultSha256": _sha256(
                old_pack / "human-reviews" / "unblinded-result.json"
            ),
            "datasetSha256": _sha256(dataset),
        },
        "generatorSha256": _sha256(Path(__file__).resolve()),
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--old-pair", required=True, type=Path)
    parser.add_argument("--new-pair", required=True, type=Path)
    parser.add_argument("--old-pack", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args()
    print(json.dumps(build_delta_pack(**vars(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
