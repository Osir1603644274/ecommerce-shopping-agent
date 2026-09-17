from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.context_multiagent_blind_review_intake_v1 import (
    sha256_file,
    validate_submission,
)
from evaluation.context_multiagent_blind_adjudication_v1 import (
    build_package,
    finalize,
)
from evaluation.context_multiagent_blind_unblind_v1 import unblind


SCORES = {
    "constraintFidelity": 5,
    "evidenceDiscipline": 5,
    "taskProgression": 5,
    "usefulness": 5,
}


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def prepare(
    tmp_path: Path,
    reviewer_preferences: tuple[tuple[str, str], ...] = (
        ("reviewer01", "B"),
        ("reviewer02", "A"),
    ),
):
    run = tmp_path / "run"
    pack = run / "blind"
    public01 = [{"itemId": "blind-01", "candidateA": "left", "candidateB": "right"}]
    public02 = [{"itemId": "blind-01", "candidateA": "right", "candidateB": "left"}]
    write_jsonl(pack / "reviewer01.jsonl", public01)
    write_jsonl(pack / "reviewer02.jsonl", public02)
    submissions = {}
    for reviewer, preference in reviewer_preferences:
        submission = pack / f"{reviewer}-submission.jsonl"
        write_jsonl(
            submission,
            [{
                "itemId": "blind-01",
                "review": {
                    "candidateA": SCORES,
                    "candidateB": SCORES,
                    "overallPreference": preference,
                    "reason": "bounded",
                },
            }],
        )
        public = pack / f"{reviewer}.jsonl"
        normalized, receipt = validate_submission(
            public_packet=public, submission=submission, reviewer_id=reviewer
        )
        completed = pack / f"{reviewer}-reviewed.jsonl"
        write_jsonl(completed, normalized)
        receipt["normalizedOutputSha256"] = sha256_file(completed)
        receipt_path = pack / f"{reviewer}-receipt.json"
        write_json(receipt_path, receipt)
        submissions[reviewer] = (completed, receipt_path)
    mapping = {
        "items": [{
            "itemId": "blind-01",
            "scenarioId": "scenario-1",
            "reviewer01": {"A": "CTX1b", "B": "MA1"},
            "reviewer02": {"A": "MA1", "B": "CTX1b"},
        }]
    }
    write_json(pack / "SEALED_DO_NOT_SHARE.json", mapping)
    write_json(
        pack / "receipt.json",
        {
            "reviewer01Sha256": sha256_file(pack / "reviewer01.jsonl"),
            "reviewer02Sha256": sha256_file(pack / "reviewer02.jsonl"),
            "sealedMappingSha256": sha256_file(pack / "SEALED_DO_NOT_SHARE.json"),
        },
    )
    write_json(
        run / "summary.json",
        {
            "resultHash": "result-1",
            "engineeringDecision": "ENGINEERING_HOLD",
            "effectivenessDecision": "BOUNDED_DESCRIPTIVE_HOLD_NO_UNTOUCHED_CONFIRMATION",
            "productionDefaultDecision": "HOLD_KEEP_CURRENT_DEFAULT",
        },
    )
    return pack, submissions


def test_intake_and_unblind_preserve_mirror_and_arm_identity(tmp_path: Path) -> None:
    pack, submissions = prepare(tmp_path)
    completed01, receipt01 = submissions["reviewer01"]
    completed02, receipt02 = submissions["reviewer02"]
    attestation = pack / "attestation.json"
    write_json(
        attestation,
        {
            "reviewer01AndReviewer02AreDifferentHumans": True,
            "reviewer01Independent": True,
            "reviewer02Independent": True,
            "bothMappingBlindUntilReviewsFrozen": True,
            "bothAutomaticResultsBlindUntilReviewsFrozen": True,
            "unblindingAllowed": True,
            "reviewer01CompletedSha256": sha256_file(completed01),
            "reviewer02CompletedSha256": sha256_file(completed02),
            "reviewer01IntakeReceiptSha256": sha256_file(receipt01),
            "reviewer02IntakeReceiptSha256": sha256_file(receipt02),
        },
    )
    result = unblind(
        pack_dir=pack,
        reviewer01_completed=completed01,
        reviewer02_completed=completed02,
        reviewer01_receipt=receipt01,
        reviewer02_receipt=receipt02,
        attestation_path=attestation,
        output_dir=pack / "unblind",
    )
    assert result["status"] == "HUMAN_BLIND_REVIEW_COMPLETE"
    assert result["preferenceCountsAcrossReviews"] == {"CTX1b": 0, "MA1": 2, "tie": 0}
    assert result["automaticEngineeringDecisionUnchanged"] == "ENGINEERING_HOLD"


def test_unblind_rejects_before_reading_mapping_without_attestation(tmp_path: Path) -> None:
    pack, submissions = prepare(tmp_path)
    (pack / "SEALED_DO_NOT_SHARE.json").write_text("not json", encoding="utf-8")
    attestation = pack / "attestation.json"
    write_json(attestation, {"unblindingAllowed": False})
    with pytest.raises(ValueError, match="attestations are required"):
        unblind(
            pack_dir=pack,
            reviewer01_completed=submissions["reviewer01"][0],
            reviewer02_completed=submissions["reviewer02"][0],
            reviewer01_receipt=submissions["reviewer01"][1],
            reviewer02_receipt=submissions["reviewer02"][1],
            attestation_path=attestation,
            output_dir=pack / "unblind",
        )
    assert not (pack / "unblind").exists()


def test_adjudication_distribution_contains_only_split_items_and_no_identity_leak(
    tmp_path: Path,
) -> None:
    pack, submissions = prepare(
        tmp_path,
        reviewer_preferences=(("reviewer01", "B"), ("reviewer02", "B")),
    )
    completed01, receipt01 = submissions["reviewer01"]
    completed02, receipt02 = submissions["reviewer02"]
    attestation = pack / "attestation.json"
    write_json(
        attestation,
        {
            "reviewer01AndReviewer02AreDifferentHumans": True,
            "reviewer01Independent": True,
            "reviewer02Independent": True,
            "bothMappingBlindUntilReviewsFrozen": True,
            "bothAutomaticResultsBlindUntilReviewsFrozen": True,
            "unblindingAllowed": True,
            "reviewer01CompletedSha256": sha256_file(completed01),
            "reviewer02CompletedSha256": sha256_file(completed02),
            "reviewer01IntakeReceiptSha256": sha256_file(receipt01),
            "reviewer02IntakeReceiptSha256": sha256_file(receipt02),
        },
    )
    unblind_dir = pack / "unblind"
    result = unblind(
        pack_dir=pack,
        reviewer01_completed=completed01,
        reviewer02_completed=completed02,
        reviewer01_receipt=receipt01,
        reviewer02_receipt=receipt02,
        attestation_path=attestation,
        output_dir=unblind_dir,
    )
    assert result["status"] == "HOLD_PENDING_THIRD_BLIND_ADJUDICATION"
    distribution = tmp_path / "distribution"
    receipt = build_package(
        unblinded_result_path=unblind_dir / "unblinded-result.json",
        source_packet=unblind_dir / "adjudicator.jsonl",
        output_dir=distribution,
    )
    assert receipt["itemCount"] == 1
    assert receipt["identityLeakCount"] == 0
    public_text = (distribution / "adjudicator.jsonl").read_text(encoding="utf-8").lower()
    assert "ctx1b" not in public_text
    assert "ma1" not in public_text
    assert "reviewer01" not in public_text
    assert "reviewer02" not in public_text


def test_third_blind_adjudication_finalizes_only_split_item(tmp_path: Path) -> None:
    pack, submissions = prepare(
        tmp_path,
        reviewer_preferences=(("reviewer01", "B"), ("reviewer02", "B")),
    )
    completed01, receipt01 = submissions["reviewer01"]
    completed02, receipt02 = submissions["reviewer02"]
    first_attestation = pack / "attestation.json"
    write_json(
        first_attestation,
        {
            "reviewer01AndReviewer02AreDifferentHumans": True,
            "reviewer01Independent": True,
            "reviewer02Independent": True,
            "bothMappingBlindUntilReviewsFrozen": True,
            "bothAutomaticResultsBlindUntilReviewsFrozen": True,
            "unblindingAllowed": True,
            "reviewer01CompletedSha256": sha256_file(completed01),
            "reviewer02CompletedSha256": sha256_file(completed02),
            "reviewer01IntakeReceiptSha256": sha256_file(receipt01),
            "reviewer02IntakeReceiptSha256": sha256_file(receipt02),
        },
    )
    unblind_dir = pack / "unblind"
    unblind(
        pack_dir=pack,
        reviewer01_completed=completed01,
        reviewer02_completed=completed02,
        reviewer01_receipt=receipt01,
        reviewer02_receipt=receipt02,
        attestation_path=first_attestation,
        output_dir=unblind_dir,
    )
    distribution = tmp_path / "distribution"
    build_package(
        unblinded_result_path=unblind_dir / "unblinded-result.json",
        source_packet=unblind_dir / "adjudicator.jsonl",
        output_dir=distribution,
    )
    adjudicator_submission = tmp_path / "adjudicator-submission.jsonl"
    write_jsonl(
        adjudicator_submission,
        [
            {
                "itemId": "blind-01",
                "review": {
                    "candidateA": SCORES,
                    "candidateB": SCORES,
                    "overallPreference": "A",
                    "reason": "bounded adjudication",
                },
            }
        ],
    )
    normalized, intake = validate_submission(
        public_packet=distribution / "adjudicator.jsonl",
        submission=adjudicator_submission,
        reviewer_id="adjudicator",
    )
    adjudicator_completed = tmp_path / "adjudicator-reviewed.jsonl"
    write_jsonl(adjudicator_completed, normalized)
    intake["normalizedOutputSha256"] = sha256_file(adjudicator_completed)
    adjudicator_receipt = tmp_path / "adjudicator-receipt.json"
    write_json(adjudicator_receipt, intake)
    adjudicator_attestation = tmp_path / "adjudicator-attestation.json"
    write_json(
        adjudicator_attestation,
        {
            "adjudicatorIsDifferentHumanFromReviewer01AndReviewer02": True,
            "adjudicatorIndependent": True,
            "mappingBlindUntilReviewFrozen": True,
            "priorReviewsBlindUntilReviewFrozen": True,
            "automaticResultsBlindUntilReviewFrozen": True,
            "adjudicationAllowed": True,
            "adjudicatorCompletedSha256": sha256_file(adjudicator_completed),
            "adjudicatorIntakeReceiptSha256": sha256_file(adjudicator_receipt),
        },
    )
    final = finalize(
        pack_dir=pack,
        unblinded_result_path=unblind_dir / "unblinded-result.json",
        adjudicator_public=distribution / "adjudicator.jsonl",
        adjudicator_completed=adjudicator_completed,
        adjudicator_receipt=adjudicator_receipt,
        adjudicator_attestation=adjudicator_attestation,
        output_dir=tmp_path / "final",
    )
    assert final["status"] == "HUMAN_BLIND_REVIEW_COMPLETE"
    assert final["finalPreferenceCountsByItem"] == {
        "CTX1b": 1,
        "MA1": 0,
        "tie": 0,
    }
    assert final["automaticEngineeringDecisionUnchanged"] == "ENGINEERING_HOLD"
