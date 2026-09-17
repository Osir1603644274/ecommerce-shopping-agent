import asyncio
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app, _fast_observation_fields
from app.web_query_intake import (
    WebQueryIntakeStore,
    capture_health,
    capture_web_query,
    capture_web_query_diagnostic,
    capture_web_query_observation,
    decode_observation_list_field,
    mark_web_query_outcome,
    redact_explicit_secrets,
    set_web_query_intake_store,
)


def test_store_persists_ordered_turns_and_deduplicates_request_id():
    with TemporaryDirectory() as directory:
        store = WebQueryIntakeStore(Path(directory) / "queries.sqlite3")
        first = store.record_submission(
            request_id="req-1",
            session_id="session-a",
            message="有没有适合打游戏的手机",
            route="/agent/chat-llm/stream",
            mode="chat",
        )
        duplicate = store.record_submission(
            request_id="req-1",
            session_id="session-a",
            message="不会覆盖原记录",
            route="/agent/chat-llm/stream",
            mode="chat",
        )
        second = store.record_submission(
            request_id="req-2",
            session_id="session-a",
            message="我要华为的",
            route="/agent/debug-turns",
            mode="step_debug",
        )

        assert first.turn_number == 1
        assert duplicate.message == first.message
        assert second.turn_number == 2
        assert store.mark_outcome("req-1", "completed") is True
        rows = store.list_records(session_id="session-a")
        assert [item.request_id for item in rows] == ["req-1", "req-2"]
        assert rows[0].outcome == "completed"
        assert rows[1].outcome == "pending"


def test_store_redacts_credentials_but_keeps_query_hash_and_delete_control():
    with TemporaryDirectory() as directory:
        store = WebQueryIntakeStore(Path(directory) / "queries.sqlite3")
        raw = "帮我测试 api_key=secret-value-123 和 sk-abcdefghijklmnop"
        record = store.record_submission(
            request_id="req-secret",
            session_id="session-secret",
            message=raw,
            route="/agent/chat-llm/stream",
            mode="chat",
        )

        assert record.redaction_applied is True
        assert "secret-value-123" not in record.message
        assert "sk-abcdefghijklmnop" not in record.message
        assert len(record.message_sha256) == 64
        assert store.delete_session("session-secret") == 1
        assert store.list_records(session_id="session-secret") == []


def test_concurrent_session_writes_receive_unique_ordered_turn_numbers():
    with TemporaryDirectory() as directory:
        store = WebQueryIntakeStore(Path(directory) / "queries.sqlite3")

        def write(index: int):
            return store.record_submission(
                request_id=f"req-concurrent-{index}",
                session_id="session-concurrent",
                message=f"询问 {index}",
                route="/agent/chat-llm/stream",
                mode="chat",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            records = list(executor.map(write, range(20)))

        assert sorted(record.turn_number for record in records) == list(range(1, 21))
        stored = store.list_records(session_id="session-concurrent")
        assert [record.turn_number for record in stored] == list(range(1, 21))


def test_bounded_capture_failure_does_not_raise_into_agent_path():
    class BrokenStore:
        def record_submission(self, **_kwargs):
            raise RuntimeError("database unavailable")

        def mark_outcome(self, *_args, **_kwargs):
            raise RuntimeError("database unavailable")

    before = capture_health()["writeFailures"]
    set_web_query_intake_store(BrokenStore())
    try:
        with patch("app.web_query_intake.settings.web_query_intake_enabled", True):
            result = asyncio.run(capture_web_query(
                request_id="req-failed",
                session_id="session-failed",
                message="仍然应该继续回答",
                route="/agent/chat-llm/stream",
                mode="chat",
            ))
            updated = asyncio.run(mark_web_query_outcome("req-failed", "failed"))
        assert result is None
        assert updated is False
        assert capture_health()["writeFailures"] >= before + 2
    finally:
        set_web_query_intake_store(None)


def test_redaction_leaves_normal_shopping_query_unchanged():
    query = "有没有适合打游戏的手机，最好是华为"
    assert redact_explicit_secrets(query) == (query, False)


def test_gated_export_and_session_delete_api():
    with TemporaryDirectory() as directory:
        store = WebQueryIntakeStore(Path(directory) / "queries.sqlite3")
        store.record_submission(
            request_id="req-api-1",
            session_id="session-api",
            message="有没有适合打游戏的手机",
            route="/agent/chat-llm/stream",
            mode="chat",
        )
        set_web_query_intake_store(store)
        client = TestClient(app)
        try:
            with (
                patch("app.agent_trace.settings.agent_trace_debug_enabled", True),
                patch("app.agent_trace.settings.agent_trace_debug_key", "test-key"),
            ):
                denied = client.get("/internal/debug/web-query-intake")
                exported = client.get(
                    "/internal/debug/web-query-intake?sessionId=session-api",
                    headers={"X-Agent-Debug-Key": "test-key"},
                )
                deleted = client.delete(
                    "/internal/debug/web-query-intake/sessions/session-api",
                    headers={"X-Agent-Debug-Key": "test-key"},
                )

            assert denied.status_code == 403
            assert exported.status_code == 200
            assert exported.json()["records"][0]["message"] == "有没有适合打游戏的手机"
            assert deleted.json() == {
                "sessionId": "session-api", "deletedRecords": 1,
            }
            assert store.list_records(session_id="session-api") == []
        finally:
            set_web_query_intake_store(None)


def test_streaming_web_route_captures_even_when_agent_request_fails_early():
    with TemporaryDirectory() as directory:
        store = WebQueryIntakeStore(Path(directory) / "queries.sqlite3")
        set_web_query_intake_store(store)
        client = TestClient(app)
        try:
            with (
                patch("app.web_query_intake.settings.web_query_intake_enabled", True),
                patch("app.main.settings.deepseek_api_key", ""),
            ):
                response = client.post(
                    "/agent/chat-llm/stream",
                    json={
                        "message": "你好呀",
                        "sessionId": "session-stream-intake",
                        "domainHint": "ecommerce",
                    },
                )

            assert response.status_code == 200
            rows = store.list_records(session_id="session-stream-intake")
            assert len(rows) == 1
            assert rows[0].message == "你好呀"
            assert rows[0].route == "/agent/chat-llm/stream"
            assert rows[0].mode == "chat"
            assert rows[0].outcome == "failed"
            assert rows[0].failure_code == "missing_api_key"
            diagnostics = store.read_diagnostics(limit=1)
            assert len(diagnostics) == 1
            assert diagnostics[0]["request_id"] == rows[0].request_id
            assert diagnostics[0]["session_id"] == "session-stream-intake"
            assert diagnostics[0]["payload"]["answer"].startswith(
                "还没有配置 DeepSeek API Key"
            )
            assert diagnostics[0]["payload"]["trace"]["status"] == "missing_api_key"
        finally:
            set_web_query_intake_store(None)


def test_observation_record_round_trips_with_upsert_idempotence():
    with TemporaryDirectory() as directory:
        store = WebQueryIntakeStore(Path(directory) / "queries.sqlite3")
        fields = {
            "task_id": "task-preview-1",
            "session_id": "session-obs",
            "route": "/agent/chat-llm/stream",
            "extraction_route": "deterministic_complete",
            "extraction_reason": "complete_controlled_coverage",
            "mentioned_keys": ["screen_originality"],
            "covered_keys": ["screen_originality"],
            "uncovered_keys": [],
            "task_manager_model_calls": 0,
            "task_manager_duration_ms": 0.0,
            "task_state_model_calls": 0,
            "task_state_duration_ms": 0.0,
            "preview_eligible": True,
            "preview_status": "provisional",
            "preview_cache_status": "cold",
            "preview_duration_ms": 42.5,
            "preview_candidate_ids": [101, 102, 103],
            "final_duration_ms": 3100.25,
            "final_candidate_ids": [102, 104],
            "preview_final_removed": [101, 103],
            "preview_final_added": [104],
            "preview_final_reordered": True,
        }
        assert store.record_observation("req-obs", **fields) is True

        rows = store.read_observations(request_id="req-obs")
        assert len(rows) == 1
        row = rows[0]
        assert row["request_id"] == "req-obs"
        assert row["task_id"] == "task-preview-1"
        assert row["session_id"] == "session-obs"
        assert row["extraction_route"] == "deterministic_complete"
        assert row["preview_status"] == "provisional"
        assert row["preview_cache_status"] == "cold"
        assert row["preview_eligible"] == 1
        assert row["preview_duration_ms"] == 42.5
        assert row["preview_candidate_ids"] == "[101,102,103]"
        assert row["final_candidate_ids"] == "[102,104]"
        assert row["preview_final_reordered"] == 1
        assert row["task_manager_model_calls"] == 0

        # Upsert overwrites by request_id, never duplicates.
        assert store.record_observation(
            "req-obs", preview_eligible=False, final_candidate_ids=[102]
        ) is True
        rows = store.read_observations(request_id="req-obs")
        assert len(rows) == 1
        assert rows[0]["preview_eligible"] == 0
        assert rows[0]["final_candidate_ids"] == "[102]"


def test_diagnostic_capture_is_server_owned_redacted_and_latest_first():
    with TemporaryDirectory() as directory:
        store = WebQueryIntakeStore(Path(directory) / "queries.sqlite3")
        store.record_submission(
            request_id="req-diag-1",
            session_id="session-diag",
            message="第一轮",
            route="/agent/chat-llm/stream",
            mode="chat",
        )
        assert store.record_diagnostic(
            "req-diag-1",
            session_id="session-diag",
            task_id="task-diag-1",
            route="/agent/chat-llm/stream",
            payload={
                "answer": "内部 token=secret-value-123 已隐藏",
                "taskState": {"revision": 7},
                "tool_trace": [{"tool": "search_products"}],
            },
        ) is True

        rows = store.read_diagnostics(limit=1)
        assert len(rows) == 1
        assert rows[0]["request_id"] == "req-diag-1"
        assert rows[0]["task_id"] == "task-diag-1"
        assert rows[0]["payload"]["taskState"]["revision"] == 7
        assert rows[0]["payload"]["tool_trace"][0]["tool"] == "search_products"
        assert "secret-value-123" not in rows[0]["payload"]["answer"]
        assert rows[0]["redaction_applied"] is True


def test_bounded_diagnostic_failure_is_a_noop_on_answer_path():
    class _BrokenStore:
        def record_diagnostic(self, request_id, **fields):
            raise RuntimeError("diagnostic backend unavailable")

    set_web_query_intake_store(_BrokenStore())
    try:
        with patch("app.web_query_intake.settings.web_query_intake_enabled", True):
            result = asyncio.run(capture_web_query_diagnostic(
                "req-diag-failed",
                session_id="session-diag",
                task_id=None,
                route="/agent/chat-llm/stream",
                payload={"answer": "仍应正常返回"},
            ))
        assert result is False
    finally:
        set_web_query_intake_store(None)


def test_observation_migration_is_idempotent():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "queries.sqlite3"
        store = WebQueryIntakeStore(path)
        store.record_observation("req-1", final_candidate_ids=[1])
        # Reopening a new store over the same file must not re-create the table
        # or lose already-recorded rows.
        reopened = WebQueryIntakeStore(path)
        reopened.record_observation("req-2", final_candidate_ids=[2])

        rows = reopened.read_observations(limit=50)
        assert len(rows) == 2
        assert {row["request_id"] for row in rows} == {"req-1", "req-2"}


def test_observation_write_failure_is_a_noop_on_the_answer_path():
    directory = tempfile.mkdtemp()
    store = WebQueryIntakeStore(Path(directory) / "q.sqlite3")

    class _BrokenStore:
        def record_observation(self, request_id, **fields):
            raise RuntimeError("observation backend unavailable")

    set_web_query_intake_store(_BrokenStore())
    try:
        with patch("app.web_query_intake.settings.web_query_intake_enabled", True):
            result = asyncio.run(
                capture_web_query_observation("req-x", final_candidate_ids=[9])
            )
        assert result is False
    finally:
        set_web_query_intake_store(store)
        shutil.rmtree(directory, ignore_errors=True)


def test_observation_e2e_from_fast_fields_single_encoded_one_loads_to_list():
    """Repair 3 E2E: ``_fast_observation_fields`` passes raw lists; the store
    single-encodes them; reading a row back needs exactly one ``json.loads`` per
    array field to yield the list."""
    from app.domains.ecommerce.fast_response import FastAnalysis

    fast = FastAnalysis(
        status="full",
        route="deterministic_complete",
        reason="complete_controlled_coverage",
        mentioned_keys=["screen_originality", "brand"],
        covered_keys=["screen_originality", "brand"],
        uncovered_keys=[],
        requirements=[],
        use_cases=["camera_title_claim"],
        allow_task_manager_bypass=True,
        allow_task_state_bypass=True,
        allow_product_preview=True,
    )
    preview_event = {
        "type": "preview",
        "trace": {
            "previewCandidateIds": ["101", "102", "103"],
            "previewStatus": "provisional",
            "cacheStatus": "cold",
            "previewDurationMs": 42.5,
        },
    }
    fields = _fast_observation_fields(
        fast,
        {
            "modelCalls": {"task_manager": 0, "task_state": 0},
            "llmDurationMs": {"task_manager": 0.0, "task_state": 0.0},
        },
        task_id="task-obs-e2e",
        route="/agent/chat-llm/stream",
        session_id="session-obs-e2e",
        preview_event=preview_event,
        final_candidate_ids=["102", "104"],
        final_duration_ms=3100.25,
    )

    with TemporaryDirectory() as directory:
        store = WebQueryIntakeStore(Path(directory) / "queries.sqlite3")
        assert store.record_observation("req-obs-e2e", **fields) is True

        rows = store.read_observations(request_id="req-obs-e2e")
        assert len(rows) == 1
        row = rows[0]
        # One json.loads per array field -> the list, in order.
        assert json.loads(row["mentioned_keys"]) == ["screen_originality", "brand"]
        assert json.loads(row["covered_keys"]) == ["screen_originality", "brand"]
        assert json.loads(row["uncovered_keys"]) == []
        assert json.loads(row["preview_candidate_ids"]) == ["101", "102", "103"]
        assert json.loads(row["final_candidate_ids"]) == ["102", "104"]
        assert json.loads(row["preview_final_removed"]) == ["101", "103"]
        assert json.loads(row["preview_final_added"]) == ["104"]
        # The tolerant helper agrees on the single-encoded shape.
        assert decode_observation_list_field(row["mentioned_keys"]) == [
            "screen_originality", "brand",
        ]
        assert decode_observation_list_field(row["preview_candidate_ids"]) == [
            "101", "102", "103",
        ]
        assert decode_observation_list_field(row["uncovered_keys"]) == []


def test_legacy_double_encoded_observation_row_remains_readable():
    """Repair 3: very early rows double-encoded by the old
    ``_fast_observation_fields`` remain readable through the tolerant helper —
    no destructive migration, the schema is untouched."""
    import sqlite3

    with TemporaryDirectory() as directory:
        path = Path(directory) / "queries.sqlite3"
        store = WebQueryIntakeStore(path)
        # Ensure the schema exists, then insert a legacy double-encoded row the
        # way the old projection wrote it (a JSON array string JSON-dumped again).
        store.record_observation("req-warmup")
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "INSERT INTO web_query_observation("
                "request_id, mentioned_keys, covered_keys, uncovered_keys, "
                "preview_candidate_ids, final_candidate_ids, recorded_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "req-legacy",
                    json.dumps("[\"screen_originality\", \"brand\"]"),
                    json.dumps("[\"screen_originality\"]"),
                    json.dumps("[]"),
                    json.dumps("[101, 102, 103]"),
                    json.dumps("[102]"),
                    "2026-08-17T00:00:00+00:00",
                ),
            )
            connection.commit()
        finally:
            connection.close()

        rows = store.read_observations(request_id="req-legacy")
        assert len(rows) == 1
        row = rows[0]
        # Raw column is the legacy double-encoded string.
        assert row["preview_candidate_ids"].startswith('"')
        # The tolerant helper decodes both layers in one call.
        assert decode_observation_list_field(row["preview_candidate_ids"]) == [
            101, 102, 103,
        ]
        assert decode_observation_list_field(row["mentioned_keys"]) == [
            "screen_originality", "brand",
        ]
        assert decode_observation_list_field(row["uncovered_keys"]) == []
