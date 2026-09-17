"""Durable ordering fence for Java's source-authoritative review snapshots.

SQLite serializes local worker processes. A prepared revision is committed before
index IO so a crash cannot let an older request resurrect a deleted review.
This receipt file must be retained together with the indexes it protects.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

from .settings import settings


def apply_review_projection(review_id: str, revision: int | None,
                            payload: dict[str, Any], apply: Callable[[], None]) -> bool:
    path = Path(settings.review_projection_receipts_path)
    if revision is None and not path.exists():
        apply()  # Compatibility before this index has received versioned snapshots.
        return True
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ValueError("invalid review projection revision")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode()).hexdigest()
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS receipt(
            review_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
            digest TEXT NOT NULL, status TEXT NOT NULL)""")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT revision,digest,status FROM receipt WHERE review_id=?",(review_id,)).fetchone()
        if revision is None:
            if row:
                raise ValueError("versioned review requires X-Projection-Revision")
            apply()
            db.commit()
            return True
        if row and row[0] > revision:
            db.rollback()
            return False
        if row and row[0] == revision:
            if row[1] != digest:
                raise ValueError("review revision payload conflict")
            if row[2] == "APPLIED":
                db.rollback()
                return False
        else:
            db.execute("""INSERT INTO receipt VALUES(?,?,?,'PREPARED')
                ON CONFLICT(review_id) DO UPDATE SET revision=excluded.revision,
                digest=excluded.digest,status='PREPARED'""",(review_id,revision,digest))
        db.commit()

        # Another process may have prepared a newer snapshot between transactions.
        # Holding this lock during IO prevents its index writes from overtaking ours.
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT revision,status FROM receipt WHERE review_id=?",(review_id,)).fetchone()
        if current[0] != revision or current[1] == "APPLIED":
            db.rollback()
            return False
        apply()
        db.execute("UPDATE receipt SET status='APPLIED' WHERE review_id=? AND revision=?",(review_id,revision))
        db.commit()
        return True
    finally:
        db.close()
