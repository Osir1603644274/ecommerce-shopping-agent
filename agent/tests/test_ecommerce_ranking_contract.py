from copy import deepcopy

import pytest

from app.domains.ecommerce.ranking_contract import (
    RANKING_FORMULA,
    RANKING_TIE_BREAK,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
    TwoStageRankingContractError,
    normalize_persisted_ranking_values,
    normalize_search_products_detail,
)


def valid_search_detail(
    *,
    pool: list[int] | None = None,
    ranked: list[int] | None = None,
) -> dict:
    pool = [101, 202, 303] if pool is None else pool
    ranked = [303, 101] if ranked is None else ranked
    candidates = []
    evidence = []
    for product_id in ranked:
        ref = f"product:{product_id}:title"
        evidence.append({"ref": ref, "field": "title", "rawValue": str(product_id)})
        candidates.append({
            "id": product_id,
            "title": str(product_id),
            "priceStatus": "unverified",
            "snapshotPriceMinor": None,
            "currency": "CNY",
            "evidenceRefs": [ref],
        })
    return {
        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
        "candidatePoolIds": list(pool),
        "rankedItemIds": list(ranked),
        "candidateIds": list(ranked),
        "candidates": candidates,
        "retrievalTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "candidatePoolCount": len(pool),
            "authoritativeFactCount": len(pool),
        },
        "rankingTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "inputCandidateCount": len(pool),
            "rankedItemCount": len(ranked),
            "tieBreak": RANKING_TIE_BREAK,
            "formula": RANKING_FORMULA,
        },
        "citationTrace": {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "sourceTool": "search_products",
            "rankedItemIds": list(ranked),
            "evidenceRefCount": len(evidence),
            "binding": "current_successful_tool_call_ranked_items_only",
        },
        "evidenceRefs": [item["ref"] for item in evidence],
        "evidence": evidence,
        "eliminated": [],
    }


def test_valid_two_stage_contract_normalizes_compatibility_alias():
    output = normalize_search_products_detail(valid_search_detail())

    assert output.candidate_pool_ids == (101, 202, 303)
    assert output.ranked_item_ids == (303, 101)
    values = output.normalized_values()
    assert values == {
        "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
        "candidatePoolIds": [101, 202, 303],
        "rankedItemIds": [303, 101],
        "productIds": [303, 101],
        "evidenceRefs": ["product:303:title", "product:101:title"],
        "candidateSupport": {
            "hasCompleteMatch": True,
            "fullySupportedProductIds": [303, 101],
            "closestAlternativeProductIds": [],
            "hardUnknownsByProduct": {},
            "productPresentations": [
                {
                    "productId": product_id,
                    "title": str(product_id),
                    "brand": None,
                    "priceMinor": None,
                    "currency": "CNY",
                    "priceStatus": "unverified",
                    "priceDataNature": None,
                    "pricePolicy": None,
                    "priceDisclosure": None,
                    "selectionType": "full_match",
                    "titleEvidenceRef": f"product:{product_id}:title",
                    "brandEvidenceRef": None,
                    "priceEvidenceRef": None,
                    "attributes": [
                        {
                            "key": key,
                            "status": "unknown",
                            "value": None,
                            "evidenceRef": None,
                        }
                        for key in (
                            "os", "battery_health", "screen_originality",
                            "motherboard_repair", "battery_originality",
                            "scratch_level", "shell_condition",
                        )
                    ],
                }
                for product_id in (303, 101)
            ],
        },
    }


def test_presentation_is_derived_from_bound_facts_and_unknown_price_stays_unknown():
    output = normalize_search_products_detail(valid_search_detail())

    card = output.candidate_support["productPresentations"][0]

    assert card["productId"] == 303
    assert card["title"] == "303"
    assert card["titleEvidenceRef"] == "product:303:title"
    assert card["priceStatus"] == "unverified"
    assert card["priceMinor"] is None
    assert card["priceEvidenceRef"] is None


def test_persisted_presentation_rejects_cross_product_identity():
    values = normalize_search_products_detail(valid_search_detail()).normalized_values()
    values["candidateSupport"]["productPresentations"][0]["productId"] = 202

    with pytest.raises(TwoStageRankingContractError) as exc:
        normalize_persisted_ranking_values(values)

    assert exc.value.code == "product_presentation_identity_mismatch"


def test_persisted_presentation_rejects_unknown_price_promoted_to_zero():
    values = normalize_search_products_detail(valid_search_detail()).normalized_values()
    values["candidateSupport"]["productPresentations"][0]["priceMinor"] = 0

    with pytest.raises(TwoStageRankingContractError) as exc:
        normalize_persisted_ranking_values(values)

    assert exc.value.code == "product_presentation_price_invalid"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda row: row.update(candidatePoolIds=[]), "ranking_ids_missing"),
        (lambda row: row.update(candidatePoolIds=[101, 101]), "duplicate_ranking_id"),
        (lambda row: row.update(candidatePoolIds=[True, 202]), "invalid_ranking_id"),
        (lambda row: row.update(candidatePoolIds=[0, 202]), "invalid_ranking_id"),
        (lambda row: row.update(candidatePoolIds=[-1, 202]), "invalid_ranking_id"),
        (lambda row: row.update(candidatePoolIds=["101", 202]), "invalid_ranking_id"),
        (lambda row: row.update(rankedItemIds=[404]), "ranked_item_outside_candidate_pool"),
        (lambda row: row.update(candidateIds=[101, 303]), "candidate_alias_mismatch"),
        (lambda row: row["candidates"].reverse(), "candidate_row_order_mismatch"),
        (lambda row: row.pop("contractVersion"), "unsupported_ranking_contract_version"),
        (lambda row: row.update(contractVersion="unknown"), "unsupported_ranking_contract_version"),
    ],
)
def test_attack_matrix_rejects_invalid_two_stage_outputs(mutation, code):
    detail = valid_search_detail()
    mutation(detail)

    with pytest.raises(TwoStageRankingContractError) as exc:
        normalize_search_products_detail(detail)

    assert exc.value.code == code


def test_citations_cannot_reference_pool_only_product():
    detail = valid_search_detail()
    detail["evidence"].append(
        {"ref": "product:202:title", "field": "title", "rawValue": "202"}
    )
    detail["evidenceRefs"].append("product:202:title")
    detail["citationTrace"]["evidenceRefCount"] += 1

    with pytest.raises(TwoStageRankingContractError) as exc:
        normalize_search_products_detail(detail)

    assert exc.value.code == "citation_outside_ranked_items"


def test_fully_eliminated_pool_is_valid_but_has_no_ranked_rows_or_evidence():
    detail = valid_search_detail(pool=[101, 202], ranked=[])

    output = normalize_search_products_detail(detail)

    assert output.candidate_pool_ids == (101, 202)
    assert output.ranked_item_ids == ()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("candidatePoolIds", [101, 101], "duplicate_ranking_id"),
        ("rankedItemIds", [404], "ranked_item_outside_candidate_pool"),
        ("productIds", [101, 303], "product_ids_alias_mismatch"),
        ("evidenceRefs", ["product:101:title", "product:303:title"], "normalized_evidence_refs_mismatch"),
        ("contractVersion", "unknown", "unsupported_ranking_contract_version"),
    ],
)
def test_persisted_contract_rejects_tampering(field, value, code):
    values = normalize_search_products_detail(valid_search_detail()).normalized_values()
    values[field] = value

    with pytest.raises(TwoStageRankingContractError) as exc:
        normalize_persisted_ranking_values(values)

    assert exc.value.code == code


def test_contract_validation_does_not_mutate_tool_detail():
    detail = valid_search_detail()
    before = deepcopy(detail)
    normalize_search_products_detail(detail)
    assert detail == before
