from __future__ import annotations

from pathlib import Path

import pytest

from evaluation.shopping_memory_v14_candidate_depth_diagnostic_v1 import (
    recall_curve,
    target_flags,
)
from evaluation.shopping_memory_v14_retrieval_dev_v2 import FullCatalogFts
from tests.test_shopping_memory_v14_retrieval_dev_v2 import build_index


def test_target_flags_bind_text_and_category(tmp_path: Path):
    index = tmp_path / "index.sqlite3"
    categories = tmp_path / "categories.sqlite3"
    build_index(index, categories)
    engine = FullCatalogFts(index, categories)
    try:
        assert target_flags(engine, '"cassava"', "snacks", "1") == (True, True)
        assert target_flags(engine, '"cassava"', "kitchenware", "1") == (False, False)
        assert target_flags(engine, '"missing"', "snacks", "1") == (True, False)
    finally:
        engine.close()


def test_recall_curve_counts_only_observed_positive_ranks():
    assert recall_curve([0, 1, 60, 400], (50, 100, 500)) == {
        "50": pytest.approx(0.25),
        "100": pytest.approx(0.5),
        "500": pytest.approx(0.75),
    }
