"""Build mirrored packets for independent AI judges; never calls a model."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parent
DATASET = PACKAGE / "conversations.jsonl"
ARMS = ("RAW_FULL_CONTROL", "CONTEXT_TREATMENT")
SEED = 20260902


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_packets(
    outputs: list[dict[str, Any]],
    conversations: list[dict[str, Any]],
    seed: int = SEED,
) -> dict[str, Any]:
    conversation_map = {row["conversationId"]: row for row in conversations}
    pairs: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in outputs:
        if row.get("schemaVersion") != "real-user-multiturn-paired-output-v1":
            raise ValueError("unexpected paired output schema")
        if row.get("arm") not in ARMS:
            raise ValueError("unexpected arm")
        if row.get("status") != "SUCCEEDED":
            raise ValueError("blind packets require successful paired outputs")
        if int(row.get("semanticTurn", 0)) < 2:
            continue
        answer = row.get("finalAnswer")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("finalAnswer must be non-empty")
        if row.get("finalAnswerSha256") != sha_text(answer):
            raise ValueError("finalAnswerSha256 mismatch")
        key = (row["conversationId"], row["turnId"])
        arm_rows = pairs.setdefault(key, {})
        if row["arm"] in arm_rows:
            raise ValueError(f"duplicate arm output for {key}")
        arm_rows[row["arm"]] = row

    expected = sum(len(row["turns"]) - 1 for row in conversations)
    if len(pairs) != expected or any(set(pair) != set(ARMS) for pair in pairs.values()):
        raise ValueError("paired followup set is incomplete")

    rng = random.Random(seed)
    judge01: list[dict[str, Any]] = []
    judge02: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []
    for ordinal, key in enumerate(sorted(pairs), 1):
        conversation_id, turn_id = key
        conversation = conversation_map[conversation_id]
        turn_index = next(i for i, turn in enumerate(conversation["turns"]) if turn["turnId"] == turn_id)
        if turn_index == 0:
            raise ValueError("first turns are warmups and cannot be judged")
        pair = pairs[key]
        expected_users = [item["rawUserText"] for item in conversation["turns"][: turn_index + 1]]
        for arm in ARMS:
            dialogue = pair[arm].get("dialogue")
            if not isinstance(dialogue, list) or len(dialogue) != len(expected_users) * 2:
                raise ValueError("dialogue must contain the complete same-arm user/assistant history")
            if [item.get("role") for item in dialogue] != [role for _ in expected_users for role in ("user", "assistant")]:
                raise ValueError("dialogue roles must alternate user then assistant")
            if [dialogue[index * 2].get("content") for index in range(len(expected_users))] != expected_users:
                raise ValueError("dialogue user text differs from the frozen real-user sequence")
            if dialogue[-1].get("content") != pair[arm]["finalAnswer"]:
                raise ValueError("finalAnswer must equal the final assistant dialogue message")
            if pair[arm].get("dialogueSha256") != sha_text(canonical(dialogue)):
                raise ValueError("dialogueSha256 mismatch")
        first = ARMS[rng.randrange(2)]
        second = ARMS[1] if first == ARMS[0] else ARMS[0]
        common = {
            "schemaVersion": "real-user-multiturn-ai-blind-item-v1",
            "itemId": f"rumr-blind-{ordinal:02d}",
            "conversationId": conversation_id,
            "turnId": turn_id,
            "priorUserTurns": [
                {"semanticTurn": item["semanticTurn"], "rawUserText": item["rawUserText"]}
                for item in conversation["turns"][:turn_index]
            ],
            "currentUserText": conversation["turns"][turn_index]["rawUserText"],
            "evidenceBoundary": "Within each candidate, only its publicEvidence is established product evidence; listing claims and missing fields remain unverified.",
        }
        candidate = lambda arm: {
            "dialogue": pair[arm]["dialogue"],
            "publicEvidence": pair[arm].get("publicEvidence", []),
            "currentAnswer": pair[arm]["finalAnswer"],
        }
        judge01.append({**common, "candidateA": candidate(first), "candidateB": candidate(second)})
        judge02.append({**common, "candidateA": candidate(second), "candidateB": candidate(first)})
        mapping.append(
            {
                "itemId": common["itemId"],
                "conversationId": conversation_id,
                "turnId": turn_id,
                "judge01": {"A": first, "B": second},
                "judge02": {"A": second, "B": first},
                "answerSha256": {arm: pair[arm]["finalAnswerSha256"] for arm in ARMS},
                "dialogueSha256": {arm: pair[arm]["dialogueSha256"] for arm in ARMS},
            }
        )
    return {"judge01": judge01, "judge02": judge02, "mapping": mapping}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(canonical(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paired-outputs", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError(f"refusing to overwrite {args.output}")
    built = build_packets(load_jsonl(args.paired_outputs), load_jsonl(DATASET), args.seed)
    args.output.mkdir(parents=True, exist_ok=False)
    judge01 = args.output / "judge01.jsonl"
    judge02 = args.output / "judge02.jsonl"
    sealed = args.output / "SEALED_DO_NOT_SHARE.json"
    write_jsonl(judge01, built["judge01"])
    write_jsonl(judge02, built["judge02"])
    sealed.write_text(
        json.dumps(
            {
                "schemaVersion": "real-user-multiturn-ai-blind-mapping-v1",
                "seed": args.seed,
                "items": built["mapping"],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    receipt = {
        "schemaVersion": "real-user-multiturn-ai-blind-package-receipt-v1",
        "status": "HOLD_PENDING_TWO_INDEPENDENT_AI_JUDGES",
        "reviewerType": "independent_ai_judge",
        "humanReviewClaimAllowed": False,
        "itemCountPerJudge": len(built["judge01"]),
        "seed": args.seed,
        "pairedOutputsSha256": hashlib.sha256(args.paired_outputs.read_bytes()).hexdigest(),
        "datasetSha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "judge01Sha256": hashlib.sha256(judge01.read_bytes()).hexdigest(),
        "judge02Sha256": hashlib.sha256(judge02.read_bytes()).hexdigest(),
        "sealedMappingSha256": hashlib.sha256(sealed.read_bytes()).hexdigest(),
        "formalJudgeRunExecuted": False,
    }
    (args.output / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
