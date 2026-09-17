from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from evaluation.shopping_memory_v14_public_dev_multipositive_v1 import (
    FullCatalogPool,
    active_preferences,
    relevance_grade,
)
from tests.test_shopping_memory_v14_retrieval_dev_v2 import build_index


def build_aspects(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE product_aspect(
            product_rowid INTEGER NOT NULL,
            attribute_key TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            PRIMARY KEY(product_rowid,attribute_key,normalized_value)
        ) WITHOUT ROWID;
        CREATE INDEX aspect_lookup ON product_aspect(attribute_key,normalized_value,product_rowid);
        INSERT INTO product_aspect VALUES(1,'flavor','barbecue');
        INSERT INTO product_aspect VALUES(2,'appliance','rice-cooker');
    """)
    connection.commit()
    connection.close()


def test_relevance_grade_is_deterministic_and_graded():
    assert relevance_grade(3, 3) == 3
    assert relevance_grade(2, 3) == 2
    assert relevance_grade(1, 3) == 1
    assert relevance_grade(0, 3) == 0
    with pytest.raises(Exception, match="invalid relevance"):
        relevance_grade(4, 3)


def test_active_preferences_ignores_indifferent():
    episode = {"preferences": [
        {"preferenceKind": "prefer", "attributeKey": "flavor", "normalizedValue": "barbecue"},
        {"preferenceKind": "indifferent", "attributeKey": "organic", "normalizedValue": "yes"},
    ]}
    assert active_preferences(episode) == (("flavor", "barbecue"),)


def test_pool_excludes_history_and_matches_candidate_aspects(tmp_path: Path):
    index = tmp_path / "index.sqlite3"
    categories = tmp_path / "categories.sqlite3"
    aspects = tmp_path / "aspects.sqlite3"
    build_index(index, categories)
    build_aspects(aspects)
    pool = FullCatalogPool(index, categories, aspects)
    try:
        candidates = pool.candidates('"cassava" OR "chips"', "snacks", "missing", limit=1)
        assert candidates[0].product_id == "1"
        assert pool.candidate_matches(candidates, (("flavor", "barbecue"),))[1] == {
            ("flavor", "barbecue")
        }
        assert pool.historical_aspects("1") == {("flavor", "barbecue")}
        assert pool.tuple_support("snacks", "flavor", "barbecue") == 1
    finally:
        pool.close()
