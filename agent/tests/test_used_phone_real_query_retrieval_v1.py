from pathlib import Path

from agent.evaluation.used_phone_real_query_retrieval_v1 import (
    evaluate_bundle,
    load_bundle,
)


BUNDLE = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "derived"
    / "ecommerce"
    / "used_phone_real_query_qrel_v1"
)


def test_real_query_bundle_identity_and_sparse_qrel_semantics():
    documents, queries, qrels = load_bundle(BUNDLE)

    assert len(documents) == 252
    assert len(queries) == 259
    assert len(qrels) == 259
    assert sum(
        int(row["relevance"]) > 0
        for rows in qrels.values()
        for row in rows
    ) == 254
    assert sum(
        int(row["relevance"]) == 0
        for rows in qrels.values()
        for row in rows
    ) == 5


def test_train_ablation_reports_fields_without_treating_unjudged_as_negative():
    report = evaluate_bundle(BUNDLE, split="train", include_vector=False)

    assert report["split"] == "train"
    assert report["sparseQrel"] is True
    assert report["unjudgedIsNegative"] is False
    assert set(report["variants"]) == {
        "legacy_concat", "title_only", "title_brand", "title_brand_attribute",
        "title_brand_attribute_expanded",
    }
    for metrics in report["variants"].values():
        assert metrics["queryCount"] == 228
        assert metrics["positiveQueryCount"] == 224
        assert metrics["negativeOnlyQueryCount"] == 4
        assert 0 <= metrics["hitAt1"] <= 1
        assert 0 <= metrics["hitAt3"] <= 1
        assert 0 <= metrics["recallAt50"] <= 1
        assert metrics["hardConstraintViolationRate"] is None
