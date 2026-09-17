from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evaluation.shopping_memory_v14_retrieval_dev_v1 import (
    FullCatalogFts,
    RetrievalDevError,
    fts_query,
    materialize,
    ranking_metrics,
    shopping_query,
)


def build_index(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE product_locator(rowid INTEGER PRIMARY KEY,product_id TEXT UNIQUE,byte_offset INTEGER,byte_length INTEGER);
        CREATE VIRTUAL TABLE product_fts USING fts5(product_id UNINDEXED,contents,tokenize='unicode61 remove_diacritics 2');
    """)
    connection.execute("INSERT INTO product_locator VALUES(1,'1',0,10)")
    connection.execute("INSERT INTO product_locator VALUES(2,'2',10,10)")
    connection.execute("INSERT INTO product_fts(rowid,product_id,contents) VALUES(1,'1','cassava barbecue chips')")
    connection.execute("INSERT INTO product_fts(rowid,product_id,contents) VALUES(2,'2','rice cooker')")
    connection.commit()
    connection.close()


def test_query_removes_date_line_and_uses_safe_or_terms():
    raw = "Current Date: 2026/01/09 (Fri)\ncassava chips"
    assert shopping_query(raw) == "cassava chips"
    assert fts_query(raw) == '"cassava" OR "chips"'
    with pytest.raises(RetrievalDevError, match="blank"):
        shopping_query(" \n ")


def test_read_only_fts_returns_stable_target_and_metrics(tmp_path: Path):
    index = tmp_path / "index.sqlite3"
    build_index(index)
    engine = FullCatalogFts(index)
    try:
        results = engine.search('"cassava" OR "chips"')
        assert results[0][0] == "1"
        assert engine.contains("1") is True
        assert engine.contains("missing") is False
    finally:
        engine.close()
    assert ranking_metrics(["2", "1"], "1") == {
        "targetRank": 2,
        "targetRecallAt50": 1.0,
        "hitAt10": 1.0,
        "nDCGAt10": pytest.approx(1.0 / 1.584962500721156),
        "MRRAt50": 0.5,
    }


def test_materialization_binds_trace_and_refuses_overwrite(tmp_path: Path):
    runner = tmp_path / "runner.py"
    contract = tmp_path / "contract.json"
    runner.write_text("runner", encoding="utf-8")
    contract.write_text("{}", encoding="utf-8")
    output = tmp_path / "result"
    report = {"decision": "HOLD"}
    path = materialize(output, report, [{"candidateIds": ["1"]}], runner_path=runner, contract_path=contract)
    assert path.is_file() and (output / "receipt.json").is_file()
    assert json.loads((output / "receipt.json").read_text(encoding="utf-8"))["validationRead"] is False
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        materialize(output, report, [], runner_path=runner, contract_path=contract)
