"""Fail-closed intake preparation for the next real used-phone Query batch."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "used-phone-real-query-batch-v2"
_PHONE_TERMS = (
    "手机", "二手", "iphone", "ios", "安卓", "android", "苹果", "华为",
    "荣耀", "oppo", "vivo", "小米", "红米", "三星", "一加", "真我",
    "iqoo", "魅族", "努比亚", "红魔",
)
_FOLLOWUP_TERMS = (
    "苹果", "华为", "荣耀", "oppo", "vivo", "小米", "红米", "三星",
    "一加", "真我", "iqoo", "便宜", "预算", "价格", "原装", "电池",
    "屏幕", "拍照", "游戏", "第一个", "第二个", "前两个", "这两个",
)
_AUTOMATED_SESSION_PREFIXES = (
    "human-e2e-", "intake-cold-verify-", "pytest-", "test-", "debug-",
)


def _normalized_message(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", value, flags=re.UNICODE).casefold()


def prepare_real_query_candidates(
    records: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Select only intact human web queries; never infer corrupted text."""
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_messages: set[str] = set()
    phone_context_sessions: set[str] = set()
    total = 0
    for record in records:
        total += 1
        request_id = str(record.get("request_id") or "")
        session_id = str(record.get("session_id") or "")
        message = str(record.get("message") or "")
        message_sha = str(record.get("message_sha256") or "") or hashlib.sha256(
            message.encode("utf-8")
        ).hexdigest()
        reasons: list[str] = []
        if session_id.casefold().startswith(_AUTOMATED_SESSION_PREFIXES):
            reasons.append("automated_or_debug_session")
        if record.get("mode") != "chat":
            reasons.append("not_chat_mode")
        if record.get("outcome") != "completed":
            reasons.append("not_completed")
        if bool(record.get("redaction_applied")):
            reasons.append("redacted_content_not_verbatim")
        if not message.strip():
            reasons.append("empty_message")
        if "\ufffd" in message:
            reasons.append("unicode_replacement_character_irrecoverable")
        normalized = _normalized_message(message)
        direct_phone_signal = any(term in normalized for term in _PHONE_TERMS)
        contextual_followup = (
            session_id in phone_context_sessions
            and any(term in normalized for term in _FOLLOWUP_TERMS)
        )
        if not direct_phone_signal and not contextual_followup:
            reasons.append("not_deterministically_phone_related")
        stable_message_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if normalized and stable_message_id in seen_messages:
            reasons.append("duplicate_normalized_message")
        if reasons:
            rejected.append({
                "requestId": request_id,
                "sessionId": session_id,
                "turnNumber": int(record.get("turn_number") or 0),
                "messageSha256": message_sha,
                "reasons": sorted(set(reasons)),
            })
            continue
        seen_messages.add(stable_message_id)
        phone_context_sessions.add(session_id)
        accepted.append({
            "schemaVersion": SCHEMA_VERSION,
            "candidateQueryId": f"uphqv2-intake-{len(accepted) + 1:03d}",
            "rawQuery": message,
            "source": {
                "type": "web_query_intake",
                "humanAuthoredConfirmed": False,
                "humanAuthoredStatus": "pending_project_owner_confirmation",
                "requestId": request_id,
                "sessionId": session_id,
                "turnNumber": int(record.get("turn_number") or 0),
                "submittedAt": str(record.get("submitted_at") or ""),
                "messageSha256": message_sha,
            },
            "relation": "multi_turn_followup" if contextual_followup else "single_turn",
            "status": "PENDING_HUMAN_SELECTION_NOT_QREL",
            "answerDerivedStatus": "pending_project_owner_confirmation",
            "labelAccessed": False,
        })
    return {
        "schemaVersion": SCHEMA_VERSION,
        "status": "PENDING_HUMAN_SELECTION_NOT_QREL",
        "recordCount": total,
        "acceptedCandidateCount": len(accepted),
        "rejectedRecordCount": len(rejected),
        "corruptedTextWasReconstructed": False,
        "acceptedCandidates": accepted,
        "rejectedRecords": rejected,
    }


def read_intake_records_read_only(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT request_id, session_id, turn_number, submitted_at, mode, "
            "outcome, message, message_sha256, redaction_applied, failure_code "
            "FROM web_query_intake ORDER BY submitted_at, request_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
