from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from agent.evaluation.used_phone_human_qrel_v1 import (
    build_bundle,
    build_review_rows,
    freeze_human_qrels,
    preserve_existing_human_judgments,
    read_jsonl,
    validate_review_rows,
    validate_synthetic_prices,
)


ROOT = Path(__file__).resolve().parents[2]
ANNOTATION_DIR = (
    ROOT / "data" / "annotations" / "ecommerce" / "used_phone_human_qrel_v1"
)
SPARSE_DIR = (
    ROOT / "data" / "derived" / "ecommerce" / "used_phone_real_query_qrel_v1"
)
SYNTHETIC_PRICE_DIR = (
    ROOT / "data" / "derived" / "ecommerce" /
    "used_phone_synthetic_reference_price_v1"
)


def _build_rows() -> list[dict]:
    return build_review_rows(
        seed_queries=read_jsonl(ANNOTATION_DIR / "seed_queries.jsonl"),
        documents=read_jsonl(SPARSE_DIR / "documents.jsonl"),
        sparse_qrels=read_jsonl(SPARSE_DIR / "qrels.jsonl"),
        pool_depth=12,
    )


def test_first_batch_is_unjudged_and_covers_key_use_case_products() -> None:
    rows = _build_rows()
    assert len(rows) == 12
    by_query = {row["queryId"]: row for row in rows}
    game_ids = {item["productId"] for item in by_query["uphq-001"]["candidates"]}
    battery_ids = {item["productId"] for item in by_query["uphq-002"]["candidates"]}
    assert {320635, 634685, 1620782, 1957430, 2459636} <= game_ids
    assert {614290, 956966, 1620782, 1721544, 2625578} <= battery_ids
    assert all(
        candidate["judgment"]["grade"] is None
        and candidate["judgment"]["label"] == "unjudged"
        for row in rows
        for candidate in row["candidates"]
    )
    assert all(row["humanConfirmed"] is False for row in rows)


def test_legacy_sparse_qrel_is_pool_provenance_not_a_copied_label() -> None:
    rows = _build_rows()
    row = next(item for item in rows if item["queryId"] == "uphq-007")
    assert any(
        "legacy_sparse_qrel_pool_only" in candidate["selectionSources"]
        for candidate in row["candidates"]
    )
    assert all(candidate["judgment"]["grade"] is None for candidate in row["candidates"])


def test_vector_and_cross_encoder_are_auditable_pool_sources() -> None:
    seeds = read_jsonl(ANNOTATION_DIR / "seed_queries.jsonl")[:1]
    documents = read_jsonl(SPARSE_DIR / "documents.jsonl")
    sparse_qrels = read_jsonl(SPARSE_DIR / "qrels.jsonl")
    product_ids = [int(row["product_id"]) for row in documents]

    def vector_ranker(_query: str, _products: list[dict]) -> list[int]:
        return list(reversed(product_ids))

    def cross_encoder(_query: str, products: list[dict]) -> list[tuple[int, float]]:
        return [
            (int(product["id"]), float(len(products) - index))
            for index, product in enumerate(reversed(products))
        ]

    rows = build_review_rows(
        seed_queries=seeds,
        documents=documents,
        sparse_qrels=sparse_qrels,
        pool_depth=5,
        cross_encoder_input_depth=8,
        vector_ranker=vector_ranker,
        cross_encoder_ranker=cross_encoder,
        retrieval_metadata={"test": True},
    )
    row = rows[0]
    assert row["pool"]["systems"] == [
        "bm25_title",
        "bm25_fields",
        "bm25_expanded_fields",
        "vector_title",
        "cross_encoder_title",
    ]
    assert row["pool"]["crossEncoderInputCount"] >= 8
    assert any(
        "vector_title" in candidate["selectionSources"]
        for candidate in row["candidates"]
    )
    assert any(
        "cross_encoder_title" in candidate["selectionSources"]
        and "cross_encoder_title" in candidate["retrievalScores"]
        for candidate in row["candidates"]
    )


def test_confusion_audit_adds_unseen_candidates_without_preassigning_labels() -> None:
    seeds = copy.deepcopy(read_jsonl(ANNOTATION_DIR / "seed_queries.jsonl")[:1])
    seeds[0]["confusionAuditQueries"] = [
        {"query": "大内存手机", "rationale": "大内存不等于大电池"},
        {"query": "大屏手机", "rationale": "大屏不等于大电池"},
    ]
    rows = build_review_rows(
        seed_queries=seeds,
        documents=read_jsonl(SPARSE_DIR / "documents.jsonl"),
        sparse_qrels=read_jsonl(SPARSE_DIR / "qrels.jsonl"),
        pool_depth=5,
        confusion_audit_depth=2,
    )
    row = rows[0]
    audit = row["pool"]["confusionAudit"]
    assert audit["enabled"] is True
    assert audit["poolSourcesAreNotLabels"] is True
    assert len(audit["probes"]) == 2
    audit_ids = {
        product_id
        for probe in audit["probes"]
        for product_id in probe["selectedProductIds"]
    }
    audit_candidates = [
        candidate for candidate in row["candidates"]
        if "confusion_audit_pool_only" in candidate["selectionSources"]
    ]
    assert {candidate["productId"] for candidate in audit_candidates} == audit_ids
    assert audit_candidates
    assert all(candidate["judgment"]["grade"] is None for candidate in audit_candidates)


def test_pool_upgrade_preserves_candidate_level_human_judgment() -> None:
    old_rows = _build_rows()
    candidate = old_rows[0]["candidates"][0]
    candidate["judgment"].update({
        "grade": 1,
        "label": "marginal",
        "evidenceBasis": ["title_claim"],
        "reason": "人工已确认",
        "reviewStatus": "human_confirmed",
        "reviewerId": "project-owner-human-01",
        "reviewedAt": "2026-08-16T20:00:00+08:00",
    })
    new_rows = _build_rows()
    merged = preserve_existing_human_judgments(new_rows, old_rows)
    merged_candidate = next(
        item for item in merged[0]["candidates"]
        if item["productId"] == candidate["productId"]
    )
    assert merged_candidate["judgment"] == candidate["judgment"]
    validate_review_rows(merged)


def test_review_validation_rejects_forged_or_incomplete_human_confirmation() -> None:
    rows = _build_rows()
    forged = copy.deepcopy(rows)
    forged[0]["humanConfirmed"] = True
    with pytest.raises(ValueError, match="wrong status"):
        validate_review_rows(forged)

    incomplete = copy.deepcopy(rows)
    incomplete[0]["humanConfirmed"] = True
    incomplete[0]["reviewStatus"] = "human_confirmed"
    with pytest.raises(ValueError, match="lacks reviewer identity/time"):
        validate_review_rows(incomplete)


def test_review_validation_requires_grade_label_consistency_and_reason() -> None:
    rows = _build_rows()
    row = rows[0]
    row["humanConfirmed"] = True
    row["reviewStatus"] = "human_confirmed"
    row["reviewerId"] = "human-reviewer"
    row["reviewedAt"] = "2026-08-16T12:00:00+08:00"
    row["candidates"][0]["judgment"].update({
        "grade": 3,
        "label": "relevant",
        "reason": "标题直接包含游戏用途",
        "reviewStatus": "human_confirmed",
        "reviewerId": "human-reviewer",
        "reviewedAt": "2026-08-16T12:00:00+08:00",
    })
    with pytest.raises(ValueError, match="grade/label mismatch"):
        validate_review_rows(rows)
    row["candidates"][0]["judgment"]["label"] = "highly_relevant"
    row["candidates"][0]["judgment"]["reason"] = ""
    with pytest.raises(ValueError, match="lacks reason"):
        validate_review_rows(rows)


def test_freeze_requires_every_query_and_candidate_to_be_human_reviewed() -> None:
    rows = _build_rows()
    with pytest.raises(ValueError, match="pending queries"):
        freeze_human_qrels(rows)

    one = copy.deepcopy(rows[:1])
    one[0].update({
        "humanConfirmed": True,
        "reviewStatus": "human_confirmed",
        "reviewerId": "human-reviewer",
        "reviewedAt": "2026-08-16T12:00:00+08:00",
    })
    for index, candidate in enumerate(one[0]["candidates"]):
        candidate["judgment"].update({
            "grade": 3 if index == 0 else None,
            "label": "highly_relevant" if index == 0 else "unknown",
            "evidenceBasis": ["title_claim"] if index == 0 else [],
            "reason": "可观察标题支持" if index == 0 else "现有字段不足",
            "reviewStatus": "human_confirmed",
            "reviewerId": "human-reviewer",
            "reviewedAt": "2026-08-16T12:00:00+08:00",
        })
    qrels = freeze_human_qrels(one)
    assert len(qrels) == 1
    assert qrels[0]["labelSource"] == "human"
    assert qrels[0]["relevance"] == 3


def test_build_bundle_writes_pending_manifest_without_gold_claim(tmp_path: Path) -> None:
    output = tmp_path / "batch.jsonl"
    manifest_path = tmp_path / "manifest.json"
    manifest = build_bundle(
        seed_path=ANNOTATION_DIR / "seed_queries.jsonl",
        document_path=SPARSE_DIR / "documents.jsonl",
        sparse_qrel_path=SPARSE_DIR / "qrels.jsonl",
        output_path=output,
        manifest_path=manifest_path,
        pool_depth=8,
    )
    assert manifest["status"] == "PENDING_HUMAN_REVIEW_NOT_GOLD"
    assert manifest["queryCount"] == 12
    assert manifest["humanJudgmentCount"] == 0
    assert manifest["safety"]["existingQrelGradesCopied"] is False
    assert len(read_jsonl(output)) == 12
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == manifest


def test_synthetic_prices_are_identity_bound_and_exposed_as_non_real_evidence(
    tmp_path: Path,
) -> None:
    output = tmp_path / "batch.jsonl"
    manifest = build_bundle(
        seed_path=ANNOTATION_DIR / "seed_queries.jsonl",
        document_path=SPARSE_DIR / "documents.jsonl",
        sparse_qrel_path=SPARSE_DIR / "qrels.jsonl",
        output_path=output,
        manifest_path=tmp_path / "manifest.json",
        pool_depth=8,
        synthetic_price_path=SYNTHETIC_PRICE_DIR / "prices.jsonl",
        synthetic_price_manifest_path=SYNTHETIC_PRICE_DIR / "manifest.json",
    )
    candidate = read_jsonl(output)[0]["candidates"][0]
    price = candidate["syntheticReferencePrice"]
    assert price["policy"] == "budget_and_ranking"
    assert price["dataNature"] == "synthetic"
    assert "非真实报价" in price["disclosureZh"]
    assert manifest["inputs"]["syntheticPrices"]["rowCount"] == 252
    assert manifest["safety"]["syntheticPricePolicy"]["enabled"] is True


def test_synthetic_price_identity_mismatch_fails_closed() -> None:
    documents = read_jsonl(SPARSE_DIR / "documents.jsonl")
    prices = read_jsonl(SYNTHETIC_PRICE_DIR / "prices.jsonl")
    with pytest.raises(ValueError, match="exactly match catalog identities"):
        validate_synthetic_prices(prices[:-1], documents)

    forged = copy.deepcopy(prices)
    forged[0]["dataNature"] = "observed"
    with pytest.raises(ValueError, match="invalid synthetic price provenance"):
        validate_synthetic_prices(forged, documents)


def test_review_validation_rejects_forged_candidate_synthetic_price() -> None:
    rows = _build_rows()
    rows[0]["candidates"][0]["syntheticReferencePrice"] = {
        "referencePriceMinor": 100_000,
        "currency": "CNY",
        "dataNature": "observed",
        "priceStatus": "synthetic",
        "labelZh": "模拟参考价",
        "disclosureZh": "AI 合成，非真实报价",
        "policy": "budget_and_ranking",
        "sourceSchemaVersion": "used-phone-synthetic-reference-price-v1",
    }
    with pytest.raises(ValueError, match="invalid candidate synthetic price"):
        validate_review_rows(rows)
