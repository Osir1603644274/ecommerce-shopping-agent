from __future__ import annotations

from agent.evaluation.used_phone_real_query_batch_v2 import prepare_real_query_candidates


def _record(request_id: str, message: str, *, session: str = "s1", turn: int = 1) -> dict:
    return {
        "request_id": request_id,
        "session_id": session,
        "turn_number": turn,
        "submitted_at": "2026-08-17T00:00:00+00:00",
        "mode": "chat",
        "outcome": "completed",
        "message": message,
        "message_sha256": request_id.rjust(64, "0"),
        "redaction_applied": 0,
        "failure_code": None,
    }


def test_real_query_batch_accepts_intact_phone_query_and_contextual_followup() -> None:
    audit = prepare_real_query_candidates([
        _record("1", "有没有适合打游戏的手机"),
        _record("2", "我要华为的", turn=2),
    ])

    assert audit["acceptedCandidateCount"] == 2
    assert audit["rejectedRecordCount"] == 0
    assert audit["acceptedCandidates"][0]["relation"] == "single_turn"
    assert audit["acceptedCandidates"][1]["relation"] == "multi_turn_followup"
    assert all(
        item["source"]["humanAuthoredStatus"]
        == "pending_project_owner_confirmation"
        for item in audit["acceptedCandidates"]
    )
    assert all(
        item["answerDerivedStatus"] == "pending_project_owner_confirmation"
        for item in audit["acceptedCandidates"]
    )


def test_real_query_batch_rejects_corruption_greetings_duplicates_and_redaction() -> None:
    rows = [
        _record("1", "��û���ʺϴ���Ϸ���ֻ�"),
        _record("2", "你好呀", session="s2"),
        _record("3", "安卓二手机百元", session="s3"),
        _record("4", "安卓 二手机 百元", session="s4"),
        {**_record("5", "苹果手机 token=[REDACTED_SECRET]", session="s5"), "redaction_applied": 1},
        _record("6", "有没有适合打游戏的手机", session="human-e2e-replay"),
    ]
    audit = prepare_real_query_candidates(rows)

    assert audit["acceptedCandidateCount"] == 1
    assert audit["rejectedRecordCount"] == 5
    reasons = {item["requestId"]: item["reasons"] for item in audit["rejectedRecords"]}
    assert "unicode_replacement_character_irrecoverable" in reasons["1"]
    assert "not_deterministically_phone_related" in reasons["2"]
    assert "duplicate_normalized_message" in reasons["4"]
    assert "redacted_content_not_verbatim" in reasons["5"]
    assert "automated_or_debug_session" in reasons["6"]
