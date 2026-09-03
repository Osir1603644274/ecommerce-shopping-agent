"""Build mirrored anonymous review packets from an accepted ReAct V1 pair."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_value(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: expected object")
        rows.append(value)
    return rows


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
        newline="\n",
    )


def _receipt_map(path: Path) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    for row in _read_jsonl(path):
        scenario_id = row.get("scenarioId")
        turn_index = row.get("turnIndex")
        answer = row.get("answer")
        if type(scenario_id) is not str or type(turn_index) is not int or type(answer) is not str:
            raise ValueError(f"{path}: invalid receipt identity or answer")
        key = (scenario_id, turn_index)
        if key in result:
            raise ValueError(f"{path}: duplicate receipt {key}")
        result[key] = row
    return result


def _public_scenarios(path: Path) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for row in _read_jsonl(path):
        scenario_id = row.get("scenarioId")
        turns = row.get("turns")
        if type(scenario_id) is not str or not isinstance(turns, list):
            raise ValueError(f"{path}: invalid public scenario")
        texts: list[str] = []
        for turn in turns:
            if not isinstance(turn, dict) or type(turn.get("text")) is not str:
                raise ValueError(f"{path}: invalid public turn in {scenario_id}")
            texts.append(turn["text"])
        if not texts or scenario_id in result:
            raise ValueError(f"{path}: empty or duplicate scenario {scenario_id}")
        result[scenario_id] = texts
    return result


def _dialogue(
    *, scenario_id: str, through: int, turns: list[str], receipts: dict[tuple[str, int], dict[str, Any]]
) -> list[dict[str, str]]:
    if through < 1 or through > len(turns):
        raise ValueError(f"{scenario_id}: invalid target turn {through}")
    dialogue: list[dict[str, str]] = []
    for turn_index in range(1, through + 1):
        receipt = receipts.get((scenario_id, turn_index))
        if receipt is None:
            raise ValueError(f"{scenario_id}: missing receipt for turn {turn_index}")
        dialogue.extend((
            {"role": "user", "text": turns[turn_index - 1]},
            {"role": "assistant", "text": receipt["answer"]},
        ))
    return dialogue


def _blind_row(
    *, item_id: str, dialogue_a: list[dict[str, str]], dialogue_b: list[dict[str, str]]
) -> dict[str, Any]:
    return {
        "schemaVersion": "react-v1-architecture-24-blind-item-v1",
        "itemId": item_id,
        "candidateA": {"dialogue": dialogue_a},
        "candidateB": {"dialogue": dialogue_b},
        "rubric": {
            "constraintFidelity": "1-5; follows active hard constraints",
            "evidenceDiscipline": "1-5; does not turn unknown claims into facts",
            "taskProgression": "1-5; safely answers, advances, or clarifies",
            "usefulness": "1-5; relevant and actionable",
            "overallPreference": "A, B, tie, or unjudgeable",
        },
        "review": None,
    }


def build_pack(*, pair_dir: Path, dataset: Path, output_dir: Path, seed: str) -> dict[str, Any]:
    paired_path = pair_dir / "paired_result.json"
    binding_path = pair_dir / "run_binding.json"
    fixed_manifest_path = pair_dir / "fixed" / "manifest.json"
    react_manifest_path = pair_dir / "react" / "manifest.json"
    fixed_receipts_path = pair_dir / "fixed" / "receipts.jsonl"
    react_receipts_path = pair_dir / "react" / "receipts.jsonl"
    paired = _read_json(paired_path)
    binding = _read_json(binding_path)
    fixed_manifest = _read_json(fixed_manifest_path)
    react_manifest = _read_json(react_manifest_path)

    if not (
        paired.get("status") == "ACCEPT"
        and paired.get("blindPackEligible") is True
        and paired.get("bothArmsSafetyAccept") is True
        and paired.get("coverage") == "full_suite"
        and paired.get("selectedScenarioCount") == 24
        and paired.get("packageScenarioCount") == 24
        and fixed_manifest.get("status") == "ACCEPT"
        and react_manifest.get("status") == "ACCEPT"
        and fixed_manifest.get("scenarioCount") == 24
        and react_manifest.get("scenarioCount") == 24
    ):
        raise ValueError("paired full-suite safety gate is not ACCEPT")
    if _sha256(fixed_manifest_path) != paired.get("fixedManifestSha256"):
        raise ValueError("fixed manifest hash mismatch")
    if _sha256(react_manifest_path) != paired.get("reactManifestSha256"):
        raise ValueError("react manifest hash mismatch")
    logical_binding = dict(binding)
    declared_binding_hash = logical_binding.pop("bindingSha256", None)
    if declared_binding_hash != _hash_value(logical_binding):
        raise ValueError("run binding self-hash mismatch")
    if paired.get("bindingSha256") != declared_binding_hash:
        raise ValueError("paired result binding hash mismatch")
    package_hash = binding.get("packageFilesSha256", {}).get("public/scenarios.jsonl")
    if package_hash != _sha256(dataset):
        raise ValueError("public scenario dataset hash mismatch")
    if _sha256(fixed_receipts_path) != fixed_manifest.get("receiptsSha256"):
        raise ValueError("fixed receipts hash mismatch")
    if _sha256(react_receipts_path) != react_manifest.get("receiptsSha256"):
        raise ValueError("react receipts hash mismatch")

    fixed = _receipt_map(fixed_receipts_path)
    react = _receipt_map(react_receipts_path)
    if not fixed or set(fixed) != set(react) or len(fixed) != 33:
        raise ValueError("paired receipts must contain the same complete 33-turn set")
    public = _public_scenarios(dataset)
    candidates = paired.get("behaviorDifferenceCandidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("no judgeable behavior difference candidates")

    reviewer_one: list[dict[str, Any]] = []
    reviewer_two: list[dict[str, Any]] = []
    sealed: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for ordinal, candidate in enumerate(candidates, 1):
        if not isinstance(candidate, dict):
            raise ValueError("invalid behavior difference candidate")
        key = (candidate.get("scenarioId"), candidate.get("turnIndex"))
        if type(key[0]) is not str or type(key[1]) is not int or key in seen:
            raise ValueError("invalid or duplicate behavior difference identity")
        seen.add(key)
        fixed_row, react_row = fixed.get(key), react.get(key)
        if fixed_row is None or react_row is None or fixed_row["answer"] == react_row["answer"]:
            raise ValueError(f"difference candidate is not an observed answer difference: {key}")
        if _hash_text(fixed_row["answer"]) != candidate.get("fixedAnswerSha256"):
            raise ValueError(f"fixed answer hash mismatch: {key}")
        if _hash_text(react_row["answer"]) != candidate.get("reactAnswerSha256"):
            raise ValueError(f"react answer hash mismatch: {key}")
        scenario_turns = public.get(key[0])
        if scenario_turns is None:
            raise ValueError(f"public dataset is missing {key[0]}")
        fixed_dialogue = _dialogue(
            scenario_id=key[0], through=key[1], turns=scenario_turns, receipts=fixed
        )
        react_dialogue = _dialogue(
            scenario_id=key[0], through=key[1], turns=scenario_turns, receipts=react
        )
        item_id = f"blind-{ordinal:02d}"
        swap = hashlib.sha256(f"{seed}:{item_id}".encode()).digest()[0] & 1
        if swap:
            one_dialogues = (react_dialogue, fixed_dialogue)
            labels = {"A": "react_v1", "B": "fixed_v1"}
        else:
            one_dialogues = (fixed_dialogue, react_dialogue)
            labels = {"A": "fixed_v1", "B": "react_v1"}
        reviewer_one.append(_blind_row(
            item_id=item_id, dialogue_a=one_dialogues[0], dialogue_b=one_dialogues[1]
        ))
        reviewer_two.append(_blind_row(
            item_id=item_id, dialogue_a=one_dialogues[1], dialogue_b=one_dialogues[0]
        ))
        sealed.append({
            "itemId": item_id,
            "source": {"scenarioId": key[0], "turnIndex": key[1]},
            "reviewer01": labels,
            "reviewer02": {"A": labels["B"], "B": labels["A"]},
            "fixedRequestId": fixed_row.get("requestId"),
            "reactRequestId": react_row.get("requestId"),
        })

    reviewer_one_path = output_dir / "reviewer01.jsonl"
    reviewer_two_path = output_dir / "reviewer02.jsonl"
    sealed_path = output_dir / "sealed" / "mapping.json"
    instructions_path = output_dir / "HUMAN_REVIEW_INSTRUCTIONS.md"
    manifest_path = output_dir / "manifest.json"
    _write_jsonl(reviewer_one_path, reviewer_one)
    _write_jsonl(reviewer_two_path, reviewer_two)
    _write_json(sealed_path, {
        "schemaVersion": "react-v1-architecture-24-blind-mapping-v1",
        "seed": seed,
        "items": sealed,
    })
    instructions_path.write_text(
        "# 独立盲审说明\n\n"
        "两名审稿人分别只读取 reviewer01.jsonl 或 reviewer02.jsonl，评分前不得读取 sealed/。\n\n"
        "逐项填写四个 1-5 分维度、overallPreference（A/B/tie/unjudgeable）及简短理由。"
        "完成后分别保存结果，并确认二人独立完成且评分前未查看映射。\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = {
        "schemaVersion": "react-v1-architecture-24-blind-manifest-v1",
        "status": "AWAITING_TWO_HUMAN_BLIND_REVIEWS",
        "answerQualityStatus": "HOLD_PENDING_TWO_HUMAN_BLIND_REVIEWS",
        "minimumReviewers": 2,
        "reviewerIndependenceMustBeConfirmedByHumans": True,
        "reviewerMustNotReadSealedDirectory": True,
        "mirroredAssignment": True,
        "itemCountPerReviewer": len(reviewer_one),
        "inputs": {
            "pairedResultSha256": _sha256(paired_path),
            "runBindingFileSha256": _sha256(binding_path),
            "runBindingLogicalSha256": declared_binding_hash,
            "fixedManifestSha256": _sha256(fixed_manifest_path),
            "reactManifestSha256": _sha256(react_manifest_path),
            "fixedReceiptsSha256": _sha256(fixed_receipts_path),
            "reactReceiptsSha256": _sha256(react_receipts_path),
            "datasetSha256": _sha256(dataset),
        },
        "outputs": {
            "reviewer01Sha256": _sha256(reviewer_one_path),
            "reviewer02Sha256": _sha256(reviewer_two_path),
            "instructionsSha256": _sha256(instructions_path),
            "sealedMappingSha256": _sha256(sealed_path),
            "seedSha256": hashlib.sha256(seed.encode()).hexdigest(),
        },
        "generatorSha256": _sha256(Path(__file__).resolve()),
    }
    _write_json(manifest_path, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair-dir", required=True, type=Path)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args()
    print(json.dumps(build_pack(**vars(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
