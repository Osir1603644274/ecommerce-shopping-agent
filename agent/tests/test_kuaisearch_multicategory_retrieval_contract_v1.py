from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from evaluation import kuaisearch_multicategory_retrieval_contract_v1 as contract


ROOT = Path(__file__).resolve().parents[2]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")


def _queries(count: int = 507) -> list[dict]:
    return [{"queryId": f"q-{i:03d}", "query": f"商品 {i}", "split": "test" if i % 2 else "train",
             "linkedCategoryKeys": ["input-only/category"]} for i in range(count)]


def test_projection_is_exactly_507_unique_and_label_free(tmp_path: Path) -> None:
    source = tmp_path / "queries.jsonl"
    _write_jsonl(source, _queries())
    rows = contract.project_queries(source)
    assert len(rows) == 507
    assert len({row["queryId"] for row in rows}) == 507
    assert all(set(row) == {"queryId", "query", "split"} for row in rows)
    assert "linkedCategoryKeys" not in json.dumps(rows, ensure_ascii=False)


def test_projection_rejects_duplicate_and_wrong_count(tmp_path: Path) -> None:
    source = tmp_path / "queries.jsonl"
    rows = _queries()
    rows[-1]["queryId"] = rows[0]["queryId"]
    _write_jsonl(source, rows)
    with pytest.raises(contract.ContractError, match="duplicate"):
        contract.project_queries(source)
    _write_jsonl(source, _queries(506))
    with pytest.raises(contract.ContractError, match="expected 507"):
        contract.project_queries(source)


def test_model_cache_missing_or_sha_drift_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(contract.ContractError, match="missing Dense"):
        contract.freeze_model_snapshot(tmp_path / "missing", label="Dense ONNX")
    cache = tmp_path / "dense"
    cache.mkdir()
    (cache / "model.onnx").write_bytes(b"stable")
    frozen = contract.freeze_model_snapshot(cache, label="Dense ONNX")
    (cache / "model.onnx").write_bytes(b"drifted")
    with pytest.raises(contract.ContractError, match="SHA drift"):
        contract.verify_snapshot_pin(frozen, label="Dense ONNX")


def test_cross_encoder_dependencies_missing_or_drift_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(contract.ContractError, match="missing CrossEncoder dependencies"):
        contract.freeze_model_snapshot(tmp_path / "missing", label="CrossEncoder dependencies")
    deps = tmp_path / "deps"
    deps.mkdir()
    (deps / "loader.py").write_bytes(b"stable")
    frozen = contract.freeze_model_snapshot(deps, label="CrossEncoder dependencies")
    pycache = deps / "__pycache__"
    pycache.mkdir()
    (pycache / "loader.cpython-312.pyc").write_bytes(b"volatile-1")
    (deps / "loader.tmp").write_bytes(b"volatile-1")
    assert contract.freeze_model_snapshot(deps, label="CrossEncoder dependencies")["snapshotSha256"] == frozen["snapshotSha256"]
    (pycache / "loader.cpython-312.pyc").write_bytes(b"volatile-2")
    (deps / "loader.tmp").write_bytes(b"volatile-2")
    assert contract.freeze_model_snapshot(deps, label="CrossEncoder dependencies")["snapshotSha256"] == frozen["snapshotSha256"]
    (deps / "loader.py").write_bytes(b"drift")
    with pytest.raises(contract.ContractError, match="SHA drift"):
        contract.verify_snapshot_pin(frozen, label="CrossEncoder dependencies")


def test_prepare_freezes_documents_and_refuses_nonempty_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    breadth_manifest = tmp_path / "manifest.json"
    breadth_manifest.write_text('{"dataset":"frozen"}\n', encoding="utf-8")
    queries = tmp_path / "queries.jsonl"
    _write_jsonl(queries, _queries())
    documents = tmp_path / "documents.jsonl"
    _write_jsonl(documents, [{"doc_id": f"d-{i}", "title": "商品", "brand": "品牌"} for i in range(46079)])
    dense = tmp_path / "dense"
    dense.mkdir()
    (dense / "model.onnx").write_bytes(b"dense")
    ce = tmp_path / "ce"
    ce.mkdir()
    (ce / "model.safetensors").write_bytes(b"ce")
    deps = tmp_path / "deps"
    deps.mkdir()
    (deps / "loader.py").write_bytes(b"deps")
    metadata = deps / "huggingface_hub-1.28.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: huggingface_hub\nVersion: 1.28.0\n", encoding="utf-8")
    monkeypatch.setattr(contract, "EXPECTED_BREADTH_MANIFEST_SHA256", contract.sha256_file(breadth_manifest))
    monkeypatch.setattr(contract, "EXPECTED_DOCUMENTS_SHA256", contract.sha256_file(documents))
    monkeypatch.setattr(contract, "EXPECTED_DOCUMENT_ROWS", 46079)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    code_paths = [
        ROOT / "agent/evaluation/kuaisearch_multicategory_retrieval_contract_v1.py",
        ROOT / "agent/scripts/prepare_kuaisearch_multicategory_retrieval_g0_v1.py",
        ROOT / "agent/evaluation/kuaisearch_multicategory_retrieval_baseline_v1.py",
        ROOT / "agent/scripts/run_kuaisearch_multicategory_retrieval_baseline_v1.py",
        ROOT / "agent/evaluation/kuaisearch_multicategory_retrieval_scorer_v1.py",
        ROOT / "agent/scripts/score_kuaisearch_multicategory_retrieval_g1_v1.py",
    ]
    output = tmp_path / "g0"
    result = contract.prepare_g0(output_dir=output, breadth_manifest=breadth_manifest, breadth_queries=queries,
                                documents=documents, dense_cache=dense, cross_encoder_cache=ce,
                                cross_encoder_deps=deps,
                                code_paths=code_paths)
    assert result["status"] == "PREPARED_NOT_RANKED"
    g0_schema = json.loads((ROOT / "agent/evaluation/schemas/kuaisearch_multicategory_retrieval_g0_v1.schema.json").read_text(encoding="utf-8"))
    models_schema = json.loads((ROOT / "agent/evaluation/schemas/kuaisearch_multicategory_retrieval_models_v1.schema.json").read_text(encoding="utf-8"))
    prereg_schema = json.loads((ROOT / "agent/evaluation/schemas/kuaisearch_multicategory_retrieval_preregistration_v1.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(g0_schema).validate(json.loads((output / "manifest.json").read_text(encoding="utf-8")))
    Draft202012Validator(models_schema).validate(json.loads((output / "models.manifest.json").read_text(encoding="utf-8")))
    Draft202012Validator(prereg_schema).validate(json.loads((output / "preregistration.json").read_text(encoding="utf-8")))
    verified = contract.verify_g0_bundle(g0_dir=output, runtime_code_paths=code_paths,
                                         dense_cache=dense, cross_cache=ce, cross_deps=deps, strict=True)
    assert verified["status"] == "VERIFIED"
    assert set(verified["g0Binding"]) == {"manifestSha256", "inputsManifestSha256", "modelsManifestSha256", "preregistrationSha256", "codePins"}
    assert len(verified["g0Binding"]["codePins"]) == 6
    embedded_g0 = output / "schemas" / contract.SCHEMA_BASENAMES[0]
    embedded_bytes = embedded_g0.read_bytes()
    embedded_g0.write_bytes(embedded_bytes + b"\n")
    with pytest.raises(contract.ContractError, match="artifact SHA/bytes drift"):
        contract.verify_g0_bundle(g0_dir=output, runtime_code_paths=code_paths,
                                  dense_cache=dense, cross_cache=ce, cross_deps=deps)
    embedded_g0.write_bytes(embedded_bytes)
    current_schema_dir = tmp_path / "current-schemas"
    current_schema_dir.mkdir()
    for schema_name in contract.SCHEMA_BASENAMES:
        source = ROOT / "agent/evaluation/schemas" / schema_name
        (current_schema_dir / schema_name).write_bytes(source.read_bytes())
    drifted_source = current_schema_dir / contract.SCHEMA_BASENAMES[-1]
    drifted_source.write_bytes(drifted_source.read_bytes() + b"\n")
    monkeypatch.setattr(contract, "schema_source_paths", lambda: [current_schema_dir / name for name in contract.SCHEMA_BASENAMES])
    with pytest.raises(contract.ContractError, match="current schema SHA drift"):
        contract.verify_g0_bundle(g0_dir=output, runtime_code_paths=code_paths,
                                  dense_cache=dense, cross_cache=ce, cross_deps=deps)
    monkeypatch.setattr(contract, "schema_source_paths", lambda: [ROOT / "agent/evaluation/schemas" / name for name in contract.SCHEMA_BASENAMES])
    (output / "queries.jsonl").write_bytes((output / "queries.jsonl").read_bytes() + b"\n")
    with pytest.raises(contract.ContractError, match="artifact SHA/bytes drift"):
        contract.verify_g0_bundle(g0_dir=output, runtime_code_paths=code_paths,
                                  dense_cache=dense, cross_cache=ce, cross_deps=deps)
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8"))["inputCount"] == {"queries": 507, "documents": 46079}
    projected = [json.loads(line) for line in (output / "queries.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(projected) == 507 and all(set(row) == {"queryId", "query", "split"} for row in projected)
    (output / "sentinel").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(FileExistsError, match="non-empty"):
        contract.prepare_g0(output_dir=output, breadth_manifest=breadth_manifest, breadth_queries=queries,
                           documents=documents, dense_cache=dense, cross_encoder_cache=ce,
                           cross_encoder_deps=deps,
                           code_paths=code_paths)


def test_preregistration_freezes_system_semantics_and_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "true")
    deps = tmp_path / "deps"
    metadata = deps / "huggingface_hub-1.28.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text("Name: huggingface_hub\nVersion: 1.28.0\n", encoding="utf-8")
    snapshots = {"root": str(deps), "files": [{"path": "model.onnx", "sha256": "0" * 64, "bytes": 1}]}
    paths = []
    for name in contract.FINAL_CODE_PIN_BASENAMES:
        paths.append(ROOT / ("agent/evaluation/" + name) if name.startswith("kuaisearch") else ROOT / ("agent/scripts/" + name))
    prereg = contract.build_preregistration(code_paths=paths, dense=snapshots, cross_encoder=snapshots,
                                            dependencies=snapshots, documents_sha256="d" * 64,
                                            schema_pin_rows=[{"path": f"schemas/{name}", "sha256": "0" * 64, "bytes": 1} for name in contract.SCHEMA_BASENAMES],
                                            top_k=100, seed=0)
    assert prereg["systems"]["bm25"] == {
        "fieldWeights": {"title": 1.0, "attributeText": 0.45, "brand": 0.25},
        "titleControl": {"enabled": True, "field": "title"},
    }
    assert prereg["systems"]["dense"]["dimension"] == 512
    assert prereg["systems"]["dense"]["normalization"] == "L2"
    assert prereg["systems"]["crossEncoder"]["revision"] == contract.CROSS_ENCODER_REVISION
    assert prereg["systems"]["crossEncoder"]["dependencies"] == snapshots
    assert prereg["indexIdentity"] == contract.compute_index_identity("d" * 64)
    assert contract.compute_index_identity(contract.EXPECTED_DOCUMENTS_SHA256) == "ce481bcff557ab206e532c820641f0f79fa9b0cd339888e797dc087eff2b40ff"
    assert prereg["environment"]["offline"] is True
    assert prereg["environment"]["runtimeOverlay"]["huggingface_hub"] == "1.28.0"
    assert prereg["environment"]["packages"]["huggingface_hub"] == "1.28.0"
    assert prereg["environment"]["hostPackages"]["huggingface_hub"] != prereg["environment"]["runtimeOverlay"]["huggingface_hub"]


def test_formal_preregistration_fails_closed_without_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    with pytest.raises(contract.ContractError, match="HF_HUB_OFFLINE"):
        contract.environment_pin(Path("."))


@pytest.mark.parametrize("drift", [
    ("python", "3.13"), ("platform", "drifted-platform"),
    ("hostPackages", {"huggingface_hub": "drift"}),
    ("runtimeOverlay", {"huggingface_hub": "drift"}),
    ("packages", {"fastembed": "drift"}),
    ("hardware", {"gpu": {"name": "drift", "totalVramBytes": None}, "cpu": "c", "platform": "p", "totalRamBytes": 1, "python": "3.12"}),
])
def test_environment_binding_rejects_each_frozen_field(drift: tuple[str, object]) -> None:
    expected = {
        "python": "3.12", "platform": "p", "packages": {"fastembed": "1", "huggingface_hub": "1.28"},
        "hostPackages": {"huggingface_hub": "0.36"}, "runtimeOverlay": {"huggingface_hub": "1.28"},
        "offline": True, "networkPolicy": "NO_NETWORK_CACHE_ONLY",
        "hardware": {"gpu": {"name": None, "totalVramBytes": None}, "cpu": "c", "platform": "p", "totalRamBytes": 1, "python": "3.12"},
    }
    current = copy.deepcopy(expected)
    current[drift[0]] = drift[1]
    with pytest.raises(contract.ContractError, match="environment evidence drift"):
        contract.verify_environment_binding(expected, current)


def test_schemas_are_valid() -> None:
    for path in (ROOT / "agent/evaluation/schemas").glob("kuaisearch_multicategory_retrieval_*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
