from dataclasses import FrozenInstanceError

import pytest

from app.domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_REGISTRY,
    USED_PHONE_ATTRIBUTE_RULESET_VERSION,
    materialize_used_phone_product_attributes,
    observe_used_phone_attributes,
    used_phone_attribute_ruleset_payload,
    used_phone_attribute_ruleset_sha256,
)


def test_registry_is_immutable_and_self_describing():
    os_spec = USED_PHONE_ATTRIBUTE_REGISTRY["os"]
    assert os_spec.type == "enum"
    assert os_spec.unit == "enum"
    assert os_spec.operators == ("eq", "in", "not_in")
    assert os_spec.allowed_values == ("ios", "android")
    assert os_spec.semantic_status == "controlled_interpretation_not_source_ground_truth"

    with pytest.raises(TypeError):
        USED_PHONE_ATTRIBUTE_REGISTRY["other"] = os_spec  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        os_spec.key = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    "raw_value",
    [
        "苹果",
        "苹果 iPhone 15 Pro 原装屏保护膜 90%+",
        "原装屏保护膜",
        "这是一台 ios 手机",
    ],
)
def test_does_not_infer_from_brand_title_or_substrings(raw_value):
    observations = observe_used_phone_attributes(raw_value)
    assert all(item.status == "unknown" for item in observations.values())
    assert all(item.fact is None for item in observations.values())


def test_exact_tokens_are_casefolded_and_whitespace_normalized():
    observations = observe_used_phone_attributes(
        "  IoS  , 90%+ , 原装屏,主板未维修,原装电池,轻微划痕,外壳正常 "
    )
    assert observations["os"].fact.value == "ios"
    assert observations["battery_health"].fact.value == "90_plus"
    assert observations["screen_originality"].fact.value == "original"
    assert observations["motherboard_repair"].fact.value == "not_repaired"
    assert observations["battery_originality"].fact.value == "original"
    assert observations["scratch_level"].fact.value == "light"
    assert observations["shell_condition"].fact.value == "normal"
    assert observations["os"].matched_raw_tokens == ("IoS",)


def test_multiple_os_values_conflict_and_produce_no_fact():
    observation = observe_used_phone_attributes("ios,安卓")["os"]
    assert observation.status == "conflict"
    assert observation.fact is None
    assert observation.matched_raw_tokens == ("安卓", "ios")


def test_duplicate_aliases_for_same_value_do_not_conflict():
    observation = observe_used_phone_attributes("安卓,android/安卓,安卓")["os"]
    assert observation.status == "known"
    assert observation.fact.value == "android"
    assert observation.matched_raw_tokens == ("android/安卓", "安卓", "安卓")


def test_multiple_battery_health_values_conflict_and_produce_no_fact():
    observation = observe_used_phone_attributes("90%+,80%-90%")[
        "battery_health"
    ]
    assert observation.status == "conflict"
    assert observation.fact is None
    assert observation.matched_raw_tokens == ("80%-90%", "90%+")


def test_non_original_screen_requires_the_complete_exact_alias():
    exact = observe_used_phone_attributes("非原装内屏/外屏")["screen_originality"]
    partial = observe_used_phone_attributes("非原装内屏")["screen_originality"]
    assert exact.status == "known"
    assert exact.fact.value == "non_original"
    assert partial.status == "unknown"


def test_observations_are_order_independent_and_immutable():
    first = observe_used_phone_attributes("90%+,ios,原装屏")
    second = observe_used_phone_attributes("原装屏,90%+,ios")
    assert first == second

    with pytest.raises(TypeError):
        first["os"] = second["os"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        first["os"].status = "unknown"  # type: ignore[misc]


def test_missing_values_are_explicitly_unknown():
    observations = observe_used_phone_attributes(None)
    assert tuple(observations) == (
        "battery_health",
        "battery_originality",
        "motherboard_repair",
        "os",
        "scratch_level",
        "screen_originality",
        "shell_condition",
    )
    assert all(item.status == "unknown" for item in observations.values())
    assert all(item.fact is None for item in observations.values())
    assert all(item.matched_raw_tokens == () for item in observations.values())


def test_ruleset_provenance_is_canonical_and_includes_exact_aliases():
    payload = used_phone_attribute_ruleset_payload()
    assert payload["matching"] == (
        "exact_comma_delimited_token_casefold_whitespace_normalized"
    )
    assert payload["titleBrandSellerUsedForFact"] is False
    assert payload["unboundPercentageUsedForFact"] is False
    assert payload["version"] == USED_PHONE_ATTRIBUTE_RULESET_VERSION
    assert payload["fields"]["os"]["aliases"]["android"] == [
        "Android/安卓",
        "安卓",
    ]
    digest = used_phone_attribute_ruleset_sha256()
    assert len(digest) == 64
    assert digest == "c4f7400d9697a8c5246b4c56740031e8239b7642b494d3508db6f02f7f205b1a"


def test_requirement_registry_and_frozen_evidence_payload_share_value_domains():
    fields = used_phone_attribute_ruleset_payload()["fields"]
    assert set(fields) == set(USED_PHONE_ATTRIBUTE_REGISTRY)
    for key, spec in USED_PHONE_ATTRIBUTE_REGISTRY.items():
        assert set(spec.allowed_values) == set(fields[key]["allowedValues"])
        assert spec.operators == ("eq", "in", "not_in")
        assert fields[key]["operators"] == ["IN", "NOT_IN"]


@pytest.mark.parametrize(
    ("raw_value", "key"),
    [
        ("主板维修", "motherboard_repair"),
        ("原装", "battery_originality"),
        ("有划痕", "scratch_level"),
        ("外壳有磕碰 外壳有缺失", "shell_condition"),
        ("电池健康90%", "battery_health"),
    ],
)
def test_opaque_or_partial_tokens_are_not_upgraded_to_facts(raw_value, key):
    observation = observe_used_phone_attributes(raw_value)[key]
    assert observation.status == "unknown"
    assert observation.fact is None


def test_new_field_conflicts_remain_conflicts_without_facts():
    observations = observe_used_phone_attributes(
        "主板未维修,主板有过维修,原装电池,非原装电池,无划痕,明显划痕,"
        "外壳正常,外壳有磕碰"
    )
    for key in (
        "motherboard_repair",
        "battery_originality",
        "scratch_level",
        "shell_condition",
    ):
        assert observations[key].status == "conflict"
        assert observations[key].fact is None


def test_snapshot_materialization_keeps_conflict_raw_but_never_normalizes_it():
    rows = materialize_used_phone_product_attributes(
        "iOS,主板未维修,原装电池,无划痕,明显划痕,外壳正常"
    )
    by_key = {row["key"]: row for row in rows}
    assert by_key["motherboard_repair"]["normalizedText"] == "not_repaired"
    assert by_key["battery_originality"]["normalizedText"] == "original"
    assert by_key["shell_condition"]["normalizedText"] == "normal"
    assert by_key["scratch_level"]["normalizedText"] is None
    assert by_key["scratch_level"]["rawValue"] == (
        "iOS,主板未维修,原装电池,无划痕,明显划痕,外壳正常"
    )
    assert by_key["scratch_level"]["confidence"] == 0.0
