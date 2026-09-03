"""Deterministic, no-model verification for the V2 real-user replay package."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator


PACKAGE = Path(__file__).resolve().parent
LEGACY_V1 = PACKAGE.with_name("real_user_multiturn_replay_20260902_v1")
ROOT = PACKAGE.parents[2]
DATASET = PACKAGE / "conversations.jsonl"
RECEIPTS = PACKAGE / "session_source_receipts.jsonl"
DB = ROOT / ".runtime/used-phone-demo-439/web-query-intake.sqlite3"
PACKAGE_ID = "real_user_multiturn_replay_20260902_v2"
ARMS = ("RAW_FULL_CONTROL", "CONTEXT_TREATMENT")
ROW_COLUMNS = (
    "request_id", "session_id", "turn_number", "submitted_at", "updated_at",
    "route", "mode", "outcome", "message", "message_sha256",
    "redaction_applied", "failure_code",
)
DIRECT_IDENTIFIER_PATTERNS = (
    re.compile(r"req-[0-9a-f]{8,}", re.I),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    re.compile(r"https?://", re.I),
)
V1_BASELINE_HASHES = {
    "README.md": "8653743d259a2939a9975a188429857d199db0b93b3fae53f44d614be4d33615",
    "README_FOR_AI_JUDGES.md": "c12bf91148cd59d8bac1fcb6c6bb59fa079129d11202fa293be9e733b613da58",
    "ai_judge_response.schema.json": "0e046156468f53f620a55a807ef6dd8d2675cec041fad78c5e0cf5fd35dd7247",
    "blind_packet_builder.py": "169dcc9f7cc1858fec8b423c74cb27f95e4e02dc9970aabfca1d898d04ef127e",
    "build_replay_schedule.py": "a7a81ccdc3bb23a27b96e899187c842d56481401b07a66fb854c452f84da4c6a",
    "conversation.schema.json": "7224530981aa40edda7c177e770e9b3e8247360f7738dbbc3307f7b2237057e8",
    "conversations.jsonl": "982c98cdbf26d153150c3d106637e3c8d8649080fc014ffadb9caefda49c7879",
    "multi_agent_evaluation_contract.json": "396e9137d81cf9d5f90539a5860b014c599a9a4272833964050ded7cb530d760",
    "package_manifest.json": "24d6e88f96ac529e4234434a3fcdc71e102389e8f266ff52a2b38fd190e50832",
    "paired_output.schema.json": "32f15acf1c5f269a4353b308f3a5f626a4ead0d5ffa33db51d2094835fd3a947",
    "reference_context_cases.json": "215efaa2e12053ec6c1e1ae5add1034742e2482f56998ca0b85247c74305e128",
    "replay_contract.json": "50e4ecf6aaea4f6e6c8376ad94b731a1f6309b866c2761b41109d349d5c74db7",
    "SHA256SUMS.txt": "52b4b313292a84dd4bfd45dafa27db6e21b6d787e39c7ec9f28ecce3699eb8e1",
    "source_dedup_leakage_audit.json": "e06961c32c8ab419db745c802c1b97f249e9d2d02373eb9330ad43e715f5d45c",
    "source_files.json": "ff3912a8c6538eedd15368e74f70a7060cd6088bf1737228e0f10b44653d2447",
    "test_package.py": "b9558bae56b499ae67b3cd89b7023b28dd14a30f9a2e005c754a2262e773fcad",
    "verification.json": "c65892b997d159fcda1641dec693d2b7a2aab409e333f2231dd64d2c95a4cb86",
    "verify_package.py": "be78692e0a7612fb619993f5c901205f0c258ec7267e521a1afc48b34a22d326",
}


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_text(value: str) -> str:
    return sha_bytes(value.encode("utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), f"{path}: expected object"
    return value


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        assert isinstance(value, dict), f"{path}:{number}: expected object"
        assert line == canonical(value), f"{path}:{number}: JSONL is not canonical"
        rows.append(value)
    return rows


def load_module(filename: str, module_name: str):
    path = PACKAGE / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def schema_validator(filename: str) -> Draft202012Validator:
    schema = load_json(PACKAGE / filename)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def assert_schema(validator: Draft202012Validator, value: Any, label: str) -> None:
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    assert not errors, f"{label}: schema violation: {errors[0].message if errors else ''}"


def source_row_index() -> dict[str, dict[str, Any]]:
    assert DB.exists(), f"missing source DB: {DB}"
    connection = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        query = "select " + ",".join(ROW_COLUMNS) + " from web_query_intake"
        result: dict[str, dict[str, Any]] = {}
        for row in connection.execute(query):
            value = dict(row)
            digest = sha_text(canonical(value))
            assert digest not in result, "source row hash collision or duplicate"
            result[digest] = value
        return result
    finally:
        connection.close()


def _verify_authorship_locator(locator: str, evidence_path: Path) -> None:
    text = evidence_path.read_text(encoding="utf-8")
    if "#" not in locator:
        return
    fragment = locator.split("#", 1)[1]
    line_match = re.fullmatch(r"L(\d+)-L(\d+)", fragment)
    if line_match:
        start, end = map(int, line_match.groups())
        assert 1 <= start <= end <= len(text.splitlines()), f"invalid evidence line locator: {locator}"
    else:
        assert fragment in text, f"authorship evidence fragment missing: {locator}"


def _answer(conversation_id: str, turn_id: str, arm: str) -> str:
    lane = 1 if arm == ARMS[0] else 2
    return f"{conversation_id}|{turn_id}|candidate-{lane}"


def dummy_outputs(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    schedule_builder = load_module("build_replay_schedule.py", "rumr_v2_schedule_for_dummy")
    schedule = schedule_builder.build_schedule(conversations, 20260902)
    schedule_map = {(row["conversationId"], row["turnId"], row["arm"]): row for row in schedule}
    rows: list[dict[str, Any]] = []
    for conversation in conversations:
        for turn_index, turn in enumerate(conversation["turns"]):
            for arm in ARMS:
                lane = 1 if arm == ARMS[0] else 2
                schedule_row = schedule_map[(conversation["conversationId"], turn["turnId"], arm)]
                binding = {
                    "packageId": PACKAGE_ID,
                    "executionOrdinal": schedule_row["executionOrdinal"],
                    "scheduleRowSha256": schedule_row["scheduleRowSha256"],
                    "runId": f"run-{conversation['conversationId']}-lane-{lane}",
                    "taskId": f"task-{conversation['conversationId']}-lane-{lane}",
                    "sessionId": f"session-{conversation['conversationId']}-lane-{lane}",
                    "branchPointStateHash": sha_text(f"branch|{conversation['conversationId']}"),
                    "inputMessageSha256": turn["messageSha256"],
                }
                arm_state_payload = {
                    "packageId": PACKAGE_ID,
                    "conversationId": conversation["conversationId"],
                    "arm": arm,
                    "runId": binding["runId"],
                    "taskId": binding["taskId"],
                    "sessionId": binding["sessionId"],
                    "branchPointStateHash": binding["branchPointStateHash"],
                }
                binding["armStateBindingSha256"] = sha_text(canonical(arm_state_payload))
                trace = {
                    "preStateRevision": turn_index,
                    "postStateRevision": turn_index + 1,
                    "controlledWorldHash": sha_text("controlled-world|frozen-v2"),
                    "modelConfigurationHash": sha_text("model-configuration|frozen-v2"),
                    "toolConfigurationHash": sha_text("tool-configuration|frozen-v2"),
                    "budgetConfigurationHash": sha_text("budget-configuration|frozen-v2"),
                    "policyConfigurationHash": sha_text("policy-configuration|frozen-v2"),
                    "contextBindingHash": sha_text(f"context|{conversation['conversationId']}|{lane}|{turn['turnId']}"),
                    "referenceContextBindingHash": sha_text(f"reference|{conversation['conversationId']}|{lane}|{turn['turnId']}"),
                    "candidateScopeHash": sha_text(f"scope|{conversation['conversationId']}|{lane}|{turn['turnId']}"),
                    "toolCalls": [],
                    "modelCalls": [],
                    "promptTokens": 100 + turn_index,
                    "completionTokens": 10,
                    "totalTokens": 110 + turn_index,
                    "durationMs": 1.0 + turn_index,
                    "automaticRetryCount": 0,
                }
                dialogue: list[dict[str, str]] = []
                for history_turn in conversation["turns"][: turn_index + 1]:
                    dialogue.append({"role": "user", "content": history_turn["rawUserText"]})
                    dialogue.append({"role": "assistant", "content": _answer(conversation["conversationId"], history_turn["turnId"], arm)})
                answer = _answer(conversation["conversationId"], turn["turnId"], arm)
                rows.append(
                    {
                        "schemaVersion": "real-user-multiturn-paired-output-v2",
                        "conversationId": conversation["conversationId"],
                        "turnId": turn["turnId"],
                        "semanticTurn": turn["semanticTurn"],
                        "arm": arm,
                        "executionBinding": binding,
                        "executionBindingSha256": sha_text(canonical(binding)),
                        "trace": trace,
                        "traceSha256": sha_text(canonical(trace)),
                        "status": "SUCCEEDED",
                        "dialogue": dialogue,
                        "dialogueSha256": sha_text(canonical(dialogue)),
                        "finalAnswer": answer,
                        "finalAnswerSha256": sha_text(answer),
                        "publicEvidence": [],
                        "failureCode": None,
                    }
                )
    return rows


def _expect_rejected(
    builder: Any,
    conversations: list[dict[str, Any]],
    outputs: list[dict[str, Any]],
    mutate: Callable[[list[dict[str, Any]]], None],
    expected: str,
) -> None:
    forged = copy.deepcopy(outputs)
    mutate(forged)
    try:
        builder.build_packets(forged, conversations, 20260902)
    except ValueError as error:
        assert expected in str(error), f"unexpected fail-closed reason: {error}"
    else:
        raise AssertionError(f"adversarial mutation was accepted: {expected}")


def run_adversarial_checks(builder: Any, conversations: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> int:
    def forge_history(rows: list[dict[str, Any]]) -> None:
        target = next(row for row in rows if row["conversationId"] == "rumr-v1-c008" and row["turnId"] == "rumr-v1-c008-t03" and row["arm"] == ARMS[0])
        opposite = next(row for row in rows if row["conversationId"] == "rumr-v1-c008" and row["turnId"] == "rumr-v1-c008-t01" and row["arm"] == ARMS[1])
        target["dialogue"][1]["content"] = opposite["finalAnswer"]
        target["dialogueSha256"] = sha_text(canonical(target["dialogue"]))

    def leak_raw_label(rows: list[dict[str, Any]]) -> None:
        target = rows[0]
        target["finalAnswer"] = "internal lane: RAW_FULL_CONTROL"
        target["finalAnswerSha256"] = sha_text(target["finalAnswer"])
        target["dialogue"][-1]["content"] = target["finalAnswer"]
        target["dialogueSha256"] = sha_text(canonical(target["dialogue"]))

    def leak_other_label(rows: list[dict[str, Any]]) -> None:
        target = rows[1]
        target["publicEvidence"] = [{"note": "CONTEXT_TREATMENT"}]

    def leak_arm_field(rows: list[dict[str, Any]]) -> None:
        target = rows[1]
        target["publicEvidence"] = [{"arm": "CONTEXT_TREATMENT"}]

    def add_unknown_property(rows: list[dict[str, Any]]) -> None:
        rows[0]["unboundTrace"] = "forbidden"

    _expect_rejected(builder, conversations, outputs, forge_history, "assistant history does not match")
    _expect_rejected(builder, conversations, outputs, leak_raw_label, "arm label leaked")
    _expect_rejected(builder, conversations, outputs, leak_other_label, "arm label leaked")
    _expect_rejected(builder, conversations, outputs, leak_arm_field, "arm or execution label leaked")
    _expect_rejected(builder, conversations, outputs, add_unknown_property, "schema violation")
    return 5


def _verify_checksum_inventory() -> int:
    checksum_path = PACKAGE / "SHA256SUMS.txt"
    listed: dict[str, str] = {}
    lines = checksum_path.read_text(encoding="utf-8").splitlines()
    assert lines == sorted(lines, key=lambda line: line.split("  ", 1)[1].casefold()), "checksum inventory must be path-sorted"
    for line in lines:
        expected, relative = line.split("  ", 1)
        assert relative not in listed, f"duplicate checksum entry: {relative}"
        listed[relative] = expected
    actual_files = {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS.txt" and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    assert set(listed) == actual_files, f"checksum inventory coverage mismatch: missing={sorted(actual_files-set(listed))} extra={sorted(set(listed)-actual_files)}"
    for relative, expected in listed.items():
        assert re.fullmatch(r"[0-9a-f]{64}", expected), f"invalid checksum: {relative}"
        assert sha_bytes((PACKAGE / relative).read_bytes()) == expected, f"checksum mismatch: {relative}"
    return len(listed)


def _verify_v1_unchanged() -> int:
    actual = {
        path.relative_to(LEGACY_V1).as_posix(): sha_bytes(path.read_bytes())
        for path in LEGACY_V1.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }
    assert actual == V1_BASELINE_HASHES, "legacy v1 package changed after v2 repair began"
    return len(actual)


def verify_all() -> dict[str, Any]:
    conversation_validator = schema_validator("conversation.schema.json")
    paired_validator = schema_validator("paired_output.schema.json")
    receipt_validator = schema_validator("session_source_receipt.schema.json")
    judge_validator = schema_validator("ai_judge_response.schema.json")

    conversations = load_jsonl(DATASET)
    receipts = load_jsonl(RECEIPTS)
    assert len(conversations) == len(receipts) == 8
    for index, conversation in enumerate(conversations, 1):
        assert_schema(conversation_validator, conversation, f"conversation {index}")
    for index, receipt in enumerate(receipts, 1):
        assert_schema(receipt_validator, receipt, f"source receipt {index}")

    ids = [row["conversationId"] for row in conversations]
    assert ids == [f"rumr-v1-c{i:03d}" for i in range(1, 9)]
    assert len(set(ids)) == len(ids)
    source_rows = source_row_index()
    turn_ids: list[str] = []
    texts: list[str] = []
    record_hashes: list[str] = []
    request_fingerprints: list[str] = []
    session_fingerprints: list[str] = []
    receipt_map = {row["conversationId"]: row for row in receipts}
    assert set(receipt_map) == set(ids)

    for conversation in conversations:
        turns = conversation["turns"]
        assert conversation["humanAuthorship"] == "PROJECT_OWNER_REAL_WEB_CONFIRMED"
        assert conversation["sameSession"] is True
        assert [turn["semanticTurn"] for turn in turns] == list(range(1, len(turns) + 1))
        assert [turn["sourceOrdinal"] for turn in turns] == sorted(turn["sourceOrdinal"] for turn in turns)
        receipt = receipt_map[conversation["conversationId"]]
        assert receipt["sourceSessionFingerprint"] == conversation["sourceSessionFingerprint"]
        assert receipt["sourceLocator"] == conversation["sourceLocator"]
        assert receipt["authorshipEvidenceLocator"] == conversation["authorshipEvidenceLocator"]
        assert receipt["turnCount"] == len(turns)
        assert receipt["sourceOrdinals"] == [turn["sourceOrdinal"] for turn in turns]
        assert receipt["sourceRecordSha256s"] == [turn["sourceRecordSha256"] for turn in turns]
        assert receipt["messageSha256s"] == [turn["messageSha256"] for turn in turns]
        assert receipt["sourceRowSetSha256"] == sha_text(canonical(receipt["sourceRecordSha256s"]))
        evidence_path = ROOT / receipt["authorshipEvidenceFile"]["path"]
        assert evidence_path.exists(), f"missing authorship evidence: {evidence_path}"
        assert sha_bytes(evidence_path.read_bytes()) == receipt["authorshipEvidenceFile"]["sha256"]
        _verify_authorship_locator(receipt["authorshipEvidenceLocator"], evidence_path)

        for turn in turns:
            assert turn["turnId"] == f"{conversation['conversationId']}-t{turn['semanticTurn']:02d}"
            assert turn["sourceOutcome"] == "completed"
            assert sha_text(turn["rawUserText"]) == turn["messageSha256"]
            assert all(not pattern.search(turn["rawUserText"]) for pattern in DIRECT_IDENTIFIER_PATTERNS)
            source = source_rows.get(turn["sourceRecordSha256"])
            assert source is not None, f"source row not found: {turn['turnId']}"
            assert source["message"] == turn["rawUserText"]
            assert source["message_sha256"] == turn["messageSha256"]
            assert source["turn_number"] == turn["sourceOrdinal"]
            assert sha_text("session:" + source["session_id"]) == conversation["sourceSessionFingerprint"]
            assert sha_text("request:" + source["request_id"]) == turn["sourceRequestFingerprint"]
            turn_ids.append(turn["turnId"])
            texts.append(turn["rawUserText"])
            record_hashes.append(turn["sourceRecordSha256"])
            request_fingerprints.append(turn["sourceRequestFingerprint"])
        session_fingerprints.append(conversation["sourceSessionFingerprint"])

    assert len(turn_ids) == len(set(turn_ids)) == 27
    assert len(set(texts)) == 26
    assert len(set(record_hashes)) == 27
    assert len(set(request_fingerprints)) == 27
    assert len(set(session_fingerprints)) == 8
    assert sum(len(row["turns"]) - 1 for row in conversations) == 19
    assert sha_bytes(DATASET.read_bytes()) == sha_bytes((LEGACY_V1 / "conversations.jsonl").read_bytes())

    source_files = load_json(PACKAGE / "source_files.json")
    for item in source_files["files"]:
        path = ROOT / item["path"]
        assert path.exists(), f"missing source evidence: {item['path']}"
        assert sha_bytes(path.read_bytes()) == item["sha256"], f"source hash mismatch: {item['path']}"

    audit = load_json(PACKAGE / "source_dedup_leakage_audit.json")
    assert audit["counts"]["conversationCount"] == 8
    assert audit["counts"]["rawUserTurnCount"] == 27
    assert audit["counts"]["followupTurnCount"] == 19
    assert audit["counts"]["unseenConversationCount"] == 0
    assert audit["sessionGrouping"]["splitUnit"] == "conversation"
    assert audit["boundaries"]["isOnlineHumanTest"] is False
    assert audit["boundaries"]["isUnseenEvaluation"] is False

    replay = load_json(PACKAGE / "replay_contract.json")
    assert replay["schemaVersion"] == "real-user-multiturn-ab-replay-contract-v2"
    assert replay["splitPolicy"]["splitTurnsAcrossPartitions"] is False
    assert replay["counts"]["pairedScoredFollowupTurnCount"] == 19
    assert replay["mechanicalGates"]["allFiftyFourArmTurnsExecutionBound"] is True
    assert replay["mechanicalGates"]["allAssistantHistoryRecursivelySameArmBound"] is True

    dummy = dummy_outputs(conversations)
    assert len(dummy) == 54
    for index, row in enumerate(dummy, 1):
        assert_schema(paired_validator, row, f"paired output {index}")
    builder = load_module("blind_packet_builder.py", "rumr_v2_blind_builder")
    built_a = builder.build_packets(dummy, conversations, 20260902)
    built_b = builder.build_packets(dummy, conversations, 20260902)
    assert canonical(built_a) == canonical(built_b)
    assert len(built_a["judge01"]) == len(built_a["judge02"]) == 19
    assert len(built_a["mapping"]) == 19
    public_rows = built_a["judge01"] + built_a["judge02"]
    assert all("RAW_FULL_CONTROL" not in canonical(row) and "CONTEXT_TREATMENT" not in canonical(row) for row in public_rows)
    adversarial_checks = run_adversarial_checks(builder, conversations, dummy)

    schedule_builder = load_module("build_replay_schedule.py", "rumr_v2_schedule_verify")
    schedule_a = schedule_builder.build_schedule(conversations, 20260902)
    schedule_b = schedule_builder.build_schedule(conversations, 20260902)
    assert canonical(schedule_a) == canonical(schedule_b)
    assert len(schedule_a) == 54
    assert len({row["scheduleRowSha256"] for row in schedule_a}) == 54
    assert all(row["scheduleRowSha256"] == schedule_builder.schedule_binding(row) for row in schedule_a)
    assert sum(bool(row["scoredFollowup"]) for row in schedule_a) == 38
    assert all(row["automaticRetries"] == 0 for row in schedule_a)

    judge_example = {
        "schemaVersion": "real-user-multiturn-ai-judge-response-v2",
        "itemId": "rumr-blind-01",
        "reviewerType": "independent_ai_judge",
        "judgeModel": "offline-schema-example-not-run",
        "judgeRunId": "offline-schema-example-not-run",
        "review": {
            "candidateA": {"constraintFidelity": 3, "referenceResolution": 3, "evidenceDiscipline": 3, "taskProgression": 3, "usefulness": 3},
            "candidateB": {"constraintFidelity": 3, "referenceResolution": 3, "evidenceDiscipline": 3, "taskProgression": 3, "usefulness": 3},
            "overallPreference": "tie",
            "reason": "schema validation fixture only; no judge executed",
        },
    }
    assert_schema(judge_validator, judge_example, "AI judge schema fixture")

    manifest = load_json(PACKAGE / "package_manifest.json")
    assert manifest["packageId"] == PACKAGE_ID
    assert manifest["status"] == "FROZEN_PRE_EXECUTION_NO_MODEL_OR_JUDGE_RUN"
    assert manifest["dataset"]["sha256"] == sha_bytes(DATASET.read_bytes())
    assert manifest["dataset"]["byteIdenticalToLegacyV1"] is True
    assert manifest["execution"] == {
        "modelCalls": 0,
        "agentRuns": 0,
        "formalAbRuns": 0,
        "formalAiJudgeRuns": 0,
        "formalHumanReviews": 0,
        "productionDefaultsChanged": False,
    }

    v1_files_verified = _verify_v1_unchanged()
    checksum_files_verified = _verify_checksum_inventory()
    return {
        "status": "PASS",
        "conversationCount": 8,
        "sessionSourceReceiptsVerified": 8,
        "rawUserTurnCount": 27,
        "uniqueExactUserTextCount": 26,
        "pairedFollowupCount": 19,
        "sourceRowsVerified": 27,
        "sourceEvidenceFilesVerified": len(source_files["files"]),
        "jsonSchemasChecked": 4,
        "jsonInstancesValidated": 8 + 8 + 54 + 1,
        "checksumInventoryFilesVerified": checksum_files_verified,
        "legacyV1FilesUnchanged": v1_files_verified,
        "blindItemsPerJudgeDryRun": 19,
        "scheduledArmTurnsDryRun": 54,
        "adversarialFailClosedChecks": adversarial_checks,
        "modelCalls": 0,
        "agentRuns": 0,
        "formalAbRuns": 0,
        "formalJudgeRuns": 0,
        "scope": "development_regression_real_user_historical_log_replay",
        "unseenOrSealedEligible": False,
    }


def _group_texts(conversations: list[dict[str, Any]]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for conversation in conversations:
        for turn in conversation["turns"]:
            grouped[turn["rawUserText"]].append(turn["turnId"])
    return dict(grouped)


def main() -> int:
    print(json.dumps(verify_all(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
