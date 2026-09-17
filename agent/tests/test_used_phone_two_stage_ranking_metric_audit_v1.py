import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent.evaluation import used_phone_public_agent_runner_v1 as public_runner
from agent.evaluation import used_phone_two_stage_ranking_metric_audit_v1 as audit
from agent.evaluation import used_phone_two_stage_ranking_scorer_v3 as scorer
from tests.test_used_phone_two_stage_ranking_scorer_v3 import (
    _fixture,
    _record_path_io,
    _score,
)


def _audit_fixture(tmp_path, monkeypatch):
    paths, contract, predictions = _fixture(tmp_path, monkeypatch)
    legacy_score = _score(paths)
    paths["legacy_score"] = tmp_path / "legacy-score.json"
    paths["legacy_score"].write_bytes(scorer.canonical_bytes(legacy_score))
    paths["audit_prereg"] = tmp_path / audit.AUDIT_PREREGISTRATION_FILENAME
    preregistration = {
        "caseRoles": {
            "UPV2-RK-D10": ["multi_turn", "query_state_composition"],
        },
        "diagnosticDefinitions": {
            "sameCutoffRecallDeltaAt20": "fixture definition",
        },
        "labelBoundary": "AI-designed product-grounded, not human gold",
        "legacyScoreSha256": scorer.sha256_file(paths["legacy_score"]),
        "manifestSha256": scorer.sha256_file(paths["manifest"]),
        "predictionSha256": scorer.sha256_file(paths["predictions"]),
        "purpose": "post-run diagnostic audit; not a quality gate",
        "runContractSha256": scorer.sha256_file(paths["run_contract"]),
        "schemaVersion": (
            "used-phone-two-stage-ranking-public-dev-metric-audit-"
            "preregistration-v1"
        ),
        "split": "dev",
    }
    paths["audit_prereg"].write_bytes(scorer.canonical_bytes(preregistration))
    monkeypatch.setattr(audit, "EVALUATOR_ASSET_DIR", tmp_path)
    monkeypatch.setattr(
        audit, "AUDIT_PREREGISTRATION_SHA256", scorer.sha256_file(paths["audit_prereg"])
    )
    monkeypatch.setattr(
        audit, "FROZEN_PREDICTION_SHA256", scorer.sha256_file(paths["predictions"])
    )
    monkeypatch.setattr(
        audit, "FROZEN_MANIFEST_SHA256", scorer.sha256_file(paths["manifest"])
    )
    monkeypatch.setattr(
        audit, "FROZEN_RUN_CONTRACT_SHA256", scorer.sha256_file(paths["run_contract"])
    )
    monkeypatch.setattr(
        audit, "LEGACY_SCORE_SHA256", scorer.sha256_file(paths["legacy_score"])
    )
    return paths, contract, predictions


def _audit(paths):
    return audit.audit_public_dev_metrics(
        predictions_path=paths["predictions"],
        manifest_path=paths["manifest"],
        legacy_score_path=paths["legacy_score"],
        public_dev_judgments_path=paths["judgments"],
        audit_preregistration_path=paths["audit_prereg"],
    )


def test_metric_audit_adds_denominators_ceilings_and_same_cutoff_diagnostics(
    tmp_path, monkeypatch,
):
    paths, _contract, _predictions = _audit_fixture(tmp_path, monkeypatch)

    result = _audit(paths)
    row = result["perCase"][0]

    assert row["relevantTotal"] == 2
    assert row["candidatePoolRelevantHitCount"] == 2
    assert row["candidatePoolTop20RelevantHitCount"] == 2
    assert row["finalRelevantHitCount"] == 1
    assert row["candidatePoolRecallCeilingAt50"] == 1.0
    assert row["finalRecallCeilingAt20"] == 1.0
    assert row["candidatePoolCeilingUtilizationAt50"] == 1.0
    assert row["finalCeilingUtilizationAt20"] == 0.5
    assert row["poolToFinalRelevantRetention"] == 0.5
    assert row["sameCutoffRecallDeltaAt20"] == -0.5
    assert row["legacyRerankingRecallDelta"] == -0.5
    assert row["top10HardViolationDenominator"] == 10
    assert row["missingTop10RankCount"] == 9
    assert result["legacyMetricNotice"]["status"] == (
        "retained_for_historical_compatibility_only"
    )
    assert "different cutoffs" in result["legacyMetricNotice"]["warning"]


def test_case_diagnostics_exposes_dcg_ordering_loss_and_zero_relevant_handling():
    prediction = {
        "caseId": "fixture",
        "candidatePoolIds": ["1", "2", "3"],
        "rankedItemIds": ["2", "1"],
        "evidenceCitations": [],
    }
    judgments = {
        "1": {
            "eligible": True, "gain": 3, "judgmentStratum": "fully_satisfied",
            "checks": [],
        },
        "2": {
            "eligible": True, "gain": 1,
            "judgmentStratum": "soft_missing_or_unsatisfied", "checks": [],
        },
        "3": {
            "eligible": False, "gain": 0, "judgmentStratum": "hard_fail",
            "checks": [],
        },
    }

    row = audit._case_diagnostics(prediction, judgments)

    assert row["actualDcgAt10"] < row["returnedSetIdealDcgAt10"]
    assert row["orderingEfficiencyAt10"] < 1.0
    assert row["orderingLossDcgAt10"] > 0
    assert row["idealDcgAt10"] == row["returnedSetIdealDcgAt10"]
    assert row["missingTop10RankCount"] == 8

    zero = {
        item: {**value, "eligible": False, "gain": None,
               "judgmentStratum": "hard_unknown_or_conflict"}
        for item, value in judgments.items()
    }
    zero_row = audit._case_diagnostics(prediction, zero)
    assert zero_row["candidatePoolRecallAt50"] is None
    assert zero_row["finalRecallAt20"] is None
    assert zero_row["poolToFinalRelevantRetention"] is None
    assert zero_row["ndcgAt10"] == 0.0
    assert zero_row["orderingEfficiencyAt10"] is None


def test_prediction_authentication_failure_opens_no_metric_audit_assets(
    tmp_path, monkeypatch,
):
    paths, contract, _predictions = _audit_fixture(tmp_path, monkeypatch)
    contract["protocolVersion"] = "forged-protocol"
    from tests.test_used_phone_two_stage_ranking_scorer_v3 import _seal_contract
    _seal_contract(paths, contract)
    reads = _record_path_io(monkeypatch)

    with pytest.raises(ValueError, match="run protocol/code/data contract mismatch"):
        _audit(paths)

    protected = {
        paths["audit_prereg"].resolve(),
        paths["legacy_score"].resolve(),
        paths["judgments"].resolve(),
    }
    assert not [event for event in reads if event[1] in protected]


def test_metric_audit_exactly_reproduces_legacy_metrics(tmp_path, monkeypatch):
    paths, _contract, _predictions = _audit_fixture(tmp_path, monkeypatch)
    result = _audit(paths)
    legacy = json.loads(paths["legacy_score"].read_text(encoding="utf-8"))["metrics"]

    assert result["metrics"]["candidatePoolRecallAt50"] == legacy[
        "candidatePoolRecallAt50"
    ]
    assert result["metrics"]["finalRecallAt20"] == legacy["finalRecallAt20"]
    assert result["metrics"]["legacyRerankingRecallDelta"] == legacy[
        "rerankingRecallDelta"
    ]
    assert result["metrics"]["ndcgAt10"] == legacy["ndcgAt10"]
    assert result["metrics"]["citationAccuracy"] == legacy["citationAccuracy"]


@pytest.mark.parametrize(
    "artifact_kind",
    ["audit_preregistration", "legacy_scorer_source", "audit_source"],
)
def test_production_runtime_cannot_read_metric_audit_assets(
    tmp_path, artifact_kind,
):
    cases = tmp_path / "cases_public.jsonl"
    catalog = tmp_path / "catalog.jsonl"
    run_dir = tmp_path / "run"
    cases.write_text("{}\n", encoding="utf-8")
    catalog.write_text("{}\n", encoding="utf-8")
    run_dir.mkdir()
    artifacts = {
        "audit_preregistration": (
            audit.EVALUATOR_ASSET_DIR / audit.AUDIT_PREREGISTRATION_FILENAME
        ).resolve(),
        "legacy_scorer_source": Path(scorer.__file__).resolve(),
        "audit_source": Path(audit.__file__).resolve(),
    }
    artifact = artifacts[artifact_kind]
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
        public_cases_path=cases, public_catalog_path=catalog, run_dir=run_dir,
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
