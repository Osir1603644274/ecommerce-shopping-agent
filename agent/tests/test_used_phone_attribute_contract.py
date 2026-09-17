from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import evaluation.used_phone_attribute_contract as contract_module
from evaluation.used_phone_attribute_contract import (
    AttributeContractError,
    DATASET_REVISION,
    build_used_phone_attribute_contract,
)


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "used_phone_attribute_contract"
    / "evidence_products_audit.jsonl"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _input(tmp_path: Path) -> Path:
    path = tmp_path / "input" / "evidence_products_audit.jsonl"
    path.parent.mkdir()
    shutil.copyfile(FIXTURE, path)
    return path


def _build(input_path: Path, output_dir: Path):
    with _fixture_pins(input_path):
        return build_used_phone_attribute_contract(
            input_path=input_path,
            output_dir=output_dir,
            expected_input_sha256=_sha(input_path),
            dataset_revision=DATASET_REVISION,
            expected_item_count=3,
        )


@contextmanager
def _fixture_pins(input_path: Path):
    """Exercise the builder with a repository fixture without weakening real pins."""

    with (
        patch.object(contract_module, "PINNED_INPUT_SHA256", _sha(input_path)),
        patch.object(contract_module, "EXPECTED_ITEM_COUNT", 3),
    ):
        yield


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


def test_fixture_build_is_explicit_grounded_and_audited(tmp_path: Path):
    input_path = _input(tmp_path)
    output = tmp_path / "out"
    result = _build(input_path, output)

    rows = _rows(output / "catalog.jsonl")
    assert [row["itemId"] for row in rows] == ["p1", "p2", "p3"]
    assert tuple(rows[0]["attributes"]) == (
        "battery_health",
        "os",
        "screen_originality",
    )
    known = rows[0]["attributes"]["os"]
    assert known["status"] == "known"
    assert known["fact"]["value"] == "ios"
    assert known["matchedRawTokens"] == ["IoS"]
    assert known["evidenceRefs"] == [
        {
            "field": "attr_value",
            "lineNumber": 10,
            "matchedRawTokens": ["IoS"],
            "rawValue": "IoS,90%+,原装屏",
            "source": "relevance",
        }
    ]
    assert known["semanticStatus"] == (
        "controlled_interpretation_not_source_ground_truth"
    )
    assert known["humanConfirmed"] is False
    assert rows[1]["attributes"]["os"]["status"] == "conflict"
    assert rows[1]["attributes"]["battery_health"]["status"] == "conflict"
    assert all(
        observation["status"] == "unknown"
        for observation in rows[2]["attributes"].values()
    )

    audit = result["audit"]
    assert audit["itemCount"] == 3
    assert audit["input"]["ignoredRowCount"] == 1
    assert audit["sourceEvidenceRefs"] == {
        "duplicateRawValueRefCount": 0,
        "duplicateRefCount": 1,
        "multiRefProductCount": 1,
        "sourceRefCount": 4,
        "uniqueRefCount": 3,
    }
    assert audit["attributes"]["screen_originality"] == {
        "conflictCount": 0,
        "knownCount": 2,
        "knownCoverage": 0.666667,
        "unknownCount": 1,
        "valueDistribution": {"non_original": 1, "original": 1},
    }
    manifest = result["manifest"]
    assert manifest["boundaries"]["readsOnlyEvidenceProductsAudit"] is True
    assert manifest["boundaries"]["networkUsed"] is False
    assert manifest["outputs"]["catalog.jsonl"]["sha256"] == _sha(
        output / "catalog.jsonl"
    )
    assert manifest["outputs"]["audit.json"]["sha256"] == _sha(
        output / "audit.json"
    )
    manifest_zeroed = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    expected_self_digest = manifest_zeroed["outputs"]["manifest.json"]["sha256"]
    manifest_zeroed["outputs"]["manifest.json"]["sha256"] = "0" * 64
    payload = (
        json.dumps(
            manifest_zeroed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    assert hashlib.sha256(payload).hexdigest() == expected_self_digest


def test_wrong_sha_and_revision_are_rejected_before_output(tmp_path: Path):
    input_path = _input(tmp_path)
    with pytest.raises(AttributeContractError, match="repository-pinned"):
        build_used_phone_attribute_contract(
            input_path=input_path,
            output_dir=tmp_path / "bad-sha",
            expected_input_sha256="0" * 64,
            dataset_revision=DATASET_REVISION,
            expected_item_count=3,
        )
    with _fixture_pins(input_path):
        with pytest.raises(AttributeContractError, match="pinned full revision"):
            build_used_phone_attribute_contract(
                input_path=input_path,
                output_dir=tmp_path / "bad-revision",
                expected_input_sha256=_sha(input_path),
                dataset_revision=DATASET_REVISION[:-1] + "0",
                expected_item_count=3,
            )


def test_duplicate_target_item_is_rejected(tmp_path: Path):
    input_path = _input(tmp_path)
    rows = _rows(input_path)
    rows.append(rows[0])
    _rewrite(input_path, rows)
    with pytest.raises(AttributeContractError, match="duplicate target itemId"):
        _build(input_path, tmp_path / "out")


def test_controlled_normalized_token_without_exact_raw_ref_is_rejected(
    tmp_path: Path,
):
    input_path = _input(tmp_path)
    rows = _rows(input_path)
    rows[2]["normalizedAttrValues"].append("ios")
    _rewrite(input_path, rows)
    with pytest.raises(AttributeContractError, match="lack matching exact raw evidence"):
        _build(input_path, tmp_path / "out")


def test_wrong_input_basename_and_existing_output_are_rejected(tmp_path: Path):
    input_path = _input(tmp_path)
    wrong_name = tmp_path / "input" / "other.jsonl"
    shutil.copyfile(input_path, wrong_name)
    with _fixture_pins(wrong_name):
        with pytest.raises(AttributeContractError, match="input must be named"):
            build_used_phone_attribute_contract(
                input_path=wrong_name,
                output_dir=tmp_path / "wrong-name",
                expected_input_sha256=_sha(wrong_name),
                dataset_revision=DATASET_REVISION,
                expected_item_count=3,
            )

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        _build(input_path, existing)


def test_two_builds_are_byte_identical(tmp_path: Path):
    input_path = _input(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    _build(input_path, first)
    _build(input_path, second)
    for name in ("catalog.jsonl", "audit.json", "manifest.json"):
        assert (first / name).read_bytes() == (second / name).read_bytes()


def test_non_target_rows_are_not_validated_as_used_phone_products(tmp_path: Path):
    input_path = _input(tmp_path)
    rows = _rows(input_path)
    rows[-1] = {"categoryKey": "not-target", "arbitrary": object.__name__}
    _rewrite(input_path, rows)
    result = _build(input_path, tmp_path / "out")
    assert result["audit"]["input"]["ignoredRowCount"] == 1


def test_cross_product_relevance_line_owner_is_rejected(tmp_path: Path):
    input_path = _input(tmp_path)
    rows = _rows(input_path)
    rows[2]["evidenceRefs"] = [dict(rows[0]["evidenceRefs"][0])]
    rows[2]["normalizedAttrValues"] = list(rows[0]["normalizedAttrValues"])
    _rewrite(input_path, rows)
    with pytest.raises(AttributeContractError, match="exactly one item/rawValue"):
        _build(input_path, tmp_path / "out")


def test_caller_cannot_repin_mutated_input(tmp_path: Path):
    input_path = _input(tmp_path)
    rows = _rows(input_path)
    rows[2]["title"] = "tampered"
    _rewrite(input_path, rows)
    with pytest.raises(AttributeContractError, match="repository-pinned"):
        build_used_phone_attribute_contract(
            input_path=input_path,
            output_dir=tmp_path / "out",
            expected_input_sha256=_sha(input_path),
            dataset_revision=DATASET_REVISION,
            expected_item_count=3,
        )
