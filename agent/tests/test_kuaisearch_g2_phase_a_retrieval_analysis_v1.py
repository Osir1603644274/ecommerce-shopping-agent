import json

import pytest

from agent.evaluation.kuaisearch_g2_phase_a_retrieval_analysis_v1 import analyze, materialize, validate_bundle


def test_phase_a_analysis_scores_real_frozen_rankings_without_winner_claim(tmp_path):
    report = analyze(bootstrap_samples=500, bootstrap_seed=20260824)
    assert report["queryCount"] == 12
    assert report["qrelCount"] == 124
    assert report["coverageScope"] == "SECOND_REVIEW_TEST_LANE_ONLY"
    assert report["decision"] == "NO_RETRIEVAL_WINNER_PHASE_A_PROCESS_GATES_FAILED"
    assert report["descriptiveLeader"] == "rrf_bm25_dense"
    assert report["metricsByK"]["3"]["rrf_bm25_dense"]["ndcgLowerBound"] == pytest.approx(0.414107, abs=1e-6)
    assert report["metricsByK"]["3"]["bm25_fields"]["decidableCoverage"] == pytest.approx(0.805556, abs=1e-6)
    assert report["processGates"]["exactAgreement"]["passed"] is False
    assert report["processGates"]["unknownRate"]["passed"] is False
    assert len(report["pairedBootstrapAt3"]["comparisons"]) == 10


def test_phase_a_materialized_report_has_no_private_candidate_identity(tmp_path):
    materialize(tmp_path)
    validate_bundle(tmp_path)
    serialized = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert "blindCandidateId" not in serialized
    assert "docId" not in serialized
    assert "ksd-" not in serialized
    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["report"]["path"] == "external-output/report.json"
    assert manifest["report"]["pathScope"] == "EXTERNAL_OUTPUT_BASENAME_ONLY"


def test_phase_a_validation_rejects_metric_tampering(tmp_path):
    materialize(tmp_path)
    report_path = tmp_path / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["metricsByK"]["3"]["bm25_fields"]["ndcgLowerBound"] = 1.0
    report_path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="replay mismatch|artifact binding mismatch"):
        validate_bundle(tmp_path)
