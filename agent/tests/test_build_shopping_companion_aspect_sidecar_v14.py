from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from evaluation.build_shopping_companion_aspect_sidecar_v14 import (
    build_sidecar,
    product_aspects,
)


def row(product_id: str, attributes: object, options: object) -> bytes:
    value = {
        "id": product_id,
        "contents": "sample",
        "product": {"product_id": product_id, "attributes": attributes, "options": options},
    }
    return (json.dumps(value, separators=(",", ":")) + "\n").encode()


def test_product_aspects_preserves_multivalue_and_deduplicates():
    shapes: Counter[str] = Counter()
    value = product_aspects({
        "attributes": {"Color Family": ["Dark Blue", "Red"]},
        "options": [{"Color Family": ["Red"], "Size": "XL"}],
    }, shapes)
    assert value == {
        ("color_family", "dark-blue"), ("color_family", "red"), ("size", "xl")
    }
    assert shapes["multiValuedField"] == 1


def test_build_sidecar_binds_source_rows_and_lookup(tmp_path: Path):
    source = tmp_path / "products.jsonl"
    payload = row("1", {"Flavor": ["BBQ", "Salt"]}, []) + row("2", {}, [{"Flavor": "BBQ"}])
    source.write_bytes(payload)
    database = tmp_path / "aspects.sqlite3"
    report = build_sidecar(
        source, database,
        expected_bytes=len(payload), expected_rows=2,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
        minimum_free_bytes=0, progress_every=0,
    )
    assert report["decision"] == "FULL_CATALOG_ASPECT_SIDECAR_ACCEPT"
    assert report["sidecar"]["aspectRows"] == 3
    connection = sqlite3.connect(database)
    try:
        assert connection.execute(
            "SELECT product_rowid FROM product_aspect WHERE attribute_key='flavor' AND normalized_value='bbq' ORDER BY product_rowid"
        ).fetchall() == [(1,), (2,)]
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_build_refuses_existing_database(tmp_path: Path):
    source = tmp_path / "products.jsonl"
    source.write_bytes(row("1", {}, []))
    database = tmp_path / "aspects.sqlite3"
    database.write_bytes(b"occupied")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        build_sidecar(source, database, minimum_free_bytes=0)
