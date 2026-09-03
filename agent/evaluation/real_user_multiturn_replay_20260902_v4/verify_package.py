"""Deterministic, no-model verification for the V4 real-user replay package."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import random
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from jsonschema import Draft202012Validator


PACKAGE = Path(__file__).resolve().parent
LEGACY_V1 = PACKAGE.with_name("real_user_multiturn_replay_20260902_v1")
LEGACY_V2 = PACKAGE.with_name("real_user_multiturn_replay_20260902_v2")
LEGACY_V3 = PACKAGE.with_name("real_user_multiturn_replay_20260902_v3")
ROOT = PACKAGE.parents[2]
DATASET = PACKAGE / "conversations.jsonl"
RECEIPTS = PACKAGE / "session_source_receipts.jsonl"
DB = ROOT / ".runtime/used-phone-demo-439/web-query-intake.sqlite3"
PACKAGE_ID = "real_user_multiturn_replay_20260902_v4"
EXECUTION_AUTHORITY = PACKAGE / "execution_authority.json"
EXECUTION_SNAPSHOT = PACKAGE / "execution_config_snapshot.json"
EXECUTION_RECEIPT_SCHEMA = PACKAGE / "execution_receipt.schema.json"
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
V2_BASELINE_HASHES = {
    "README.md": "854d9b2a92249b4fb23f3d17732c08911948015d4d3b3add32eface9cc071db0",
    "README_FOR_AI_JUDGES.md": "3c1c1099b0861b651e512da93d80a7c468794ef21b70b576cc673cc29e870f6f",
    "ai_judge_response.schema.json": "3e083e1656d60c96ec2d4e7aac6f7295956f1fe41efd3fbbb94d89f5c9d066be",
    "blind_packet_builder.py": "b6850dd173f54ec457898726ff657abd06607f201500cff00831fa1607df2504",
    "build_replay_schedule.py": "7a24817ac4def2bda2071ca62c61618b4fbef34e81a6f3efef4c43a0e75abaf6",
    "conversation.schema.json": "7224530981aa40edda7c177e770e9b3e8247360f7738dbbc3307f7b2237057e8",
    "conversations.jsonl": "982c98cdbf26d153150c3d106637e3c8d8649080fc014ffadb9caefda49c7879",
    "multi_agent_evaluation_contract.json": "b3cd61421b576342aed8801d5e3995d0916cb173f7ce22641dc47ed40aace2c6",
    "package_manifest.json": "248fa3a4fb11883d7e4a51f51b9f216773232d2681a57ffa22d056df93a83bd4",
    "paired_output.schema.json": "e24aedf85222152b9573ac837471ee3fe3dfaac602e40aedec17449964318244",
    "reference_context_cases.json": "215efaa2e12053ec6c1e1ae5add1034742e2482f56998ca0b85247c74305e128",
    "replay_contract.json": "e047fea258ea02901d5010d954896f17dfb42b29c1539a66435d99df1d078ef2",
    "session_source_receipt.schema.json": "ae61ddf09e530eaa2d36a1dc6c801c7cf62fbbca7878aa1eabfd192fff73efd4",
    "session_source_receipts.jsonl": "081ba3bd2889ec6e852698911378e2773219ce40120be20c3b65d8597b182669",
    "SHA256SUMS.txt": "155eb85a3f1a61123ffb9441a9f3e07c8202b1446941282bf2e066ac3fa21010",
    "source_dedup_leakage_audit.json": "12c8b611172e2241f4f59c5561b03dda508b97497032e6c438ed4966e3c80c6a",
    "source_files.json": "ff3912a8c6538eedd15368e74f70a7060cd6088bf1737228e0f10b44653d2447",
    "test_package.py": "5be4593dc7ae9b999e15e5833ac400a465934c7cff4e72599fa5892669757e99",
    "verification.json": "41d1cea221f7facf7865626f7d55b353ac11bcb4c9341ce020976cd755780ddb",
    "verify_package.py": "5391fa975238b8cdc98a2b5a8c91ebaf8826061d7bacfe8ec59ce221d21d642b",
}
V3_BASELINE_HASHES = {
    "README.md": "6f77ac7d1a1914e403aad5633c4683d0390a72d14104f2caa324922e1a117b0f",
    "README_FOR_AI_JUDGES.md": "50b5e2e934766f03e3ebaac151f6fd856f7ba289469185bb5f58b8757afe5b39",
    "ai_judge_response.schema.json": "168304d38ca34033115d97ec7a2db65fa272136167520de38d357a07a395fe91",
    "blind_packet_builder.py": "939d6e5ac2bef16f71abefc0bce06be86efabc8176f837775666f5ae1da8460b",
    "build_replay_schedule.py": "455cf2a8430cb104252988eac506ad1a5dabef4a898ab211dd4fa39dfbb5a1d1",
    "conversation.schema.json": "7224530981aa40edda7c177e770e9b3e8247360f7738dbbc3307f7b2237057e8",
    "conversations.jsonl": "66dbf41b2c74206351f68ba9624527a5696345f399efecdf4d859244b3348a63",
    "multi_agent_evaluation_contract.json": "2dfd1d1b03148eef9670a7508107ff11e20d8fabb9400daacd1c7f9b958517e8",
    "package_manifest.json": "6973cc80d95fb9d509f674bd7f4a4f9b305c09167cbfee03ba6394a5a3151214",
    "paired_output.schema.json": "35079a80a2e1c17bbc2718ae4d4f84640920dbd540784401bd4857082b49c5f1",
    "reference_context_cases.json": "097ba16b325ff6b5e5c1b41899986a1635fe8aa74a58de8e3ca2494ce52d5a6c",
    "replay_contract.json": "f27b92a63e1562159183845edeaca884de6a387b3723559876466afb5bc3b2ca",
    "session_source_receipt.schema.json": "fd928eaff3b590e43198e14d50a4f8c7d80a17ae2ce5c6dcca355a7c7dc119ea",
    "session_source_receipts.jsonl": "ac0cb98062b9f23f4c71589ee01ecd16dfbd1dad1df82458d9942425c1f2dd9a",
    "SHA256SUMS.txt": "72de0e60d4b45d6bd4948307dd2c9db0e94679038867456d73582cd835d835d6",
    "source_dedup_leakage_audit.json": "4b10ed9fcfdecb2c635ab21074a615deb3c74bc7d7209cca334456f3a1acc890",
    "source_files.json": "ff3912a8c6538eedd15368e74f70a7060cd6088bf1737228e0f10b44653d2447",
    "test_package.py": "97f1033082d5424ff8e60b9f91272ce110be4a5b83a7f9a847cc366b65517602",
    "verification.json": "f251f7ec0cbb39729c8de03f2768e12233ceb898774159d9019169806161242b",
    "verify_package.py": "455a5639a778585570818f4e8797d39664e23cd10180c489cad0145f61728cad",
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


def _verify_authorship_locator(locator: str, evidence_path: Path, expected_message: str | None = None) -> None:
    text = evidence_path.read_text(encoding="utf-8")
    if "#" not in locator:
        return
    fragment = locator.split("#", 1)[1]
    line_match = re.fullmatch(r"L(\d+)-L(\d+)", fragment)
    if line_match:
        start, end = map(int, line_match.groups())
        lines = text.splitlines()
        assert 1 <= start <= end <= len(lines), f"invalid evidence line locator: {locator}"
        if expected_message is not None:
            selected = "\n".join(lines[start - 1:end])
            assert expected_message in selected, f"source locator does not contain exact message: {locator}"
    else:
        assert fragment in text, f"authorship evidence fragment missing: {locator}"


def _answer(conversation_id: str, turn_id: str, arm: str) -> str:
    lane = 1 if arm == ARMS[0] else 2
    return f"{conversation_id}|{turn_id}|candidate-{lane}"


def dummy_outputs(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    schedule_builder = load_module("build_replay_schedule.py", "rumr_v4_schedule_for_dummy")
    expected_trace_hashes = load_json(EXECUTION_AUTHORITY)["expectedTraceHashes"]
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
                    **expected_trace_hashes,
                    "contextBindingHash": sha_text(f"context|{conversation['conversationId']}|{lane}|{turn['turnId']}"),
                    "referenceContextBindingHash": sha_text(f"reference|{conversation['conversationId']}|{lane}|{turn['turnId']}"),
                    "candidateScopeHash": sha_text(f"scope|{conversation['conversationId']}|{lane}|{turn['turnId']}"),
                    "toolCalls": [],
                    "modelCalls": [],
                    "promptTokens": 0,
                    "completionTokens": 0,
                    "totalTokens": 0,
                    "durationMs": 1.0 + turn_index,
                    "automaticRetryCount": 0,
                }
                dialogue: list[dict[str, str]] = []
                for history_turn in conversation["turns"][: turn_index + 1]:
                    dialogue.append({"role": "user", "content": history_turn["rawUserText"]})
                    dialogue.append({"role": "assistant", "content": _answer(conversation["conversationId"], history_turn["turnId"], arm)})
                answer = _answer(conversation["conversationId"], turn["turnId"], arm)
                row = {
                        "schemaVersion": "real-user-multiturn-paired-output-v4",
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
                trace_binding_payload = {
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
                row["traceBindingSha256"] = sha_text(canonical(trace_binding_payload))
                rows.append(row)
    return rows


def dummy_runner_receipt(outputs: list[dict[str, Any]]) -> dict[str, Any]:
    authority = load_json(EXECUTION_AUTHORITY)
    receipt = {
        "schemaVersion": "real-user-multiturn-execution-receipt-v4",
        "packageId": PACKAGE_ID,
        "ownership": "RUNNER_OWNED",
        "producer": "formal_real_user_multiturn_replay_runner",
        "runSetId": "no-model-static-verification-fixture",
        "executionAuthoritySha256": sha_bytes(EXECUTION_AUTHORITY.read_bytes()),
        "executionConfigSnapshotSha256": sha_bytes(EXECUTION_SNAPSHOT.read_bytes()),
        "pairedOutputsCanonicalSha256": sha_text("".join(canonical(row) + "\n" for row in outputs)),
        "pairedOutputRowCount": len(outputs),
        "expectedTraceHashes": authority["expectedTraceHashes"],
        "observedModelCallCount": sum(len(row["trace"]["modelCalls"]) for row in outputs),
        "observedToolCallCount": sum(len(row["trace"]["toolCalls"]) for row in outputs),
    }
    receipt["receiptBindingSha256"] = sha_text(canonical(receipt))
    return receipt


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
        builder.build_packets(forged, conversations, dummy_runner_receipt(forged), 20260902)
    except ValueError as error:
        assert expected in str(error), f"unexpected fail-closed reason: {error}"
    else:
        raise AssertionError(f"adversarial mutation was accepted: {expected}")


def run_adversarial_checks(builder: Any, conversations: list[dict[str, Any]], outputs: list[dict[str, Any]]) -> int:
    def rebind_trace(row: dict[str, Any]) -> None:
        row["traceSha256"] = sha_text(canonical(row["trace"]))
        binding = row["executionBinding"]
        payload = {
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
        row["traceBindingSha256"] = sha_text(canonical(payload))

    def replace_answer_and_bound_history(rows: list[dict[str, Any]], target: dict[str, Any], value: str) -> None:
        semantic_turn = target["semanticTurn"]
        target["finalAnswer"] = value
        target["finalAnswerSha256"] = sha_text(value)
        for row in rows:
            if row["conversationId"] == target["conversationId"] and row["arm"] == target["arm"] and row["semanticTurn"] >= semantic_turn:
                row["dialogue"][semantic_turn * 2 - 1]["content"] = value
                row["dialogueSha256"] = sha_text(canonical(row["dialogue"]))

    def forge_history(rows: list[dict[str, Any]]) -> None:
        target = next(row for row in rows if row["conversationId"] == "rumr-v1-c002" and row["turnId"] == "rumr-v1-c002-t03" and row["arm"] == ARMS[0])
        opposite = next(row for row in rows if row["conversationId"] == "rumr-v1-c002" and row["turnId"] == "rumr-v1-c002-t01" and row["arm"] == ARMS[1])
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

    def swap_cross_arm_trace(rows: list[dict[str, Any]]) -> None:
        target = next(row for row in rows if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == ARMS[0])
        source = next(row for row in rows if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == ARMS[1])
        target["trace"] = copy.deepcopy(source["trace"])
        target["traceSha256"] = sha_text(canonical(target["trace"]))

    def swap_cross_session_trace(rows: list[dict[str, Any]]) -> None:
        target = next(row for row in rows if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == ARMS[0])
        source = next(row for row in rows if row["conversationId"] == "rumr-v1-c003" and row["turnId"] == "rumr-v1-c003-t02" and row["arm"] == ARMS[0])
        target["trace"] = copy.deepcopy(source["trace"])
        target["traceSha256"] = sha_text(canonical(target["trace"]))

    def leak_trace_sha(rows: list[dict[str, Any]]) -> None:
        target = next(row for row in rows if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == ARMS[0])
        replace_answer_and_bound_history(rows, target, target["traceSha256"])

    def leak_context_binding(rows: list[dict[str, Any]]) -> None:
        target = rows[0]
        target["publicEvidence"] = [{"note": target["trace"]["contextBindingHash"]}]

    def leak_reference_binding(rows: list[dict[str, Any]]) -> None:
        target = next(row for row in rows if row["conversationId"] == "rumr-v1-c001" and row["turnId"] == "rumr-v1-c001-t02" and row["arm"] == ARMS[0])
        replace_answer_and_bound_history(rows, target, target["trace"]["referenceContextBindingHash"])

    def leak_candidate_scope(rows: list[dict[str, Any]]) -> None:
        target = rows[0]
        target["publicEvidence"] = [{"note": target["trace"]["candidateScopeHash"]}]

    def leak_mixed_unicode_field(rows: list[dict[str, Any]]) -> None:
        rows[0]["publicEvidence"] = [{"ＴrＡcＥ-ＳhＡ_２５６": "redacted"}]

    def drift_preregistered_model_hash(rows: list[dict[str, Any]]) -> None:
        rows[0]["trace"]["modelConfigurationHash"] = sha_text("attacker-rebound-drift")
        rebind_trace(rows[0])

    _expect_rejected(builder, conversations, outputs, forge_history, "assistant history does not match")
    _expect_rejected(builder, conversations, outputs, leak_raw_label, "arm label leaked")
    _expect_rejected(builder, conversations, outputs, leak_other_label, "arm label leaked")
    _expect_rejected(builder, conversations, outputs, leak_arm_field, "arm or execution label leaked")
    _expect_rejected(builder, conversations, outputs, add_unknown_property, "schema violation")
    _expect_rejected(builder, conversations, outputs, swap_cross_arm_trace, "traceBindingSha256 mismatch")
    _expect_rejected(builder, conversations, outputs, swap_cross_session_trace, "traceBindingSha256 mismatch")
    _expect_rejected(builder, conversations, outputs, leak_trace_sha, "execution or trace identifier leaked")
    _expect_rejected(builder, conversations, outputs, leak_context_binding, "execution or trace identifier leaked")
    _expect_rejected(builder, conversations, outputs, leak_reference_binding, "execution or trace identifier leaked")
    _expect_rejected(builder, conversations, outputs, leak_candidate_scope, "execution or trace identifier leaked")
    _expect_rejected(builder, conversations, outputs, leak_mixed_unicode_field, "arm or execution label leaked")
    _expect_rejected(builder, conversations, outputs, drift_preregistered_model_hash, "differs from preregistered execution authority")
    return 13


def run_visible_label_variant_checks(builder: Any) -> int:
    rng = random.Random(20260902)
    rejected = 0
    separators = ("-", "_", ".", "/", " ", "：")
    for label in sorted(builder.FORBIDDEN_VISIBLE_KEYS):
        alphanumeric = "".join(character for character in label if character.isalnum())
        random_case = "".join(
            character.upper() if character.isalpha() and rng.randrange(2) else character.lower()
            for character in alphanumeric
        )
        separated = "".join(
            character + (separators[index % len(separators)] if index + 1 < len(alphanumeric) else "")
            for index, character in enumerate(random_case)
        )
        fullwidth = "".join(
            chr(ord(character) + 0xFEE0) if "!" <= character <= "~" else character
            for character in random_case
        )
        for variant in {label, label.upper(), random_case, separated, fullwidth}:
            try:
                builder._reject_visible_arm_leakage({variant: "redacted"}, set())
            except ValueError:
                rejected += 1
            else:
                raise AssertionError(f"sensitive reviewer-visible label variant was accepted: {variant!r}")
    assert rejected >= len(builder.FORBIDDEN_VISIBLE_KEYS) * 4
    return rejected


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


def _verify_legacy_unchanged(path: Path, expected: dict[str, str], label: str) -> int:
    actual = {
        item.relative_to(path).as_posix(): sha_bytes(item.read_bytes())
        for item in path.rglob("*")
        if item.is_file() and "__pycache__" not in item.parts and item.suffix != ".pyc"
    }
    assert actual == expected, f"legacy {label} package changed after v4 repair began"
    return len(actual)


def verify_all() -> dict[str, Any]:
    conversation_validator = schema_validator("conversation.schema.json")
    paired_validator = schema_validator("paired_output.schema.json")
    receipt_validator = schema_validator("session_source_receipt.schema.json")
    execution_receipt_validator = schema_validator("execution_receipt.schema.json")
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
        assert len(receipt["turnReceipts"]) == len(turns)
        evidence_path = ROOT / receipt["authorshipEvidenceFile"]["path"]
        assert evidence_path.exists(), f"missing authorship evidence: {evidence_path}"
        assert sha_bytes(evidence_path.read_bytes()) == receipt["authorshipEvidenceFile"]["sha256"]
        _verify_authorship_locator(receipt["authorshipEvidenceLocator"], evidence_path)

        turn_receipt_map = {row["turnId"]: row for row in receipt["turnReceipts"]}
        assert set(turn_receipt_map) == {turn["turnId"] for turn in turns}
        for turn in turns:
            assert turn["turnId"].startswith(conversation["conversationId"] + "-t")
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
            turn_receipt = turn_receipt_map[turn["turnId"]]
            for field in ("semanticTurn", "sourceOrdinal", "messageSha256", "sourceRecordSha256", "sourceRequestFingerprint"):
                assert turn_receipt[field] == turn[field], f"turn receipt mismatch: {turn['turnId']}:{field}"
            expected_db_locator = (
                ".runtime/used-phone-demo-439/web-query-intake.sqlite3"
                f"#table=web_query_intake&canonicalRowSha256={turn['sourceRecordSha256']}"
            )
            assert turn_receipt["databaseCanonicalLocator"] == expected_db_locator
            turn_binding_payload = {
                "conversationId": conversation["conversationId"],
                **{key: value for key, value in turn_receipt.items() if key != "turnSourceBindingSha256"},
            }
            assert turn_receipt["turnSourceBindingSha256"] == sha_text(canonical(turn_binding_payload))
            turn_evidence_path = ROOT / turn_receipt["authorshipEvidenceLocator"].split("#", 1)[0]
            assert turn_evidence_path == evidence_path
            _verify_authorship_locator(turn_receipt["authorshipEvidenceLocator"], turn_evidence_path, turn["rawUserText"])
            turn_ids.append(turn["turnId"])
            texts.append(turn["rawUserText"])
            record_hashes.append(turn["sourceRecordSha256"])
            request_fingerprints.append(turn["sourceRequestFingerprint"])
        session_fingerprints.append(conversation["sourceSessionFingerprint"])

    assert len(turn_ids) == len(set(turn_ids)) == 21
    assert len(set(texts)) == 20
    assert len(set(record_hashes)) == 21
    assert len(set(request_fingerprints)) == 21
    assert len(set(session_fingerprints)) == 8
    assert sum(len(row["turns"]) - 1 for row in conversations) == 13
    c008 = conversations[-1]
    assert [(turn["turnId"], turn["semanticTurn"], turn["sourceOrdinal"]) for turn in c008["turns"]] == [
        ("rumr-v1-c008-t07", 1, 7),
        ("rumr-v1-c008-t08", 2, 9),
    ]
    removed_c008_hashes = {
        "a7e26839821ec80f6ced1c94aaed7c3ee1b4e745dea1bdf8eb42dbd9643686a8",
        "9f9fcd7b6d7e81da27dce256f9e682b363013cd611d45e809f9ea8cbf41433e9",
        "f52c7140e7a83635ce4094a52b171546c1bdb64003c02a042dee2bc27865337a",
        "f6ff7fdcbcc1c09088864da1505ee83316c5b631c5e81b25172bcd34bebb643b",
        "38aeecb2fad182b12815d3b0d6ed60f1057347b9524d2d00f3fe02e12a68e21e",
        "78cf177834cc33a53df82cd22deed2c557e96805318dfc4ebd323ed70a223a77",
    }
    assert removed_c008_hashes.isdisjoint(record_hashes)

    source_files = load_json(PACKAGE / "source_files.json")
    for item in source_files["files"]:
        path = ROOT / item["path"]
        assert path.exists(), f"missing source evidence: {item['path']}"
        assert sha_bytes(path.read_bytes()) == item["sha256"], f"source hash mismatch: {item['path']}"

    audit = load_json(PACKAGE / "source_dedup_leakage_audit.json")
    assert audit["counts"]["conversationCount"] == 8
    assert audit["counts"]["rawUserTurnCount"] == 21
    assert audit["counts"]["uniqueExactUserTextCount"] == 20
    assert audit["counts"]["followupTurnCount"] == 13
    assert audit["counts"]["unseenConversationCount"] == 0
    assert {row["turnId"] for row in audit["excludedInsufficientAuthorshipEvidenceTurns"]} == {
        "rumr-v1-c008-t01", "rumr-v1-c008-t02", "rumr-v1-c008-t03",
        "rumr-v1-c008-t04", "rumr-v1-c008-t05", "rumr-v1-c008-t06",
    }
    assert audit["sessionGrouping"]["splitUnit"] == "conversation"
    assert audit["boundaries"]["isOnlineHumanTest"] is False
    assert audit["boundaries"]["isUnseenEvaluation"] is False

    replay = load_json(PACKAGE / "replay_contract.json")
    assert replay["schemaVersion"] == "real-user-multiturn-ab-replay-contract-v4"
    assert replay["splitPolicy"]["splitTurnsAcrossPartitions"] is False
    assert replay["counts"]["pairedScoredFollowupTurnCount"] == 13
    assert replay["mechanicalGates"]["allFortyTwoArmTurnsExecutionBound"] is True
    assert replay["mechanicalGates"]["allThirteenFollowupsHaveBothArms"] is True
    assert replay["mechanicalGates"]["allTraceCompositeBindingsValidate"] is True
    assert replay["mechanicalGates"]["allAssistantHistoryRecursivelySameArmBound"] is True
    assert replay["mechanicalGates"]["allRowsEqualPreregisteredExecutionAuthorityHashes"] is True
    assert replay["mechanicalGates"]["runnerOwnedExecutionReceiptRequired"] is True

    authority = load_json(EXECUTION_AUTHORITY)
    assert authority["status"] == "PREREGISTERED_BEFORE_FORMAL_EXECUTION"
    assert authority["formalRunnerReceiptContract"]["required"] is True
    assert authority["threatModel"]["formalExecutionWitnessRequired"] is True
    assert "cryptographic tamper resistance" in authority["threatModel"]["doesNotClaim"][0]

    reference = load_json(PACKAGE / "reference_context_cases.json")
    assert len(reference["cases"]) == 7
    assert all(row["turnId"] in turn_ids for row in reference["cases"])
    multi_agent = load_json(PACKAGE / "multi_agent_evaluation_contract.json")
    assert all(turn_id in turn_ids for turn_id in multi_agent["candidateTurnIds"])

    dummy = dummy_outputs(conversations)
    runner_receipt = dummy_runner_receipt(dummy)
    assert len(dummy) == 42
    for index, row in enumerate(dummy, 1):
        assert_schema(paired_validator, row, f"paired output {index}")
    assert_schema(execution_receipt_validator, runner_receipt, "runner execution receipt fixture")
    builder = load_module("blind_packet_builder.py", "rumr_v4_blind_builder")
    built_a = builder.build_packets(dummy, conversations, runner_receipt, 20260902)
    built_b = builder.build_packets(dummy, conversations, runner_receipt, 20260902)
    assert canonical(built_a) == canonical(built_b)
    assert len(built_a["judge01"]) == len(built_a["judge02"]) == 13
    assert len(built_a["mapping"]) == 13
    public_rows = built_a["judge01"] + built_a["judge02"]
    assert all("RAW_FULL_CONTROL" not in canonical(row) and "CONTEXT_TREATMENT" not in canonical(row) for row in public_rows)
    adversarial_checks = run_adversarial_checks(builder, conversations, dummy)
    unicode_label_variants_rejected = run_visible_label_variant_checks(builder)

    schedule_builder = load_module("build_replay_schedule.py", "rumr_v4_schedule_verify")
    schedule_a = schedule_builder.build_schedule(conversations, 20260902)
    schedule_b = schedule_builder.build_schedule(conversations, 20260902)
    assert canonical(schedule_a) == canonical(schedule_b)
    assert len(schedule_a) == 42
    assert len({row["scheduleRowSha256"] for row in schedule_a}) == 42
    assert all(row["scheduleRowSha256"] == schedule_builder.schedule_binding(row) for row in schedule_a)
    assert sum(bool(row["scoredFollowup"]) for row in schedule_a) == 26
    assert all(row["automaticRetries"] == 0 for row in schedule_a)

    judge_example = {
        "schemaVersion": "real-user-multiturn-ai-judge-response-v4",
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
    assert manifest["dataset"]["removedInsufficientAuthorshipEvidenceTurnCount"] == 6
    assert manifest["executionAuthority"] == {
        "path": "execution_authority.json",
        "configSnapshot": "execution_config_snapshot.json",
        "runnerOwnedReceiptSchema": "execution_receipt.schema.json",
        "expectedTraceHashCount": 5,
        "formalRunnerReceiptRequired": True,
    }
    assert manifest["checksumInventory"]["expectedFileCountExcludingSelf"] == 22
    assert manifest["execution"] == {
        "modelCalls": 0,
        "agentRuns": 0,
        "formalAbRuns": 0,
        "formalAiJudgeRuns": 0,
        "formalHumanReviews": 0,
        "productionDefaultsChanged": False,
    }

    v1_files_verified = _verify_legacy_unchanged(LEGACY_V1, V1_BASELINE_HASHES, "v1")
    v2_files_verified = _verify_legacy_unchanged(LEGACY_V2, V2_BASELINE_HASHES, "v2")
    v3_files_verified = _verify_legacy_unchanged(LEGACY_V3, V3_BASELINE_HASHES, "v3")
    checksum_files_verified = _verify_checksum_inventory()
    return {
        "status": "PASS",
        "conversationCount": 8,
        "sessionSourceReceiptsVerified": 8,
        "rawUserTurnCount": 21,
        "uniqueExactUserTextCount": 20,
        "pairedFollowupCount": 13,
        "sourceRowsVerified": 21,
        "perTurnSourceLocatorsVerified": 21,
        "sourceEvidenceFilesVerified": len(source_files["files"]),
        "jsonSchemasChecked": 5,
        "jsonInstancesValidated": 8 + 8 + 42 + 1 + 1,
        "checksumInventoryFilesVerified": checksum_files_verified,
        "legacyV1FilesUnchanged": v1_files_verified,
        "legacyV2FilesUnchanged": v2_files_verified,
        "legacyV3FilesUnchanged": v3_files_verified,
        "blindItemsPerJudgeDryRun": 13,
        "scheduledArmTurnsDryRun": 42,
        "adversarialFailClosedChecks": adversarial_checks,
        "unicodeFieldLabelVariantsRejected": unicode_label_variants_rejected,
        "runnerOwnedReceiptValidated": True,
        "preregisteredExecutionAuthorityHashesValidated": 5,
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
