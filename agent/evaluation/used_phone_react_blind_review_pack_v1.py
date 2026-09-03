"""Build a leakage-minimized A/B review pack from paired ReAct/fixed receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


TARGETS = ("UPHB-V1-015", "UPHB-V1-022", "UPHB-V1-024")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _scenario_turns(
    dataset: Path,
    target_scenarios: tuple[str, ...],
) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {}
    for row in _read_jsonl(dataset):
        scenario_id = row.get("scenarioId")
        if scenario_id not in target_scenarios:
            continue
        turns = row.get("turns")
        if not isinstance(turns, list) or any(
            not isinstance(turn, dict)
            or type(turn.get("turnId")) is not str
            or type(turn.get("text")) is not str
            for turn in turns
        ):
            raise ValueError(f"{scenario_id}: invalid public turns")
        result[scenario_id] = [
            {"turnId": turn["turnId"], "text": turn["text"]} for turn in turns
        ]
    if set(result) != set(target_scenarios):
        raise ValueError("dataset does not contain the exact blind-review targets")
    return result


def _receipt_map(
    path: Path,
    target_scenarios: tuple[str, ...],
) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in _read_jsonl(path):
        scenario_id, turn_id = row.get("scenarioId"), row.get("turnId")
        if scenario_id not in target_scenarios:
            continue
        if type(turn_id) is not str or type(row.get("answer")) is not str:
            raise ValueError(f"{path}: missing turnId or answer")
        key = (scenario_id, turn_id)
        if key in result:
            raise ValueError(f"{path}: duplicate receipt {key}")
        result[key] = row
    return result


def _transcript(
    turns: list[dict[str, str]], receipts: dict[tuple[str, str], dict[str, Any]],
    scenario_id: str,
) -> list[dict[str, str]]:
    transcript: list[dict[str, str]] = []
    for turn in turns:
        key = (scenario_id, turn["turnId"])
        if key not in receipts:
            raise ValueError(f"missing receipt {key}")
        transcript.extend((
            {"role": "user", "content": turn["text"]},
            {"role": "assistant", "content": receipts[key]["answer"]},
        ))
    return transcript


def build_pack(
    *,
    dataset: Path,
    react_paths: list[Path],
    fixed_paths: list[Path],
    public_output: Path,
    mapping_output: Path,
    manifest_output: Path,
    seed: str,
    target_scenarios: tuple[str, ...] = TARGETS,
) -> dict[str, Any]:
    if not react_paths or len(react_paths) != len(fixed_paths):
        raise ValueError("react/fixed receipt counts must be equal and non-zero")
    if not target_scenarios or len(set(target_scenarios)) != len(target_scenarios):
        raise ValueError("target scenarios must be unique and non-empty")
    turns_by_scenario = _scenario_turns(dataset, target_scenarios)
    public_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []

    for pair_index, (react_path, fixed_path) in enumerate(
        zip(react_paths, fixed_paths, strict=True), 1
    ):
        react = _receipt_map(react_path, target_scenarios)
        fixed = _receipt_map(fixed_path, target_scenarios)
        expected = {
            (scenario_id, turn["turnId"])
            for scenario_id, turns in turns_by_scenario.items()
            for turn in turns
        }
        if set(react) != expected or set(fixed) != expected:
            raise ValueError(f"pair {pair_index}: receipts do not match target turns")
        for scenario_id in target_scenarios:
            item_id = f"blind-{pair_index:02d}-{scenario_id.lower()}"
            react_transcript = _transcript(
                turns_by_scenario[scenario_id], react, scenario_id
            )
            fixed_transcript = _transcript(
                turns_by_scenario[scenario_id], fixed, scenario_id
            )
            swap = int.from_bytes(
                hashlib.sha256(f"{seed}:{item_id}".encode()).digest()[:8], "big"
            ) % 2 == 1
            candidates = (
                {"A": fixed_transcript, "B": react_transcript}
                if swap else
                {"A": react_transcript, "B": fixed_transcript}
            )
            labels = (
                {"A": "fixed_v1", "B": "react_v0"}
                if swap else
                {"A": "react_v0", "B": "fixed_v1"}
            )
            public_rows.append({
                "schemaVersion": "used-phone-blind-review-item-v1",
                "itemId": item_id,
                "scenarioId": scenario_id,
                "candidateA": candidates["A"],
                "candidateB": candidates["B"],
                "rubric": {
                    "constraintFidelity": "1-5; hard constraints are never silently relaxed",
                    "evidenceDiscipline": "1-5; unknown evidence is not presented as known",
                    "taskProgression": "1-5; each turn safely advances or clarifies the task",
                    "usefulness": "1-5; response is relevant and actionable within evidence",
                    "overallPreference": "A, B, tie, or unjudgeable",
                },
                "review": None,
            })
            mapping_rows.append({
                "itemId": item_id,
                "labels": labels,
                "reactReceiptSha256": _sha256(react_path),
                "fixedReceiptSha256": _sha256(fixed_path),
                "reactRequestIds": [
                    react[(scenario_id, turn["turnId"])].get("requestId")
                    for turn in turns_by_scenario[scenario_id]
                ],
                "fixedRequestIds": [
                    fixed[(scenario_id, turn["turnId"])].get("requestId")
                    for turn in turns_by_scenario[scenario_id]
                ],
            })

    public_output.parent.mkdir(parents=True, exist_ok=True)
    mapping_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    public_output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in public_rows),
        encoding="utf-8",
    )
    mapping_output.write_text(
        json.dumps({
            "schemaVersion": "used-phone-blind-review-mapping-v1",
            "items": mapping_rows,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schemaVersion": "used-phone-blind-review-manifest-v1",
        "status": "READY_FOR_INDEPENDENT_REVIEW",
        "itemCount": len(public_rows),
        "pairedRunCount": len(react_paths),
        "scenarioIds": list(target_scenarios),
        "datasetSha256": _sha256(dataset),
        "publicPack": str(public_output),
        "publicPackSha256": _sha256(public_output),
        "sealedMapping": str(mapping_output),
        "sealedMappingSha256": _sha256(mapping_output),
        "seedSha256": hashlib.sha256(seed.encode()).hexdigest(),
        "reviewerMustNotReadSealedMapping": True,
        "answerQualityStatus": "HOLD_PENDING_INDEPENDENT_REVIEW",
    }
    manifest_output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--react-receipts", action="append", required=True, type=Path)
    parser.add_argument("--fixed-receipts", action="append", required=True, type=Path)
    parser.add_argument("--public-output", required=True, type=Path)
    parser.add_argument("--mapping-output", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--scenario-id", action="append")
    args = parser.parse_args()
    manifest = build_pack(
        dataset=args.dataset,
        react_paths=args.react_receipts,
        fixed_paths=args.fixed_receipts,
        public_output=args.public_output,
        mapping_output=args.mapping_output,
        manifest_output=args.manifest_output,
        seed=args.seed,
        target_scenarios=tuple(args.scenario_id or TARGETS),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
