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

from evaluation import kuaisearch_multicategory_public_evidence_snapshot_v1 as snapshot_module  # noqa: E402


def _load_schema() -> dict:
    schema = json.loads(snapshot_module.SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _validate_def(schema: dict, def_name: str, payload: dict) -> None:
    Draft202012Validator({"$ref": f"#/$defs/{def_name}", "$defs": schema["$defs"]}).validate(payload)


def _load_bundle(output_dir: Path) -> tuple[dict, dict]:
    snapshot = json.loads((output_dir / "snapshot.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    return snapshot, manifest


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
        "sha256": snapshot_module._sha256_bytes(replacement_bytes),
        "bytes": len(replacement_bytes),
        **({"rows": replacement_row_count} if replacement_row_count is not None else {}),
    }
    return metadata


def test_materialize_and_validate_public_snapshot(tmp_path: Path) -> None:
    schema = _load_schema()
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    snapshot, manifest = _load_bundle(output_dir)
    _validate_def(schema, "snapshot", snapshot)
    _validate_def(schema, "manifest", manifest)
    assert snapshot["status"] == "PUBLIC_EVIDENCE_READY_BENCHMARK_DECISION_PENDING"
    assert snapshot["separateFromUsedPhone439"] is True
    assert snapshot["publicEvidence"] == {
        "corpusDocuments": 46079,
        "publicQueries": 507,
        "breadthVerticals": 12,
        "breadthDocuments": 499,
        "publicBlindRows": 21459,
        "phaseAPublicSampleRows": 508,
    }
    assert snapshot["stageStates"]["g0"] == "PREPARED_NOT_RANKED"
    assert snapshot["stageStates"]["g1"] == "ACCEPT_G1_OUTPUT_G2_PENDING"
    assert snapshot["stageStates"]["g2"] == "ACCEPT_HUMAN_REVIEW_PACKAGE / HUMAN_REVIEW_PENDING"
    snapshot_module.validate_bundle(output_dir)


def test_frozen_source_metadata_covers_all_11_public_inputs() -> None:
    assert len(snapshot_module.ALLOWED_SOURCE_PATHS) == 11
    assert len(snapshot_module.FROZEN_SOURCE_METADATA) == 11
    for path in snapshot_module.ALLOWED_SOURCE_PATHS:
        metadata = snapshot_module.FROZEN_SOURCE_METADATA[str(path.resolve())]
        payload = path.read_bytes()
        assert metadata["sha256"] == snapshot_module._sha256_bytes(payload)
        assert metadata["bytes"] == len(payload)


def test_two_fresh_builds_are_byte_identical(tmp_path: Path) -> None:
    first = tmp_path / "a" / snapshot_module.ATTEMPT_ID
    second = tmp_path / "b" / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(first)
    snapshot_module.materialize_public_evidence_snapshot(second)
    assert (first / "snapshot.json").read_bytes() == (second / "snapshot.json").read_bytes()
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()


def test_read_spy_stays_within_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[Path] = []
    original = snapshot_module._read_bytes

    def recording_read(path: Path) -> bytes:
        seen.append(path.resolve())
        return original(path)

    monkeypatch.setattr(snapshot_module, "_read_bytes", recording_read)
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    assert set(seen).issubset({path.resolve() for path in snapshot_module.ALLOWED_SOURCE_PATHS})


def test_validate_rejects_snapshot_count_tamper_full_resign(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    snapshot, manifest = _load_bundle(output_dir)
    snapshot["publicEvidence"]["corpusDocuments"] = 99999
    (output_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_stage_winner_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    snapshot, manifest = _load_bundle(output_dir)
    snapshot["stageStates"]["g1"] = "WINNER_SELECTED"
    (output_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_full_resign_snapshot_and_manifest_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    snapshot, _ = _load_bundle(output_dir)
    snapshot["publicEvidence"]["breadthDocuments"] = 498
    (output_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    forged_manifest = snapshot_module._manifest_payload(output_dir)
    (output_dir / "manifest.json").write_text(
        json.dumps(forged_manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_duplicate_query_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original_queries_path = snapshot_module.G0_QUERIES_PATH
    original_allowed = snapshot_module.ALLOWED_SOURCE_PATHS
    original_metadata = snapshot_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "queries.jsonl"
    fixture.write_text(
        '{"query":"a","queryId":"dup","split":"train"}\n{"query":"b","queryId":"dup","split":"test"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(snapshot_module, "G0_QUERIES_PATH", fixture)
    monkeypatch.setattr(
        snapshot_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_queries_path, fixture),
    )
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_queries_path, fixture),
    )
    with pytest.raises(ValueError, match="duplicate queryId"):
        snapshot_module._count_jsonl_and_query_ids(fixture)


def test_validate_rejects_non_json_g0_documents_even_with_46079_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_documents_path = snapshot_module.G0_DOCUMENTS_PATH
    original_allowed = snapshot_module.ALLOWED_SOURCE_PATHS
    original_metadata = snapshot_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "documents.jsonl"
    fixture.write_text(("not-json\n" * 46079), encoding="utf-8")
    monkeypatch.setattr(snapshot_module, "G0_DOCUMENTS_PATH", fixture)
    monkeypatch.setattr(
        snapshot_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_documents_path, fixture),
    )
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_documents_path, fixture),
    )
    with pytest.raises(json.JSONDecodeError):
        snapshot_module._load_g0_documents()


def test_validate_rejects_unknown_public_blind_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original_rows_path = snapshot_module.G2_POOL_ROWS_PATH
    original_allowed = snapshot_module.ALLOWED_SOURCE_PATHS
    original_metadata = snapshot_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "review_rows.jsonl"
    fixture.write_text(
        '{"schemaVersion":"kuaisearch-multicategory-retrieval-g2-blind-pool-v1","blindCandidateId":"x","queryId":"ghost","query":"q","title":"t","brand":"b","seller_name":"s","attr_value":"a","poolPosition":1}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(snapshot_module, "G2_POOL_ROWS_PATH", fixture)
    monkeypatch.setattr(
        snapshot_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_rows_path, fixture),
    )
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_rows_path, fixture),
    )
    with pytest.raises(ValueError, match="outside 507 set"):
        snapshot_module._validate_public_blind_rows({"known"})


def test_validate_rejects_g1_manifest_winner_status_extra_even_if_resigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_manifest_path = snapshot_module.G1_MANIFEST_PATH
    original_allowed = snapshot_module.ALLOWED_SOURCE_PATHS
    original_metadata = snapshot_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "manifest.json"
    manifest = json.loads(original_manifest_path.read_text(encoding="utf-8"))
    manifest["winner"] = "forged"
    manifest["status"] = "FORGED"
    fixture.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(snapshot_module, "G1_MANIFEST_PATH", fixture)
    monkeypatch.setattr(
        snapshot_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_manifest_path, fixture),
    )
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_manifest_path, fixture),
    )
    with pytest.raises(ValueError, match="forbidden status or winner"):
        snapshot_module._validate_g1_manifest()


def test_validate_rejects_g1_historical_input_path_without_expected_suffix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_manifest_path = snapshot_module.G1_MANIFEST_PATH
    original_allowed = snapshot_module.ALLOWED_SOURCE_PATHS
    original_metadata = snapshot_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "manifest.json"
    manifest = json.loads(original_manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"]["documents"]["path"] = r"C:\\forged\\documents.jsonl"
    fixture.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(snapshot_module, "G1_MANIFEST_PATH", fixture)
    monkeypatch.setattr(
        snapshot_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_manifest_path, fixture),
    )
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_manifest_path, fixture),
    )
    with pytest.raises(ValueError, match="documents provenance path drift"):
        snapshot_module._validate_g1_manifest()


def test_validate_rejects_g2_manifest_path_traversal_hash_bytes_even_if_resigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_manifest_path = snapshot_module.G2_POOL_MANIFEST_PATH
    original_allowed = snapshot_module.ALLOWED_SOURCE_PATHS
    original_metadata = snapshot_module.FROZEN_SOURCE_METADATA
    fixture = tmp_path / "manifest.json"
    manifest = json.loads(original_manifest_path.read_text(encoding="utf-8"))
    manifest["artifact"] = {
        "path": "../outside.jsonl",
        "sha256": "0" * 64,
        "bytes": 1,
        "rows": 21459,
    }
    fixture.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    monkeypatch.setattr(snapshot_module, "G2_POOL_MANIFEST_PATH", fixture)
    monkeypatch.setattr(
        snapshot_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_manifest_path, fixture),
    )
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_manifest_path, fixture),
    )
    with pytest.raises(ValueError, match="artifact drift"):
        snapshot_module._validate_g2_public_manifest()


def test_validate_rejects_public_blind_row_duplicate_replace_even_if_resigned(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    original_rows_path = snapshot_module.G2_POOL_ROWS_PATH
    original_allowed = snapshot_module.ALLOWED_SOURCE_PATHS
    original_metadata = snapshot_module.FROZEN_SOURCE_METADATA
    lines = [
        line
        for line in original_rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    rows = [json.loads(line) for line in lines]
    replacement_rows = rows[:-1] + [dict(rows[0])]
    fixture = tmp_path / "review_rows.jsonl"
    fixture.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in replacement_rows) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(snapshot_module, "G2_POOL_ROWS_PATH", fixture)
    monkeypatch.setattr(
        snapshot_module,
        "ALLOWED_SOURCE_PATHS",
        _patched_allowed_paths(original_allowed, original_rows_path, fixture),
    )
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        _patched_frozen_metadata(original_metadata, original_rows_path, fixture),
    )
    query_rows, query_map = snapshot_module._count_jsonl_and_query_ids(snapshot_module.G0_QUERIES_PATH)
    assert query_rows == 507
    with pytest.raises(ValueError, match="duplicate blind candidate identity|duplicate blind pool position"):
        snapshot_module._validate_public_blind_rows(query_map)


@pytest.mark.parametrize("bad_key", ["strategy", "rank", "docId", "grade", "category", "salt"])
def test_validate_rejects_sensitive_output_field_tamper(tmp_path: Path, bad_key: str) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    snapshot, _ = _load_bundle(output_dir)
    snapshot[bad_key] = "leak"
    (output_dir / "snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_extra_regular_file(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    (output_dir / "extra.json").write_text('{"ok":true}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unexpected output artifact set"):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_nested_directory(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    (output_dir / "nested").mkdir()
    with pytest.raises(ValueError, match="unexpected output artifact set"):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_mocked_root_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    monkeypatch.setattr(snapshot_module.Path, "is_symlink", lambda self: self == output_dir)
    with pytest.raises(ValueError, match="bundle directory symlink rejected"):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_mocked_root_reparse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)

    class ReparseStat:
        st_file_attributes = 0x0400

    monkeypatch.setattr(snapshot_module, "_bundle_dir_lstat", lambda path: ReparseStat())
    with pytest.raises(ValueError, match="bundle directory reparse point rejected"):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_source_artifact_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    _, manifest = _load_bundle(output_dir)
    manifest["artifacts"][0]["bytes"] += 1
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_canonical_digest_full_resign_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    _, manifest = _load_bundle(output_dir)
    manifest["canonicalDigest"] = "0" * 64
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest drift"):
        snapshot_module.validate_bundle(output_dir)


def test_validate_rejects_code_pins_full_resign_tamper(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    snapshot_module.materialize_public_evidence_snapshot(output_dir)
    _, manifest = _load_bundle(output_dir)
    manifest["codePins"][0]["sha256"] = "0" * 64
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="manifest drift"):
        snapshot_module.validate_bundle(output_dir)


def test_materialize_rejects_non_empty_output_dir(tmp_path: Path) -> None:
    output_dir = tmp_path / snapshot_module.ATTEMPT_ID
    output_dir.mkdir(parents=True)
    (output_dir / "already.txt").write_text("occupied\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        snapshot_module.materialize_public_evidence_snapshot(output_dir)


def test_read_bytes_rejects_source_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "source.json"
    fixture.write_text('{"ok":true}\n', encoding="utf-8")
    monkeypatch.setattr(snapshot_module.Path, "is_symlink", lambda self: self == fixture)
    monkeypatch.setattr(snapshot_module, "ALLOWED_SOURCE_PATHS", (fixture,))
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        {
            str(fixture.resolve()): {
                "label": "fixture",
                "sha256": snapshot_module._sha256_bytes(fixture.read_bytes()),
                "bytes": len(fixture.read_bytes()),
            }
        },
    )
    with pytest.raises(ValueError, match="source symlink rejected"):
        snapshot_module._read_bytes(fixture)


def test_read_bytes_rejects_source_reparse_point(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "source.json"
    fixture.write_text('{"ok":true}\n', encoding="utf-8")

    class ReparseStat:
        st_file_attributes = 0x0400
        st_mode = 0o100644

    monkeypatch.setattr(snapshot_module, "ALLOWED_SOURCE_PATHS", (fixture,))
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        {
            str(fixture.resolve()): {
                "label": "fixture",
                "sha256": snapshot_module._sha256_bytes(fixture.read_bytes()),
                "bytes": len(fixture.read_bytes()),
            }
        },
    )
    monkeypatch.setattr(snapshot_module, "_path_lstat", lambda path: ReparseStat())
    with pytest.raises(ValueError, match="source reparse point rejected"):
        snapshot_module._read_bytes(fixture)


def test_read_bytes_rejects_source_non_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fixture = tmp_path / "source.json"
    fixture.write_text('{"ok":true}\n', encoding="utf-8")

    class NonRegularStat:
        st_file_attributes = 0
        st_mode = 0o040755

    monkeypatch.setattr(snapshot_module, "ALLOWED_SOURCE_PATHS", (fixture,))
    monkeypatch.setattr(
        snapshot_module,
        "FROZEN_SOURCE_METADATA",
        {
            str(fixture.resolve()): {
                "label": "fixture",
                "sha256": snapshot_module._sha256_bytes(fixture.read_bytes()),
                "bytes": len(fixture.read_bytes()),
            }
        },
    )
    monkeypatch.setattr(snapshot_module, "_path_lstat", lambda path: NonRegularStat())
    with pytest.raises(ValueError, match="source non-regular file rejected"):
        snapshot_module._read_bytes(fixture)


def test_module_source_has_no_external_access_patterns() -> None:
    source = snapshot_module.MODULE_PATH.read_text(encoding="utf-8")
    assert "requests" not in source
    assert "httpx" not in source
    assert "subprocess" not in source
    assert "agent.app" not in source
