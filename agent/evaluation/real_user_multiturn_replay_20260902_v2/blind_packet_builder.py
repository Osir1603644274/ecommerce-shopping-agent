"""Build mirrored packets for independent AI judges; never calls a model."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


PACKAGE = Path(__file__).resolve().parent
DATASET = PACKAGE / "conversations.jsonl"
PAIRED_SCHEMA = PACKAGE / "paired_output.schema.json"
ARMS = ("RAW_FULL_CONTROL", "CONTEXT_TREATMENT")
PACKAGE_ID = "real_user_multiturn_replay_20260902_v2"
SEED = 20260902
FORBIDDEN_ARM_PHRASES = (
    "raw full control",
    "context treatment",
    "raw full same arm",
    "compiled context no raw history",
    "raw full",
    "compiled context",
    "control arm",
    "treatment arm",
    "raw arm",
    "compiled arm",
)
FORBIDDEN_VISIBLE_KEYS = {
    "arm",
    "arm label",
    "arm name",
    "candidate arm",
    "source arm",
    "control arm",
    "treatment arm",
    "run id",
    "task id",
    "session id",
    "branch point state hash",
    "execution ordinal",
    "execution binding",
    "execution identity",
    "schedule row",
    "schedule row sha256",
    "session alias",
    "history mode",
    "package id",
    "arm state binding",
    "arm state binding sha256",
    "execution binding sha256",
    "trace sha256",
}


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{number}: expected object")
        rows.append(value)
    return rows


def _load_schedule_builder():
    path = PACKAGE / "build_replay_schedule.py"
    spec = importlib.util.spec_from_file_location("rumr_v2_schedule_builder", path)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load schedule builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _reject_visible_arm_leakage(value: Any, forbidden_identities: set[str], path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = _normalize_label(str(key))
            if normalized_key in FORBIDDEN_VISIBLE_KEYS:
                raise ValueError(f"arm or execution label leaked at {path}.{key}")
            _reject_visible_arm_leakage(child, forbidden_identities, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_visible_arm_leakage(child, forbidden_identities, f"{path}[{index}]")
        return
    if isinstance(value, str):
        normalized = _normalize_label(value)
        if any(phrase in normalized for phrase in FORBIDDEN_ARM_PHRASES):
            raise ValueError(f"arm label leaked at {path}")
        if any(identity and identity in value for identity in forbidden_identities):
            raise ValueError(f"execution identity leaked at {path}")


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(PAIRED_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _arm_state_payload(row: dict[str, Any]) -> dict[str, str]:
    binding = row["executionBinding"]
    return {
        "packageId": PACKAGE_ID,
        "conversationId": row["conversationId"],
        "arm": row["arm"],
        "runId": binding["runId"],
        "taskId": binding["taskId"],
        "sessionId": binding["sessionId"],
        "branchPointStateHash": binding["branchPointStateHash"],
    }


def validate_outputs(
    outputs: list[dict[str, Any]],
    conversations: list[dict[str, Any]],
    seed: int = SEED,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    validator = _schema_validator()
    conversation_map = {row["conversationId"]: row for row in conversations}
    if len(conversation_map) != len(conversations):
        raise ValueError("duplicate conversationId in frozen dataset")

    schedule_builder = _load_schedule_builder()
    schedule = schedule_builder.build_schedule(conversations, seed)
    schedule_map = {
        (row["conversationId"], row["turnId"], row["arm"]): row for row in schedule
    }
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    identity_owners: dict[tuple[str, str], tuple[str, str]] = {}
    configuration_values: dict[str, set[str]] = {
        field: set()
        for field in (
            "controlledWorldHash",
            "modelConfigurationHash",
            "toolConfigurationHash",
            "budgetConfigurationHash",
            "policyConfigurationHash",
        )
    }

    for number, row in enumerate(outputs, 1):
        errors = sorted(validator.iter_errors(row), key=lambda error: list(error.absolute_path))
        if errors:
            path = ".".join(str(part) for part in errors[0].absolute_path) or "$"
            raise ValueError(f"paired output {number} schema violation at {path}: {errors[0].message}")
        key = (row["conversationId"], row["turnId"], row["arm"])
        if key in indexed:
            raise ValueError(f"duplicate arm output for {key}")
        expected_schedule = schedule_map.get(key)
        if expected_schedule is None:
            raise ValueError(f"output does not belong to frozen schedule: {key}")
        conversation = conversation_map.get(row["conversationId"])
        if conversation is None:
            raise ValueError(f"unknown conversationId: {row['conversationId']}")
        turn = next((item for item in conversation["turns"] if item["turnId"] == row["turnId"]), None)
        if turn is None or row["semanticTurn"] != turn["semanticTurn"]:
            raise ValueError(f"turn binding mismatch: {key}")

        binding = row["executionBinding"]
        if binding["executionOrdinal"] != expected_schedule["executionOrdinal"]:
            raise ValueError(f"executionOrdinal mismatch: {key}")
        if binding["scheduleRowSha256"] != expected_schedule["scheduleRowSha256"]:
            raise ValueError(f"scheduleRowSha256 mismatch: {key}")
        if binding["inputMessageSha256"] != turn["messageSha256"]:
            raise ValueError(f"inputMessageSha256 mismatch: {key}")
        if row["executionBindingSha256"] != sha_text(canonical(binding)):
            raise ValueError(f"executionBindingSha256 mismatch: {key}")
        if binding["armStateBindingSha256"] != sha_text(canonical(_arm_state_payload(row))):
            raise ValueError(f"armStateBindingSha256 mismatch: {key}")
        if row["traceSha256"] != sha_text(canonical(row["trace"])):
            raise ValueError(f"traceSha256 mismatch: {key}")
        if row["trace"]["postStateRevision"] < row["trace"]["preStateRevision"]:
            raise ValueError(f"state revision regressed: {key}")
        if row["trace"]["totalTokens"] != row["trace"]["promptTokens"] + row["trace"]["completionTokens"]:
            raise ValueError(f"token total mismatch: {key}")
        for field, values in configuration_values.items():
            values.add(row["trace"][field])
        if row["dialogueSha256"] != sha_text(canonical(row["dialogue"])):
            raise ValueError(f"dialogueSha256 mismatch: {key}")
        if row["finalAnswerSha256"] != sha_text(row["finalAnswer"]):
            raise ValueError(f"finalAnswerSha256 mismatch: {key}")
        indexed[key] = row

        owner = (row["conversationId"], row["arm"])
        for field in ("runId", "taskId", "sessionId"):
            identity_key = (field, binding[field])
            prior_owner = identity_owners.setdefault(identity_key, owner)
            if prior_owner != owner:
                raise ValueError(f"cross-arm or cross-session {field} reuse")

    if set(indexed) != set(schedule_map):
        missing = sorted(set(schedule_map) - set(indexed))
        extra = sorted(set(indexed) - set(schedule_map))
        raise ValueError(f"complete 54-row paired output set required; missing={missing[:2]} extra={extra[:2]}")
    for field, values in configuration_values.items():
        if len(values) != 1:
            raise ValueError(f"paired execution does not share one frozen {field}")

    for conversation in conversations:
        branch_hashes = {
            indexed[(conversation["conversationId"], conversation["turns"][0]["turnId"], arm)]["executionBinding"]["branchPointStateHash"]
            for arm in ARMS
        }
        if len(branch_hashes) != 1:
            raise ValueError("paired arms do not share one branchPointStateHash")

    forbidden_identities = {
        value
        for row in outputs
        for value in (
            row["executionBinding"]["runId"],
            row["executionBinding"]["taskId"],
            row["executionBinding"]["sessionId"],
            row["executionBinding"]["branchPointStateHash"],
            row["executionBinding"]["scheduleRowSha256"],
            row["executionBinding"]["armStateBindingSha256"],
            row["executionBindingSha256"],
            row["trace"]["controlledWorldHash"],
            row["trace"]["modelConfigurationHash"],
            row["trace"]["toolConfigurationHash"],
            row["trace"]["budgetConfigurationHash"],
            row["trace"]["policyConfigurationHash"],
        )
        if len(value) >= 6
    }

    for conversation in conversations:
        for arm in ARMS:
            rows = [indexed[(conversation["conversationId"], turn["turnId"], arm)] for turn in conversation["turns"]]
            first_binding = rows[0]["executionBinding"]
            fixed_identity = (first_binding["runId"], first_binding["taskId"], first_binding["sessionId"])
            fixed_branch_point = first_binding["branchPointStateHash"]
            previous_post_revision: int | None = None
            for turn_index, row in enumerate(rows):
                binding = row["executionBinding"]
                if (binding["runId"], binding["taskId"], binding["sessionId"]) != fixed_identity:
                    raise ValueError("execution identity changed within a conversation arm")
                if binding["branchPointStateHash"] != fixed_branch_point:
                    raise ValueError("branchPointStateHash changed within a conversation arm")
                if previous_post_revision is not None and row["trace"]["preStateRevision"] != previous_post_revision:
                    raise ValueError("same-arm state revision chain is discontinuous")
                previous_post_revision = row["trace"]["postStateRevision"]
                if row["status"] != "SUCCEEDED":
                    raise ValueError("blind packets require successful outputs for every warmup and scored turn")

                frozen_turns = conversation["turns"][: turn_index + 1]
                dialogue = row["dialogue"]
                if len(dialogue) != len(frozen_turns) * 2:
                    raise ValueError("dialogue must contain complete same-arm user/assistant history")
                expected_roles = [role for _ in frozen_turns for role in ("user", "assistant")]
                if [item["role"] for item in dialogue] != expected_roles:
                    raise ValueError("dialogue roles must alternate user then assistant")
                if [dialogue[index * 2]["content"] for index in range(len(frozen_turns))] != [item["rawUserText"] for item in frozen_turns]:
                    raise ValueError("dialogue user text differs from the frozen real-user sequence")
                for history_index, history_turn in enumerate(frozen_turns):
                    expected_answer = indexed[(conversation["conversationId"], history_turn["turnId"], arm)]["finalAnswer"]
                    if dialogue[history_index * 2 + 1]["content"] != expected_answer:
                        raise ValueError("assistant history does not match the bound same-arm prior output")
                if dialogue[-1]["content"] != row["finalAnswer"]:
                    raise ValueError("finalAnswer must equal the final assistant dialogue message")
                _reject_visible_arm_leakage(
                    {
                        "dialogue": dialogue,
                        "publicEvidence": row["publicEvidence"],
                        "currentAnswer": row["finalAnswer"],
                    },
                    forbidden_identities,
                )
    return indexed


def build_packets(
    outputs: list[dict[str, Any]],
    conversations: list[dict[str, Any]],
    seed: int = SEED,
) -> dict[str, Any]:
    indexed = validate_outputs(outputs, conversations, seed)
    rng = random.Random(seed)
    judge01: list[dict[str, Any]] = []
    judge02: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []
    ordinal = 0
    for conversation in conversations:
        for turn_index, turn in enumerate(conversation["turns"]):
            if turn_index == 0:
                continue
            ordinal += 1
            pair = {
                arm: indexed[(conversation["conversationId"], turn["turnId"], arm)]
                for arm in ARMS
            }
            first = ARMS[rng.randrange(2)]
            second = ARMS[1] if first == ARMS[0] else ARMS[0]
            common = {
                "schemaVersion": "real-user-multiturn-ai-blind-item-v2",
                "itemId": f"rumr-blind-{ordinal:02d}",
                "conversationId": conversation["conversationId"],
                "turnId": turn["turnId"],
                "priorUserTurns": [
                    {"semanticTurn": item["semanticTurn"], "rawUserText": item["rawUserText"]}
                    for item in conversation["turns"][:turn_index]
                ],
                "currentUserText": turn["rawUserText"],
                "evidenceBoundary": "Within each candidate, only its publicEvidence is established product evidence; listing claims and missing fields remain unverified.",
            }

            def candidate(arm: str) -> dict[str, Any]:
                return {
                    "dialogue": pair[arm]["dialogue"],
                    "publicEvidence": pair[arm]["publicEvidence"],
                    "currentAnswer": pair[arm]["finalAnswer"],
                }

            judge01.append({**common, "candidateA": candidate(first), "candidateB": candidate(second)})
            judge02.append({**common, "candidateA": candidate(second), "candidateB": candidate(first)})
            mapping.append(
                {
                    "itemId": common["itemId"],
                    "conversationId": conversation["conversationId"],
                    "turnId": turn["turnId"],
                    "judge01": {"A": first, "B": second},
                    "judge02": {"A": second, "B": first},
                    "answerSha256": {arm: pair[arm]["finalAnswerSha256"] for arm in ARMS},
                    "dialogueSha256": {arm: pair[arm]["dialogueSha256"] for arm in ARMS},
                    "executionBindingSha256": {arm: pair[arm]["executionBindingSha256"] for arm in ARMS},
                    "traceSha256": {arm: pair[arm]["traceSha256"] for arm in ARMS},
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
                "schemaVersion": "real-user-multiturn-ai-blind-mapping-v2",
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
        "schemaVersion": "real-user-multiturn-ai-blind-package-receipt-v2",
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
