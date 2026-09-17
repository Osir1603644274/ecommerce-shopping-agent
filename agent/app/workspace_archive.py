"""Private durable conversation archive, never a retrieval/training/gold input.

Uses the existing intake directory, but no automatic age-based deletion. Redis
remains the execution/transaction authority. History is deliberately read-only.
"""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .settings import settings
from .web_query_intake import redact_explicit_secrets


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if not re.search(
            r'password|passwd|secret|authorization|cookie|csrf|access.?token|refresh.?token|api.?key|prompt|reasoning|thinking', k, re.I)}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return redact_explicit_secrets(value)[0] if isinstance(value, str) else value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def conversation_id(state):
    return state.get('conversationId') or digest(state['engine'])[:32]


class WorkspaceArchive:
    def __init__(self, path=None):
        self.path = Path(path or Path(settings.web_query_intake_path).with_name('conversations.sqlite3'))

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS workspace_conversations (
                owner TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL,
                first_seen_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(owner,id));
            CREATE TABLE IF NOT EXISTS workspace_messages (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                conversation_id TEXT NOT NULL, request_id TEXT NOT NULL, role TEXT NOT NULL,
                captured_at TEXT NOT NULL, source TEXT NOT NULL,
                original_sha256 TEXT NOT NULL, payload_json TEXT NOT NULL,
                UNIQUE(owner,conversation_id,request_id,role));
            CREATE TABLE IF NOT EXISTS workspace_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL,
                conversation_id TEXT NOT NULL, request_id TEXT, kind TEXT NOT NULL,
                captured_at TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(owner,conversation_id,kind,payload_sha256));
            CREATE INDEX IF NOT EXISTS workspace_history ON workspace_messages(owner,conversation_id,seq);
        ''')
        return db

    def record(self, owner_key, state, *, kind='snapshot', payload=None):
        owner, cid = digest(owner_key), conversation_id(state)
        now = datetime.now(timezone.utc).isoformat()
        title = next((m['content'][:60] for m in state.get('messages', []) if m['role'] == 'user'), '新对话')
        with closing(self.connect()) as db, db:
            db.execute('''INSERT INTO workspace_conversations VALUES(?,?,?,?,?)
                ON CONFLICT(owner,id) DO UPDATE SET updated_at=excluded.updated_at,
                title=CASE WHEN workspace_conversations.title='新对话' THEN excluded.title ELSE workspace_conversations.title END''',
                (owner, cid, clean(title), now, now))
            for msg in state.get('messages', []):
                safe = {k: v for k, v in clean(msg).items() if k in {'role','content','requestId','cards','flow','source','submittedAt'}}
                serialized = json.dumps(safe, ensure_ascii=False, sort_keys=True)
                original_hash = digest(msg['content'])
                prior = db.execute('SELECT original_sha256 FROM workspace_messages WHERE owner=? AND conversation_id=? AND request_id=? AND role=?',
                    (owner, cid, msg['requestId'], msg['role'])).fetchone()
                if prior and prior[0] != original_hash:
                    raise ValueError('archive_message_identity_conflict')
                db.execute('INSERT OR IGNORE INTO workspace_messages(owner,conversation_id,request_id,role,captured_at,source,original_sha256,payload_json) VALUES(?,?,?,?,?,?,?,?)',
                    (owner, cid, msg['requestId'], msg['role'], now, msg.get('source', 'legacy_unverified'), original_hash, serialized))
            if payload is not None:
                encoded = json.dumps(clean(payload), ensure_ascii=False, sort_keys=True, default=str)
                db.execute('INSERT OR IGNORE INTO workspace_events(owner,conversation_id,request_id,kind,captured_at,payload_sha256,payload_json) VALUES(?,?,?,?,?,?,?)',
                    (owner, cid, payload.get('requestId'), kind, now, digest(encoded), encoded))
        return cid

    def history(self, owner_key, offset=0):
        with closing(self.connect()) as db:
            # Keep empty execution/event records for audit, but do not list them
            # as conversations. Filter BEFORE pagination, not after LIMIT.
            rows = db.execute('''SELECT id,title,first_seen_at,updated_at FROM workspace_conversations c
                WHERE owner=? AND EXISTS (SELECT 1 FROM workspace_messages m
                    WHERE m.owner=c.owner AND m.conversation_id=c.id)
                ORDER BY updated_at DESC,id LIMIT 51 OFFSET ?''',
                (digest(owner_key), offset)).fetchall()
        return dict(conversations=[dict(r) for r in rows[:50]], nextOffset=offset+50 if len(rows)>50 else None)

    def read(self, owner_key, cid):
        with closing(self.connect()) as db:
            head = db.execute('SELECT id,title FROM workspace_conversations WHERE owner=? AND id=?', (digest(owner_key), cid)).fetchone()
            if not head:
                return None
            rows = db.execute('SELECT payload_json FROM workspace_messages WHERE owner=? AND conversation_id=? ORDER BY seq', (digest(owner_key), cid)).fetchall()
        return dict(head) | {'messages': [json.loads(r[0]) for r in rows], 'readOnly': True}


def get_archive():
    return WorkspaceArchive()
