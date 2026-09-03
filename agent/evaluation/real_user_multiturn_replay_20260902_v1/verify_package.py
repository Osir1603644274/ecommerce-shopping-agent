"""Deterministic, no-model verification for the real-user replay package."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


PACKAGE = Path(__file__).resolve().parent
ROOT = PACKAGE.parents[2]
DATASET = PACKAGE / "conversations.jsonl"
DB = ROOT / ".runtime/used-phone-demo-439/web-query-intake.sqlite3"
ROW_COLUMNS = (
    "request_id", "session_id", "turn_number", "submitted_at", "updated_at",
    "route", "mode", "outcome", "message", "message_sha256",
    "redaction_applied", "failure_code",
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DIRECT_IDENTIFIER_PATTERNS = (
    re.compile(r"req-[0-9a-f]{8,}", re.I),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"),
    re.compile(r"https?://", re.I),
)


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_text(value: str) -> str:
    return sha_bytes(value.encode("utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            value = json.loads(line)
            assert isinstance(value, dict), f"{path}:{number}: expected object"
            assert line == canonical(value), f"{path}:{number}: JSONL is not canonical"
            rows.append(value)
    return rows


def load_blind_builder():
    path = PACKAGE / "blind_packet_builder.py"
    spec = importlib.util.spec_from_file_location("rumr_blind_builder", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_schedule_builder():
    path = PACKAGE / "build_replay_schedule.py"
    spec = importlib.util.spec_from_file_location("rumr_schedule_builder", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def dummy_outputs(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for conversation in conversations:
        for turn_index, turn in enumerate(conversation["turns"][1:], 1):
            for arm in ("RAW_FULL_CONTROL", "CONTEXT_TREATMENT"):
                answer = f"{conversation['conversationId']}|{turn['turnId']}|candidate-{1 if arm == 'RAW_FULL_CONTROL' else 2}"
                dialogue = []
                for history_index, history_turn in enumerate(conversation["turns"][: turn_index + 1]):
                    dialogue.append({"role": "user", "content": history_turn["rawUserText"]})
                    history_answer = (
                        answer
                        if history_index == turn_index
                        else f"{conversation['conversationId']}|{history_turn['turnId']}|candidate-{1 if arm == 'RAW_FULL_CONTROL' else 2}"
                    )
                    dialogue.append({"role": "assistant", "content": history_answer})
                rows.append(
                    {
                        "schemaVersion": "real-user-multiturn-paired-output-v1",
                        "conversationId": conversation["conversationId"],
                        "turnId": turn["turnId"],
                        "semanticTurn": turn["semanticTurn"],
                        "arm": arm,
                        "status": "SUCCEEDED",
                        "dialogue": dialogue,
                        "dialogueSha256": sha_text(canonical(dialogue)),
                        "finalAnswer": answer,
                        "finalAnswerSha256": sha_text(answer),
                        "publicEvidence": [],
                    }
                )
    return rows


def verify_all() -> dict[str, Any]:
    conversations = load_jsonl(DATASET)
    assert len(conversations) == 8
    ids = [row["conversationId"] for row in conversations]
    assert ids == [f"rumr-v1-c{i:03d}" for i in range(1, 9)]
    assert len(set(ids)) == len(ids)

    turn_ids: list[str] = []
    texts: list[str] = []
    record_hashes: list[str] = []
    request_fingerprints: list[str] = []
    session_fingerprints: list[str] = []
    source_rows = source_row_index()
    for conversation in conversations:
        assert conversation["schemaVersion"] == "real-user-multiturn-conversation-v1"
        assert conversation["humanAuthorship"] == "PROJECT_OWNER_REAL_WEB_CONFIRMED"
        assert conversation["sameSession"] is True
        assert HEX64.fullmatch(conversation["sourceSessionFingerprint"])
        session_fingerprints.append(conversation["sourceSessionFingerprint"])
        turns = conversation["turns"]
        assert len(turns) >= 2
        assert [turn["semanticTurn"] for turn in turns] == list(range(1, len(turns) + 1))
        assert [turn["sourceOrdinal"] for turn in turns] == sorted(turn["sourceOrdinal"] for turn in turns)
        for turn in turns:
            assert turn["turnId"] == f"{conversation['conversationId']}-t{turn['semanticTurn']:02d}"
            assert turn["sourceOutcome"] == "completed"
            assert sha_text(turn["rawUserText"]) == turn["messageSha256"]
            assert all(not pattern.search(turn["rawUserText"]) for pattern in DIRECT_IDENTIFIER_PATTERNS)
            assert HEX64.fullmatch(turn["sourceRecordSha256"])
            assert HEX64.fullmatch(turn["sourceRequestFingerprint"])
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

    assert len(turn_ids) == 27 and len(set(turn_ids)) == 27
    assert len(set(texts)) == 26
    duplicate_clusters = {text: ids for text, ids in _group_texts(conversations).items() if len(ids) > 1}
    assert duplicate_clusters == {
        "预算1200左右，希望华为手机": ["rumr-v1-c003-t01", "rumr-v1-c004-t01"]
    }
    assert len(set(record_hashes)) == 27
    assert len(set(request_fingerprints)) == 27
    assert len(set(session_fingerprints)) == 8
    assert sum(len(row["turns"]) - 1 for row in conversations) == 19

    dataset_text = DATASET.read_text(encoding="utf-8")
    assert not re.search(r"req-[0-9a-f]{8,}", dataset_text, re.I)
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", dataset_text, re.I)

    source_files = load_json(PACKAGE / "source_files.json")
    for item in source_files["files"]:
        path = ROOT / item["path"]
        assert path.exists(), f"missing source evidence: {item['path']}"
        assert sha_bytes(path.read_bytes()) == item["sha256"], f"source hash mismatch: {item['path']}"

    audit = load_json(PACKAGE / "source_dedup_leakage_audit.json")
    assert audit["counts"] == {
        "conversationCount": 8,
        "rawUserTurnCount": 27,
        "uniqueExactUserTextCount": 26,
        "followupTurnCount": 19,
        "unseenConversationCount": 0,
        "sealedConversationCount": 0,
    }
    assert audit["sessionGrouping"]["splitUnit"] == "conversation"
    assert audit["sessionGrouping"]["singleTurnRandomSplitAllowed"] is False
    assert audit["boundaries"]["isOnlineHumanTest"] is False
    assert audit["boundaries"]["isUnseenEvaluation"] is False

    replay = load_json(PACKAGE / "replay_contract.json")
    assert replay["splitPolicy"]["unit"] == "conversation"
    assert replay["splitPolicy"]["splitTurnsAcrossPartitions"] is False
    assert replay["counts"]["pairedScoredFollowupTurnCount"] == 19
    assert replay["judgment"]["reviewerType"] == "independent_ai_judge"
    assert replay["judgment"]["humanReviewerClaimAllowed"] is False

    reference = load_json(PACKAGE / "reference_context_cases.json")
    assert all(row["turnId"] in turn_ids for row in reference["cases"])
    assert all(not row["turnId"].endswith("t01") for row in reference["cases"])
    multi_agent = load_json(PACKAGE / "multi_agent_evaluation_contract.json")
    assert all(turn_id in turn_ids for turn_id in multi_agent["candidateTurnIds"])
    assert multi_agent["review"]["humanClaimAllowed"] is False

    builder = load_blind_builder()
    dummy = dummy_outputs(conversations)
    built_a = builder.build_packets(dummy, conversations, 20260902)
    built_b = builder.build_packets(dummy, conversations, 20260902)
    assert canonical(built_a) == canonical(built_b)
    assert len(built_a["judge01"]) == len(built_a["judge02"]) == 19
    assert len(built_a["mapping"]) == 19
    assert all("RAW_FULL_CONTROL" not in canonical(row) and "CONTEXT_TREATMENT" not in canonical(row) for row in built_a["judge01"] + built_a["judge02"])

    schedule_builder = load_schedule_builder()
    schedule_a = schedule_builder.build_schedule(conversations, 20260902)
    schedule_b = schedule_builder.build_schedule(conversations, 20260902)
    assert canonical(schedule_a) == canonical(schedule_b)
    assert len(schedule_a) == 54
    assert sum(bool(row["scoredFollowup"]) for row in schedule_a) == 38
    assert all(row["automaticRetries"] == 0 for row in schedule_a)

    manifest = load_json(PACKAGE / "package_manifest.json")
    assert manifest["status"] == "FROZEN_PRE_EXECUTION_NO_MODEL_OR_JUDGE_RUN"
    assert manifest["dataset"]["sha256"] == sha_bytes(DATASET.read_bytes())
    for relative, expected in manifest["packageFiles"].items():
        assert sha_bytes((PACKAGE / relative).read_bytes()) == expected, f"package hash mismatch: {relative}"

    checksum_lines = (PACKAGE / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines()
    assert checksum_lines
    for line in checksum_lines:
        expected, relative = line.split("  ", 1)
        assert sha_bytes((PACKAGE / relative).read_bytes()) == expected, f"checksum inventory mismatch: {relative}"

    return {
        "status": "PASS",
        "conversationCount": 8,
        "rawUserTurnCount": 27,
        "uniqueExactUserTextCount": 26,
        "pairedFollowupCount": 19,
        "sourceRowsVerified": 27,
        "sourceEvidenceFilesVerified": len(source_files["files"]),
        "checksumInventoryFilesVerified": len(checksum_lines),
        "directIdentifierMatches": 0,
        "blindItemsPerJudgeDryRun": 19,
        "scheduledArmTurnsDryRun": 54,
        "modelCalls": 0,
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
