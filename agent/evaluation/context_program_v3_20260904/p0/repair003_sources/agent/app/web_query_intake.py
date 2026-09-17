"""Durable, privacy-bounded capture of queries submitted by the web client.

The SQLite store is intentionally isolated from TaskState, prompts, retrieval,
and qrels.  Writes are bounded and best-effort: capture failure is observable
but never changes the Agent response path.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .settings import settings


logger = logging.getLogger(__name__)
SCHEMA_VERSION = "web-query-intake-v1"
DIAGNOSTIC_SCHEMA_VERSION = "web-query-diagnostic-v1"
QueryMode = Literal["chat", "step_debug"]
QueryOutcome = Literal["pending", "completed", "failed", "cancelled"]

_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    re.compile(
        r"(?i)\b(?:api[_ -]?key|token|password|passwd|secret)\s*[:=]\s*"
        r"[^\s,;]{4,}"
    ),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)


class WebQueryRecord(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    request_id: str = Field(alias="requestId")
    session_id: str = Field(alias="sessionId")
    turn_number: int = Field(alias="turnNumber", ge=1)
    submitted_at: str = Field(alias="submittedAt")
    updated_at: str = Field(alias="updatedAt")
    route: str
    mode: QueryMode
    outcome: QueryOutcome
    message: str
    message_sha256: str = Field(alias="messageSha256")
    redaction_applied: bool = Field(alias="redactionApplied")
    failure_code: str | None = Field(default=None, alias="failureCode")


def _json_dumps(value: object) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def decode_observation_list_field(value: object) -> list[Any] | None:
    """Decode a ``web_query_observation`` JSON-array column back to a list.

    Rows recorded by this codebase are single-encoded (``_json_dumps`` of a
    list), so one ``json.loads`` yields the list.  Very early rows may be
    double-encoded (the ``_fast_observation_fields`` projection pre-dumped the
    value before ``record_observation`` re-dumped it); both shapes decode
    here, so legacy rows stay readable without any migration.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return list(value)
    decoded = json.loads(value)
    if isinstance(decoded, str):
        decoded = json.loads(decoded)
    return list(decoded)


def redact_explicit_secrets(message: str) -> tuple[str, bool]:
    redacted = message
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[REDACTED_SECRET]", redacted)
    return redacted, redacted != message


def _redact_diagnostic_value(value: Any) -> tuple[Any, bool]:
    """Redact explicit credentials without changing the live response object."""
    if isinstance(value, str):
        return redact_explicit_secrets(value)
    if isinstance(value, list):
        redacted_items: list[Any] = []
        changed = False
        for item in value:
            redacted_item, item_changed = _redact_diagnostic_value(item)
            redacted_items.append(redacted_item)
            changed = changed or item_changed
        return redacted_items, changed
    if isinstance(value, dict):
        redacted_mapping: dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            redacted_item, item_changed = _redact_diagnostic_value(item)
            redacted_mapping[str(key)] = redacted_item
            changed = changed or item_changed
        return redacted_mapping, changed
    return value, False


class WebQueryIntakeStore:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or settings.web_query_intake_path)
        self._lock = threading.RLock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        if self._initialized:
            return connection
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS web_query_intake (
                request_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                turn_number INTEGER NOT NULL,
                submitted_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                route TEXT NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('chat', 'step_debug')),
                outcome TEXT NOT NULL CHECK (
                    outcome IN ('pending', 'completed', 'failed', 'cancelled')
                ),
                message TEXT NOT NULL,
                message_sha256 TEXT NOT NULL,
                redaction_applied INTEGER NOT NULL CHECK (redaction_applied IN (0, 1)),
                failure_code TEXT,
                UNIQUE(session_id, turn_number)
            );
            CREATE INDEX IF NOT EXISTS idx_web_query_intake_session
                ON web_query_intake(session_id, turn_number);
            CREATE INDEX IF NOT EXISTS idx_web_query_intake_submitted
                ON web_query_intake(submitted_at);
            CREATE TABLE IF NOT EXISTS web_query_intake_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """)
        connection.execute(
            "INSERT OR REPLACE INTO web_query_intake_meta(key, value) VALUES (?, ?)",
            ("schemaVersion", SCHEMA_VERSION),
        )
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS web_query_observation (
                request_id TEXT PRIMARY KEY,
                task_id TEXT,
                session_id TEXT,
                route TEXT,
                extraction_route TEXT,
                extraction_reason TEXT,
                mentioned_keys TEXT,
                covered_keys TEXT,
                uncovered_keys TEXT,
                task_manager_model_calls INTEGER NOT NULL DEFAULT 0,
                task_manager_duration_ms REAL NOT NULL DEFAULT 0,
                task_state_model_calls INTEGER NOT NULL DEFAULT 0,
                task_state_duration_ms REAL NOT NULL DEFAULT 0,
                preview_eligible INTEGER NOT NULL DEFAULT 0,
                preview_status TEXT,
                preview_cache_status TEXT,
                preview_duration_ms REAL,
                preview_candidate_ids TEXT,
                final_duration_ms REAL,
                final_candidate_ids TEXT,
                preview_final_removed TEXT,
                preview_final_added TEXT,
                preview_final_reordered INTEGER NOT NULL DEFAULT 0,
                recorded_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS web_query_diagnostic (
                request_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                task_id TEXT,
                route TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                payload_bytes INTEGER NOT NULL,
                redaction_applied INTEGER NOT NULL CHECK (redaction_applied IN (0, 1))
            );
            CREATE INDEX IF NOT EXISTS idx_web_query_diagnostic_latest
                ON web_query_diagnostic(recorded_at DESC);
            CREATE INDEX IF NOT EXISTS idx_web_query_diagnostic_session
                ON web_query_diagnostic(session_id, recorded_at DESC);
            """)
        connection.execute(
            "INSERT OR REPLACE INTO web_query_intake_meta(key, value) VALUES (?, ?)",
            ("observationSchemaVersion", "web-query-observation-v1"),
        )
        connection.execute(
            "INSERT OR REPLACE INTO web_query_intake_meta(key, value) VALUES (?, ?)",
            ("diagnosticSchemaVersion", DIAGNOSTIC_SCHEMA_VERSION),
        )
        connection.commit()
        self._initialized = True
        return connection

    @staticmethod
    def _row(row: sqlite3.Row) -> WebQueryRecord:
        return WebQueryRecord(
            requestId=row["request_id"],
            sessionId=row["session_id"],
            turnNumber=row["turn_number"],
            submittedAt=row["submitted_at"],
            updatedAt=row["updated_at"],
            route=row["route"],
            mode=row["mode"],
            outcome=row["outcome"],
            message=row["message"],
            messageSha256=row["message_sha256"],
            redactionApplied=bool(row["redaction_applied"]),
            failureCode=row["failure_code"],
        )

    def record_submission(
        self,
        *,
        request_id: str,
        session_id: str | None,
        message: str,
        route: str,
        mode: QueryMode,
    ) -> WebQueryRecord:
        stored_message, redacted = redact_explicit_secrets(message)
        message_sha = hashlib.sha256(message.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        stable_session_id = session_id or f"ephemeral:{request_id}"
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM web_query_intake WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                return self._row(existing)
            turn_number = int(connection.execute(
                "SELECT COALESCE(MAX(turn_number), 0) + 1 FROM web_query_intake "
                "WHERE session_id = ?",
                (stable_session_id,),
            ).fetchone()[0])
            connection.execute(
                """
                INSERT INTO web_query_intake(
                    request_id, session_id, turn_number, submitted_at, updated_at,
                    route, mode, outcome, message, message_sha256,
                    redaction_applied, failure_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL)
                """,
                (
                    request_id, stable_session_id, turn_number, now, now, route,
                    mode, stored_message, message_sha, int(redacted),
                ),
            )
            retention_days = max(int(settings.web_query_intake_retention_days), 1)
            cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
            connection.execute(
                "DELETE FROM web_query_intake WHERE submitted_at < ?",
                (cutoff,),
            )
            row = connection.execute(
                "SELECT * FROM web_query_intake WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            assert row is not None
            return self._row(row)

    def mark_outcome(
        self,
        request_id: str,
        outcome: QueryOutcome,
        *,
        failure_code: str | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "UPDATE web_query_intake SET outcome = ?, updated_at = ?, "
                "failure_code = ? WHERE request_id = ?",
                (outcome, now, failure_code, request_id),
            )
            return cursor.rowcount == 1

    def record_observation(
        self,
        request_id: str,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        route: str | None = None,
        extraction_route: str | None = None,
        extraction_reason: str | None = None,
        mentioned_keys: list[str] | None = None,
        covered_keys: list[str] | None = None,
        uncovered_keys: list[str] | None = None,
        task_manager_model_calls: int = 0,
        task_manager_duration_ms: float = 0.0,
        task_state_model_calls: int = 0,
        task_state_duration_ms: float = 0.0,
        preview_eligible: bool = False,
        preview_status: str | None = None,
        preview_cache_status: str | None = None,
        preview_duration_ms: float | None = None,
        preview_candidate_ids: list[int] | None = None,
        final_duration_ms: float | None = None,
        final_candidate_ids: list[int] | None = None,
        preview_final_removed: list[int] | None = None,
        preview_final_added: list[int] | None = None,
        preview_final_reordered: bool = False,
    ) -> bool:
        """Upsert one best-effort runtime observation for a request.

        The observation is audit-only: it is never fed back into prompts,
        retrieval or qrels, and a write failure must not change the answer path.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO web_query_observation(
                    request_id, task_id, session_id, route, extraction_route,
                    extraction_reason, mentioned_keys, covered_keys, uncovered_keys,
                    task_manager_model_calls, task_manager_duration_ms,
                    task_state_model_calls, task_state_duration_ms,
                    preview_eligible, preview_status, preview_cache_status,
                    preview_duration_ms, preview_candidate_ids,
                    final_duration_ms, final_candidate_ids,
                    preview_final_removed, preview_final_added,
                    preview_final_reordered, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    task_id = excluded.task_id,
                    session_id = excluded.session_id,
                    route = excluded.route,
                    extraction_route = excluded.extraction_route,
                    extraction_reason = excluded.extraction_reason,
                    mentioned_keys = excluded.mentioned_keys,
                    covered_keys = excluded.covered_keys,
                    uncovered_keys = excluded.uncovered_keys,
                    task_manager_model_calls = excluded.task_manager_model_calls,
                    task_manager_duration_ms = excluded.task_manager_duration_ms,
                    task_state_model_calls = excluded.task_state_model_calls,
                    task_state_duration_ms = excluded.task_state_duration_ms,
                    preview_eligible = excluded.preview_eligible,
                    preview_status = excluded.preview_status,
                    preview_cache_status = excluded.preview_cache_status,
                    preview_duration_ms = excluded.preview_duration_ms,
                    preview_candidate_ids = excluded.preview_candidate_ids,
                    final_duration_ms = excluded.final_duration_ms,
                    final_candidate_ids = excluded.final_candidate_ids,
                    preview_final_removed = excluded.preview_final_removed,
                    preview_final_added = excluded.preview_final_added,
                    preview_final_reordered = excluded.preview_final_reordered,
                    recorded_at = excluded.recorded_at
                """,
                (
                    request_id,
                    task_id,
                    session_id,
                    route,
                    extraction_route,
                    extraction_reason,
                    _json_dumps(mentioned_keys),
                    _json_dumps(covered_keys),
                    _json_dumps(uncovered_keys),
                    int(task_manager_model_calls),
                    float(task_manager_duration_ms),
                    int(task_state_model_calls),
                    float(task_state_duration_ms),
                    int(bool(preview_eligible)),
                    preview_status,
                    preview_cache_status,
                    preview_duration_ms,
                    _json_dumps(preview_candidate_ids),
                    final_duration_ms,
                    _json_dumps(final_candidate_ids),
                    _json_dumps(preview_final_removed),
                    _json_dumps(preview_final_added),
                    int(bool(preview_final_reordered)),
                    now,
                ),
            )
            return cursor.rowcount == 1

    def read_observations(
        self,
        *,
        request_id: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 10000))
        with self._lock, closing(self._connect()) as connection, connection:
            if request_id:
                rows = connection.execute(
                    "SELECT * FROM web_query_observation WHERE request_id = ? "
                    "LIMIT ?",
                    (request_id, bounded_limit),
                ).fetchall()
            elif session_id:
                rows = connection.execute(
                    "SELECT * FROM web_query_observation WHERE session_id = ? "
                    "ORDER BY recorded_at ASC LIMIT ?",
                    (session_id, bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM web_query_observation "
                    "ORDER BY recorded_at ASC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
            return [dict(row) for row in rows]

    def record_diagnostic(
        self,
        request_id: str,
        *,
        session_id: str | None,
        task_id: str | None,
        route: str,
        payload: dict[str, Any],
    ) -> bool:
        """Freeze the server-owned response bundle for zero-ID debugging.

        This table is diagnostic-only and is never read by prompts, retrieval,
        ranking, qrels, or benchmark scoring.
        """
        redacted_payload, redaction_applied = _redact_diagnostic_value(payload)
        payload_json = json.dumps(
            redacted_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        payload_bytes = len(payload_json.encode("utf-8"))
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        stable_session_id = session_id or f"ephemeral:{request_id}"
        retention_days = max(int(settings.web_query_intake_retention_days), 1)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        with self._lock, closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO web_query_diagnostic(
                    request_id, session_id, task_id, route, recorded_at,
                    payload_json, payload_sha256, payload_bytes, redaction_applied
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    task_id = excluded.task_id,
                    route = excluded.route,
                    recorded_at = excluded.recorded_at,
                    payload_json = excluded.payload_json,
                    payload_sha256 = excluded.payload_sha256,
                    payload_bytes = excluded.payload_bytes,
                    redaction_applied = excluded.redaction_applied
                """,
                (
                    request_id,
                    stable_session_id,
                    task_id,
                    route,
                    now,
                    payload_json,
                    payload_sha256,
                    payload_bytes,
                    int(redaction_applied),
                ),
            )
            connection.execute(
                "DELETE FROM web_query_diagnostic WHERE recorded_at < ?",
                (cutoff,),
            )
            return cursor.rowcount == 1

    def read_diagnostics(
        self,
        *,
        request_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Read newest diagnostic bundles first; no request id is required."""
        bounded_limit = max(1, min(int(limit), 200))
        with self._lock, closing(self._connect()) as connection, connection:
            if request_id:
                rows = connection.execute(
                    "SELECT * FROM web_query_diagnostic WHERE request_id = ? LIMIT ?",
                    (request_id, bounded_limit),
                ).fetchall()
            elif session_id:
                rows = connection.execute(
                    "SELECT * FROM web_query_diagnostic WHERE session_id = ? "
                    "ORDER BY recorded_at DESC LIMIT ?",
                    (session_id, bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM web_query_diagnostic "
                    "ORDER BY recorded_at DESC LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
        diagnostics: list[dict[str, Any]] = []
        for row in rows:
            diagnostic = dict(row)
            diagnostic["payload"] = json.loads(diagnostic.pop("payload_json"))
            diagnostic["redaction_applied"] = bool(
                diagnostic["redaction_applied"]
            )
            diagnostics.append(diagnostic)
        return diagnostics

    def list_records(
        self,
        *,
        session_id: str | None = None,
        limit: int = 1000,
    ) -> list[WebQueryRecord]:
        bounded_limit = max(1, min(int(limit), 10000))
        with self._lock, closing(self._connect()) as connection, connection:
            if session_id:
                rows = connection.execute(
                    "SELECT * FROM web_query_intake WHERE session_id = ? "
                    "ORDER BY turn_number ASC LIMIT ?",
                    (session_id, bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM web_query_intake ORDER BY submitted_at ASC "
                    "LIMIT ?",
                    (bounded_limit,),
                ).fetchall()
            return [self._row(row) for row in rows]

    def delete_session(self, session_id: str) -> int:
        with self._lock, closing(self._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM web_query_diagnostic WHERE session_id = ?",
                (session_id,),
            )
            connection.execute(
                "DELETE FROM web_query_observation WHERE session_id = ?",
                (session_id,),
            )
            cursor = connection.execute(
                "DELETE FROM web_query_intake WHERE session_id = ?",
                (session_id,),
            )
            return int(cursor.rowcount)

    def stats(self) -> dict[str, Any]:
        with self._lock, closing(self._connect()) as connection, connection:
            total = int(connection.execute(
                "SELECT COUNT(*) FROM web_query_intake"
            ).fetchone()[0])
            outcomes = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT outcome, COUNT(*) FROM web_query_intake GROUP BY outcome"
                ).fetchall()
            }
            return {
                "schemaVersion": SCHEMA_VERSION,
                "path": str(self.path),
                "recordCount": total,
                "outcomes": outcomes,
                "retentionDays": max(int(settings.web_query_intake_retention_days), 1),
            }


_store: WebQueryIntakeStore | None = None
_health_lock = threading.Lock()
_health = {"writeAttempts": 0, "writeFailures": 0, "lastFailureClass": None}


def get_web_query_intake_store() -> WebQueryIntakeStore:
    global _store
    if _store is None:
        _store = WebQueryIntakeStore()
    return _store


def set_web_query_intake_store(store: WebQueryIntakeStore | None) -> None:
    global _store
    _store = store


def capture_health() -> dict[str, Any]:
    with _health_lock:
        return dict(_health)


async def _bounded(
    operation: Any,
    *args: Any,
    timeout_seconds: float | None = None,
    **kwargs: Any,
) -> Any | None:
    if not settings.web_query_intake_enabled:
        return None
    with _health_lock:
        _health["writeAttempts"] += 1
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(operation, *args, **kwargs),
            timeout=max(
                float(
                    timeout_seconds
                    if timeout_seconds is not None
                    else settings.web_query_intake_write_timeout_seconds
                ),
                0.01,
            ),
        )
    except Exception as exc:
        with _health_lock:
            _health["writeFailures"] += 1
            _health["lastFailureClass"] = type(exc).__name__
        logger.warning(
            "web query intake operation failed: operation=%s errorClass=%s",
            getattr(operation, "__name__", "unknown"),
            type(exc).__name__,
        )
        return None


async def capture_web_query(
    *,
    request_id: str,
    session_id: str | None,
    message: str,
    route: str,
    mode: QueryMode,
) -> WebQueryRecord | None:
    return await _bounded(
        get_web_query_intake_store().record_submission,
        request_id=request_id,
        session_id=session_id,
        message=message,
        route=route,
        mode=mode,
    )


async def mark_web_query_outcome(
    request_id: str,
    outcome: QueryOutcome,
    *,
    failure_code: str | None = None,
) -> bool:
    result = await _bounded(
        get_web_query_intake_store().mark_outcome,
        request_id,
        outcome,
        failure_code=failure_code,
    )
    return bool(result)


async def capture_web_query_observation(
    request_id: str,
    **fields: Any,
) -> bool:
    """Best-effort runtime observation write; never alters the answer path."""
    result = await _bounded(
        get_web_query_intake_store().record_observation,
        request_id,
        **fields,
    )
    return bool(result)


async def capture_web_query_diagnostic(
    request_id: str,
    *,
    session_id: str | None,
    task_id: str | None,
    route: str,
    payload: dict[str, Any],
) -> bool:
    """Best-effort full-turn freeze; never alters the answer path."""
    result = await _bounded(
        get_web_query_intake_store().record_diagnostic,
        request_id,
        timeout_seconds=max(
            float(settings.web_query_intake_write_timeout_seconds),
            2.0,
        ),
        session_id=session_id,
        task_id=task_id,
        route=route,
        payload=payload,
    )
    return bool(result)


def read_web_query_observations(
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    return get_web_query_intake_store().read_observations(
        request_id=request_id,
        session_id=session_id,
        limit=limit,
    )


__all__ = [
    "SCHEMA_VERSION",
    "DIAGNOSTIC_SCHEMA_VERSION",
    "WebQueryIntakeStore",
    "WebQueryRecord",
    "capture_health",
    "capture_web_query",
    "capture_web_query_diagnostic",
    "capture_web_query_observation",
    "get_web_query_intake_store",
    "mark_web_query_outcome",
    "read_web_query_observations",
    "redact_explicit_secrets",
    "set_web_query_intake_store",
]
