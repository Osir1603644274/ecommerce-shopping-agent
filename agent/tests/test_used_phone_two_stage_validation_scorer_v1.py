from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from agent.evaluation import used_phone_two_stage_validation_scorer_v1 as scorer


def _write_jsonl(path, rows):
    path.write_bytes(b"".join(scorer.canonical_bytes(row) for row in rows))


def _fixture(tmp_path, monkeypatch):
    attempt = tmp_path / "validation-attempt-fixture"
    attempt.mkdir()
    predictions = []
    validation_rows = []
    for case_id in scorer.VALIDATION_CASE_IDS:
        predictions.append({
            "caseId": case_id,
            "rankingContractVersion": scorer._base.RANKING_CONTRACT_VERSION,
            "candidatePoolIds": ["1", "2", "3"],
            "rankedItemIds": ["1", "2"],
            "evidenceCitations": [],
        })
        for item in range(1, 253):
            validation_rows.append({
                "caseId": case_id,
                "checks": [],
                "eligible": item in {1, 2},
                "gain": 3 if item == 1 else 1 if item == 2 else 0,
                "itemId": str(item),
                "judgmentStratum": (
                    "fully_satisfied" if item == 1
                    else "soft_missing_or_unsatisfied" if item == 2
                    else "hard_fail"
                ),
                "schemaVersion": "used-phone-ranking-judgment-v2",
                "split": "validation",
            })
    predictions_path = attempt / "predictions.jsonl"
    manifest_path = attempt / "manifest.json"
    contract_path = attempt / "run_contract.json"
    _write_jsonl(predictions_path, predictions)
    contract = {
        "allowSealedTest": False,
        "attributeContractCodeSha256": scorer._base.ATTRIBUTE_CONTRACT_CODE_SHA256,
        "attributeRuleset": scorer._base.ATTRIBUTE_RULESET_VERSION,
        "caseCount": 30,
        "javaCatalog": scorer._base._expected_java_catalog(),
        "model": scorer._base.NO_MODEL_NAME,
        "modelEndpoint": scorer._base.NO_MODEL_ENDPOINT,
        "productionCodeScopeSha256": scorer._base.PRODUCTION_CODE_SCOPE_SHA256,
        "productionRuntimeConfig": scorer._base.PRODUCTION_RUNTIME_CONFIG,
        "protocolVersion": scorer.PROTOCOL_VERSION,
        "publicCasesSha256": scorer._base.PUBLIC_CASES_SHA256,
        "publicCatalogSha256": scorer._base.PUBLIC_CATALOG_SHA256,
        "runnerCodeSha256": scorer._code_hashes(scorer.RUNNER_FILES),
        "selectedCaseIds": list(scorer.VALIDATION_CASE_IDS),
        "selectedSplits": ["validation"],
        "timeoutSeconds": 90.0,
    }
    contract_sha = hashlib.sha256(scorer.canonical_bytes(contract)).hexdigest()
    contract_path.write_bytes(scorer.canonical_bytes({
        "contract": contract,
        "contractSha256": contract_sha,
        "schemaVersion": scorer._base.RUN_CONTRACT_SCHEMA_VERSION,
    }))
    execution = {case_id: True for case_id in scorer.VALIDATION_CASE_IDS}
    manifest = {
        "businessWriteNetworkUsed": False,
        "contract": deepcopy(contract),
        "contractSha256": contract_sha,
        "execution": execution,
        "failedCaseIds": [],
        "failureOutcomes": {},
        "hiddenArtifactsRead": False,
        "modelCallCount": 0,
        "modelNetworkCallCount": 0,
        "modelNetworkUsed": False,
        "prediction": {
            "path": "predictions.jsonl",
            "rowCount": 10,
            "sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
        },
        "schemaVersion": scorer._base.MANIFEST_SCHEMA_VERSION,
        "selectedCaseIds": list(scorer.VALIDATION_CASE_IDS),
        "selectedSplits": ["validation"],
        "succeededCaseIds": list(scorer.VALIDATION_CASE_IDS),
        "twoStagePredictionSchemaVersion": scorer._base.PREDICTION_SCHEMA_VERSION,
    }
    manifest_path.write_bytes(scorer.canonical_bytes(manifest))

    prereg = {
        "authorizedRunDirectories": [attempt.name],
        "caseIds": list(scorer.VALIDATION_CASE_IDS),
        "frozenBeforeValidationPrediction": True,
        "metricDefinitions": {},
        "schemaVersion": "used-phone-two-stage-ranking-validation10-preregistration-v1",
        "split": "validation",
    }
    prereg_path = tmp_path / scorer.PREREGISTRATION_FILENAME
    prereg_path.write_bytes(scorer.canonical_bytes(prereg))
    validation_payload = b"".join(
        scorer.canonical_bytes(row) for row in validation_rows
    )
    opaque_dev = b"{}\n" * scorer.VALIDATION_BLOCK_ROW_COUNT
    test_sentinel = b"THIS_IS_NOT_JSON_AND_MUST_NOT_BE_READ\n"
    hidden_path = tmp_path / scorer.HIDDEN_FILENAME
    hidden_path.write_bytes(opaque_dev + validation_payload + test_sentinel)
    monkeypatch.setattr(scorer, "PREREGISTRATION_DIR", tmp_path)
    monkeypatch.setattr(
        scorer, "PREREGISTRATION_SHA256",
        hashlib.sha256(prereg_path.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(scorer, "EVALUATOR_ASSET_DIR", tmp_path)
    monkeypatch.setattr(scorer, "HIDDEN_SOURCE_BYTE_COUNT", hidden_path.stat().st_size)
    monkeypatch.setattr(scorer, "VALIDATION_BLOCK_BYTE_COUNT", len(validation_payload))
    monkeypatch.setattr(
        scorer, "VALIDATION_BLOCK_SHA256",
        hashlib.sha256(validation_payload).hexdigest(),
    )
    return {
        "attempt": attempt,
        "contract": contract,
        "contract_path": contract_path,
        "hidden": hidden_path,
        "manifest": manifest_path,
        "predictions": predictions_path,
        "prereg": prereg_path,
    }


def _score(paths):
    return scorer.score_validation10(
        predictions_path=paths["predictions"],
        manifest_path=paths["manifest"],
        validation_judgments_path=paths["hidden"],
        validation_preregistration_path=paths["prereg"],
    )


def test_validation_score_parses_only_v_block_and_reports_no_item_ids(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    result = _score(paths)
    assert result["split"] == "validation"
    assert result["metrics"]["candidatePoolCeilingUtilizationAt50"] == 1.0
    assert result["metrics"]["finalCeilingUtilizationAt20"] == 1.0
    assert result["metrics"]["sameCutoffRecallDeltaAt20"] == 0.0
    assert result["metrics"]["orderingEfficiencyAt10"] == 1.0
    serialized = json.dumps(result, sort_keys=True)
    assert '"itemId"' not in serialized
    assert "candidatePoolIds" not in serialized
    assert "rankedItemIds" not in serialized


def test_authentication_failure_opens_neither_prereg_nor_validation_labels(
    tmp_path, monkeypatch,
):
    paths = _fixture(tmp_path, monkeypatch)
    manifest = json.loads(paths["manifest"].read_bytes())
    manifest["prediction"]["sha256"] = "0" * 64
    paths["manifest"].write_bytes(scorer.canonical_bytes(manifest))
    monkeypatch.setattr(
        scorer, "_load_preregistration",
        lambda *_args, **_kwargs: pytest.fail("prereg opened before authentication"),
    )
    monkeypatch.setattr(
        scorer, "_load_validation_judgments",
        lambda *_args, **_kwargs: pytest.fail("labels opened before authentication"),
    )
    with pytest.raises(ValueError, match="manifest/prediction identity mismatch"):
        _score(paths)


def test_cross_run_prediction_path_cannot_open_validation_labels(tmp_path, monkeypatch):
    paths = _fixture(tmp_path, monkeypatch)
    other = tmp_path / "other-run"
    other.mkdir()
    other_prediction = other / "predictions.jsonl"
    other_prediction.write_bytes(paths["predictions"].read_bytes())
    monkeypatch.setattr(
        scorer, "_load_validation_judgments",
        lambda *_args, **_kwargs: pytest.fail("labels opened for cross-run prediction"),
    )
    with pytest.raises(ValueError, match="manifest-bound predictions.jsonl"):
        scorer.score_validation10(
            predictions_path=other_prediction,
            manifest_path=paths["manifest"],
            validation_judgments_path=paths["hidden"],
            validation_preregistration_path=paths["prereg"],
        )


@pytest.mark.parametrize(
    "mutate,pattern",
    [
        (lambda m, c: c.update(productionCodeScopeSha256="0" * 64), "protocol/code/data"),
        (lambda m, c: m.update(selectedSplits=["dev"]), "only the validation split"),
        (lambda m, c: m.update(selectedCaseIds=list(reversed(scorer.VALIDATION_CASE_IDS))), "identity/order"),
        (lambda m, c: m.update(twoStagePredictionSchemaVersion="forged"), "schema identity"),
        (lambda m, c: m.update(hiddenArtifactsRead=True), "hidden/write/no-model"),
    ],
)
def test_validation_scorer_rejects_cross_identity_before_labels(
    tmp_path, monkeypatch, mutate, pattern,
):
    paths = _fixture(tmp_path, monkeypatch)
    manifest = json.loads(paths["manifest"].read_bytes())
    contract_doc = json.loads(paths["contract_path"].read_bytes())
    contract = contract_doc["contract"]
    mutate(manifest, contract)
    if contract != contract_doc["contract"]:
        raise AssertionError("unexpected contract alias behavior")
    if contract["productionCodeScopeSha256"] == "0" * 64:
        contract_sha = hashlib.sha256(scorer.canonical_bytes(contract)).hexdigest()
        contract_doc["contractSha256"] = contract_sha
        manifest["contract"] = deepcopy(contract)
        manifest["contractSha256"] = contract_sha
        paths["contract_path"].write_bytes(scorer.canonical_bytes(contract_doc))
    paths["manifest"].write_bytes(scorer.canonical_bytes(manifest))
    monkeypatch.setattr(
        scorer, "_load_validation_judgments",
        lambda *_args, **_kwargs: pytest.fail("labels opened after auth failure"),
    )
    with pytest.raises(ValueError, match=pattern):
        _score(paths)
