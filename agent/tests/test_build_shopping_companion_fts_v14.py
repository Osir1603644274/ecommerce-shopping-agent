from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from evaluation.build_shopping_companion_fts_v14 import build_index, materialize


def write_catalog(path: Path) -> bytes:
    rows = [{
        "id": "2",
        "contents": "plain rice cooker",
        "product": {"product_id": "2", "category": "Kitchenware"},
    }, {
        "id": "1",
        "contents": "barbecue cassava chips gluten free",
        "product": {"product_id": "1", "category": "Snacks"},
    }]
    raw = b"".join(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for row in rows
    )
    path.write_bytes(raw)
    return raw


def test_build_index_binds_offsets_and_deterministic_fts(tmp_path: Path):
    catalog = tmp_path / "products.jsonl"
    raw = write_catalog(catalog)
    database = tmp_path / "catalog.building.sqlite3"
    report = build_index(
        catalog,
        database,
        expected_bytes=len(raw),
        expected_rows=2,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        progress_every=0,
        minimum_free_bytes=0,
    )
    assert report["decision"] == "FULL_CATALOG_FTS_BUILD_ACCEPT"
    assert report["explicitBoundaries"]["queryExecution"] is False
    connection = sqlite3.connect(database)
    try:
        result = connection.execute(
            "SELECT product_id FROM product_fts WHERE product_fts MATCH ? ORDER BY bm25(product_fts), product_id LIMIT 50",
            ("cassava chips",),
        ).fetchall()
        offset, length = connection.execute(
            "SELECT byte_offset,byte_length FROM product_locator WHERE product_id='1'"
        ).fetchone()
    finally:
        connection.close()
    assert result == [("1",)]
    assert json.loads(raw[offset:offset + length])["id"] == "1"


def test_source_mismatch_fails_closed_and_materialize_never_overwrites(tmp_path: Path):
    catalog = tmp_path / "products.jsonl"
    raw = write_catalog(catalog)
    database = tmp_path / "catalog.building.sqlite3"
    with pytest.raises(RuntimeError, match="source identity mismatch"):
        build_index(
            catalog,
            database,
            expected_bytes=len(raw),
            expected_rows=2,
            expected_sha256="0" * 64,
            progress_every=0,
            minimum_free_bytes=0,
        )

    valid_database = tmp_path / "valid.building.sqlite3"
    report = build_index(
        catalog,
        valid_database,
        expected_bytes=len(raw),
        expected_rows=2,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
        progress_every=0,
        minimum_free_bytes=0,
    )
    builder = tmp_path / "builder.py"
    contract = tmp_path / "contract.json"
    builder.write_text("builder", encoding="utf-8")
    contract.write_text("{}", encoding="utf-8")
    final = tmp_path / "catalog.sqlite3"
    receipts = tmp_path / "receipts"
    materialize(final, receipts, report, builder_path=builder, contract_path=contract)
    assert final.is_file() and (receipts / "receipt.json").is_file()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        materialize(final, tmp_path / "other", report, builder_path=builder, contract_path=contract)
