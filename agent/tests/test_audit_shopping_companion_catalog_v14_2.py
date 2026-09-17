from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluation.audit_shopping_companion_catalog_v14_2 import (
    CATALOG_REVISION,
    CatalogAuditError,
    audit_catalog,
    category_id,
    category_ids,
    load_references,
    materialize,
)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def fixture_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    catalog = tmp_path / "products.jsonl"
    values = tmp_path / "catalog-values.jsonl"
    targets = tmp_path / "train-target-catalog.jsonl"
    write_jsonl(catalog, [{
        "id": "1",
        "contents": "barbecue snack gift box",
        "product": {
            "product_id": "1",
            "category": "Groceries - Food Staples - Snacks",
            "attributes": {"flavor": ["Barbecue", "Spicy"]},
            "options": [{"package": ["Box"]}],
        },
    }, {
        "id": "2",
        "contents": "another barbecue snack",
        "product": {
            "product_id": "2",
            "category": "Groceries - Snacks",
            "attributes": {"flavor": ["Barbecue"]},
            "options": [],
        },
    }])
    write_jsonl(values, [{
        "catalogRevision": CATALOG_REVISION,
        "categoryId": "snacks",
        "attributeKey": "flavor",
        "normalizedValue": "barbecue",
    }, {
        "catalogRevision": CATALOG_REVISION,
        "categoryId": "snacks",
        "attributeKey": "package",
        "normalizedValue": "box",
    }])
    write_jsonl(targets, [{
        "productId": "1",
        "categoryId": "snacks",
        "aspects": [{"attributeKey": "flavor", "normalizedValue": "barbecue"}],
    }])
    return catalog, values, targets


def test_audit_accepts_exact_source_and_counts_attributes_plus_options(tmp_path: Path):
    catalog, values, targets = fixture_files(tmp_path)
    preferences, target_aspects, target_ids = load_references(
        values, targets, enforce_pinned_hashes=False,
    )
    raw = catalog.read_bytes()
    report, support = audit_catalog(
        catalog,
        preferences,
        target_aspects,
        target_ids,
        expected_bytes=len(raw),
        expected_rows=2,
        expected_sha256=hashlib.sha256(raw).hexdigest(),
    )
    assert report["decision"] == "CATALOG_STRUCTURE_ACCEPT"
    assert report["coverage"]["preferenceTupleRate"] == 1.0
    assert report["coverage"]["trainTargetProductIdRate"] == 1.0
    assert report["shapeCounters"]["multiValuedField"] == 1
    by_key = {(row["attributeKey"], row["normalizedValue"]): row for row in support}
    assert by_key[("flavor", "barbecue")]["productSupport"] == 2
    assert by_key[("package", "box")]["productSupport"] == 1


def test_category_identity_uses_leaf_of_official_breadcrumb():
    assert category_id("Automotive - Cars - Car Care Equipment") == "car-care-equipment"
    assert category_id("Exercise & Fitness Equipment") == "exercise-fitness-equipment"


def test_category_identity_indexes_every_exact_breadcrumb_segment():
    assert category_ids("Automotive - Cars - Car Care Equipment") == (
        "automotive", "cars", "car-care-equipment",
    )


def test_hash_or_coverage_failure_holds_without_quality_claim(tmp_path: Path):
    catalog, values, targets = fixture_files(tmp_path)
    preferences, target_aspects, target_ids = load_references(
        values, targets, enforce_pinned_hashes=False,
    )
    report, _ = audit_catalog(
        catalog,
        preferences | {("snacks", "flavor", "missing")},
        target_aspects,
        target_ids,
        expected_bytes=catalog.stat().st_size,
        expected_rows=2,
        expected_sha256="0" * 64,
    )
    assert report["decision"] == "HOLD_CATALOG_STRUCTURE"
    assert report["gates"]["sourceSha256Exact"] is False
    assert report["explicitBoundaries"]["qualityRun"] is False
    assert report["explicitBoundaries"]["sealedRead"] is False


def test_reference_revision_and_materialization_fail_closed(tmp_path: Path):
    catalog, values, targets = fixture_files(tmp_path)
    rows = [json.loads(line) for line in values.read_text(encoding="utf-8").splitlines()]
    rows[0]["catalogRevision"] = "wrong"
    write_jsonl(values, rows)
    with pytest.raises(CatalogAuditError, match="revision mismatch"):
        load_references(values, targets, enforce_pinned_hashes=False)

    output = tmp_path / "result"
    report = {"decision": "HOLD", "source": {"sha256": "a" * 64}}
    materialize(
        output,
        report,
        [],
        catalog_values_path=values,
        train_targets_path=targets,
    )
    assert (output / "receipt.json").is_file()
    assert (output / "SHA256SUMS.txt").is_file()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        materialize(
            output,
            report,
            [],
            catalog_values_path=values,
            train_targets_path=targets,
        )
