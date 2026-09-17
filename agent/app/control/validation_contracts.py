"""Single source of truth for Harness tool contracts.

Planner, Executor, Validator and the public runtime router must agree on the
same tool surface.  Keeping separate name lists made it possible to route a
tool into the Harness even though one of the later phases did not understand
its output contract.
"""

from dataclasses import dataclass
from typing import Any

from ..domains.ecommerce.ranking_contract import (
    NORMALIZED_RANKING_OUTPUT_FIELDS,
    TWO_STAGE_RANKING_CONTRACT_VERSION,
    TwoStageRankingContractError,
    normalize_persisted_ranking_values,
)


@dataclass(frozen=True, slots=True)
class RuntimeToolContract:
    """Server-owned contract for one tool allowed in the persisted Harness."""

    expected_outputs: frozenset[str]
    normalized_output_fields: frozenset[str] = frozenset()
    validated_evidence_fields: tuple[str, ...] = ()
    contract_version: str | None = None


RUNTIME_TOOL_CONTRACTS: dict[str, RuntimeToolContract] = {
    "search_shops": RuntimeToolContract(
        frozenset({"requiresShopId"}),
        frozenset({"shopId"}),
    ),
    "get_shop_detail": RuntimeToolContract(
        frozenset({"requiresShopDetail"}),
        validated_evidence_fields=(
            "id", "name", "type", "address", "images", "averageScore",
            "openHours", "evidenceRefs",
        ),
    ),
    "search_shop_reviews": RuntimeToolContract(
        frozenset({"requiresReviewEvidence"}),
        validated_evidence_fields=(
            "count", "reviews", "citations", "evidenceRefs",
        ),
    ),
    "search_knowledge": RuntimeToolContract(
        frozenset({"requiresReviewEvidence"}),
        validated_evidence_fields=(
            "count", "chunks", "citations", "evidenceRefs",
        ),
    ),
    "search_products": RuntimeToolContract(
        frozenset({"requiresProductCandidates"}),
        NORMALIZED_RANKING_OUTPUT_FIELDS,
        (
            "contractVersion", "candidatePoolIds", "rankedItemIds",
            "candidateIds", "candidates", "retrievalTrace", "rankingTrace",
            "citationTrace", "evidence", "evidenceRefs", "eliminated",
        ),
        TWO_STAGE_RANKING_CONTRACT_VERSION,
    ),
    "get_product_details": RuntimeToolContract(
        frozenset({"requiresProductDetails"}),
        frozenset({"productIds"}),
        ("products", "evidence", "evidenceRefs"),
    ),
    "compare_products": RuntimeToolContract(
        frozenset({"requiresGuideDecision", "requiresEvidenceComparison"}),
        frozenset({"productIds"}),
        (
            "category", "requirements", "products", "comparisonMatrix",
            "evidence", "evidenceRefs", "hasCompleteMatch", "rankingTrace",
            "contractVersion", "comparisonMode", "scopeId", "userQuery", "productIds", "candidates", "knowledge", "answerConstraint",
        ),
    ),
    "search_product_evidence": RuntimeToolContract(
        frozenset({"requiresProductEvidence"}), frozenset({"productIds"}),
        ("productIds", "query", "knowledge"),
    ),
    "search_places": RuntimeToolContract(
        frozenset({"requiresPlaceCandidates"}),
        frozenset({"placeId"}),
        ("count", "total", "items", "catalogVersion", "scope", "scopeNotice"),
    ),
    "get_place_detail": RuntimeToolContract(
        frozenset({"requiresPlaceDetail"}),
        validated_evidence_fields=(
            "place", "catalogVersion", "scope", "scopeNotice", "answerConstraint",
        ),
    ),
    "list_shop_types": RuntimeToolContract(
        frozenset({"requiresShopTypes"}),
        validated_evidence_fields=("keyword", "count", "names"),
    ),
    "recommend_shops": RuntimeToolContract(
        frozenset({"requiresShopRecommendations"}),
        validated_evidence_fields=("userId", "count", "shops", "dataNotice"),
    ),
}

# Backward-compatible read-only projection used by existing callers/tests.
EXPECTED_OUTPUT_CONTRACTS_BY_TOOL: dict[str, frozenset[str]] = {
    name: contract.expected_outputs
    for name, contract in RUNTIME_TOOL_CONTRACTS.items()
}


class ExpectedOutputContractError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def expected_output_contracts_for_tool(tool_name: str) -> tuple[str, ...]:
    contract = RUNTIME_TOOL_CONTRACTS.get(tool_name)
    return tuple(sorted(contract.expected_outputs if contract else ()))


def harness_contract_tool_names() -> frozenset[str]:
    """Return exactly the tools with a complete persisted Harness contract."""

    return frozenset(RUNTIME_TOOL_CONTRACTS)


def normalized_output_fields_for_tool(tool_name: str) -> frozenset[str]:
    contract = RUNTIME_TOOL_CONTRACTS.get(tool_name)
    return contract.normalized_output_fields if contract else frozenset()


def validated_evidence_fields_for_tool(tool_name: str) -> tuple[str, ...]:
    contract = RUNTIME_TOOL_CONTRACTS.get(tool_name)
    return contract.validated_evidence_fields if contract else ()


def validate_normalized_output_values(
    tool_name: str,
    values: object,
) -> None:
    """Validate stored step-output values before reuse or validation."""

    contract = RUNTIME_TOOL_CONTRACTS.get(tool_name)
    if contract is None or not contract.normalized_output_fields:
        raise ExpectedOutputContractError(
            "normalized_output_contract_missing",
            f"工具没有注册规范化输出契约：{tool_name}",
        )
    if not isinstance(values, dict) or set(values) != contract.normalized_output_fields:
        raise ExpectedOutputContractError(
            "normalized_output_contract_mismatch",
            f"工具 {tool_name} 的规范化输出字段不匹配",
        )
    if tool_name == "search_products":
        try:
            normalize_persisted_ranking_values(values)
        except TwoStageRankingContractError as exc:
            raise ExpectedOutputContractError(exc.code, str(exc)) from exc


def validate_expected_output_declaration(
    tool_name: str,
    expected_output: dict[str, Any],
) -> None:
    """Reject output fields that the Runtime cannot deterministically validate."""

    contract = RUNTIME_TOOL_CONTRACTS.get(tool_name)
    if contract is None:
        raise ExpectedOutputContractError(
            "unsupported_expected_output",
            f"工具尚未注册Validator输出契约：{tool_name}",
        )
    unsupported = sorted(set(expected_output) - contract.expected_outputs)
    if unsupported:
        raise ExpectedOutputContractError(
            "unsupported_expected_output",
            f"工具 {tool_name} 不支持expectedOutput字段："
            + ", ".join(unsupported),
        )
    invalid_flags = sorted(
        name for name, required in expected_output.items() if required is not True
    )
    if invalid_flags:
        raise ExpectedOutputContractError(
            "invalid_expected_output",
            "expectedOutput当前只接受值为true的必需产出："
            + ", ".join(invalid_flags),
        )
