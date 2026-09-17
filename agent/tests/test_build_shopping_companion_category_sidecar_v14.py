from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from evaluation.build_shopping_companion_category_sidecar_v14 import build_sidecar


def fixture(tmp_path: Path) -> tuple[Path, Path, bytes]:
    catalog = tmp_path / "products.jsonl"
    rows = [{"id": "1", "contents": "phone", "product": {"category": "Electronics - Mobiles - Smartphones"}},
            {"id": "2", "contents": "case", "product": {"category": "Electronics - Mobile Accessories"}}]
    raw = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
    catalog.write_bytes(raw)
    fts = tmp_path / "fts.sqlite3"
    connection = sqlite3.connect(fts)
    connection.executescript("""
        CREATE TABLE product_locator(rowid INTEGER PRIMARY KEY,product_id TEXT UNIQUE,byte_offset INTEGER,byte_length INTEGER);
        CREATE VIRTUAL TABLE product_fts USING fts5(product_id UNINDEXED,contents);
        INSERT INTO product_locator VALUES(1,'1',0,1);
        INSERT INTO product_locator VALUES(2,'2',1,1);
    """)
    connection.commit()
    connection.close()
    return catalog, fts, raw


def test_sidecar_indexes_every_breadcrumb_membership(tmp_path: Path):
    catalog, fts, raw = fixture(tmp_path)
    building = tmp_path / "categories.building.sqlite3"
    report = build_sidecar(
        catalog,
        fts,
        building,
        expected_source_bytes=len(raw),
        expected_source_rows=2,
        expected_source_sha256=hashlib.sha256(raw).hexdigest(),
        expected_fts_sha256=hashlib.sha256(fts.read_bytes()).hexdigest(),
        minimum_free_bytes=0,
        progress_every=0,
    )
    assert report["decision"] == "FULL_CATALOG_CATEGORY_SIDECAR_ACCEPT"
    assert report["sidecar"]["productRows"] == 2
    assert report["sidecar"]["categoryMemberships"] == 5
    connection = sqlite3.connect(building)
    try:
        rows = connection.execute(
            "SELECT category_id,product_rowid FROM product_category ORDER BY category_id,product_rowid"
        ).fetchall()
    finally:
        connection.close()
    assert ("electronics", 1) in rows and ("electronics", 2) in rows
    assert ("mobiles", 1) in rows and ("smartphones", 1) in rows
