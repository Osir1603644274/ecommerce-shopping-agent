from types import SimpleNamespace

from app.control.validation_contracts import (
    RUNTIME_TOOL_CONTRACTS,
    harness_contract_tool_names,
    normalized_output_fields_for_tool,
    validated_evidence_fields_for_tool,
)
from app.domains.ecommerce.ranking_contract import (
    NORMALIZED_RANKING_OUTPUT_FIELDS,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
)
from app.llm import (
    HARNESS_CONTRACT_TOOL_NAMES,
    _is_harness_contract_covered,
    route_tool_schemas,
)
from app.tools import TOOL_SCHEMAS


def _state(goal: str):
    return SimpleNamespace(
        active_plan=None,
        goal=goal,
        task_type="local_life",
    )


def test_router_and_phase_contracts_share_one_registry() -> None:
    assert HARNESS_CONTRACT_TOOL_NAMES == harness_contract_tool_names()
    assert HARNESS_CONTRACT_TOOL_NAMES == frozenset(RUNTIME_TOOL_CONTRACTS)


def test_every_public_tool_has_a_complete_unified_contract() -> None:
    public_names = {schema["function"]["name"] for schema in TOOL_SCHEMAS}
    assert public_names <= HARNESS_CONTRACT_TOOL_NAMES


def test_intermediate_output_contracts_are_registered_explicitly() -> None:
    assert normalized_output_fields_for_tool("search_shops") == {"shopId"}
    assert (
        normalized_output_fields_for_tool("search_products")
        == NORMALIZED_RANKING_OUTPUT_FIELDS
    )
    assert (
        RUNTIME_TOOL_CONTRACTS["search_products"].contract_version
        == TWO_STAGE_RANKING_CONTRACT_VERSION
    )
    assert normalized_output_fields_for_tool("get_shop_detail") == frozenset()
    assert normalized_output_fields_for_tool("unknown") == frozenset()


def test_small_talk_is_context_only_not_legacy_fallback() -> None:
    assert _is_harness_contract_covered("你好", _state("你好")) is True


def test_recommendation_contract_uses_unified_harness() -> None:
    message = "根据我的历史给我推荐两家店"
    assert _is_harness_contract_covered(message, _state(message)) is True


def test_transaction_intent_exposes_only_read_only_shopping_tools() -> None:
    decision = route_tool_schemas("购买这个手机并下单")
    names = {schema["function"]["name"] for schema in decision.schemas}
    assert names == {
        "search_products", "get_product_details", "compare_products",
        "rerank_products_in_scope",
    }


def test_unknown_route_is_not_silently_treated_as_context_only() -> None:
    decision = route_tool_schemas("请替我执行一个尚未注册的复杂业务动作")
    assert decision.kind == "unknown"
    assert decision.schemas == ()


def test_validated_evidence_projection_comes_from_contract_registry() -> None:
    assert validated_evidence_fields_for_tool("get_shop_detail") == (
        "id", "name", "type", "address", "images", "averageScore",
        "openHours", "evidenceRefs",
    )
    assert validated_evidence_fields_for_tool("compare_products") == (
        "category", "requirements", "products", "comparisonMatrix",
        "evidence", "evidenceRefs", "hasCompleteMatch", "rankingTrace",
    )
