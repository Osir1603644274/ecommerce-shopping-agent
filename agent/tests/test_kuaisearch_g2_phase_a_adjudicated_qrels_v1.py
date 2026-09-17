import json
import shutil
import pytest

from agent.evaluation import kuaisearch_g2_phase_a_adjudicated_qrels_v1 as adjudication
from agent.evaluation.kuaisearch_g2_phase_a_adjudicated_qrels_v1 import PUBLIC_SAMPLE, REVIEWER_A, REVIEWER_B, PROPOSAL, build_records, materialize, validate_bundle


def test_g2_adjudicated_qrels_preserve_unknown_and_exact_closure(tmp_path):
    records, counts = build_records(REVIEWER_A, REVIEWER_B, PROPOSAL)
    assert counts["pairedRows"] == 124
    assert counts["queries"] == 12
    assert counts["agreementRows"] == 82
    assert counts["approvedAdjudicationRows"] == 42
    assert any(row["relevance"] == "U" for row in records)
    sample = {tuple(item[field] for field in ("queryId", "blindCandidateId")) for item in map(json.loads, PUBLIC_SAMPLE.read_text(encoding="utf-8").splitlines())}
    assert all(row["queryId"].startswith("ksq-") and (row["queryId"], row["blindCandidateId"]) in sample for row in records)
    assert sum(row["resolutionSource"] == "USER_APPROVED_AI_PROPOSAL" for row in records) == 42
    assert sum(row["resolutionSource"] == "USER_APPROVED_AI_PROPOSAL" and row["hardConstraintConflict"] != row["reviewerAConflict"] for row in records) == 10
    assert {row["resolutionSource"] for row in records} == {"REVIEWER_A_B_AGREEMENT", "USER_APPROVED_AI_PROPOSAL"}
    materialize(tmp_path)
    validate_bundle(tmp_path)


def test_g2_adjudicated_qrels_rejects_tampered_public_identity(tmp_path):
    materialize(tmp_path)
    qrels = tmp_path / "adjudicated_qrels.jsonl"
    rows = [json.loads(line) for line in qrels.read_text(encoding="utf-8").splitlines()]
    rows[0]["blindCandidateId"] = "0" * 64
    qrels.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="source binding, score, or conflict replay validation failed|artifact hash drift"):
        validate_bundle(tmp_path)


def test_g2_adjudicated_qrels_rejects_public_sample_not_bound_by_phase_manifest(tmp_path):
    tampered = tmp_path / "public_review_sample.jsonl"
    shutil.copyfile(PUBLIC_SAMPLE, tampered)
    rows = tampered.read_text(encoding="utf-8").splitlines()
    first = json.loads(rows[0])
    first["blindCandidateId"] = "0" * 64
    rows[0] = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    tampered.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="frozen Phase A manifest"):
        build_records(REVIEWER_A, REVIEWER_B, PROPOSAL, tampered)


def test_g2_materialize_never_leaves_pass_receipt_before_core_validation(tmp_path, monkeypatch):
    def fail_core(_output_dir):
        raise ValueError("forced core replay failure")

    monkeypatch.setattr(adjudication, "_validate_core", fail_core)
    with pytest.raises(ValueError, match="forced core replay failure"):
        materialize(tmp_path)
    assert not (tmp_path / "receipt.json").exists()
