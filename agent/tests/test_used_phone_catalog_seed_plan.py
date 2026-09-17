from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import evaluation.used_phone_catalog_seed_plan as plan_module
from evaluation.used_phone_catalog_seed_plan import (
    UsedPhoneCatalogSeedPlanError,
    _strict_positive_int,
    build_used_phone_catalog_seed_plan,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "used_phone_catalog_seed_plan"
    / "catalog.jsonl"
)
FIXTURE_STATUS_COUNTS = {
    "battery_health": {"known": 1, "conflict": 0, "unknown": 1},
    "battery_originality": {"known": 2, "conflict": 0, "unknown": 0},
    "motherboard_repair": {"known": 2, "conflict": 0, "unknown": 0},
    "os": {"known": 1, "conflict": 1, "unknown": 0},
    "scratch_level": {"known": 2, "conflict": 0, "unknown": 0},
    "screen_originality": {"known": 2, "conflict": 0, "unknown": 0},
    "shell_condition": {"known": 1, "conflict": 0, "unknown": 1},
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _input(tmp_path: Path) -> Path:
    path = tmp_path / "input" / "catalog.jsonl"
    path.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, path)
    return path


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _rewrite(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
        newline="\n",
    )


@contextmanager
def _fixture_pins(path: Path):
    with (
        patch.object(plan_module, "EXPECTED_CATALOG_SHA256", _sha(path)),
        patch.object(plan_module, "EXPECTED_PRODUCT_COUNT", 2),
        patch.object(plan_module, "EXPECTED_STATUS_COUNTS", FIXTURE_STATUS_COUNTS),
    ):
        yield


def _build(path: Path, output: Path):
    with _fixture_pins(path):
        return build_used_phone_catalog_seed_plan(
            catalog_path=path,
            output_dir=output,
        )


def test_fixture_builds_exact_db_rows_without_database_or_title_inference(tmp_path):
    source = _input(tmp_path)
    output = tmp_path / "out"
    result = _build(source, output)

    products = _rows(output / "product.jsonl")
    attributes = _rows(output / "product_attribute.jsonl")
    assert [row["id"] for row in products] == [1001, 1002]
    assert len(attributes) == 12
    assert set(products[0]) == {
        "attribute_text", "brand", "category_l1", "category_l2", "category_l3",
        "currency", "data_nature", "dataset_revision", "id", "price_status",
        "provenance_url", "seller", "snapshot_price_minor", "source",
        "source_item_id", "source_license", "title",
    }
    assert set(attributes[0]) == {
        "attribute_key", "confidence", "evidence_field", "extraction_method",
        "normalized_boolean", "normalized_number", "normalized_text", "product_id",
        "raw_value", "unit", "value_type",
    }

    second = {
        row["attribute_key"]: row
        for row in attributes
        if row["product_id"] == 1002
    }
    assert "battery_health" not in second
    assert "shell_condition" not in second
    assert second["battery_originality"]["normalized_text"] == "non_original"
    assert second["os"]["normalized_text"] is None
    assert second["os"]["confidence"] == 0.0
    assert second["os"]["raw_value"].startswith("iOS,安卓")
    assert result["manifest"]["boundaries"] == {
        "conflictProducesAssertion": False,
        "databaseConnected": False,
        "databaseWritten": False,
        "modelUsed": False,
        "networkUsed": False,
        "requiresDedicatedBenchmarkDatabase": True,
        "titleBrandSellerUsedForAttributeFact": False,
        "unknownProducesAttributeRow": False,
    }
    assert result["audit"]["statusCounts"] == FIXTURE_STATUS_COUNTS
    assert result["manifest"]["input"] == {
        "artifactRelativePath": plan_module.FROZEN_CATALOG_ARTIFACT,
        "basename": "catalog.jsonl",
        "locationBoundary": "upstream_independent_worktree_frozen_artifact",
        "sha256": _sha(source),
    }


def test_missing_external_frozen_catalog_fails_before_publication(tmp_path):
    output = tmp_path / "out"
    with pytest.raises(FileNotFoundError):
        build_used_phone_catalog_seed_plan(
            catalog_path=tmp_path / plan_module.FROZEN_CATALOG_ARTIFACT,
            output_dir=output,
        )
    assert not output.exists()


def test_two_dry_runs_are_byte_identical_and_existing_output_fails_closed(tmp_path):
    source = _input(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    _build(source, first)
    _build(source, second)
    for name in (
        "product.jsonl",
        "product_attribute.jsonl",
        "audit.json",
        "manifest.json",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _build(source, first)


def test_repository_pin_rejects_fixture_without_test_only_pin(tmp_path):
    source = _input(tmp_path)
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="catalog SHA mismatch"):
        build_used_phone_catalog_seed_plan(
            catalog_path=source,
            output_dir=tmp_path / "out",
        )


def test_duplicate_item_is_rejected_before_publication(tmp_path):
    source = _input(tmp_path)
    rows = _rows(source)
    rows.append(rows[0])
    _rewrite(source, rows)
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="duplicate itemId"):
        _build(source, tmp_path / "out")
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    "invalid_item_id",
    [True, 0, -1, 1002, 1002.0, "0", "-1", "01001", str(2**63)],
)
def test_noncanonical_or_out_of_range_database_id_is_rejected(
    tmp_path, invalid_item_id
):
    source = _input(tmp_path)
    rows = _rows(source)
    rows[1]["itemId"] = invalid_item_id
    _rewrite(source, rows)
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="invalid numeric itemId"):
        _build(source, tmp_path / "invalid-item-id")


@pytest.mark.parametrize("invalid_product_id", [True, 0, -1, "1", 1.0, 2**63])
def test_output_product_identity_contract_rejects_bool_and_non_integer_types(
    invalid_product_id,
):
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="invalid product id"):
        _strict_positive_int(invalid_product_id, label="product id")


@pytest.mark.parametrize("invalid_line", [True, 0, -1, "10", 10.0, 2**63])
def test_controlled_ref_line_requires_strict_positive_integer(tmp_path, invalid_line):
    source = _input(tmp_path)
    rows = _rows(source)
    for observation in rows[0]["attributes"].values():
        for ref in observation["evidenceRefs"]:
            ref["lineNumber"] = invalid_line
    _rewrite(source, rows)
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="invalid evidence line"):
        _build(source, tmp_path / "invalid-controlled-line")


@pytest.mark.parametrize("invalid_line", [True, 0, -1, "1", 1.0, 2**63])
def test_audit_line_requires_strict_positive_integer(tmp_path, invalid_line):
    source = _input(tmp_path)
    rows = _rows(source)
    rows[0]["provenance"]["evidenceProductAuditLineNumber"] = invalid_line
    _rewrite(source, rows)
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="invalid provenance identity"):
        _build(source, tmp_path / "invalid-audit-line")


@pytest.mark.parametrize("invalid_count", [True, 0, -1, "1", 1.0, 2**63])
def test_provenance_counts_require_strict_positive_integer(tmp_path, invalid_count):
    source = _input(tmp_path)
    rows = _rows(source)
    rows[0]["provenance"]["sourceRefCount"] = invalid_count
    _rewrite(source, rows)
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="invalid provenance identity"):
        _build(source, tmp_path / "invalid-ref-count")


def test_cross_product_evidence_line_reuse_is_rejected(tmp_path):
    source = _input(tmp_path)
    rows = _rows(source)
    for observation in rows[1]["attributes"].values():
        for ref in observation["evidenceRefs"]:
            ref["lineNumber"] = 10
    _rewrite(source, rows)
    with pytest.raises(
        UsedPhoneCatalogSeedPlanError,
        match="evidence line 10 is bound to multiple products",
    ):
        _build(source, tmp_path / "out")


def test_cross_product_audit_line_identity_is_rejected(tmp_path):
    source = _input(tmp_path)
    rows = _rows(source)
    rows[1]["provenance"]["evidenceProductAuditLineNumber"] = 1
    _rewrite(source, rows)
    with pytest.raises(
        UsedPhoneCatalogSeedPlanError,
        match="audit line 1 is bound to multiple items",
    ):
        _build(source, tmp_path / "out")


def test_frozen_canonical_value_tamper_is_rejected(tmp_path):
    source = _input(tmp_path)
    rows = _rows(source)
    rows[1]["attributes"]["battery_originality"]["value"] = "original"
    _rewrite(source, rows)
    with pytest.raises(
        UsedPhoneCatalogSeedPlanError,
        match="frozen/production observation mismatch",
    ):
        _build(source, tmp_path / "out")


def test_missing_group_and_wrong_status_counts_are_rejected(tmp_path):
    source = _input(tmp_path)
    rows = _rows(source)
    del rows[0]["attributes"]["shell_condition"]
    _rewrite(source, rows)
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="seven-group mismatch"):
        _build(source, tmp_path / "missing-group")

    source = _input(tmp_path / "count")
    wrong_counts = {
        **FIXTURE_STATUS_COUNTS,
        "os": {"known": 2, "conflict": 0, "unknown": 0},
    }
    with (
        patch.object(plan_module, "EXPECTED_CATALOG_SHA256", _sha(source)),
        patch.object(plan_module, "EXPECTED_PRODUCT_COUNT", 2),
        patch.object(plan_module, "EXPECTED_STATUS_COUNTS", wrong_counts),
    ):
        with pytest.raises(
            UsedPhoneCatalogSeedPlanError,
            match="status counts do not match",
        ):
            build_used_phone_catalog_seed_plan(
                catalog_path=source,
                output_dir=tmp_path / "wrong-counts",
            )


def test_ref_token_tamper_and_wrong_basename_are_rejected(tmp_path):
    source = _input(tmp_path)
    rows = _rows(source)
    rows[0]["attributes"]["os"]["evidenceRefs"][0]["matchedRawTokens"] = [
        "安卓"
    ]
    _rewrite(source, rows)
    with pytest.raises(UsedPhoneCatalogSeedPlanError, match="token mismatch"):
        _build(source, tmp_path / "token-tamper")

    wrong_name = tmp_path / "input" / "other.jsonl"
    shutil.copyfile(source, wrong_name)
    with _fixture_pins(wrong_name):
        with pytest.raises(UsedPhoneCatalogSeedPlanError, match="must be named"):
            build_used_phone_catalog_seed_plan(
                catalog_path=wrong_name,
                output_dir=tmp_path / "wrong-name",
            )
