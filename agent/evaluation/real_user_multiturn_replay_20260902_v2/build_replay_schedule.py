"""Build a deterministic paired schedule; this module never runs the SUT or a model."""

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


def load_conversations(path: Path = DATASET) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def schedule_binding(row: dict[str, Any]) -> str:
    payload = {key: value for key, value in row.items() if key != "scheduleRowSha256"}
    return sha_text(canonical(payload))


def build_schedule(conversations: list[dict[str, Any]], seed: int = SEED) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    schedule: list[dict[str, Any]] = []
    ordinal = 0
    for conversation in conversations:
        for turn in conversation["turns"]:
            order = list(ARMS)
            rng.shuffle(order)
            for arm in order:
                ordinal += 1
                row = {
                    "schemaVersion": "real-user-multiturn-replay-schedule-row-v2",
                    "executionOrdinal": ordinal,
                    "conversationId": conversation["conversationId"],
                    "turnId": turn["turnId"],
                    "semanticTurn": turn["semanticTurn"],
                    "arm": arm,
                    "rawUserText": turn["rawUserText"],
                    "inputMessageSha256": turn["messageSha256"],
                    "scoredFollowup": turn["semanticTurn"] > 1,
                    "sessionAlias": f"{conversation['conversationId']}--{arm.lower()}",
                    "historyMode": "raw_full_same_arm" if arm == "RAW_FULL_CONTROL" else "compiled_context_no_raw_history",
                    "automaticRetries": 0,
                }
                row["scheduleRowSha256"] = schedule_binding(row)
                schedule.append(row)
    return schedule


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = build_schedule(load_conversations(args.dataset), args.seed)
    payload = "".join(canonical(row) + "\n" for row in rows)
    if args.output:
        if args.output.exists():
            raise RuntimeError(f"refusing to overwrite {args.output}")
        args.output.write_text(payload, encoding="utf-8", newline="\n")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
