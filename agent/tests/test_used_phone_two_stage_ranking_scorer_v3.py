from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from jsonschema.exceptions import ValidationError

from agent.evaluation import used_phone_two_stage_ranking_scorer_v3 as scorer
from agent.evaluation import build_used_phone_two_stage_public_dev_assets_v3 as builder
from agent.evaluation import used_phone_public_agent_runner_v1 as public_runner


def _write_jsonl(path, rows):
    path.write_bytes(b"".join(scorer.canonical_bytes(row) for row in rows))


def _seal_contract(paths, contract, manifest_overrides=None):
    contract_sha = hashlib.sha256(scorer.canonical_bytes(contract)).hexdigest()
    paths["run_contract"].write_bytes(scorer.canonical_bytes({
        "schemaVersion": scorer.RUN_CONTRACT_SCHEMA_VERSION,
        "contractSha256": contract_sha,
        "contract": contract,
    }))
    execution = {case_id: True for case_id in scorer.PUBLIC_DEV_CASE_IDS}
    manifest = {
        "schemaVersion": scorer.MANIFEST_SCHEMA_VERSION,
        "contractSha256": contract_sha,
        "contract": deepcopy(contract),
        "selectedCaseIds": list(scorer.PUBLIC_DEV_CASE_IDS),
        "selectedSplits": ["dev"],
        "execution": execution,
        "succeededCaseIds": list(scorer.PUBLIC_DEV_CASE_IDS),
        "failedCaseIds": [],
        "failureOutcomes": {},
        "prediction": {
            "path": "predictions.jsonl",
            "rowCount": len(scorer.PUBLIC_DEV_CASE_IDS),
            "sha256": scorer.sha256_file(paths["predictions"]),
        },
        "modelCallCount": 0,
        "modelNetworkCallCount": 0,
        "modelNetworkUsed": False,
        "businessWriteNetworkUsed": False,
        "hiddenArtifactsRead": False,
        "twoStagePredictionSchemaVersion": scorer.PREDICTION_SCHEMA_VERSION,
    }
    manifest.update(manifest_overrides or {})
    paths["manifest"].write_bytes(scorer.canonical_bytes(manifest))
    return manifest


def _fixture(tmp_path, monkeypatch):
    cases = [
        {"caseId": case_id, "split": "dev"}
        for case_id in scorer.PUBLIC_DEV_CASE_IDS
    ]
    judgments = []
    predictions = []
    for case_id in scorer.PUBLIC_DEV_CASE_IDS:
        for item in range(1, 253):
            judgments.append({
                "caseId": case_id,
                "schemaVersion": "used-phone-ranking-judgment-v2",
                "split": "dev",
                "itemId": str(item),
                "eligible": item in {1, 2},
                "gain": 3 if item == 1 else 1 if item == 2 else 0,
                "judgmentStratum": (
                    "fully_satisfied" if item == 1
                    else "soft_missing_or_unsatisfied" if item == 2
                    else "hard_fail"
                ),
                "checks": [],
            })
        predictions.append({
            "caseId": case_id,
            "rankingContractVersion": scorer.RANKING_CONTRACT_VERSION,
            "candidatePoolIds": ["1", "2", "3"],
            "rankedItemIds": ["1"],
            "evidenceCitations": [],
        })
    paths = {
        "cases": tmp_path / "cases_public.jsonl",
        "judgments": tmp_path / scorer.PUBLIC_DEV_JUDGMENTS_FILENAME,
        "predictions": tmp_path / "predictions.jsonl",
        "prereg": tmp_path / scorer.PUBLIC_DEV_PREREGISTRATION_FILENAME,
        "manifest": tmp_path / "manifest.json",
        "run_contract": tmp_path / "run_contract.json",
    }
    _write_jsonl(paths["cases"], cases)
    _write_jsonl(paths["judgments"], judgments)
    _write_jsonl(paths["predictions"], predictions)
    fixture_preregistration = {
        "schemaVersion": "fixture-public-dev-preregistration-v3",
        "caseIds": list(scorer.PUBLIC_DEV_CASE_IDS),
    }
    paths["prereg"].write_bytes(scorer.canonical_bytes(fixture_preregistration))
    monkeypatch.setattr(scorer, "PUBLIC_CASES_SHA256", scorer.sha256_file(paths["cases"]))
    monkeypatch.setattr(scorer, "EVALUATOR_ASSET_DIR", tmp_path)
    monkeypatch.setattr(
        scorer, "PUBLIC_DEV_JUDGMENTS_SHA256", scorer.sha256_file(paths["judgments"])
    )
    monkeypatch.setattr(
        scorer, "PUBLIC_DEV_JUDGMENTS_BYTE_COUNT", paths["judgments"].stat().st_size
    )
    monkeypatch.setattr(
        scorer,
        "PUBLIC_DEV_PREREGISTRATION_SHA256",
        scorer.sha256_file(paths["prereg"]),
    )
    monkeypatch.setattr(
        scorer, "public_dev_preregistration", lambda: fixture_preregistration
    )
    contract = {
        "protocolVersion": scorer.PROTOCOL_VERSION,
        "publicCasesSha256": scorer.PUBLIC_CASES_SHA256,
        "publicCatalogSha256": scorer.PUBLIC_CATALOG_SHA256,
        "productionCodeScopeSha256": scorer.PRODUCTION_CODE_SCOPE_SHA256,
        "runnerCodeSha256": scorer._code_hashes(scorer.RUNNER_FILES),
        "attributeContractCodeSha256": scorer.ATTRIBUTE_CONTRACT_CODE_SHA256,
        "attributeRuleset": scorer.ATTRIBUTE_RULESET_VERSION,
        "javaCatalog": scorer._expected_java_catalog(),
        "model": scorer.NO_MODEL_NAME,
        "modelEndpoint": scorer.NO_MODEL_ENDPOINT,
        "productionRuntimeConfig": scorer.PRODUCTION_RUNTIME_CONFIG,
        "timeoutSeconds": 90.0,
        "caseCount": 30,
        "selectedCaseIds": list(scorer.PUBLIC_DEV_CASE_IDS),
        "selectedSplits": ["dev"],
        "allowSealedTest": False,
    }
    _seal_contract(paths, contract)
    return paths, contract, predictions


def _score(paths):
    return scorer.score_public_dev(
        predictions_path=paths["predictions"],
        manifest_path=paths["manifest"],
        public_dev_judgments_path=paths["judgments"],
        public_dev_preregistration_path=paths["prereg"],
    )


def _record_path_io(monkeypatch):
    reads = []
    original_read_bytes = Path.read_bytes
    original_read_text = Path.read_text
    original_open = Path.open

    def record(method, original):
        def wrapped(path, *args, **kwargs):
            resolved = path.resolve()
            reads.append((method, resolved))
            return original(path, *args, **kwargs)
        return wrapped

    monkeypatch.setattr(Path, "read_bytes", record("read_bytes", original_read_bytes))
    monkeypatch.setattr(Path, "read_text", record("read_text", original_read_text))
    monkeypatch.setattr(Path, "open", record("open", original_open))
    return reads


def _assert_evaluator_assets_unread(reads, paths):
    protected = {
        paths["judgments"].resolve(),
        paths["prereg"].resolve(),
    }
    assert not [
        event for event in reads if event[1] in protected
    ], "dev labels/preregistration opened before prediction authentication"


def test_asset_builder_copies_only_original_dev_bytes(tmp_path, monkeypatch):
    case_ids = ("UPV2-RK-D01", "UPV2-RK-V01", "UPV2-RK-T01")
    source = tmp_path / "judgments_hidden.jsonl"
    rows = []
    for case_id, split in zip(case_ids, ("dev", "validation", "test")):
        for item in range(1, 253):
            rows.append({
                "caseId": case_id,
                "checks": [],
                "eligible": item == 1,
                "gain": 3 if item == 1 else 0,
                "itemId": str(item),
                "judgmentStratum": (
                    "fully_satisfied" if item == 1 else "hard_fail"
                ),
                "schemaVersion": "used-phone-ranking-judgment-v2",
                "split": split,
            })
    _write_jsonl(source, rows)
    expected = b"".join(
        scorer.canonical_bytes(row) for row in rows if row["split"] == "dev"
    )
    monkeypatch.setattr(builder, "ALL_CASE_IDS", case_ids)
    monkeypatch.setattr(builder, "PUBLIC_DEV_CASE_IDS", (case_ids[0],))
    monkeypatch.setattr(builder, "FULL_JUDGMENTS_SHA256", scorer.sha256_file(source))
    monkeypatch.setattr(builder, "PUBLIC_DEV_JUDGMENT_COUNT", 252)
    monkeypatch.setattr(builder, "PUBLIC_DEV_JUDGMENTS_BYTE_COUNT", len(expected))
    monkeypatch.setattr(
        builder,
        "PUBLIC_DEV_JUDGMENTS_SHA256",
        hashlib.sha256(expected).hexdigest(),
    )

    result = builder.build_public_dev_assets(
        source_judgments_path=source,
        output_dir=tmp_path / "evaluator-assets",
    )

    output = Path(result["judgmentsPath"])
    assert output.read_bytes() == expected
    assert {row["split"] for row in scorer._load_jsonl(output)} == {"dev"}
    assert not any(b"UPV2-RK-V" in line or b"UPV2-RK-T" in line for line in output.read_bytes().splitlines())
    assert builder.build_public_dev_assets(
        source_judgments_path=source,
        output_dir=tmp_path / "evaluator-assets",
    )["judgmentsSha256"] == hashlib.sha256(expected).hexdigest()
    output.write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="refusing to overwrite frozen evaluator asset"):
        builder.build_public_dev_assets(
            source_judgments_path=source,
            output_dir=tmp_path / "evaluator-assets",
        )


def test_asset_builder_rejects_unpinned_full_source(tmp_path, monkeypatch):
    source = tmp_path / "judgments_hidden.jsonl"
    source.write_bytes(b"{}\n")
    monkeypatch.setattr(builder, "FULL_JUDGMENTS_SHA256", "0" * 64)

    with pytest.raises(ValueError, match="invalid frozen judgment identity|SHA mismatch"):
        builder.build_public_dev_assets(
            source_judgments_path=source,
            output_dir=tmp_path / "evaluator-assets",
        )


def test_two_stage_scorer_attributes_pool_and_final_metrics_separately(
    tmp_path, monkeypatch,
):
    paths, _contract, _predictions = _fixture(tmp_path, monkeypatch)

    reads = _record_path_io(monkeypatch)
    result = _score(paths)

    assert result["metrics"]["candidatePoolRecallAt50"] == 1.0
    assert result["metrics"]["finalRecallAt20"] == 0.5
    assert result["metrics"]["rerankingRecallDelta"] == -0.5
    assert result["inputs"]["runContractSha256"] == scorer.sha256_file(
        paths["run_contract"]
    )
    assert result["evaluatorIdentity"]["protocolVersion"] == scorer.PROTOCOL_VERSION
    assert result["evaluatorIdentity"]["evaluatorIdentityVersion"] == (
        "used-phone-two-stage-ranking-evaluator-v3-prediction-first"
    )
    assert result["schemaVersion"] == (
        "used-phone-two-stage-ranking-score-v3-prediction-authenticated-before-labels"
    )
    prediction_io = [
        method for method, path in reads
        if path == paths["predictions"].resolve()
    ]
    assert prediction_io == ["read_bytes", "open"]
    protected = {
        paths["judgments"].resolve(),
        paths["prereg"].resolve(),
    }
    assert {path for _method, path in reads if path in protected} == protected


def test_forged_contract_cannot_open_dev_labels_or_preregistration(
    tmp_path, monkeypatch,
):
    paths, contract, _predictions = _fixture(tmp_path, monkeypatch)
    contract["protocolVersion"] = "forged-protocol"
    _seal_contract(paths, contract)
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValueError, match="run protocol/code/data contract mismatch"):
        _score(paths)

    _assert_evaluator_assets_unread(reads, paths)


def test_prediction_sha_drift_cannot_open_dev_labels_or_preregistration(
    tmp_path, monkeypatch,
):
    paths, _contract, _predictions = _fixture(tmp_path, monkeypatch)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["prediction"]["sha256"] = "0" * 64
    paths["manifest"].write_bytes(scorer.canonical_bytes(manifest))
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValueError, match="manifest/prediction identity mismatch"):
        _score(paths)

    _assert_evaluator_assets_unread(reads, paths)


def test_manifest_prediction_path_drift_cannot_open_dev_labels_or_preregistration(
    tmp_path, monkeypatch,
):
    paths, _contract, _predictions = _fixture(tmp_path, monkeypatch)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["prediction"]["path"] = "attacker-predictions.jsonl"
    paths["manifest"].write_bytes(scorer.canonical_bytes(manifest))
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValueError, match="manifest/prediction identity mismatch"):
        _score(paths)

    _assert_evaluator_assets_unread(reads, paths)


def test_unbound_prediction_path_cannot_open_dev_labels_or_preregistration(
    tmp_path, monkeypatch,
):
    paths, _contract, _predictions = _fixture(tmp_path, monkeypatch)
    unbound_predictions = tmp_path / "attacker-predictions.jsonl"
    unbound_predictions.write_bytes(paths["predictions"].read_bytes())
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValueError, match="manifest-bound predictions.jsonl"):
        scorer.score_public_dev(
            predictions_path=unbound_predictions,
            manifest_path=paths["manifest"],
            public_dev_judgments_path=paths["judgments"],
            public_dev_preregistration_path=paths["prereg"],
        )

    _assert_evaluator_assets_unread(reads, paths)


def test_noncanonical_prediction_cannot_open_dev_labels_or_preregistration(
    tmp_path, monkeypatch,
):
    paths, contract, predictions = _fixture(tmp_path, monkeypatch)
    payload = b"".join(
        (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        for row in predictions
    )
    assert payload != b"".join(scorer.canonical_bytes(row) for row in predictions)
    paths["predictions"].write_bytes(payload)
    _seal_contract(paths, contract)
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValueError, match="non-canonical predictions JSONL"):
        _score(paths)

    _assert_evaluator_assets_unread(reads, paths)


def test_wrong_prediction_case_order_cannot_open_dev_labels_or_preregistration(
    tmp_path, monkeypatch,
):
    paths, contract, predictions = _fixture(tmp_path, monkeypatch)
    predictions[0], predictions[1] = predictions[1], predictions[0]
    _write_jsonl(paths["predictions"], predictions)
    _seal_contract(paths, contract)
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValueError, match="prediction identity/order mismatch"):
        _score(paths)

    _assert_evaluator_assets_unread(reads, paths)


def test_prediction_schema_failure_cannot_open_dev_labels_or_preregistration(
    tmp_path, monkeypatch,
):
    paths, contract, predictions = _fixture(tmp_path, monkeypatch)
    del predictions[0]["candidatePoolIds"]
    _write_jsonl(paths["predictions"], predictions)
    _seal_contract(paths, contract)
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValidationError, match="candidatePoolIds"):
        _score(paths)

    _assert_evaluator_assets_unread(reads, paths)


def test_pool_external_ranking_cannot_open_dev_labels_or_preregistration(
    tmp_path, monkeypatch,
):
    paths, contract, predictions = _fixture(tmp_path, monkeypatch)
    predictions[0]["candidatePoolIds"] = ["1"]
    predictions[0]["rankedItemIds"] = ["2"]
    _write_jsonl(paths["predictions"], predictions)
    _seal_contract(paths, contract)
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValueError, match="outside candidatePoolIds"):
        _score(paths)

    _assert_evaluator_assets_unread(reads, paths)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocolVersion", "forged-protocol"),
        ("productionCodeScopeSha256", "0" * 64),
        ("runnerCodeSha256", {"forged.py": "0" * 64}),
        ("model", "external-model"),
        ("allowSealedTest", True),
    ],
)
def test_scorer_rejects_resealed_but_wrong_run_identity(
    tmp_path, monkeypatch, field, value,
):
    paths, contract, _predictions = _fixture(tmp_path, monkeypatch)
    contract[field] = value
    _seal_contract(paths, contract)

    with pytest.raises(ValueError, match="run protocol/code/data contract mismatch"):
        _score(paths)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hiddenArtifactsRead", True),
        ("businessWriteNetworkUsed", True),
        ("modelCallCount", 1),
        ("modelNetworkCallCount", 1),
        ("modelNetworkUsed", True),
    ],
)
def test_scorer_rejects_run_outside_hidden_write_or_model_boundary(
    tmp_path, monkeypatch, field, value,
):
    paths, contract, _predictions = _fixture(tmp_path, monkeypatch)
    _seal_contract(paths, contract, {field: value})

    with pytest.raises(ValueError, match="exceeded the hidden/write/no-model boundary"):
        _score(paths)


def test_scorer_rejects_manifest_run_contract_drift(tmp_path, monkeypatch):
    paths, _contract, _predictions = _fixture(tmp_path, monkeypatch)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    manifest["contract"]["protocolVersion"] = "forged-after-seal"
    paths["manifest"].write_bytes(scorer.canonical_bytes(manifest))

    with pytest.raises(ValueError, match="run contract/manifest identity mismatch"):
        _score(paths)


def test_scorer_rejects_prediction_schema_or_citation_outside_ranked(
    tmp_path, monkeypatch,
):
    paths, contract, predictions = _fixture(tmp_path, monkeypatch)
    predictions[0]["evidenceCitations"] = [{
        "itemId": "2",
        "group": "os",
        "source": "relevance",
        "field": "attr_value",
        "lineNumber": 1,
        "rawValue": "android",
    }]
    _write_jsonl(paths["predictions"], predictions)
    _seal_contract(paths, contract)

    with pytest.raises(ValueError, match="evidence citation outside rankedItemIds"):
        _score(paths)


def test_full_dvt_or_any_unpinned_judgment_path_is_rejected_before_open(
    tmp_path, monkeypatch,
):
    paths, _contract, _predictions = _fixture(tmp_path, monkeypatch)
    full_dvt = tmp_path.parent / "judgments_hidden.jsonl"
    full_dvt.write_text(
        '{"caseId":"UPV2-RK-V01","split":"validation"}\n',
        encoding="utf-8",
    )
    reads = []
    original_read_bytes = scorer.Path.read_bytes

    def recording_read_bytes(path, *args, **kwargs):
        reads.append(path)
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(scorer.Path, "read_bytes", recording_read_bytes)
    with pytest.raises(ValueError, match="pinned evaluator-owned asset directory"):
        scorer._load_public_dev_judgments(full_dvt)
    assert reads == []
    assert paths["judgments"].name == scorer.PUBLIC_DEV_JUDGMENTS_FILENAME


@pytest.mark.parametrize(
    ("case_id", "split"),
    [
        ("UPV2-RK-V01", "validation"),
        ("UPV2-RK-T01", "test"),
        ("UPV2-RK-X01", "dev"),
    ],
)
def test_scorer_rejects_non_dev_or_extra_case_even_if_attacker_repins_bytes(
    tmp_path, monkeypatch, case_id, split,
):
    paths, _contract, _predictions = _fixture(tmp_path, monkeypatch)
    rows = scorer._load_jsonl(paths["judgments"])
    rows[0]["caseId"] = case_id
    rows[0]["split"] = split
    _write_jsonl(paths["judgments"], rows)
    monkeypatch.setattr(
        scorer, "PUBLIC_DEV_JUDGMENTS_SHA256", scorer.sha256_file(paths["judgments"])
    )
    monkeypatch.setattr(
        scorer, "PUBLIC_DEV_JUDGMENTS_BYTE_COUNT", paths["judgments"].stat().st_size
    )

    with pytest.raises(ValueError, match="non-dev, unknown, or invalid identity"):
        _score(paths)


def test_top10_hard_violation_rate_is_fixed_k_for_short_ranking(
    tmp_path, monkeypatch,
):
    paths, contract, predictions = _fixture(tmp_path, monkeypatch)
    predictions[0]["rankedItemIds"] = ["3"]
    _write_jsonl(paths["predictions"], predictions)
    _seal_contract(paths, contract)

    result = _score(paths)

    assert result["perCase"][0]["top10HardViolationRate"] == 0.1
    assert result["metrics"]["top10HardViolationRate"] == pytest.approx(0.01)
    assert result["metricDefinitions"]["top10HardViolationRate"] == {
        "denominator": "fixed_k",
        "k": 10,
        "missingRanks": "zero_violation_contribution",
        "name": "top10HardViolationRate",
    }


def test_sut_public_isolation_cannot_read_evaluator_owned_dev_judgments(tmp_path):
    cases = tmp_path / "cases_public.jsonl"
    catalog = tmp_path / "catalog.jsonl"
    run_dir = tmp_path / "run"
    cases.write_text("{}\n", encoding="utf-8")
    catalog.write_text("{}\n", encoding="utf-8")
    run_dir.mkdir()
    artifact = (
        scorer.EVALUATOR_ASSET_DIR / scorer.PUBLIC_DEV_JUDGMENTS_FILENAME
    ).resolve()
    assert artifact.is_file()

    script = r"""
import json
import sys
from pathlib import Path
from agent.evaluation import used_phone_public_agent_runner_v1 as runner

cases, catalog, run_dir, artifact = map(Path, sys.argv[1:])
result = {"artifactOpened": False, "rejected": False}
try:
    with runner.public_runtime_isolation(
        public_cases_path=cases,
        public_catalog_path=catalog,
        run_dir=run_dir,
    ):
        artifact.read_bytes()
        result["artifactOpened"] = True
except runner.PublicIsolationError as exc:
    result["rejected"] = "denied public-run read" in str(exc)
print(json.dumps(result, sort_keys=True))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(public_runner.REPO_ROOT)
    completed = subprocess.run(
        [
            sys.executable, "-c", script, str(cases), str(catalog),
            str(run_dir), str(artifact),
        ],
        cwd=str(public_runner.REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "artifactOpened": False,
        "rejected": True,
    }


def test_scorer_cli_rejects_legacy_full_judgment_switches():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "agent.scripts.score_used_phone_two_stage_ranking_v3",
            "--predictions",
            "predictions.jsonl",
            "--manifest",
            "manifest.json",
            "--public-dev-judgments",
            "judgments_public_dev_v3.jsonl",
            "--public-dev-preregistration",
            "public_dev_preregistration_v3.json",
            "--judgments",
            "judgments_hidden.jsonl",
            "--preregistration",
            "preregistration.json",
        ],
        cwd=str(public_runner.REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 2
    assert "--public-dev-judgments" in completed.stderr
    assert "--public-dev-preregistration" in completed.stderr
    assert "unrecognized arguments" in completed.stderr
