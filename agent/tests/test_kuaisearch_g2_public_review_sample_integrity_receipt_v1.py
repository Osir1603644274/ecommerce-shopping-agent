from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = REPO_ROOT / "agent"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from evaluation import kuaisearch_g2_public_review_sample_integrity_receipt_v1 as receipt_module  # noqa: E402


def _load_schema() -> dict:
    schema = json.loads(receipt_module.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _validate_def(schema: dict, def_name: str, payload: dict) -> None:
    Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": schema["$defs"]}).validate(payload)


def _load_bundle(output_dir: Path) -> tuple[dict, dict]:
    receipt = json.loads((output_dir / "receipt.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    return receipt, manifest


def _patched_allowed_paths(
    original_allowed: tuple[Path, ...], original_path: Path, replacement_path: Path
) -> tuple[Path, ...]:
    return tuple(replacement_path if path == original_path else path for path in original_allowed)


def _patched_frozen_metadata(
    original_metadata: dict[str, dict], original_path: Path, replacement_path: Path
) -> dict[str, dict]:
    metadata = dict(original_metadata)
    previous = metadata.pop(str(original_path.resolve()))
    replacement_bytes = replacement_path.read_bytes()
    replacement_row_count = None
    if replacement_path.suffix == ".jsonl":
        replacement_row_count = sum(
            1 for line in replacement_path.read_text(encoding="utf-8").splitlines() if line.strip()
        )
    metadata[str(replacement_path.resolve())] = {
        **previous,
        "sha256": receipt_module._sha256_bytes(replacement_bytes),
        "bytes": len(replacement_bytes),
        **({"rows": replacement_row_count} if replacement_row_count is not None else {}),
    }
    return metadata


def test_materialize_and_validate_integrity_receipt(tmp_path: Path) -> None:
    schema = _load_schema()
    output_dir = tmp_path / receipt_module.ATTEMPT_ID
    receipt_module.materialize_integrity_receipt(output_dir)
    receipt, manifest = _load_bundle(output_dir)
    _validate_def(schema, "receipt", receipt)
    _validate_def(schema, "manifest", manifest)
    assert receipt["status"] == "G2_PUBLIC_REVIEW_SAMPLE_INTEGRITY_READY_HUMAN_REVIEW_PENDING"
    assert receipt["summary"] == {
        "poolRows": 21459,
        "sampleRows": 508,
        "uniqueQueries": 48,
        "trainQueries": 36,
        "testQueries": 12,
        "maxRowsPerQuery": 15,
    }
    assert len(receipt["publicReviewRows"]) == 508
    receipt_module.validate_bundle(output_dir)


def test_frozen_source_metadata_covers_exact_allowed_inputs() -> None:
    assert len(receipt_module.ALLOWED_SOURCE_PATHS) == 6
    assert len(receipt_module.FROZEN_SOURCE_METADATA) == 6
    for path in receipt_module.ALLOWED_SOURCE_PATHS:
        metadata = receipt_module.FROZEN_SOURCE_METADATA[str(path.resolve())]
        payload = path.read_bytes()
        assert metadata["sha256"] == receipt_module._sha256_bytes(payload)
        assert metadata["bytes"] == len(payload)


def test_two_fresh_builds_are_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "a" / receipt_module.ATTEMPT_ID
    second = tmp_path / "b" / receipt_module.ATTEMPT_ID
    receipt_module.materialize_integrity_receipt(first)
    receipt_module.materialize_integrity_receipt(second)
    assert (first / "receipt.json").read_bytes() == (second / "receipt.json").read_bytes()
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()


def test_validate_rejects_receipt_full_resign_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / receipt_module.ATTEMPT_ID
    receipt_module.materialize_integrity_receipt(output_dir)
    receipt, _ = _load_bundle(output_dir)
    receipt["summary"]["sampleRows"] = 507
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    forged_manifest = receipt_module._manifest_payload(output_dir)
    (output_dir / "manifest.json").write_text(
        json.dumps(forged_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        receipt_module.validate_bundle(output_dir)


def test_validate_rejects_unknown_pair_same_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_sample_path = receipt_module.G2_PHASE_A_PUBLIC_SAMPLE_PATH
    original_allowed = receipt_module.ALLOWED_SOURCE_PATHS
    original_metadata = receipt_module.FROZEN_SOURCE_METADATA
    rows = [
        json.loads(line)
        for line in original_sample_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[0]["blindCandidateId"] = "0" * 64
    fixture = tmp_path / "public_review_sample.jsonl"
    fixture.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(receipt_module, "G2_PHASE_A_PUBLIC_SAMPLE_PATH", fixture)
    monkeypatch.setattr(
        receipt_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_sample_path, fixture),
    )
    monkeypatch.setattr(
        receipt_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_sample_path, fixture),
    )
    pool = receipt_module._load_public_pool_rows()
    with pytest.raises(ValueError, match="outside pool"):
        receipt_module._load_public_sample_rows(pool)


def test_validate_rejects_duplicate_pair_same_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_sample_path = receipt_module.G2_PHASE_A_PUBLIC_SAMPLE_PATH
    original_allowed = receipt_module.ALLOWED_SOURCE_PATHS
    original_metadata = receipt_module.FROZEN_SOURCE_METADATA
    rows = [
        json.loads(line)
        for line in original_sample_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[-1] = dict(rows[0])
    rows[-1]["reviewItemId"] = "G2A-999"
    fixture = tmp_path / "public_review_sample.jsonl"
    fixture.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(receipt_module, "G2_PHASE_A_PUBLIC_SAMPLE_PATH", fixture)
    monkeypatch.setattr(
        receipt_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_sample_path, fixture),
    )
    monkeypatch.setattr(
        receipt_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_sample_path, fixture),
    )
    pool = receipt_module._load_public_pool_rows()
    with pytest.raises(ValueError, match="duplicate public sample pair"):
        receipt_module._load_public_sample_rows(pool)


def test_validate_rejects_field_drift_against_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_sample_path = receipt_module.G2_PHASE_A_PUBLIC_SAMPLE_PATH
    original_allowed = receipt_module.ALLOWED_SOURCE_PATHS
    original_metadata = receipt_module.FROZEN_SOURCE_METADATA
    rows = [
        json.loads(line)
        for line in original_sample_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows[0]["title"] = rows[0]["title"] + " drift"
    fixture = tmp_path / "public_review_sample.jsonl"
    fixture.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(receipt_module, "G2_PHASE_A_PUBLIC_SAMPLE_PATH", fixture)
    monkeypatch.setattr(
        receipt_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_sample_path, fixture),
    )
    monkeypatch.setattr(
        receipt_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_sample_path, fixture),
    )
    pool = receipt_module._load_public_pool_rows()
    with pytest.raises(ValueError, match="drift against pool"):
        receipt_module._load_public_sample_rows(pool)


def test_validate_rejects_task030_binding_tamper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_snapshot_path = receipt_module.TASK030_SNAPSHOT_PATH
    original_allowed = receipt_module.ALLOWED_SOURCE_PATHS
    original_metadata = receipt_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "snapshot.json"
    snapshot = json.loads(original_snapshot_path.read_text(encoding="utf-8"))
    snapshot["status"] = "FORGED"
    fixture.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(receipt_module, "TASK030_SNAPSHOT_PATH", fixture)
    monkeypatch.setattr(
        receipt_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_snapshot_path, fixture),
    )
    monkeypatch.setattr(
        receipt_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_snapshot_path, fixture),
    )
    with pytest.raises(ValueError, match="task030 snapshot status drift"):
        receipt_module._validate_task030_binding()


def test_validate_rejects_pool_manifest_path_tamper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_manifest_path = receipt_module.G2_POOL_PUBLIC_MANIFEST_PATH
    original_allowed = receipt_module.ALLOWED_SOURCE_PATHS
    original_metadata = receipt_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "manifest.json"
    manifest = json.loads(original_manifest_path.read_text(encoding="utf-8"))
    manifest["artifact"] = {"path": "../outside.jsonl", "sha256": "0" * 64, "bytes": 1, "rows": 21459}
    fixture.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(receipt_module, "G2_POOL_PUBLIC_MANIFEST_PATH", fixture)
    monkeypatch.setattr(
        receipt_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_manifest_path, fixture),
    )
    monkeypatch.setattr(
        receipt_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_manifest_path, fixture),
    )
    with pytest.raises(ValueError, match="artifact drift"):
        receipt_module._validate_public_pool_manifest()


def test_validate_rejects_banned_output_key(tmp_path: Path) -> None:
    output_dir = tmp_path / receipt_module.ATTEMPT_ID
    receipt_module.materialize_integrity_receipt(output_dir)
    receipt, _ = _load_bundle(output_dir)
    receipt["rank"] = 1
    (output_dir / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="banned output key marker"):
        receipt_module.validate_bundle(output_dir)


def test_validate_rejects_extra_regular_file(tmp_path: Path) -> None:
    output_dir = tmp_path / receipt_module.ATTEMPT_ID
    receipt_module.materialize_integrity_receipt(output_dir)
    (output_dir / "extra.json").write_text('{"ok":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected output artifact set"):
        receipt_module.validate_bundle(output_dir)


def test_read_bytes_rejects_source_reparse_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "source.json"
    fixture.write_text('{"ok":true}\n', encoding="utf-8")

    class ReparseStat:
        st_file_attributes = 0x0400
        st_mode = 0o100644

    monkeypatch.setattr(receipt_module, "ALLOWED_SOURCE_PATHS", (fixture,))
    monkeypatch.setattr(
        receipt_module,
        "FROZEN_SOURCE_METADATA",
        {
            str(fixture.resolve()): {
                "label": "fixture",
                "sha256": receipt_module._sha256_bytes(fixture.read_bytes()),
                "bytes": len(fixture.read_bytes()),
            }
        },
    )
    monkeypatch.setattr(receipt_module, "_path_lstat", lambda path: ReparseStat())
    with pytest.raises(ValueError, match="source reparse point rejected"):
        receipt_module._read_bytes(fixture)


def test_module_source_has_no_external_access_patterns() -> None:
    source = receipt_module.MODULE_PATH.read_text(encoding="utf-8")
    assert "requests" not in source
    assert "httpx" not in source
    assert "subprocess" not in source
    assert "agent.app" not in source
