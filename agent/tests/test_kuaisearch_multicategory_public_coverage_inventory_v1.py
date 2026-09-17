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

from evaluation import kuaisearch_multicategory_public_coverage_inventory_v1 as inventory_module  # noqa: E402


def _load_schema() -> dict:
    schema = json.loads(inventory_module.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _validate_def(schema: dict, def_name: str, payload: dict) -> None:
    Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": schema["$defs"]}).validate(payload)


def _load_bundle(output_dir: Path) -> tuple[dict, dict]:
    inventory = json.loads((output_dir / "inventory.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    return inventory, manifest


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
        "sha256": inventory_module._sha256_bytes(replacement_bytes),
        "bytes": len(replacement_bytes),
        **({"rows": replacement_row_count} if replacement_row_count is not None else {}),
    }
    return metadata


def test_materialize_and_validate_public_coverage_inventory(tmp_path: Path) -> None:
    schema = _load_schema()
    output_dir = tmp_path / inventory_module.ATTEMPT_ID
    inventory_module.materialize_public_coverage_inventory(output_dir)
    inventory, manifest = _load_bundle(output_dir)
    _validate_def(schema, "inventory", inventory)
    _validate_def(schema, "manifest", manifest)
    assert inventory["status"] == "PUBLIC_COVERAGE_INVENTORY_READY_G2_HUMAN_PENDING"
    assert inventory["summary"] == {"categories": 12, "documents": 499, "uniqueQueries": 507}
    assert len(inventory["coverage"]) == 12
    assert inventory["task030Binding"]["canonicalDigest"] == "e3e945f0d80c1926780e5a93b8db714ed5ec46f306dbc5ba5204fe422e02afb4"
    inventory_module.validate_bundle(output_dir)


def test_frozen_source_metadata_covers_exact_public_inputs() -> None:
    assert len(inventory_module.ALLOWED_SOURCE_PATHS) == 5
    assert len(inventory_module.FROZEN_SOURCE_METADATA) == 5
    assert all("review_rows" not in str(path).lower() for path in inventory_module.ALLOWED_SOURCE_PATHS)
    for path in inventory_module.ALLOWED_SOURCE_PATHS:
        metadata = inventory_module.FROZEN_SOURCE_METADATA[str(path.resolve())]
        payload = path.read_bytes()
        assert metadata["sha256"] == inventory_module._sha256_bytes(payload)
        assert metadata["bytes"] == len(payload)


def test_two_fresh_builds_are_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "a" / inventory_module.ATTEMPT_ID
    second = tmp_path / "b" / inventory_module.ATTEMPT_ID
    inventory_module.materialize_public_coverage_inventory(first)
    inventory_module.materialize_public_coverage_inventory(second)
    assert (first / "inventory.json").read_bytes() == (second / "inventory.json").read_bytes()
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()


def test_validate_rejects_inventory_full_resign_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / inventory_module.ATTEMPT_ID
    inventory_module.materialize_public_coverage_inventory(output_dir)
    inventory, _ = _load_bundle(output_dir)
    inventory["summary"]["documents"] = 498
    (output_dir / "inventory.json").write_text(
        json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    forged_manifest = inventory_module._manifest_payload(output_dir)
    (output_dir / "manifest.json").write_text(
        json.dumps(forged_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        inventory_module.validate_bundle(output_dir)


def test_validate_rejects_task030_binding_resign_tamper(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_snapshot_path = inventory_module.TASK030_SNAPSHOT_PATH
    original_allowed = inventory_module.ALLOWED_SOURCE_PATHS
    original_metadata = inventory_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "snapshot.json"
    snapshot = json.loads(original_snapshot_path.read_text(encoding="utf-8"))
    snapshot["status"] = "FORGED"
    fixture.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(inventory_module, "TASK030_SNAPSHOT_PATH", fixture)
    monkeypatch.setattr(
        inventory_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_snapshot_path, fixture),
    )
    monkeypatch.setattr(
        inventory_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_snapshot_path, fixture),
    )
    with pytest.raises(ValueError, match="task030 snapshot status drift"):
        inventory_module._validate_task030_binding()


def test_validate_rejects_document_count_preserving_duplicate_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_documents_path = inventory_module.BREADTH_DOCUMENTS_PATH
    original_allowed = inventory_module.ALLOWED_SOURCE_PATHS
    original_metadata = inventory_module.FROZEN_SOURCE_METADATA
    rows = [
        json.loads(line)
        for line in original_documents_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    forged_rows = rows[:-1] + [dict(rows[0])]
    fixture = tmp_path / "documents.jsonl"
    fixture.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in forged_rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(inventory_module, "BREADTH_DOCUMENTS_PATH", fixture)
    monkeypatch.setattr(
        inventory_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_documents_path, fixture),
    )
    monkeypatch.setattr(
        inventory_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_documents_path, fixture),
    )
    categories = inventory_module._load_categories()
    with pytest.raises(ValueError, match="breadth document identity drift"):
        inventory_module._load_documents(categories)


def test_validate_rejects_query_link_drift_same_row_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_queries_path = inventory_module.BREADTH_QUERIES_PATH
    original_allowed = inventory_module.ALLOWED_SOURCE_PATHS
    original_metadata = inventory_module.FROZEN_SOURCE_METADATA
    rows = [
        json.loads(line)
        for line in original_queries_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    forged = dict(rows[0])
    forged["linkedCategoryKeys"] = [rows[1]["linkedCategoryKeys"][0], rows[1]["linkedCategoryKeys"][0]]
    rows[0] = forged
    fixture = tmp_path / "queries.jsonl"
    fixture.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(inventory_module, "BREADTH_QUERIES_PATH", fixture)
    monkeypatch.setattr(
        inventory_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_queries_path, fixture),
    )
    monkeypatch.setattr(
        inventory_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_queries_path, fixture),
    )
    categories = inventory_module._load_categories()
    inventory_module._load_documents(categories)
    with pytest.raises(ValueError, match="breadth linked category drift"):
        inventory_module._load_queries(categories)


def test_validate_rejects_manifest_canonical_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / inventory_module.ATTEMPT_ID
    inventory_module.materialize_public_coverage_inventory(output_dir)
    _, manifest = _load_bundle(output_dir)
    manifest["canonicalDigest"] = "0" * 64
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest drift"):
        inventory_module.validate_bundle(output_dir)


def test_validate_rejects_manifest_code_pin_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / inventory_module.ATTEMPT_ID
    inventory_module.materialize_public_coverage_inventory(output_dir)
    _, manifest = _load_bundle(output_dir)
    manifest["codePins"][0]["sha256"] = "0" * 64
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest drift"):
        inventory_module.validate_bundle(output_dir)


def test_validate_rejects_extra_regular_file(tmp_path: Path) -> None:
    output_dir = tmp_path / inventory_module.ATTEMPT_ID
    inventory_module.materialize_public_coverage_inventory(output_dir)
    (output_dir / "extra.json").write_text('{"ok":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected output artifact set"):
        inventory_module.validate_bundle(output_dir)


def test_validate_rejects_mocked_root_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output_dir = tmp_path / inventory_module.ATTEMPT_ID
    inventory_module.materialize_public_coverage_inventory(output_dir)
    monkeypatch.setattr(inventory_module.Path, "is_symlink", lambda self: self == output_dir)
    with pytest.raises(ValueError, match="bundle directory symlink rejected"):
        inventory_module.validate_bundle(output_dir)


def test_read_bytes_rejects_source_reparse_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "source.json"
    fixture.write_text('{"ok":true}\n', encoding="utf-8")

    class ReparseStat:
        st_file_attributes = 0x0400
        st_mode = 0o100644

    monkeypatch.setattr(inventory_module, "ALLOWED_SOURCE_PATHS", (fixture,))
    monkeypatch.setattr(
        inventory_module,
        "FROZEN_SOURCE_METADATA",
        {
            str(fixture.resolve()): {
                "label": "fixture",
                "sha256": inventory_module._sha256_bytes(fixture.read_bytes()),
                "bytes": len(fixture.read_bytes()),
            }
        },
    )
    monkeypatch.setattr(inventory_module, "_path_lstat", lambda path: ReparseStat())
    with pytest.raises(ValueError, match="source reparse point rejected"):
        inventory_module._read_bytes(fixture)


def test_module_source_has_no_external_access_patterns() -> None:
    source = inventory_module.MODULE_PATH.read_text(encoding="utf-8")
    assert "requests" not in source
    assert "httpx" not in source
    assert "subprocess" not in source
    assert "agent.app" not in source
    assert "review_rows" not in source
