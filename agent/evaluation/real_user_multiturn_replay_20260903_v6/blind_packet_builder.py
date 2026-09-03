"""Build mirrored packets for independent AI judges; never calls a model."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import re
import unicodedata
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


PACKAGE = Path(__file__).resolve().parent
DATASET = PACKAGE / "conversations.jsonl"
PAIRED_SCHEMA = PACKAGE / "paired_output.schema.json"
EXECUTION_AUTHORITY = PACKAGE / "execution_authority.json"
EXECUTION_SNAPSHOT = PACKAGE / "execution_config_snapshot.json"
EXECUTION_RECEIPT_SCHEMA = PACKAGE / "execution_receipt.schema.json"
ROOT = PACKAGE.parents[2]
ARMS = ("RAW_FULL_CONTROL", "CONTEXT_TREATMENT")
PACKAGE_ID = "real_user_multiturn_replay_20260903_v6"
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
    "status",
    "failure code",
    "dialogue sha256",
    "final answer sha256",
    "trace",
    "trace sha256",
    "trace binding sha256",
    "input message sha256",
    "state revision",
    "pre state revision",
    "post state revision",
    "controlled world hash",
    "model configuration hash",
    "tool configuration hash",
    "budget configuration hash",
    "policy configuration hash",
    "context binding hash",
    "context binding",
    "reference context binding hash",
    "reference context binding",
    "reference binding",
    "candidate scope hash",
    "candidate scope",
    "tool calls",
    "model calls",
    "call id",
    "call type",
    "tool name",
    "usage",
    "prompt tokens",
    "completion tokens",
    "total tokens",
    "duration ms",
    "automatic retry count",
    "result status",
    "request sha256",
    "result sha256",
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
    spec = importlib.util.spec_from_file_location("rumr_v4_schedule_builder", path)
    if not spec or not spec.loader:
        raise RuntimeError("cannot load schedule builder")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalize_label(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in folded if character.isalnum())


def _reject_visible_arm_leakage(value: Any, forbidden_identities: set[str], path: str = "$") -> None:
    forbidden_labels = {_normalize_label(label) for label in FORBIDDEN_VISIBLE_KEYS}
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = _normalize_label(str(key))
            if any(label and label in normalized_key for label in forbidden_labels):
                raise ValueError(f"arm or execution label leaked at {path}.{key}")
            _reject_visible_arm_leakage(child, forbidden_identities, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_visible_arm_leakage(child, forbidden_identities, f"{path}[{index}]")
        return
    if isinstance(value, str):
        normalized = _normalize_label(value)
        if any(_normalize_label(phrase) in normalized for phrase in FORBIDDEN_ARM_PHRASES):
            raise ValueError(f"arm label leaked at {path}")
        if any(label and label in normalized for label in forbidden_labels):
            raise ValueError(f"execution or trace label leaked at {path}")
        folded_value = value.casefold()
        if any(identity and identity.casefold() in folded_value for identity in forbidden_identities):
            raise ValueError(f"execution or trace identifier leaked at {path}")


def _schema_validator() -> Draft202012Validator:
    schema = json.loads(PAIRED_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _execution_authority() -> tuple[dict[str, Any], dict[str, str]]:
    authority = json.loads(EXECUTION_AUTHORITY.read_text(encoding="utf-8"))
    snapshot = json.loads(EXECUTION_SNAPSHOT.read_text(encoding="utf-8"))
    if authority.get("packageId") != PACKAGE_ID or snapshot.get("packageId") != PACKAGE_ID:
        raise ValueError("execution authority packageId mismatch")
    receipt_contract = authority.get("formalRunnerReceiptContract", {})
    if receipt_contract.get("required") is not True or receipt_contract.get("schema") != EXECUTION_RECEIPT_SCHEMA.name:
        raise ValueError("runner-owned execution receipt is not required by execution authority")
    if authority["configSnapshot"]["path"] != EXECUTION_SNAPSHOT.name:
        raise ValueError("execution config snapshot path mismatch")
    if authority["configSnapshot"]["sha256"] != hashlib.sha256(EXECUTION_SNAPSHOT.read_bytes()).hexdigest():
        raise ValueError("execution config snapshot hash mismatch")
    field_to_component = {
        "controlledWorldHash": "controlledWorld",
        "modelConfigurationHash": "modelConfiguration",
        "toolConfigurationHash": "toolConfiguration",
        "budgetConfigurationHash": "budgetConfiguration",
        "policyConfigurationHash": "policyConfiguration",
    }
    expected = authority["expectedTraceHashes"]
    if set(expected) != set(field_to_component):
        raise ValueError("execution authority expectedTraceHashes is incomplete")
    for field, component_name in field_to_component.items():
        component = snapshot["components"][component_name]
        if expected[field] != sha_text(canonical(component)):
            raise ValueError(f"preregistered {field} does not bind its config snapshot")
        for source in component.get("sourceFiles", []):
            source_path = (ROOT / source["path"]).resolve()
            try:
                source_path.relative_to(ROOT.resolve())
            except ValueError as error:
                raise ValueError("execution snapshot source path escapes repository") from error
            if not source_path.is_file():
                raise ValueError(f"execution snapshot source missing: {source['path']}")
            if hashlib.sha256(source_path.read_bytes()).hexdigest() != source["sha256"]:
                raise ValueError(f"execution snapshot source hash mismatch: {source['path']}")
    return authority, expected


def _validate_runner_receipt(
    receipt: dict[str, Any],
    outputs: list[dict[str, Any]],
    authority: dict[str, Any],
    expected_hashes: dict[str, str],
) -> None:
    schema = json.loads(EXECUTION_RECEIPT_SCHEMA.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(receipt), key=lambda error: list(error.absolute_path))
    if errors:
        path = ".".join(str(part) for part in errors[0].absolute_path) or "$"
        raise ValueError(f"runner receipt schema violation at {path}: {errors[0].message}")
    canonical_outputs = "".join(canonical(row) + "\n" for row in outputs)
    if receipt["pairedOutputsCanonicalSha256"] != sha_text(canonical_outputs):
        raise ValueError("runner receipt paired output binding mismatch")
    if receipt["expectedTraceHashes"] != expected_hashes:
        raise ValueError("runner receipt expected trace hashes mismatch")
    if receipt["executionAuthoritySha256"] != hashlib.sha256(EXECUTION_AUTHORITY.read_bytes()).hexdigest():
        raise ValueError("runner receipt execution authority hash mismatch")
    if receipt["executionConfigSnapshotSha256"] != authority["configSnapshot"]["sha256"]:
        raise ValueError("runner receipt config snapshot hash mismatch")
    model_calls = sum(len(row["trace"]["modelCalls"]) for row in outputs)
    tool_calls = sum(len(row["trace"]["toolCalls"]) for row in outputs)
    if receipt["observedModelCallCount"] != model_calls or receipt["observedToolCallCount"] != tool_calls:
        raise ValueError("runner receipt observed call counts mismatch")
    payload = {key: value for key, value in receipt.items() if key != "receiptBindingSha256"}
    if receipt["receiptBindingSha256"] != sha_text(canonical(payload)):
        raise ValueError("runner receipt binding mismatch")


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


def _trace_binding_payload(row: dict[str, Any]) -> dict[str, Any]:
    binding = row["executionBinding"]
    return {
        "schemaVersion": "real-user-multiturn-trace-binding-v4",
        "packageId": PACKAGE_ID,
        "conversationId": row["conversationId"],
        "turnId": row["turnId"],
        "semanticTurn": row["semanticTurn"],
        "arm": row["arm"],
        "executionOrdinal": binding["executionOrdinal"],
        "runId": binding["runId"],
        "taskId": binding["taskId"],
        "sessionId": binding["sessionId"],
        "executionBindingSha256": row["executionBindingSha256"],
        "traceSha256": row["traceSha256"],
        "status": row["status"],
    }


def _collect_string_values(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_collect_string_values(child) for child in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_collect_string_values(child) for child in value)) if value else set()
    if isinstance(value, str) and value:
        return {value}
    return set()


def validate_outputs(
    outputs: list[dict[str, Any]],
    conversations: list[dict[str, Any]],
    runner_receipt: dict[str, Any],
    seed: int = SEED,
) -> dict[tuple[str, str, str], dict[str, Any]]:
    validator = _schema_validator()
    authority, expected_trace_hashes = _execution_authority()
    _validate_runner_receipt(runner_receipt, outputs, authority, expected_trace_hashes)
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
    call_id_owners: dict[str, tuple[str, str, str]] = {}
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
        if row["traceBindingSha256"] != sha_text(canonical(_trace_binding_payload(row))):
            raise ValueError(f"traceBindingSha256 mismatch: {key}")
        for field, expected_hash in expected_trace_hashes.items():
            if row["trace"][field] != expected_hash:
                raise ValueError(f"trace {field} differs from preregistered execution authority: {key}")
        if row["trace"]["postStateRevision"] < row["trace"]["preStateRevision"]:
            raise ValueError(f"state revision regressed: {key}")
        if row["trace"]["totalTokens"] != row["trace"]["promptTokens"] + row["trace"]["completionTokens"]:
            raise ValueError(f"token total mismatch: {key}")
        model_usage = {"promptTokens": 0, "completionTokens": 0, "totalTokens": 0}
        for call in row["trace"]["toolCalls"] + row["trace"]["modelCalls"]:
            if call["callId"] in call_id_owners:
                raise ValueError(f"callId reused across execution traces: {call['callId']}")
            call_id_owners[call["callId"]] = key
            if call["callType"] == "model":
                if call["usage"]["totalTokens"] != call["usage"]["promptTokens"] + call["usage"]["completionTokens"]:
                    raise ValueError(f"model call token total mismatch: {call['callId']}")
                for field in model_usage:
                    model_usage[field] += call["usage"][field]
        if model_usage != {
            "promptTokens": row["trace"]["promptTokens"],
            "completionTokens": row["trace"]["completionTokens"],
            "totalTokens": row["trace"]["totalTokens"],
        }:
            raise ValueError(f"trace token usage does not equal bound modelCalls: {key}")
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
        raise ValueError(f"complete {len(schedule_map)}-row paired output set required; missing={missing[:2]} extra={extra[:2]}")
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

    forbidden_identities: set[str] = set()
    for row in outputs:
        forbidden_identities.update(_collect_string_values(row["executionBinding"]))
        forbidden_identities.update(_collect_string_values(row["trace"]))
        forbidden_identities.update(
            {
                row["executionBindingSha256"],
                row["traceSha256"],
                row["traceBindingSha256"],
                row["dialogueSha256"],
                row["finalAnswerSha256"],
                row["status"],
            }
        )
        if isinstance(row["failureCode"], str):
            forbidden_identities.add(row["failureCode"])

    for conversation in conversations:
        for arm in ARMS:
            rows = [indexed[(conversation["conversationId"], turn["turnId"], arm)] for turn in conversation["turns"]]
            first_binding = rows[0]["executionBinding"]
            fixed_identity = (first_binding["taskId"], first_binding["sessionId"])
            fixed_branch_point = first_binding["branchPointStateHash"]
            seen_turn_run_ids: set[str] = set()
            previous_post_revision: int | None = None
            for turn_index, row in enumerate(rows):
                binding = row["executionBinding"]
                if (binding["taskId"], binding["sessionId"]) != fixed_identity:
                    raise ValueError("task/session identity changed within a conversation arm")
                if binding["runId"] in seen_turn_run_ids:
                    raise ValueError("durable runId must be unique per user turn")
                seen_turn_run_ids.add(binding["runId"])
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
    runner_receipt: dict[str, Any],
    seed: int = SEED,
) -> dict[str, Any]:
    indexed = validate_outputs(outputs, conversations, runner_receipt, seed)
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
                "schemaVersion": "real-user-multiturn-ai-blind-item-v4",
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
                    "traceBindingSha256": {arm: pair[arm]["traceBindingSha256"] for arm in ARMS},
                }
            )
    return {"judge01": judge01, "judge02": judge02, "mapping": mapping}


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(canonical(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paired-outputs", required=True, type=Path)
    parser.add_argument("--runner-receipt", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    if args.output.exists():
        raise RuntimeError(f"refusing to overwrite {args.output}")
    outputs = load_jsonl(args.paired_outputs)
    runner_receipt = json.loads(args.runner_receipt.read_text(encoding="utf-8"))
    if not isinstance(runner_receipt, dict):
        raise ValueError("runner receipt must be an object")
    built = build_packets(outputs, load_jsonl(DATASET), runner_receipt, args.seed)
    args.output.mkdir(parents=True, exist_ok=False)
    judge01 = args.output / "judge01.jsonl"
    judge02 = args.output / "judge02.jsonl"
    sealed = args.output / "SEALED_DO_NOT_SHARE.json"
    write_jsonl(judge01, built["judge01"])
    write_jsonl(judge02, built["judge02"])
    sealed.write_text(
        json.dumps(
            {
                "schemaVersion": "real-user-multiturn-ai-blind-mapping-v4",
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
        "schemaVersion": "real-user-multiturn-ai-blind-package-receipt-v4",
        "status": "HOLD_PENDING_TWO_INDEPENDENT_AI_JUDGES",
        "reviewerType": "independent_ai_judge",
        "humanReviewClaimAllowed": False,
        "itemCountPerJudge": len(built["judge01"]),
        "seed": args.seed,
        "pairedOutputsSha256": hashlib.sha256(args.paired_outputs.read_bytes()).hexdigest(),
        "runnerExecutionReceiptSha256": hashlib.sha256(args.runner_receipt.read_bytes()).hexdigest(),
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
