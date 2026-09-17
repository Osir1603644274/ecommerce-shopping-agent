from __future__ import annotations

import copy
import json

import pytest

from agent.evaluation import used_phone_439_retrieval_grid_v1 as grid
from agent.evaluation.used_phone_439_retrieval_review_package_v3 import (
    DEFAULT_RUN_DIR,
    REVIEWERS,
    _price_target_score,
    _brand_matches,
    corrected_hard_requirements,
    corrected_soft_preferences,
    validate,
    validate_mapping,
)


def _artifacts():
    private = DEFAULT_RUN_DIR / "private_evaluator"
    evaluator = grid.read_jsonl(private / "evaluator_pool_v3.jsonl")
    intents = grid.read_jsonl(private / "frozen_taskstate_intents_v3.jsonl")
    mapping = json.loads((private / "sealed_mapping_v3.json").read_text(encoding="utf-8"))
    packs = {
        reviewer: grid.read_jsonl(DEFAULT_RUN_DIR / f"distribution/{reviewer}/blind_pack.jsonl")
        for reviewer in REVIEWERS
    }
    _documents, queries = grid.load_inputs()
    splits = {str(row["scenarioId"]): str(row["split"]) for row in queries}
    return mapping, packs, evaluator, intents, splits


def test_recommended_300_is_soft_with_frozen_decay() -> None:
    assert corrected_hard_requirements()["uphqv2-019"] == []
    price = [
        row for row in corrected_soft_preferences()["uphqv2-019"]
        if row["key"] == "price_target_minor"
    ]
    assert len(price) == 1
    assert price[0]["value"] == 30000
    assert _price_target_score(24000, 30000, price[0]["matchPolicy"]) == 1.0
    assert _price_target_score(36000, 30000, price[0]["matchPolicy"]) == 1.0
    assert _price_target_score(37500, 30000, price[0]["matchPolicy"]) == pytest.approx(0.5)
    assert _price_target_score(39000, 30000, price[0]["matchPolicy"]) == 0.0


def test_xiaomi_brand_does_not_accept_redmi_mi_substring() -> None:
    assert _brand_matches("小米/MI", "xiaomi") is True
    assert _brand_matches("红米/hongmi", "xiaomi") is False


def test_attempt005_package_replays() -> None:
    validate(DEFAULT_RUN_DIR)


def test_mapping_accepts_exact_artifacts() -> None:
    mapping, packs, evaluator, intents, splits = _artifacts()
    validate_mapping(mapping, packs, evaluator, intents, splits)


@pytest.mark.parametrize(
    "mutation",
    ["delete", "duplicate", "swap", "cross_query", "add", "query_text", "intent", "extra_field", "linked_split", "extra_reviewer", "duplicate_evaluator"],
)
def test_mapping_mutations_fail_closed(mutation: str) -> None:
    mapping, packs, evaluator, intents, splits = _artifacts()
    broken = copy.deepcopy(mapping)
    broken_packs = copy.deepcopy(packs)
    broken_evaluator = copy.deepcopy(evaluator)
    reviewer = REVIEWERS[0]
    query_ids = list(broken["reviewers"][reviewer])
    first, second = query_ids[:2]
    first_candidates = broken["reviewers"][reviewer][first]["candidates"]
    second_candidates = broken["reviewers"][reviewer][second]["candidates"]
    first_token = next(iter(first_candidates))
    second_token = next(iter(second_candidates))
    if mutation == "delete":
        del first_candidates[first_token]
    elif mutation == "duplicate":
        first_candidates[first_token] = next(iter(list(first_candidates.values())[1:]))
    elif mutation == "swap":
        first_candidates[first_token], second_candidates[second_token] = (
            second_candidates[second_token], first_candidates[first_token]
        )
    elif mutation == "cross_query":
        broken["reviewers"][reviewer][first]["queryId"] = broken["reviewers"][reviewer][second]["queryId"]
    elif mutation == "query_text":
        broken_packs[reviewer][0]["query"] = "tampered"
    elif mutation == "intent":
        broken_packs[reviewer][0]["intent"]["supportedHardRequirements"] = []
    elif mutation == "extra_field":
        broken_packs[reviewer][0]["unexpectedField"] = "leak"
    elif mutation == "linked_split":
        real_query = broken["reviewers"][reviewer][first]["queryId"]
        broken["reviewers"][reviewer][first]["split"] = "sealed_test"
        for row in broken_evaluator:
            if row["queryId"] == real_query:
                row["split"] = "sealed_test"
        for row in intents:
            if row["queryId"] == real_query:
                row["split"] = "sealed_test"
    elif mutation == "extra_reviewer":
        broken["reviewers"]["reviewer03"] = {}
    elif mutation == "duplicate_evaluator":
        broken_evaluator[0]["candidates"].append(copy.deepcopy(broken_evaluator[0]["candidates"][0]))
    else:
        first_candidates["C-unexpected"] = 999999999999
    with pytest.raises(ValueError):
        validate_mapping(broken, broken_packs, broken_evaluator, intents, splits)
