from copy import deepcopy
import hashlib
import json

import pytest

from agent.evaluation import used_phone_two_stage_test_scorer_v1 as scorer


def _write_jsonl(path, rows):
    path.write_bytes(b"".join(scorer.canonical_bytes(row) for row in rows))


def _fixture(tmp_path, monkeypatch):
    attempt = tmp_path / "test-attempt-fixture"
    attempt.mkdir()
    predictions, test_rows = [], []
    for case_id in scorer.TEST_CASE_IDS:
        predictions.append({
            "caseId": case_id,
            "rankingContractVersion": scorer._base.RANKING_CONTRACT_VERSION,
            "candidatePoolIds": ["1", "2", "3"],
            "rankedItemIds": ["1", "2"], "evidenceCitations": [],
        })
        for item in range(1, 253):
            test_rows.append({
                "caseId": case_id, "checks": [], "eligible": item in {1, 2},
                "gain": 3 if item == 1 else 1 if item == 2 else 0,
                "itemId": str(item),
                "judgmentStratum": "fully_satisfied" if item == 1 else "soft_missing_or_unsatisfied" if item == 2 else "hard_fail",
                "schemaVersion": "used-phone-ranking-judgment-v2", "split": "test",
            })
    predictions_path = attempt / "predictions.jsonl"
    manifest_path = attempt / "manifest.json"
    contract_path = attempt / "run_contract.json"
    _write_jsonl(predictions_path, predictions)
    contract = {
        "allowSealedTest": True,
        "attributeContractCodeSha256": scorer._base.ATTRIBUTE_CONTRACT_CODE_SHA256,
        "attributeRuleset": scorer._base.ATTRIBUTE_RULESET_VERSION,
        "caseCount": 30, "javaCatalog": scorer._base._expected_java_catalog(),
        "model": scorer._base.NO_MODEL_NAME, "modelEndpoint": scorer._base.NO_MODEL_ENDPOINT,
        "productionCodeScopeSha256": scorer._base.PRODUCTION_CODE_SCOPE_SHA256,
        "productionRuntimeConfig": scorer._base.PRODUCTION_RUNTIME_CONFIG,
        "protocolVersion": scorer.PROTOCOL_VERSION,
        "publicCasesSha256": scorer._base.PUBLIC_CASES_SHA256,
        "publicCatalogSha256": scorer._base.PUBLIC_CATALOG_SHA256,
        "runnerCodeSha256": scorer._code_hashes(scorer.RUNNER_FILES),
        "selectedCaseIds": list(scorer.TEST_CASE_IDS), "selectedSplits": ["test"],
        "timeoutSeconds": 90.0,
    }
    contract_sha = hashlib.sha256(scorer.canonical_bytes(contract)).hexdigest()
    contract_path.write_bytes(scorer.canonical_bytes({
        "contract": contract, "contractSha256": contract_sha,
        "schemaVersion": scorer._base.RUN_CONTRACT_SCHEMA_VERSION,
    }))
    execution = {case_id: True for case_id in scorer.TEST_CASE_IDS}
    manifest = {
        "businessWriteNetworkUsed": False, "contract": deepcopy(contract),
        "contractSha256": contract_sha, "execution": execution,
        "failedCaseIds": [], "failureOutcomes": {}, "hiddenArtifactsRead": False,
        "modelCallCount": 0, "modelNetworkCallCount": 0, "modelNetworkUsed": False,
        "prediction": {"path": "predictions.jsonl", "rowCount": 10,
                       "sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest()},
        "schemaVersion": scorer._base.MANIFEST_SCHEMA_VERSION,
        "selectedCaseIds": list(scorer.TEST_CASE_IDS), "selectedSplits": ["test"],
        "succeededCaseIds": list(scorer.TEST_CASE_IDS),
        "twoStagePredictionSchemaVersion": scorer._base.PREDICTION_SCHEMA_VERSION,
    }
    manifest_path.write_bytes(scorer.canonical_bytes(manifest))
    prereg = {
        "authorizedRunDirectories": [attempt.name], "caseIds": list(scorer.TEST_CASE_IDS),
        "frozenBeforeTestPrediction": True, "metricDefinitions": {},
        "schemaVersion": "used-phone-two-stage-ranking-test10-preregistration-v1",
        "split": "test",
    }
    prereg_path = tmp_path / scorer.PREREGISTRATION_FILENAME
    prereg_path.write_bytes(scorer.canonical_bytes(prereg))
    test_payload = b"".join(scorer.canonical_bytes(row) for row in test_rows)
    hidden_path = tmp_path / scorer.HIDDEN_FILENAME
    hidden_path.write_bytes(b"NOT_PARSED\n" * scorer.ROWS_BEFORE_TEST_BLOCK + test_payload)
    monkeypatch.setattr(scorer, "PREREGISTRATION_DIR", tmp_path)
    monkeypatch.setattr(scorer, "PREREGISTRATION_SHA256", hashlib.sha256(prereg_path.read_bytes()).hexdigest())
    monkeypatch.setattr(scorer, "EVALUATOR_ASSET_DIR", tmp_path)
    monkeypatch.setattr(scorer, "HIDDEN_SOURCE_BYTE_COUNT", hidden_path.stat().st_size)
    monkeypatch.setattr(scorer, "TEST_BLOCK_BYTE_COUNT", len(test_payload))
    monkeypatch.setattr(scorer, "TEST_BLOCK_SHA256", hashlib.sha256(test_payload).hexdigest())
    return {"hidden": hidden_path, "manifest": manifest_path, "predictions": predictions_path, "prereg": prereg_path}


def _score(paths):
    return scorer.score_test10(
        predictions_path=paths["predictions"], manifest_path=paths["manifest"],
        test_judgments_path=paths["hidden"], test_preregistration_path=paths["prereg"],
    )


def test_test_score_skips_preceding_blocks_and_reports_no_item_ids(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    result = _score(paths)
    assert result["split"] == "test"
    assert result["metrics"]["candidatePoolCeilingUtilizationAt50"] == 1.0
    serialized = json.dumps(result, sort_keys=True)
    assert '"itemId"' not in serialized
    assert "candidatePoolIds" not in serialized
    assert "rankedItemIds" not in serialized


def test_auth_failure_opens_neither_prereg_nor_test_labels(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    manifest = json.loads(paths["manifest"].read_bytes())
    manifest["prediction"]["sha256"] = "0" * 64
    paths["manifest"].write_bytes(scorer.canonical_bytes(manifest))
    monkeypatch.setattr(scorer, "_load_preregistration", lambda *_a, **_k: pytest.fail("prereg opened"))
    monkeypatch.setattr(scorer, "_load_test_judgments", lambda *_a, **_k: pytest.fail("labels opened"))
    with pytest.raises(ValueError, match="manifest/prediction identity mismatch"):
        _score(paths)


def test_test_scorer_rejects_validation_identity_before_labels(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    manifest = json.loads(paths["manifest"].read_bytes())
    manifest["selectedSplits"] = ["validation"]
    paths["manifest"].write_bytes(scorer.canonical_bytes(manifest))
    monkeypatch.setattr(scorer, "_load_test_judgments", lambda *_a, **_k: pytest.fail("labels opened"))
    with pytest.raises(ValueError, match="only the test split"):
        _score(paths)
