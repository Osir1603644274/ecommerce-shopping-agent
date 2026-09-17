from __future__ import annotations

import copy
import json
import shutil

import pytest

from agent.evaluation import used_phone_439_retrieval_grid_v1 as grid
from agent.evaluation.used_phone_439_retrieval_review_package_v3 import DEFAULT_RUN_DIR
from agent.evaluation.used_phone_439_retrieval_scorer_v1 import (
    _query_metrics,
    build_adjudication_package,
    build_final_qrels,
    evaluate_qrels,
    intake_review,
    paired_bootstrap_lower,
    score_final_bundle,
    validate_final_qrel_bundle,
    validate_score_report,
    validate_review_submission,
    write_score_report,
)


def _small_review():
    pack = [{"blindQueryId": "BQ-1", "candidates": [{"candidateToken": "C-1"}, {"candidateToken": "C-2"}]}]
    review = [{
        "blindQueryId": "BQ-1",
        "reviews": [
            {"candidateToken": "C-1", "relevance": 3, "reason": "标题直接匹配", "evidenceBasis": ["title_claim"]},
            {"candidateToken": "C-2", "relevance": "unknown", "reason": "证据不足", "evidenceBasis": ["attribute_fact"]},
        ],
    }]
    return pack, review


def test_review_submission_accepts_exact_numeric_and_unknown_schema() -> None:
    pack, review = _small_review()
    validate_review_submission(pack, review)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "grade", "reason", "basis"])
def test_review_submission_fails_closed(mutation: str) -> None:
    pack, review = _small_review()
    broken = copy.deepcopy(review)
    if mutation == "missing":
        broken[0]["reviews"].pop()
    elif mutation == "duplicate":
        broken[0]["reviews"][1]["candidateToken"] = "C-1"
    elif mutation == "grade":
        broken[0]["reviews"][0]["relevance"] = 4
    elif mutation == "reason":
        broken[0]["reviews"][0]["reason"] = ""
    else:
        broken[0]["reviews"][0]["evidenceBasis"] = ["invented"]
    with pytest.raises(ValueError):
        validate_review_submission(pack, broken)


def test_query_metrics_use_binary_relevance_and_graded_ndcg() -> None:
    metrics = _query_metrics([1, 2, 3], {1: 1, 2: 3, 3: 2, 4: 3})
    assert metrics is not None
    assert metrics["pooledRecallAt50"] == pytest.approx(2 / 3)
    assert metrics["pooledHitAt3"] == 1
    assert 0 < metrics["pooledNdcgAt10"] < 1


def test_bootstrap_is_seeded_and_requires_positive_effect() -> None:
    first = paired_bootstrap_lower([0.02, 0.03, 0.04], samples=1000)
    second = paired_bootstrap_lower([0.02, 0.03, 0.04], samples=1000)
    assert first == second
    assert first > 0


def test_all_zero_qrels_fail_minimum_eligible_gate() -> None:
    evaluator = grid.read_jsonl(DEFAULT_RUN_DIR / "private_evaluator/evaluator_pool_v3.jsonl")
    qrels = [
        {"queryId": row["queryId"], "productId": candidate["productId"], "grade": 0}
        for row in evaluator for candidate in row["candidates"]
    ]
    report = evaluate_qrels(DEFAULT_RUN_DIR, qrels)
    assert report["minimumEligibleGate"] == "HOLD_INSUFFICIENT_RELEVANT_QUERIES"
    assert report["decision"]["reviewChain"] == "UNVERIFIED"
    assert report["decision"]["productionDefaultSwitch"] == "HOLD"


def _write_submission(input_dir, output_dir, reviewer, *, adjudicator=False) -> None:
    pack = grid.read_jsonl(input_dir / "blind_pack.jsonl")
    rows = []
    for query in pack:
        reviews = []
        for index, candidate in enumerate(query["candidates"]):
            if adjudicator:
                grade = 3
            elif reviewer == "reviewer01" and index == 0:
                grade = 3
            elif reviewer == "reviewer02" and index == 0:
                grade = 2
            else:
                grade = 0
            reviews.append({
                "candidateToken": candidate["candidateToken"],
                "relevance": grade,
                "reason": "独立规则判断",
                "evidenceBasis": ["title_claim"],
            })
        rows.append({"blindQueryId": query["blindQueryId"], "reviews": reviews})
    output_dir.mkdir(parents=True)
    grid.write_jsonl(output_dir / "reviewed.jsonl", rows)
    role = "adjudicator" if adjudicator else "reviewer"
    value = {
        "schemaVersion": "used-phone-439-codex-review-access-attestation-v1",
        "reviewer": reviewer,
        "role": role,
        "authority": "CODEX_ASSISTED_NOT_HUMAN_GOLD",
        "selfAttested": True,
        "taskIdentity": f"attempt012-{reviewer}",
        "inputsRead": ["contract.json", "blind_pack.jsonl", "receipt.json"],
        "blindPackSha256": grid.sha256(input_dir / "blind_pack.jsonl"),
        "contractSha256": grid.sha256(input_dir / "contract.json"),
        "distributionReceiptSha256": grid.sha256(input_dir / "receipt.json"),
        "mappingSeen": False,
        "otherReviewerSeen": False,
        "repositoryContextSeen": False,
    }
    (output_dir / "access_attestation.json").write_text(
        grid.canonical(value) + "\n", encoding="utf-8"
    )


def test_review_adjudication_final_chain_and_mapping_tamper(tmp_path) -> None:
    submissions = tmp_path / "submissions"
    intakes = tmp_path / "intakes"
    for reviewer in ("reviewer01", "reviewer02"):
        input_dir = DEFAULT_RUN_DIR / "distribution" / reviewer
        submission = submissions / reviewer
        _write_submission(input_dir, submission, reviewer)
        intake_review(input_dir, submission, intakes / reviewer, reviewer)
    adjudication = tmp_path / "adjudication"
    build_adjudication_package(DEFAULT_RUN_DIR, intakes, adjudication)
    adjudicator_submission = submissions / "adjudicator"
    _write_submission(
        adjudication / "distribution", adjudicator_submission, "adjudicator", adjudicator=True
    )
    final_bundle = tmp_path / "final"
    build_final_qrels(
        DEFAULT_RUN_DIR, intakes, adjudication, adjudicator_submission, final_bundle
    )
    assert len(validate_final_qrel_bundle(DEFAULT_RUN_DIR, intakes, adjudication, final_bundle)) == 2175
    report = score_final_bundle(DEFAULT_RUN_DIR, intakes, adjudication, final_bundle)
    assert report["decision"]["reviewChain"] == "ACCEPT"
    score_path = tmp_path / "score_report.json"
    write_score_report(
        report, DEFAULT_RUN_DIR, intakes, adjudication, final_bundle, score_path
    )
    assert validate_score_report(
        DEFAULT_RUN_DIR, intakes, adjudication, final_bundle, score_path
    ) == report
    tampered_score = json.loads(score_path.read_text(encoding="utf-8"))
    tampered_score["decision"]["reason"] = "tampered"
    score_path.write_text(grid.canonical(tampered_score) + "\n", encoding="utf-8")
    score_receipt_path = score_path.with_name(score_path.name + ".receipt.json")
    score_receipt = json.loads(score_receipt_path.read_text(encoding="utf-8"))
    score_receipt["report"] = {
        "sha256": grid.sha256(score_path), "bytes": score_path.stat().st_size
    }
    score_receipt_path.write_text(grid.canonical(score_receipt) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="does not replay"):
        validate_score_report(
            DEFAULT_RUN_DIR, intakes, adjudication, final_bundle, score_path
        )

    tampered_adjudication = tmp_path / "tampered_adjudication"
    tampered_final = tmp_path / "tampered_final"
    shutil.copytree(adjudication, tampered_adjudication)
    shutil.copytree(final_bundle, tampered_final)
    contract_path = tampered_adjudication / "distribution/contract.json"
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    contract["evidencePriority"] = list(reversed(contract["evidencePriority"]))
    contract_path.write_text(grid.canonical(contract) + "\n", encoding="utf-8")
    distribution_receipt_path = tampered_adjudication / "distribution/receipt.json"
    distribution_receipt = json.loads(distribution_receipt_path.read_text(encoding="utf-8"))
    distribution_receipt["artifacts"]["contract.json"] = {
        "sha256": grid.sha256(contract_path), "bytes": contract_path.stat().st_size
    }
    distribution_receipt_path.write_text(grid.canonical(distribution_receipt) + "\n", encoding="utf-8")
    tampered_private_receipt_path = tampered_adjudication / "private/receipt.json"
    tampered_private_receipt = json.loads(tampered_private_receipt_path.read_text(encoding="utf-8"))
    tampered_private_receipt["distributionReceiptSha256"] = grid.sha256(distribution_receipt_path)
    tampered_private_receipt_path.write_text(grid.canonical(tampered_private_receipt) + "\n", encoding="utf-8")
    tampered_attestation_path = tampered_final / "adjudication_access_attestation.json"
    tampered_attestation = json.loads(tampered_attestation_path.read_text(encoding="utf-8"))
    tampered_attestation["contractSha256"] = grid.sha256(contract_path)
    tampered_attestation["distributionReceiptSha256"] = grid.sha256(distribution_receipt_path)
    tampered_attestation_path.write_text(grid.canonical(tampered_attestation) + "\n", encoding="utf-8")
    tampered_final_receipt_path = tampered_final / "receipt.json"
    tampered_final_receipt = json.loads(tampered_final_receipt_path.read_text(encoding="utf-8"))
    tampered_final_receipt["adjudicationPrivateReceiptSha256"] = grid.sha256(tampered_private_receipt_path)
    tampered_final_receipt["adjudicationAttestationSha256"] = grid.sha256(tampered_attestation_path)
    tampered_final_receipt_path.write_text(grid.canonical(tampered_final_receipt) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="adjudication contract"):
        validate_final_qrel_bundle(
            DEFAULT_RUN_DIR, intakes, tampered_adjudication, tampered_final
        )

    mapping_path = adjudication / "private/mapping.json"
    mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
    query_items = list(mapping["queries"].values())
    first_token = next(iter(query_items[0]["candidates"]))
    second_token = next(iter(query_items[1]["candidates"]))
    query_items[0]["candidates"][first_token], query_items[1]["candidates"][second_token] = (
        query_items[1]["candidates"][second_token], query_items[0]["candidates"][first_token]
    )
    mapping_path.write_text(grid.canonical(mapping) + "\n", encoding="utf-8")
    private_receipt_path = adjudication / "private/receipt.json"
    private_receipt = json.loads(private_receipt_path.read_text(encoding="utf-8"))
    private_receipt["mappingSha256"] = grid.sha256(mapping_path)
    private_receipt_path.write_text(grid.canonical(private_receipt) + "\n", encoding="utf-8")
    final_receipt_path = final_bundle / "receipt.json"
    final_receipt = json.loads(final_receipt_path.read_text(encoding="utf-8"))
    final_receipt["adjudicationPrivateReceiptSha256"] = grid.sha256(private_receipt_path)
    final_receipt_path.write_text(grid.canonical(final_receipt) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="adjudication mapping"):
        validate_final_qrel_bundle(DEFAULT_RUN_DIR, intakes, adjudication, final_bundle)
