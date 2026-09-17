from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent.evaluation.used_phone_human_qrel_phase_v1 import (
    apply_deterministic_hard_gate,
    audit_completed_second_human_reviews,
    audit_full_catalog_residuals,
    build_second_human_blind_package,
    explicitly_satisfies,
    freeze_adjudicated_sealed_human_qrels,
    hard_constraint_status,
    run_adjudicated_sealed_baselines,
    run_nonsealed_rrf_hard_gate_baseline,
    run_preregistered_baselines,
)
from agent.evaluation.used_phone_human_qrel_v1 import (
    freeze_role_aware_human_qrels,
    read_jsonl,
)


ROOT = Path(__file__).resolve().parents[2]
ANNOTATION_DIR = ROOT / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v1"
DOCUMENTS_PATH = ROOT / "data" / "derived" / "ecommerce" / "used_phone_real_query_qrel_v1" / "documents.jsonl"
SCHEMA_DIR = ROOT / "agent" / "evaluation" / "schemas"


def _rows() -> list[dict]:
    return read_jsonl(ANNOTATION_DIR / "review_batches" / "batch_001.jsonl")


def _documents() -> list[dict]:
    return read_jsonl(DOCUMENTS_PATH)


def _completed_second_rows() -> list[dict]:
    first_by_query = {row["queryId"]: row for row in _rows()}
    completed = build_second_human_blind_package(_rows())
    for row in completed:
        row["schemaVersion"] = "used-phone-human-qrel-second-review-completed-v1"
        row["reviewProtocol"]["secondHumanReviewerId"] = "independent-human-02"
        row["reviewProtocol"]["reviewCompletedAt"] = "2026-08-17T12:00:00+08:00"
        first_by_product = {
            int(candidate["productId"]): candidate
            for candidate in first_by_query[row["queryId"]]["candidates"]
        }
        for candidate in row["candidates"]:
            first = first_by_product[int(candidate["productId"])]["judgment"]
            candidate["secondHumanJudgment"] = {
                "grade": first["grade"],
                "label": first["label"],
                "evidenceBasis": list(first["evidenceBasis"]),
                "reason": "第二位人类基于盲包中的商品证据独立完成判断。",
                "reviewStatus": "human_confirmed",
                "reviewerId": "independent-human-02",
                "reviewedAt": "2026-08-17T12:00:00+08:00",
            }
    return completed


def _adjudication(second_rows: list[dict]) -> dict:
    audit = audit_completed_second_human_reviews(_rows(), second_rows)
    audit_sha = hashlib.sha256(json.dumps(
        audit, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    labels = {
        None: "unknown", 0: "not_relevant", 1: "marginal",
        2: "relevant", 3: "highly_relevant",
    }
    return {
        "schemaVersion": "used-phone-human-qrel-adjudication-v1",
        "status": "human_adjudicated",
        "sourceBindings": {
            **audit["sourceBindings"],
            "secondReviewAuditContentSha256": audit_sha,
        },
        "adjudicator": {
            "kind": "human",
            "aiMayNotBeCountedAsHuman": True,
            "adjudicatorId": "human-adjudicator-01",
            "adjudicatedAt": "2026-08-17T13:00:00+08:00",
        },
        "queries": [{
            "queryId": row["queryId"],
            "approveExactAgreements": True,
            "agreementApprovalReason": "人工裁决者复核并批准两位审核者的一致判断。",
            "disagreementDecisions": [{
                "productId": item["productId"],
                "firstHumanGrade": item["firstHumanGrade"],
                "secondHumanGrade": item["secondHumanGrade"],
                "finalGrade": item["secondHumanGrade"],
                "finalLabel": labels[item["secondHumanGrade"]],
                "reason": "人工裁决者复核证据后采用第二位审核者的等级。",
            } for item in row["disagreements"]],
        } for row in audit["queries"]],
    }


def test_role_aware_freeze_excludes_non_static_roles_and_sealed_rows() -> None:
    freeze = freeze_role_aware_human_qrels(_rows())

    assert freeze["status"] == "PRELIMINARY_NOT_GOLD"
    assert freeze["staticMetricQueryIds"] == [
        "uphq-001", "uphq-002", "uphq-005", "uphq-007", "uphq-010",
    ]
    assert freeze["withheldSealedTestQueryIds"] == [
        "uphq-008", "uphq-009", "uphq-012",
    ]
    assert freeze["excludedFromStaticMetrics"] == {
        "capability_boundary": ["uphq-006"],
        "constraint_smoke": ["uphq-003"],
        "multi_turn_e2e": ["uphq-004"],
        "no_answer_constraint": ["uphq-011"],
    }
    assert {item["queryId"] for item in freeze["qrels"]} == set(freeze["staticMetricQueryIds"])
    assert freeze["unknownJudgmentCountExcludedNotNegative"] == 2


def test_role_aware_freeze_refuses_sealed_release_without_second_human_adjudication() -> None:
    with pytest.raises(ValueError, match="independent second-human review and adjudication"):
        freeze_role_aware_human_qrels(_rows(), include_sealed_test=True)


def test_full_catalog_residual_audit_keeps_external_products_unjudged_and_confirms_a11x_gap() -> None:
    audit = audit_full_catalog_residuals(review_rows=_rows(), documents=_documents())

    assert audit["catalogProductCount"] == 252
    assert audit["queryCount"] == 12
    assert audit["poolExternalProductsAreLabels"] is False
    assert audit["noAnswerConstraintConclusion"] == {
        "queryId": "uphq-011",
        "fullCatalogScanned": 252,
        "explicitHardConstraintMatchCount": 0,
        "conclusion": "no_explicit_catalog_answer",
        "scope": "frozen 252-product catalog and explicit title/attribute evidence only",
    }
    assert all(item["outsidePoolIsUnjudgedNotNegative"] is True for item in audit["audits"])


def test_exact_iphone_13_pro_constraint_rejects_pro_max_and_mixed_titles() -> None:
    constraint = {"key": "model", "value": "iphone_13_pro"}

    assert explicitly_satisfies({
        "item_title": "苹果 iPhone 13 Pro 二手手机",
        "brand": "苹果/Apple",
        "attr_value": "",
    }, constraint) is True
    assert explicitly_satisfies({
        "item_title": "苹果 iPhone 13 Pro Max 二手手机",
        "brand": "苹果/Apple",
        "attr_value": "",
    }, constraint) is False
    assert explicitly_satisfies({
        "item_title": "苹果13ProMax 全网通，标题另写 iPhone13Pro",
        "brand": "苹果/Apple",
        "attr_value": "",
    }, constraint) is False


def test_second_human_package_is_blinded_pending_and_validates_against_draft_2020_12() -> None:
    package = build_second_human_blind_package(_rows())
    schema = json.loads((SCHEMA_DIR / "used_phone_human_qrel_second_review_v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    assert [row["queryId"] for row in package] == ["uphq-008", "uphq-009", "uphq-012"]
    assert all(not list(validator.iter_errors(row)) for row in package)
    for row in package:
        assert row["reviewProtocol"]["aiMayNotBeCountedAsSecondHuman"] is True
        assert row["reviewProtocol"]["firstHumanLabelsDisclosed"] is False
        assert all(candidate["secondHumanJudgment"] == {
            "grade": None,
            "label": "unjudged",
            "evidenceBasis": [],
            "reason": "",
            "reviewStatus": "pending_second_human_review",
            "reviewerId": "",
            "reviewedAt": None,
        } for candidate in row["candidates"])


def test_completed_second_human_review_validates_and_stays_pending_adjudication() -> None:
    completed = _completed_second_rows()
    schema = json.loads(
        (SCHEMA_DIR / "used_phone_human_qrel_second_review_completed_v1.schema.json")
        .read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(schema)

    assert all(not list(validator.iter_errors(row)) for row in completed)
    audit = audit_completed_second_human_reviews(_rows(), completed)
    assert audit["status"] == "PENDING_HUMAN_ADJUDICATION_NOT_GOLD"
    assert audit["candidateCount"] == audit["agreementCount"]
    assert audit["disagreementCount"] == 0
    assert audit["exactGradeAgreementRate"] == 1.0
    assert audit["sealedReleaseAllowed"] is False


def test_second_human_audit_exposes_disagreement_without_releasing_labels() -> None:
    completed = _completed_second_rows()
    judgment = completed[0]["candidates"][0]["secondHumanJudgment"]
    judgment.update({"grade": 0, "label": "not_relevant"})

    audit = audit_completed_second_human_reviews(_rows(), completed)

    assert audit["disagreementCount"] == 1
    disagreement = next(
        item
        for query in audit["queries"]
        for item in query["disagreements"]
    )
    assert disagreement["secondHumanGrade"] == 0
    assert disagreement["adjudicationStatus"] == "pending_human_adjudication"
    assert disagreement["finalGrade"] is None
    assert audit["sealedReleaseAllowed"] is False


def test_second_human_audit_rejects_non_independent_reviewer_and_identity_tampering() -> None:
    same_reviewer = _completed_second_rows()
    first_reviewer = next(
        row["reviewerId"] for row in _rows()
        if row["queryId"] == same_reviewer[0]["queryId"]
    )
    same_reviewer[0]["reviewProtocol"]["secondHumanReviewerId"] = first_reviewer
    for candidate in same_reviewer[0]["candidates"]:
        candidate["secondHumanJudgment"]["reviewerId"] = first_reviewer
    with pytest.raises(ValueError, match="not independent"):
        audit_completed_second_human_reviews(_rows(), same_reviewer)

    tampered = _completed_second_rows()
    tampered[0]["candidates"][0]["itemTitle"] += "（被篡改）"
    with pytest.raises(ValueError, match="candidate evidence mismatch"):
        audit_completed_second_human_reviews(_rows(), tampered)

    incomplete = _completed_second_rows()
    incomplete[0]["candidates"].pop()
    with pytest.raises(ValueError, match="candidate identity set mismatch"):
        audit_completed_second_human_reviews(_rows(), incomplete)

    boolean_grade = _completed_second_rows()
    boolean_grade[0]["candidates"][0]["secondHumanJudgment"].update({
        "grade": True,
        "label": "marginal",
    })
    with pytest.raises(ValueError, match="grade/label mismatch"):
        audit_completed_second_human_reviews(_rows(), boolean_grade)


def test_human_adjudication_schema_and_sealed_freeze_require_explicit_approval() -> None:
    completed = _completed_second_rows()
    adjudication = _adjudication(completed)
    schema = json.loads(
        (SCHEMA_DIR / "used_phone_human_qrel_adjudication_v1.schema.json")
        .read_text(encoding="utf-8")
    )
    assert not list(Draft202012Validator(schema).iter_errors(adjudication))

    frozen = freeze_adjudicated_sealed_human_qrels(
        _rows(), completed, adjudication
    )

    assert frozen["status"] == "SEALED_QREL_READY_FOR_ONE_SHOT_OFFLINE_EVALUATION"
    assert frozen["productionReleaseAllowed"] is False
    assert frozen["queryIds"] == ["uphq-008", "uphq-009", "uphq-012"]
    assert all(item["split"] == "sealed_test" for item in frozen["qrels"])
    assert all(item["labelSource"] == "two_human_review_and_adjudication" for item in frozen["qrels"])
    assert len(frozen["qrels"]) + frozen["unknownJudgmentCountExcludedNotNegative"] == sum(
        len(row["candidates"]) for row in completed
    )


def test_sealed_freeze_rejects_forged_bindings_and_missing_disagreement_decisions() -> None:
    completed = _completed_second_rows()
    adjudication = _adjudication(completed)
    adjudication["sourceBindings"]["completedSecondReviewContentSha256"] = "0" * 64
    with pytest.raises(ValueError, match="source binding mismatch"):
        freeze_adjudicated_sealed_human_qrels(_rows(), completed, adjudication)

    disagreed = _completed_second_rows()
    disagreed[0]["candidates"][0]["secondHumanJudgment"].update({
        "grade": 0,
        "label": "not_relevant",
    })
    missing = _adjudication(disagreed)
    row_with_decision = next(
        row for row in missing["queries"] if row["disagreementDecisions"]
    )
    row_with_decision["disagreementDecisions"] = []
    with pytest.raises(ValueError, match="disagreement set mismatch"):
        freeze_adjudicated_sealed_human_qrels(_rows(), disagreed, missing)


def test_review_batch_validates_against_draft_2020_12() -> None:
    schema = json.loads((SCHEMA_DIR / "used_phone_human_qrel_review_v1.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    assert all(not list(validator.iter_errors(row)) for row in _rows())


def test_historical_baseline_artifact_does_not_present_stage_timings_as_end_to_end() -> None:
    report = json.loads(
        (ANNOTATION_DIR / "artifacts" / "preregistered_primary_retrieval_baselines_v1.json")
        .read_text(encoding="utf-8")
    )

    assert report["rankingInputContainsHumanJudgments"] is False
    assert "labelsReadAfterPredictions" not in report
    assert report["latencyMeasurementStatus"].startswith("HISTORICAL_PARTIAL_STAGE_TIMINGS")
    assert "queryLatencyMs" not in report["variants"]["bm25_vector_rrf"]
    assert report["variants"]["bm25_vector_rrf"]["stageOnlyLatencyMs"]["stage"] == "rrf_fusion_only"
    assert "queryLatencyMs" not in report["variants"]["rrf_cross_encoder"]
    assert report["variants"]["rrf_cross_encoder"]["stageOnlyLatencyMs"]["stage"] == "cross_encoder_rerank_only"


class _FakeCrossEncoder:
    def rank(self, _query: str, documents: list[dict]) -> list[tuple[int, float]]:
        return [(int(document["id"]), float(index)) for index, document in enumerate(reversed(documents))]


def test_preregistered_baselines_keep_sealed_metrics_unread(monkeypatch: pytest.MonkeyPatch) -> None:
    import agent.evaluation.used_phone_human_qrel_phase_v1 as phase

    monkeypatch.setattr(
        phase,
        "build_vector_ranker",
        lambda _products: lambda _query, products: [int(product["id"]) for product in reversed(products)],
    )
    report = run_preregistered_baselines(
        review_rows=copy.deepcopy(_rows()),
        documents=_documents(),
        cross_encoder=_FakeCrossEncoder(),  # type: ignore[arg-type]
    )

    assert report["status"] == "PRELIMINARY_HOLD"
    assert report["staticMetricQueryIds"] == [
        "uphq-001", "uphq-002", "uphq-005", "uphq-007", "uphq-010",
    ]
    assert report["withheldSealedTestQueryIds"] == ["uphq-008", "uphq-009", "uphq-012"]
    assert set(report["variants"]) == {"bm25", "title_vector", "bm25_vector_rrf", "rrf_cross_encoder"}
    assert all(variant["queryCount"] == 5 for variant in report["variants"].values())
    assert report["rankingInputFields"] == ["queryId", "retrievalQuery"]
    assert report["rankingInputContainsHumanJudgments"] is False
    assert "labelsReadAfterPredictions" not in report
    assert report["queryLatencyContract"].startswith("end-to-end per variant")
    stages = report["stageLatencyMs"]
    assert report["variants"]["bm25_vector_rrf"]["queryLatencyMs"]["mean"] == pytest.approx(
        stages["bm25"]["mean"] + stages["title_vector"]["mean"] + stages["rrf_fusion"]["mean"]
    )
    assert report["variants"]["rrf_cross_encoder"]["queryLatencyMs"]["mean"] == pytest.approx(
        report["variants"]["bm25_vector_rrf"]["queryLatencyMs"]["mean"]
        + stages["cross_encoder_rerank"]["mean"]
    )


def test_adjudicated_sealed_baselines_use_only_authenticated_qrels_after_predictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.evaluation.used_phone_human_qrel_phase_v1 as phase

    completed = _completed_second_rows()
    sealed = freeze_adjudicated_sealed_human_qrels(
        _rows(), completed, _adjudication(completed)
    )
    monkeypatch.setattr(
        phase,
        "build_vector_ranker",
        lambda _products: lambda _query, products: [
            int(product["id"]) for product in reversed(products)
        ],
    )
    report = run_adjudicated_sealed_baselines(
        review_rows=copy.deepcopy(_rows()),
        documents=_documents(),
        sealed_freeze=sealed,
        cross_encoder=_FakeCrossEncoder(),  # type: ignore[arg-type]
    )

    assert report["status"] == "SEALED_ONE_SHOT_OFFLINE_EVALUATION_COMPLETE_HOLD"
    assert report["productionReleaseAllowed"] is False
    assert report["onlineConfigurationChanged"] is False
    assert report["queryIds"] == ["uphq-008", "uphq-009", "uphq-012"]
    assert report["rankingInputFields"] == ["queryId", "retrievalQuery"]
    assert report["rankingInputContainsHumanJudgments"] is False
    assert report["labelsOpenedAfterAllPredictions"] is True
    assert set(report["variants"]) == {
        "bm25", "title_vector", "bm25_vector_rrf", "rrf_cross_encoder",
    }
    assert all(item["queryCount"] == 3 for item in report["variants"].values())


def test_adjudicated_sealed_baselines_reject_tampered_qrels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.evaluation.used_phone_human_qrel_phase_v1 as phase

    completed = _completed_second_rows()
    sealed = freeze_adjudicated_sealed_human_qrels(
        _rows(), completed, _adjudication(completed)
    )
    sealed["qrels"][0]["relevance"] = True
    monkeypatch.setattr(
        phase,
        "build_vector_ranker",
        lambda _products: lambda _query, products: [
            int(product["id"]) for product in reversed(products)
        ],
    )

    with pytest.raises(ValueError, match="invalid adjudicated sealed qrel"):
        run_adjudicated_sealed_baselines(
            review_rows=copy.deepcopy(_rows()),
            documents=_documents(),
            sealed_freeze=sealed,
            cross_encoder=_FakeCrossEncoder(),  # type: ignore[arg-type]
        )


def test_deterministic_hard_gate_distinguishes_fail_from_unknown() -> None:
    apple = {"product_id": 1, "item_title": "苹果13", "brand": "苹果/apple", "attr_value": "ios"}
    missing = {"product_id": 2, "item_title": "普通二手手机", "brand": "", "attr_value": ""}
    oppo = {"product_id": 3, "item_title": "OPPO A9", "brand": "oppo", "attr_value": "安卓"}
    brand_constraint = {"key": "brand", "operator": "eq", "value": "apple"}

    assert hard_constraint_status(apple, brand_constraint) == "pass"
    assert hard_constraint_status(missing, brand_constraint) == "unknown"
    assert hard_constraint_status(oppo, brand_constraint) == "fail"
    assert hard_constraint_status(
        {**oppo, "attr_value": "ios,安卓"},
        {"key": "os", "operator": "eq", "value": "android"},
    ) == "unknown"
    assert hard_constraint_status(
        {**apple, "item_title": "苹果 iPhone 13 Pro Max"},
        {"key": "model", "operator": "eq", "value": "iphone_13_pro"},
    ) == "fail"
    assert hard_constraint_status(
        {**apple, "item_title": "苹果13 Pro Max / iPhone 13 Pro"},
        {"key": "model", "operator": "eq", "value": "iphone_13_pro"},
    ) == "unknown"

    ranking, trace = apply_deterministic_hard_gate(
        [3, 2, 1],
        {"intent": {"hardConstraints": [brand_constraint]}},
        {1: apple, 2: missing, 3: oppo},
        candidate_depth=50,
    )
    assert ranking == [1, 2]
    assert trace["confirmedCandidateCount"] == 1
    assert trace["unknownCandidateCount"] == 1
    assert trace["eliminatedProductIds"] == [3]


def test_nonsealed_rrf_hard_gate_baseline_keeps_sealed_unread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.evaluation.used_phone_human_qrel_phase_v1 as phase

    monkeypatch.setattr(
        phase,
        "build_vector_ranker",
        lambda _products: lambda _query, products: [
            int(product["id"]) for product in reversed(products)
        ],
    )
    report = run_nonsealed_rrf_hard_gate_baseline(
        review_rows=copy.deepcopy(_rows()),
        documents=_documents(),
    )

    assert report["sealedDataRead"] is False
    assert report["staticMetricQueryIds"] == [
        "uphq-001", "uphq-002", "uphq-005", "uphq-007", "uphq-010",
    ]
    assert report["rankingInputFields"] == ["queryId", "retrievalQuery"]
    assert report["rankingInputContainsHumanJudgments"] is False
    assert report["labelsOpenedAfterAllPredictions"] is True
    raw = report["variants"]["bm25_vector_rrf"]
    gated = report["variants"]["bm25_vector_rrf_hard_gate"]
    assert gated["top3ExplicitHardViolationCount"] == 0
    assert gated["top3ExplicitHardViolationRate"] == 0
    assert gated["top3ExplicitHardViolationRate"] <= raw["top3ExplicitHardViolationRate"]
    assert report["gateTraceByQuery"]["uphq-005"]["explicitFailuresExcluded"] is True
