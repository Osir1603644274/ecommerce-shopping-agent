"""Build mirrored, label-blind review packets for the TaskState ContextPack V4 pair."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


DIMENSIONS = (
    "constraintFidelity",
    "evidenceDiscipline",
    "taskProgression",
    "usefulness",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected object")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected object")
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


def _receipt_map(path: Path) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _read_jsonl(path):
        scenario_id = row.get("scenarioId")
        turn_id = row.get("turnId")
        answer = row.get("answer")
        if not all(type(value) is str for value in (scenario_id, turn_id, answer)):
            raise ValueError(f"{path}: receipt is missing scenarioId, turnId, or answer")
        key = (scenario_id, turn_id)
        if key in result:
            raise ValueError(f"{path}: duplicate receipt {key}")
        result[key] = row
    return result


def _public_turns(dataset: Path) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for row in _read_jsonl(dataset):
        scenario_id = row.get("scenarioId")
        turns = row.get("turns")
        if type(scenario_id) is not str or not isinstance(turns, list):
            raise ValueError(f"{dataset}: invalid scenario")
        normalized: list[dict[str, str]] = []
        for turn in turns:
            if (
                not isinstance(turn, dict)
                or type(turn.get("turnId")) is not str
                or type(turn.get("text")) is not str
            ):
                raise ValueError(f"{scenario_id}: invalid public turn")
            normalized.append({"turnId": turn["turnId"], "text": turn["text"]})
        result[scenario_id] = normalized
    return result


def _history_through(
    turns: list[dict[str, str]], target_turn_id: str
) -> list[dict[str, str]]:
    history: list[dict[str, str]] = []
    for turn in turns:
        history.append(turn)
        if turn["turnId"] == target_turn_id:
            return history
    raise ValueError(f"public dataset is missing turn {target_turn_id}")


def _blind_row(
    *, item_id: str, history: list[dict[str, str]], answer_a: str, answer_b: str
) -> dict[str, Any]:
    return {
        "schemaVersion": "shopping-task-state-context-blind-item-v1",
        "itemId": item_id,
        "userTurns": history,
        "candidateA": answer_a,
        "candidateB": answer_b,
        "rubric": {
            "constraintFidelity": "1-5; follows the user's active hard constraints",
            "evidenceDiscipline": "1-5; does not present unknown claims as verified",
            "taskProgression": "1-5; safely answers, advances, or clarifies the request",
            "usefulness": "1-5; relevant and actionable for the shown conversation",
            "overallPreference": "A, B, tie, or unjudgeable",
        },
        "review": None,
    }


def build_pack(
    *,
    dataset: Path,
    control_receipts: Path,
    treatment_receipts: Path,
    live_score: Path,
    live_manifest: Path,
    reviewer_one_output: Path,
    reviewer_two_output: Path,
    sealed_mapping_output: Path,
    manifest_output: Path,
    seed: str,
) -> dict[str, Any]:
    score = _read_json(live_score)
    source_manifest = _read_json(live_manifest)
    if (
        score.get("verdict") != "READY_FOR_BLIND_REVIEW"
        or score.get("liveSafetyPassed") is not True
        or score.get("treatmentRegressions") != []
        or score.get("commonActionFailures") != []
    ):
        raise ValueError("live paired gate is not safe for blind review")
    expected_hashes = {
        control_receipts: source_manifest.get("controlReceiptsSha256"),
        treatment_receipts: source_manifest.get("treatmentReceiptsSha256"),
        live_score: source_manifest.get("scoreSha256"),
    }
    for path, expected in expected_hashes.items():
        if type(expected) is not str or _sha256(path) != expected:
            raise ValueError(f"input hash mismatch: {path}")

    control = _receipt_map(control_receipts)
    treatment = _receipt_map(treatment_receipts)
    if not control or set(control) != set(treatment):
        raise ValueError("paired receipts must contain the same non-empty turn set")
    differences = [key for key in control if control[key]["answer"] != treatment[key]["answer"]]
    if len(differences) != score.get("userVisibleDifferenceTurns"):
        raise ValueError("observed answer differences do not match the frozen live score")
    if not differences:
        raise ValueError("blind review is not allowed without a user-visible difference")

    turns_by_scenario = _public_turns(dataset)
    reviewer_one: list[dict[str, Any]] = []
    reviewer_two: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    for index, (scenario_id, turn_id) in enumerate(differences, 1):
        if scenario_id not in turns_by_scenario:
            raise ValueError(f"dataset is missing scenario {scenario_id}")
        item_id = f"blind-{index:02d}"
        history = _history_through(turns_by_scenario[scenario_id], turn_id)
        control_answer = control[(scenario_id, turn_id)]["answer"]
        treatment_answer = treatment[(scenario_id, turn_id)]["answer"]
        swap = int.from_bytes(
            hashlib.sha256(f"{seed}:{item_id}".encode()).digest()[:8], "big"
        ) % 2 == 1
        if swap:
            one_answers = (treatment_answer, control_answer)
            one_labels = {"A": "treatment", "B": "control"}
        else:
            one_answers = (control_answer, treatment_answer)
            one_labels = {"A": "control", "B": "treatment"}
        reviewer_one.append(
            _blind_row(
                item_id=item_id,
                history=history,
                answer_a=one_answers[0],
                answer_b=one_answers[1],
            )
        )
        reviewer_two.append(
            _blind_row(
                item_id=item_id,
                history=history,
                answer_a=one_answers[1],
                answer_b=one_answers[0],
            )
        )
        mappings.append({
            "itemId": item_id,
            "source": {"scenarioId": scenario_id, "turnId": turn_id},
            "reviewer01": one_labels,
            "reviewer02": {"A": one_labels["B"], "B": one_labels["A"]},
            "controlRequestId": control[(scenario_id, turn_id)].get("requestId"),
            "treatmentRequestId": treatment[(scenario_id, turn_id)].get("requestId"),
        })

    _write_jsonl(reviewer_one_output, reviewer_one)
    _write_jsonl(reviewer_two_output, reviewer_two)
    _write_json(sealed_mapping_output, {
        "schemaVersion": "shopping-task-state-context-blind-mapping-v1",
        "seed": seed,
        "items": mappings,
    })
    manifest = {
        "schemaVersion": "shopping-task-state-context-blind-manifest-v1",
        "experimentId": score.get("experimentId"),
        "generatorSha256": _sha256(Path(__file__).resolve()),
        "status": "AWAITING_TWO_HUMAN_BLIND_REVIEWS",
        "minimumReviewers": 2,
        "reviewerIndependenceMustBeConfirmedByHumans": True,
        "itemCountPerReviewer": len(differences),
        "mirroredAssignment": True,
        "reviewerMustNotReadSealedDirectory": True,
        "answerQualityStatus": "HOLD_PENDING_TWO_HUMAN_BLIND_REVIEWS",
        "inputs": {
            "datasetSha256": _sha256(dataset),
            "controlReceiptsSha256": _sha256(control_receipts),
            "treatmentReceiptsSha256": _sha256(treatment_receipts),
            "liveScoreSha256": _sha256(live_score),
            "liveManifestSha256": _sha256(live_manifest),
        },
        "outputs": {
            "reviewer01Sha256": _sha256(reviewer_one_output),
            "reviewer02Sha256": _sha256(reviewer_two_output),
            "sealedMappingSha256": _sha256(sealed_mapping_output),
            "seedSha256": hashlib.sha256(seed.encode()).hexdigest(),
        },
    }
    _write_json(manifest_output, manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--control-receipts", required=True, type=Path)
    parser.add_argument("--treatment-receipts", required=True, type=Path)
    parser.add_argument("--live-score", required=True, type=Path)
    parser.add_argument("--live-manifest", required=True, type=Path)
    parser.add_argument("--reviewer-one-output", required=True, type=Path)
    parser.add_argument("--reviewer-two-output", required=True, type=Path)
    parser.add_argument("--sealed-mapping-output", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    parser.add_argument("--seed", required=True)
    args = parser.parse_args()
    print(json.dumps(build_pack(**vars(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
