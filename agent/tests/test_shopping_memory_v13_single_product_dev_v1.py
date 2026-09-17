from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from evaluation.shopping_memory_v13_single_product_dev_v1 import (
    ALLOWED_INPUTS,
    CATALOG_PATH,
    CONTRACT_PATH,
    CORPUS_PATH,
    DEV_PATH,
    DeterministicBM25,
    DevRunnerError,
    build_documents,
    canonical_binding,
    catalog_value_set,
    cluster_bootstrap,
    eligibility,
    materialize_report,
    memory_score,
    ranking_metrics,
    rerank,
    tokenize,
    _guard,
)


def product(product_id: str, *, category: str = "snacks", value: str = "barbecue") -> dict:
    return {
        "productId": product_id,
        "categoryId": category,
        "productName": f"Product {product_id}",
        "price": 1.0,
        "aspects": [{
            "attributeKey": "flavor",
            "displayValue": value.title(),
            "normalizedValue": value,
        }],
    }


def preference(**updates) -> dict:
    value = {
        "attributeKey": "flavor",
        "catalogRevision": "catalog-v1",
        "categoryId": "snacks",
        "displayValue": "Barbecue",
        "normalizedValue": "barbecue",
        "preferenceKind": "prefer",
        "recipientScope": "self",
        "source": "user_confirmed",
    }
    value.update(updates)
    return value


def question(*, preferences=None, target=None, question_type="single_product") -> dict:
    target = target or product("2")
    return {
        "conversationId": "conversation-1",
        "questionId": "question-1",
        "questionType": question_type,
        "query": "ＢＡＲＢＥＣＵＥ barbecue 商品",
        "memoryEpisodes": [{
            "categoryId": "snacks",
            "preferences": preferences or [preference()],
        }],
        "targetProducts": [target],
    }


def fixture_inputs():
    documents = build_documents([product("1", value="plain"), product("2")])
    corpus = {item.product_id: item for item in documents}
    catalog = catalog_value_set([{
        "catalogRevision": "catalog-v1", "categoryId": "snacks",
        "attributeKey": "flavor", "normalizedValue": "barbecue",
        "displayLabel": "Barbecue",
    }])
    return documents, corpus, catalog


def test_tokenizer_is_nfkc_lower_unicode_runs_and_query_distinct():
    assert tokenize("ＡＢＣ-中文 １２3 abc") == ("abc", "中文", "123", "abc")
    assert tokenize("ＡＢＣ-中文 １２3 abc", distinct=True) == ("abc", "中文", "123")


def test_bm25_uses_frozen_tie_break_and_target_metrics():
    documents, _, _ = fixture_inputs()
    engine = DeterministicBM25(documents)
    ranked = engine.raw_scores("barbecue")
    assert ranked[0][0] == "2" and ranked[0][1] > 0
    tied = engine.raw_scores("absent-token")
    assert [item[0] for item in tied] == ["1", "2"]
    assert ranking_metrics(["2", "1"], "2") == {
        "hitAt10": 1.0, "nDCGAt10": 1.0,
        "MRRAt50": 1.0, "targetRecallAt50": 1.0,
    }


def test_eligibility_accepts_exact_positive_fixture_and_records_context_hash():
    _, corpus, catalog = fixture_inputs()
    accepted, reasons, context = eligibility(question(), corpus=corpus, catalog=catalog)
    assert accepted is True and reasons == ()
    assert context is not None and context["bindingBytes"] <= 4096
    assert len(context["evaluatorContextHash"]) == 64


@pytest.mark.parametrize(
    ("preferences", "reason"),
    [
        ([preference(), preference(normalizedValue="other")], "CATEGORY_ATTRIBUTE_NOT_UNIQUE"),
        ([preference(recipientScope="other")], "RECIPIENT_SCOPE_NOT_SELF"),
        ([preference(source="model_inferred")], "SOURCE_NOT_USER_CONFIRMED"),
        ([preference(normalizedValue="missing")], "CATALOG_TUPLE_MISSING"),
        ([preference(categoryId="other")], "PREFERENCE_EPISODE_CATEGORY_MISMATCH"),
    ],
)
def test_eligibility_emits_machine_readable_reasons(preferences, reason):
    _, corpus, catalog = fixture_inputs()
    accepted, reasons, _ = eligibility(
        question(preferences=preferences), corpus=corpus, catalog=catalog,
    )
    assert accepted is False and reason in reasons


def test_add_on_is_excluded_without_touching_quality_fields():
    _, corpus, catalog = fixture_inputs()
    accepted, reasons, context = eligibility(
        {"questionType": "add_on_deals"}, corpus=corpus, catalog=catalog,
    )
    assert accepted is False
    assert reasons == ("QUESTION_TYPE_NOT_SINGLE_PRODUCT",)
    assert context is None


def test_scoring_and_rerank_are_bounded_and_candidate_preserving():
    documents, corpus, _ = fixture_inputs()
    base = [("1", 1.0), ("2", 0.99)]
    preferences = [preference()]
    assert memory_score(preferences, corpus["2"]) == 1.0
    assert memory_score([preference(preferenceKind="indifferent")], corpus["2"]) == 0.0
    assert memory_score([preference(categoryId="other")], corpus["2"]) == 0.0
    ranked = rerank(base, documents=corpus, preferences=preferences, weight=0.08)
    assert ranked == ["2", "1"]
    assert set(ranked) == {"1", "2"} and len(ranked) == 2


def test_cluster_bootstrap_is_seeded_and_uses_conversation_clusters():
    deltas = [("a", 1.0), ("a", 0.0), ("b", 0.0)]
    first = cluster_bootstrap(deltas, iterations=200, seed=7)
    second = cluster_bootstrap(deltas, iterations=200, seed=7)
    assert first == second
    assert first["clusterCount"] == 2
    assert first["unit"] == "conversationId"
    assert first["meanDelta"] == pytest.approx(0.25)


def test_input_allowlist_contains_no_validation_or_sealed_and_rejects_unknown(tmp_path: Path):
    assert DEV_PATH.resolve() in ALLOWED_INPUTS
    assert CORPUS_PATH.resolve() in ALLOWED_INPUTS
    assert CATALOG_PATH.resolve() in ALLOWED_INPUTS
    assert CONTRACT_PATH.resolve() in ALLOWED_INPUTS
    assert all("validation" not in path.name and "sealed" not in path.name for path in ALLOWED_INPUTS)
    with pytest.raises(DevRunnerError, match="not development-allowlisted"):
        _guard(tmp_path / "validation.jsonl")


def test_binding_shape_is_deterministic_and_materialization_never_overwrites(tmp_path: Path):
    first = canonical_binding("snacks", "catalog-v1", [preference()])
    second = canonical_binding("snacks", "catalog-v1", [preference()])
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    output = tmp_path / "result-v1"
    report = {"decision": "HOLD"}
    path = materialize_report(output, report, [{"eligible": False}], [{"x": 1}])
    assert path.is_file() and (output / "SHA256SUMS.txt").is_file()
    assert (output / "trace.json").is_file() and (output / "receipt.json").is_file()
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        materialize_report(output, report, [], [])
