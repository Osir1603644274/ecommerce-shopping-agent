"""Authoritative two-stage product ranking contract.

The retrieval pool contains only products whose Java/MySQL facts were
successfully resolved.  The ranked list is an ordered subset of that pool.
This module is deliberately free of I/O so production, Executor, Validator,
and public evaluation runners can enforce exactly the same boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping, Sequence

from .used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
    USED_PHONE_ATTRIBUTE_REGISTRY,
    USED_PHONE_ATTRIBUTE_RULESET_VERSION,
    observe_used_phone_attributes,
)
from .synthetic_prices import SYNTHETIC_PRICE_DISCLOSURE_ZH, synthetic_price_value


TWO_STAGE_RANKING_CONTRACT_VERSION = "ecommerce-two-stage-ranking-v2"
MAX_CANDIDATE_POOL_ITEMS = 50
MAX_RANKED_ITEMS = 20
MAX_PRODUCT_PRESENTATION_ITEMS = 3
RANKING_TIE_BREAK = "productId_ascending"
RANKING_FORMULA = (
    "0.55*normalized_recall + 0.30*soft_requirement_match + "
    "0.15*evidence_completeness"
)
NORMALIZED_RANKING_OUTPUT_FIELDS = frozenset(
    {
        "contractVersion", "candidatePoolIds", "rankedItemIds", "productIds",
        "evidenceRefs", "candidateSupport",
    }
)
SCOPE_RERANK_CONTRACT_VERSION = "ecommerce-scope-rerank-v1"
SCOPE_RERANK_INTENTS = ("camera_title_claim", "gaming_title_claim")
SCOPE_RERANK_OUTPUT_FIELDS = frozenset(
    {
        "contractVersion", "scopeId", "inputProductIds", "rankedItemIds",
        "productIds", "rankingSignal", "degraded", "noFullSearch",
        "evidenceRefs", "candidateSupport",
    }
)
_EVIDENCE_REF_PATTERN = re.compile(r"^product:([1-9][0-9]*):.+$")
_PRODUCT_PRESENTATION_FIELDS = frozenset(
    {
        "productId", "title", "brand", "priceMinor", "currency",
        "priceStatus", "selectionType", "titleEvidenceRef",
        "brandEvidenceRef", "priceEvidenceRef", "priceDataNature",
        "pricePolicy", "priceDisclosure", "attributes",
    }
)
_LEGACY_PRODUCT_PRESENTATION_FIELDS = _PRODUCT_PRESENTATION_FIELDS - {
    "priceDataNature", "pricePolicy", "priceDisclosure",
}
_ATTRIBUTE_PRESENTATION_FIELDS = frozenset(
    {"key", "status", "value", "evidenceRef"}
)


class TwoStageRankingContractError(ValueError):
    """A fail-closed violation of the server-owned ranking contract."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class TwoStageRankingOutput:
    candidate_pool_ids: tuple[int, ...]
    ranked_item_ids: tuple[int, ...]
    evidence_refs: tuple[str, ...]
    candidate_support: dict[str, Any]

    def normalized_values(self) -> dict[str, Any]:
        ranked = list(self.ranked_item_ids)
        return {
            "contractVersion": TWO_STAGE_RANKING_CONTRACT_VERSION,
            "candidatePoolIds": list(self.candidate_pool_ids),
            "rankedItemIds": ranked,
            "productIds": list(ranked),
            "evidenceRefs": list(self.evidence_refs),
            "candidateSupport": self.candidate_support,
        }


@dataclass(frozen=True, slots=True)
class ScopeRerankOutput:
    scope_id: str
    input_product_ids: tuple[int, ...]
    ranked_item_ids: tuple[int, ...]
    ranking_signal: str
    degraded: tuple[dict[str, str], ...]
    evidence_refs: tuple[str, ...]
    candidate_support: dict[str, Any]

    def normalized_values(self) -> dict[str, Any]:
        ranked = list(self.ranked_item_ids)
        return {
            "contractVersion": SCOPE_RERANK_CONTRACT_VERSION,
            "scopeId": self.scope_id,
            "inputProductIds": list(self.input_product_ids),
            "rankedItemIds": ranked,
            "productIds": list(ranked),
            "rankingSignal": self.ranking_signal,
            "degraded": list(self.degraded),
            "noFullSearch": True,
            "evidenceRefs": list(self.evidence_refs),
            "candidateSupport": self.candidate_support,
        }


def _candidate_support(
    rows: Sequence[Mapping[str, Any]], ranked_ids: tuple[int, ...],
    *, detail: Mapping[str, Any], requirements: object = None, category: object = None,
) -> dict[str, Any]:
    strict_phone = category in {"手机", "phone"} and isinstance(requirements, list)
    controlled_requirements = (
        [item for item in requirements if isinstance(item, Mapping)
         and item.get("key") in USED_PHONE_ATTRIBUTE_REGISTRY]
        if strict_phone else []
    )
    evidence = detail.get("evidence")
    evidence_by_ref = {
        item["ref"]: item for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("ref"), str)
    } if isinstance(evidence, list) else {}
    fully_supported: list[int] = []
    alternatives: list[int] = []
    unknowns: dict[str, list[str]] = {}
    for row, product_id in zip(rows, ranked_ids, strict=True):
        checks = row.get("checks")
        if not controlled_requirements:
            fully_supported.append(product_id)
            continue
        if not isinstance(checks, list) or len(checks) != len(requirements):
            raise TwoStageRankingContractError(
                "candidate_support_mismatch", "ranked candidate checks are incomplete"
            )
        requirement_identities = [
            tuple(item.get(key) if key != "value" else repr(item.get(key)) for key in (
                "key", "operator", "value", "unit", "priority", "source"
            )) for item in requirements if isinstance(item, Mapping)
        ]
        check_identities = [
            tuple(item.get(key) if key != "expected" else repr(item.get(key)) for key in (
                "key", "operator", "expected", "unit", "priority", "source"
            )) for item in checks if isinstance(item, Mapping)
        ]
        if sorted(requirement_identities, key=repr) != sorted(check_identities, key=repr):
            raise TwoStageRankingContractError(
                "candidate_support_mismatch", "candidate checks do not match resolved requirements"
            )
        hard_unknowns = [
            str(item.get("key"))
            for item in checks
            if isinstance(item, Mapping)
            and item.get("priority") == "hard"
            and item.get("status") == "unknown"
        ]
        hard_failures = any(
            isinstance(item, Mapping)
            and item.get("priority") == "hard"
            and item.get("status") == "fail"
            for item in checks
        )
        if hard_failures:
            raise TwoStageRankingContractError(
                "hard_constraint_violation",
                "ranked candidates cannot contain a known hard-condition failure",
            )
        attributes = row.get("attributes")
        if not isinstance(attributes, list):
            raise TwoStageRankingContractError(
                "candidate_support_mismatch", "phone candidate attributes are missing"
            )
        for check in checks:
            if not isinstance(check, Mapping) or check.get("key") not in USED_PHONE_ATTRIBUTE_REGISTRY:
                continue
            key = str(check["key"])
            matches = [item for item in attributes if isinstance(item, Mapping) and item.get("key") == key]
            if len(matches) > 1:
                raise TwoStageRankingContractError(
                    "controlled_evidence_mismatch", "controlled attribute is duplicated"
                )
            if not matches:
                observed_status, actual, ref = "unknown", None, None
            else:
                attribute = matches[0]
                raw = attribute.get("rawValue")
                if (
                    not isinstance(raw, str)
                    or attribute.get("evidenceField") != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
                    or attribute.get("extractionMethod") != USED_PHONE_ATTRIBUTE_RULESET_VERSION
                ):
                    raise TwoStageRankingContractError(
                        "controlled_evidence_mismatch", "controlled attribute lacks source-bound raw evidence"
                    )
                observation = observe_used_phone_attributes(raw)[key]
                if observation.status == "known" and observation.fact is not None:
                    observed_status = "known"
                    actual = observation.fact.value
                    ref = f"product:{product_id}:attribute:{key}"
                    citation = evidence_by_ref.get(ref)
                    if (
                        attribute.get("normalizedText") != actual
                        or citation is None
                        or citation.get("field") != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
                        or citation.get("method") != USED_PHONE_ATTRIBUTE_RULESET_VERSION
                        or citation.get("rawValue") != raw
                    ):
                        raise TwoStageRankingContractError(
                            "controlled_evidence_mismatch", "controlled fact is not bound to its source evidence"
                        )
                else:
                    observed_status, actual, ref = observation.status, None, None
                    if attribute.get("normalizedText") is not None:
                        raise TwoStageRankingContractError(
                            "controlled_evidence_mismatch", "unknown fact was promoted to a known value"
                        )
            expected_status = "unknown"
            requirement = next(item for item in requirements if item.get("key") == key)
            if observed_status == "known":
                operator, expected = requirement.get("operator"), requirement.get("value")
                passed = (
                    actual == expected if operator == "eq"
                    else actual in expected if operator == "in" and isinstance(expected, list)
                    else actual not in expected if operator == "not_in" and isinstance(expected, list)
                    else False
                )
                expected_status = "pass" if passed else "fail"
            if (
                check.get("status") != expected_status
                or check.get("actual") != actual
                or check.get("evidenceRef") != ref
            ):
                raise TwoStageRankingContractError(
                    "controlled_evidence_mismatch", "candidate support disagrees with controlled raw evidence"
                )
        if hard_unknowns:
            alternatives.append(product_id)
            unknowns[str(product_id)] = hard_unknowns
        else:
            fully_supported.append(product_id)
    support = {
        "hasCompleteMatch": bool(fully_supported),
        "fullySupportedProductIds": fully_supported,
        "closestAlternativeProductIds": alternatives,
        "hardUnknownsByProduct": unknowns,
    }
    support["productPresentations"] = _product_presentations(
        rows,
        ranked_ids,
        detail=detail,
        fully_supported_ids=set(fully_supported),
    )
    return support


def _bound_fact(
    row: Mapping[str, Any],
    evidence_by_ref: Mapping[str, Mapping[str, Any]],
    *,
    product_id: int,
    field: str,
) -> tuple[Any, str | None]:
    """Return a Java fact only when its canonical evidence row binds the value."""

    value = row.get(field)
    ref = f"product:{product_id}:{field}"
    evidence = evidence_by_ref.get(ref)
    if (
        value is None
        or value == ""
        or evidence is None
        or evidence.get("field") != field
        or evidence.get("rawValue") != value
    ):
        return None, None
    return value, ref


def _product_presentations(
    rows: Sequence[Mapping[str, Any]],
    ranked_ids: tuple[int, ...],
    *,
    detail: Mapping[str, Any],
    fully_supported_ids: set[int],
    limit: int = MAX_PRODUCT_PRESENTATION_ITEMS,
) -> list[dict[str, Any]]:
    """Project a small display view from facts already bound to tool evidence.

    The projection is produced during Executor normalization.  It never accepts
    a client-supplied presentation object and it never promotes an unknown or
    conflicting controlled attribute to a fact.
    """

    evidence = detail.get("evidence")
    evidence_by_ref = {
        item["ref"]: item
        for item in evidence
        if isinstance(item, Mapping) and isinstance(item.get("ref"), str)
    } if isinstance(evidence, list) else {}
    if type(limit) is not int or not 1 <= limit <= MAX_RANKED_ITEMS:
        raise TwoStageRankingContractError(
            "product_presentation_limit_invalid",
            "product presentation limit is outside the ranked-item boundary",
        )
    presentations: list[dict[str, Any]] = []
    for row, product_id in list(zip(rows, ranked_ids, strict=True))[:limit]:
        title, title_ref = _bound_fact(
            row, evidence_by_ref, product_id=product_id, field="title"
        )
        brand, brand_ref = _bound_fact(
            row, evidence_by_ref, product_id=product_id, field="brand"
        )
        price_minor: int | None = None
        price_ref: str | None = None
        price_status = "unverified"
        price_data_nature: str | None = None
        price_policy: str | None = None
        price_disclosure: str | None = None
        if row.get("priceStatus") == "verified":
            value, ref = _bound_fact(
                row,
                evidence_by_ref,
                product_id=product_id,
                field="snapshotPriceMinor",
            )
            if type(value) is int and value >= 0 and ref is not None:
                price_minor, price_ref, price_status = value, ref, "verified"
                price_data_nature = str(row.get("dataNature") or "historical_dataset_snapshot")
                price_policy = "verified_snapshot_only"
        else:
            value, synthetic = synthetic_price_value(row, allow_budget=False)
            candidate_ref = f"product:{product_id}:syntheticReferencePriceMinor"
            citation = evidence_by_ref.get(candidate_ref)
            if (
                value is not None
                and synthetic is not None
                and citation is not None
                and citation.get("field") == "syntheticReferencePriceMinor"
                and citation.get("rawValue") == value
                and citation.get("priceStatus") == "synthetic"
                and citation.get("dataNature") == "synthetic"
                and citation.get("pricePolicy") == synthetic["policy"]
                and citation.get("rulesetSha256") == synthetic["rulesetSha256"]
                and citation.get("sourceCatalogSha256") == synthetic["sourceCatalogSha256"]
            ):
                price_minor, price_ref, price_status = value, candidate_ref, "synthetic"
                price_data_nature = "synthetic"
                price_policy = synthetic["policy"]
                price_disclosure = SYNTHETIC_PRICE_DISCLOSURE_ZH

        attributes: list[dict[str, Any]] = []
        raw_attributes = row.get("attributes")
        typed_attributes = raw_attributes if isinstance(raw_attributes, list) else []
        for key in USED_PHONE_ATTRIBUTE_REGISTRY:
            matches = [
                item for item in typed_attributes
                if isinstance(item, Mapping) and item.get("key") == key
            ]
            status, value, ref = "unknown", None, None
            if len(matches) == 1:
                attribute = matches[0]
                raw = attribute.get("rawValue")
                observation = (
                    observe_used_phone_attributes(raw)[key]
                    if isinstance(raw, str)
                    else None
                )
                if observation is not None and observation.status == "conflict":
                    status = "conflict"
                elif (
                    observation is not None
                    and observation.status == "known"
                    and observation.fact is not None
                    and attribute.get("normalizedText") == observation.fact.value
                    and attribute.get("evidenceField")
                    == USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
                    and attribute.get("extractionMethod")
                    == USED_PHONE_ATTRIBUTE_RULESET_VERSION
                ):
                    candidate_ref = f"product:{product_id}:attribute:{key}"
                    citation = evidence_by_ref.get(candidate_ref)
                    if (
                        citation is not None
                        and citation.get("field") == USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
                        and citation.get("method") == USED_PHONE_ATTRIBUTE_RULESET_VERSION
                        and citation.get("rawValue") == raw
                    ):
                        status = "known"
                        value = observation.fact.value
                        ref = candidate_ref
            attributes.append(
                {"key": key, "status": status, "value": value, "evidenceRef": ref}
            )

        currency = row.get("currency")
        if price_status == "synthetic":
            currency = "CNY"
        if not isinstance(currency, str) or not currency.strip():
            currency = None
        presentations.append({
            "productId": product_id,
            "title": title if isinstance(title, str) else None,
            "brand": brand if isinstance(brand, str) else None,
            "priceMinor": price_minor,
            "currency": currency,
            "priceStatus": price_status,
            "priceDataNature": price_data_nature,
            "pricePolicy": price_policy,
            "priceDisclosure": price_disclosure,
            "selectionType": (
                "full_match" if product_id in fully_supported_ids
                else "closest_alternative"
            ),
            "titleEvidenceRef": title_ref,
            "brandEvidenceRef": brand_ref,
            "priceEvidenceRef": price_ref,
            "attributes": attributes,
        })
    return presentations


def _validated_product_presentations(
    value: object,
    ranked_ids: tuple[int, ...],
    *,
    fully_supported_ids: set[int],
) -> list[dict[str, Any]]:
    """Validate the compact persisted presentation projection fail closed."""

    if value is None:
        # Backward compatibility for already-persisted v2 ranking outputs.
        return []
    expected_ids = list(ranked_ids[:MAX_PRODUCT_PRESENTATION_ITEMS])
    if not isinstance(value, list) or len(value) != len(expected_ids):
        raise TwoStageRankingContractError(
            "product_presentation_mismatch",
            "productPresentations must cover the first ranked items exactly",
        )
    validated: list[dict[str, Any]] = []
    for expected_id, item in zip(expected_ids, value, strict=True):
        if not isinstance(item, Mapping) or frozenset(item) not in {
            _PRODUCT_PRESENTATION_FIELDS, _LEGACY_PRODUCT_PRESENTATION_FIELDS,
        }:
            raise TwoStageRankingContractError(
                "product_presentation_mismatch",
                "product presentation fields do not match the registered contract",
            )
        legacy_price_metadata = frozenset(item) == _LEGACY_PRODUCT_PRESENTATION_FIELDS
        if item.get("productId") != expected_id:
            raise TwoStageRankingContractError(
                "product_presentation_identity_mismatch",
                "product presentation IDs must follow ranked item order",
            )
        title, brand, currency = (
            item.get("title"), item.get("brand"), item.get("currency")
        )
        if (
            (title is not None and (not isinstance(title, str) or not title or len(title) > 500))
            or (brand is not None and (not isinstance(brand, str) or not brand or len(brand) > 100))
            or (currency is not None and (not isinstance(currency, str) or not currency or len(currency) > 16))
        ):
            raise TwoStageRankingContractError(
                "product_presentation_value_invalid",
                "product presentation text fields are invalid",
            )
        expected_selection = (
            "full_match" if expected_id in fully_supported_ids else "closest_alternative"
        )
        if item.get("selectionType") != expected_selection:
            raise TwoStageRankingContractError(
                "product_presentation_support_mismatch",
                "product presentation selection type disagrees with candidate support",
            )
        price_status = item.get("priceStatus")
        price_minor = item.get("priceMinor")
        price_ref = item.get("priceEvidenceRef")
        if price_status == "verified":
            if type(price_minor) is not int or price_minor < 0 or price_ref != (
                f"product:{expected_id}:snapshotPriceMinor"
            ):
                raise TwoStageRankingContractError(
                    "product_presentation_price_invalid",
                    "verified presentation price lacks its canonical evidence binding",
                )
            if not legacy_price_metadata and (
                item.get("priceDataNature") in (None, "synthetic")
                or item.get("pricePolicy") != "verified_snapshot_only"
                or item.get("priceDisclosure") is not None
            ):
                raise TwoStageRankingContractError(
                    "product_presentation_price_invalid",
                    "verified price metadata is invalid",
                )
        elif price_status == "synthetic":
            if (
                type(price_minor) is not int
                or price_minor < 0
                or price_ref != f"product:{expected_id}:syntheticReferencePriceMinor"
                or item.get("priceDataNature") != "synthetic"
                or item.get("pricePolicy") not in {"display_only", "budget_and_ranking"}
                or item.get("priceDisclosure") != SYNTHETIC_PRICE_DISCLOSURE_ZH
            ):
                raise TwoStageRankingContractError(
                    "product_presentation_price_invalid",
                    "synthetic reference price lacks its explicit policy/provenance",
                )
        elif price_status == "unverified":
            if (
                price_minor is not None
                or price_ref is not None
                or item.get("priceDataNature") is not None
                or item.get("pricePolicy") is not None
                or item.get("priceDisclosure") is not None
            ):
                raise TwoStageRankingContractError(
                    "product_presentation_price_invalid",
                    "unverified presentation price must remain unknown",
                )
        else:
            raise TwoStageRankingContractError(
                "product_presentation_price_invalid",
                "unknown presentation price status",
            )
        for field, suffix in (
            ("titleEvidenceRef", "title"), ("brandEvidenceRef", "brand")
        ):
            ref = item.get(field)
            paired_value = title if field == "titleEvidenceRef" else brand
            if (ref is None) is not (paired_value is None) or (
                ref is not None and ref != f"product:{expected_id}:{suffix}"
            ):
                raise TwoStageRankingContractError(
                    "product_presentation_evidence_mismatch",
                    "presentation facts must use canonical evidence references",
                )
        attributes = item.get("attributes")
        if not isinstance(attributes, list) or len(attributes) != len(
            USED_PHONE_ATTRIBUTE_REGISTRY
        ):
            raise TwoStageRankingContractError(
                "product_presentation_attributes_invalid",
                "presentation must describe each controlled attribute exactly once",
            )
        validated_attributes: list[dict[str, Any]] = []
        for expected_key, attribute in zip(
            USED_PHONE_ATTRIBUTE_REGISTRY, attributes, strict=True
        ):
            if (
                not isinstance(attribute, Mapping)
                or set(attribute) != _ATTRIBUTE_PRESENTATION_FIELDS
                or attribute.get("key") != expected_key
            ):
                raise TwoStageRankingContractError(
                    "product_presentation_attributes_invalid",
                    "presentation attribute keys or fields are invalid",
                )
            status = attribute.get("status")
            actual = attribute.get("value")
            ref = attribute.get("evidenceRef")
            if status == "known":
                if (
                    actual not in USED_PHONE_ATTRIBUTE_REGISTRY[expected_key].allowed_values
                    or ref != f"product:{expected_id}:attribute:{expected_key}"
                ):
                    raise TwoStageRankingContractError(
                        "product_presentation_attribute_evidence_mismatch",
                        "known presentation attribute lacks canonical evidence",
                    )
            elif status in {"unknown", "conflict"}:
                if actual is not None or ref is not None:
                    raise TwoStageRankingContractError(
                        "product_presentation_attribute_evidence_mismatch",
                        "unknown or conflicting attribute was promoted to a fact",
                    )
            else:
                raise TwoStageRankingContractError(
                    "product_presentation_attributes_invalid",
                    "presentation attribute status is invalid",
                )
            validated_attributes.append(dict(attribute))
        normalized_item = dict(item)
        if legacy_price_metadata:
            normalized_item.update({
                "priceDataNature": None,
                "pricePolicy": None,
                "priceDisclosure": None,
            })
        validated.append({**normalized_item, "attributes": validated_attributes})
    return validated


def _validated_candidate_support(
    value: object, ranked_ids: tuple[int, ...]
) -> dict[str, Any]:
    base_fields = {
        "hasCompleteMatch", "fullySupportedProductIds",
        "closestAlternativeProductIds", "hardUnknownsByProduct",
    }
    if not isinstance(value, Mapping) or frozenset(value) not in {
        frozenset(base_fields), frozenset({*base_fields, "productPresentations"})
    }:
        raise TwoStageRankingContractError(
            "candidate_support_mismatch", "candidateSupport fields are invalid"
        )
    fully = _id_list(
        value.get("fullySupportedProductIds"), field="fullySupportedProductIds",
        maximum=MAX_RANKED_ITEMS, allow_empty=True,
    )
    alternatives = _id_list(
        value.get("closestAlternativeProductIds"), field="closestAlternativeProductIds",
        maximum=MAX_RANKED_ITEMS, allow_empty=True,
    )
    unknowns = value.get("hardUnknownsByProduct")
    if (
        type(value.get("hasCompleteMatch")) is not bool
        or value.get("hasCompleteMatch") is not bool(fully)
        or list(fully) + list(alternatives) != list(ranked_ids)
        or not isinstance(unknowns, Mapping)
        or set(unknowns) != {str(item) for item in alternatives}
        or any(
            not isinstance(groups, list) or not groups
            or not all(isinstance(group, str) and group for group in groups)
            or len(groups) != len(set(groups))
            for groups in unknowns.values()
        )
    ):
        raise TwoStageRankingContractError(
            "candidate_support_mismatch",
            "candidateSupport does not partition ranked items by hard-condition support",
        )
    result = {
        "hasCompleteMatch": bool(fully),
        "fullySupportedProductIds": list(fully),
        "closestAlternativeProductIds": list(alternatives),
        "hardUnknownsByProduct": {str(k): list(v) for k, v in unknowns.items()},
    }
    if "productPresentations" in value:
        result["productPresentations"] = _validated_product_presentations(
            value.get("productPresentations"),
            ranked_ids,
            fully_supported_ids=set(fully),
        )
    return result


def _id_list(
    value: object,
    *,
    field: str,
    maximum: int,
    allow_empty: bool = False,
) -> tuple[int, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise TwoStageRankingContractError(
            "ranking_ids_missing",
            f"{field} must be a non-empty list",
        )
    if len(value) > maximum:
        raise TwoStageRankingContractError(
            "ranking_ids_over_limit",
            f"{field} exceeds its maximum of {maximum}",
        )
    if not all(type(item) is int and item > 0 for item in value):
        raise TwoStageRankingContractError(
            "invalid_ranking_id",
            f"{field} must contain only positive integer IDs",
        )
    if len(value) != len(set(value)):
        raise TwoStageRankingContractError(
            "duplicate_ranking_id",
            f"{field} must not contain duplicate IDs",
        )
    return tuple(value)


def _require_version(payload: Mapping[str, Any]) -> None:
    version = payload.get("contractVersion")
    if version != TWO_STAGE_RANKING_CONTRACT_VERSION:
        raise TwoStageRankingContractError(
            "unsupported_ranking_contract_version",
            "missing or unknown two-stage ranking contractVersion",
        )


def _validate_candidate_rows(
    rows: object,
    ranked_ids: tuple[int, ...],
) -> list[Mapping[str, Any]]:
    if not isinstance(rows, list) or len(rows) != len(ranked_ids):
        raise TwoStageRankingContractError(
            "candidate_row_order_mismatch",
            "candidates must contain exactly one row for each rankedItemId",
        )
    row_ids: list[int] = []
    typed_rows: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise TwoStageRankingContractError(
                "candidate_row_order_mismatch",
                "every candidate row must be an object",
            )
        row_id = row.get("id")
        if type(row_id) is not int or row_id <= 0:
            raise TwoStageRankingContractError(
                "candidate_row_order_mismatch",
                "every candidate row must expose a positive integer id",
            )
        row_ids.append(row_id)
        typed_rows.append(row)
    if tuple(row_ids) != ranked_ids:
        raise TwoStageRankingContractError(
            "candidate_row_order_mismatch",
            "candidate row IDs must exactly equal rankedItemIds in order",
        )
    return typed_rows


def _evidence_product_id(ref: object) -> int:
    match = _EVIDENCE_REF_PATTERN.fullmatch(ref) if isinstance(ref, str) else None
    if match is None:
        raise TwoStageRankingContractError(
            "invalid_evidence_reference",
            "ranking evidence must use a canonical product:<id>:... reference",
        )
    return int(match.group(1))


def _validate_evidence(
    detail: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    ranked_ids: tuple[int, ...],
) -> tuple[str, ...]:
    evidence = detail.get("evidence")
    evidence_refs = detail.get("evidenceRefs")
    if not isinstance(evidence, list) or not isinstance(evidence_refs, list):
        raise TwoStageRankingContractError(
            "ranking_evidence_missing",
            "evidence and evidenceRefs must both be lists",
        )
    refs: list[str] = []
    evidence_by_ref: dict[str, Mapping[str, Any]] = {}
    ranked_set = set(ranked_ids)
    for item in evidence:
        if not isinstance(item, Mapping):
            raise TwoStageRankingContractError(
                "invalid_ranking_evidence", "every evidence row must be an object"
            )
        ref = item.get("ref")
        product_id = _evidence_product_id(ref)
        if product_id not in ranked_set:
            raise TwoStageRankingContractError(
                "citation_outside_ranked_items",
                "evidence can only cite a product in rankedItemIds",
            )
        assert isinstance(ref, str)
        if ref in evidence_by_ref:
            raise TwoStageRankingContractError(
                "duplicate_evidence_reference", "evidence refs must be unique"
            )
        refs.append(ref)
        evidence_by_ref[ref] = item
    if evidence_refs != refs:
        raise TwoStageRankingContractError(
            "evidence_alias_mismatch",
            "evidenceRefs must exactly equal evidence refs in order",
        )

    row_ref_order: list[str] = []
    citation_refs: list[str] = []
    for row, ranked_id in zip(rows, ranked_ids, strict=True):
        row_refs = row.get("evidenceRefs")
        if (
            not isinstance(row_refs, list)
            or not row_refs
            or len(row_refs) != len(set(row_refs))
        ):
            raise TwoStageRankingContractError(
                "invalid_candidate_evidence_refs",
                "every ranked candidate must have a non-empty unique evidenceRefs list",
            )
        citation_refs.append(row_refs[0])
        for ref in row_refs:
            if _evidence_product_id(ref) != ranked_id or ref not in evidence_by_ref:
                raise TwoStageRankingContractError(
                    "candidate_evidence_mismatch",
                    "candidate evidence must be canonical and belong to that ranked row",
                )
            if ref not in row_ref_order:
                row_ref_order.append(ref)
    if row_ref_order != refs:
        raise TwoStageRankingContractError(
            "candidate_evidence_mismatch",
            "top-level evidence must exactly equal ranked candidate evidence",
        )
    return tuple(citation_refs)


def _validate_traces(
    detail: Mapping[str, Any],
    candidate_pool_ids: tuple[int, ...],
    ranked_item_ids: tuple[int, ...],
) -> None:
    retrieval = detail.get("retrievalTrace")
    ranking = detail.get("rankingTrace")
    citation = detail.get("citationTrace")
    if not all(isinstance(item, Mapping) for item in (retrieval, ranking, citation)):
        raise TwoStageRankingContractError(
            "ranking_trace_missing",
            "retrievalTrace, rankingTrace, and citationTrace are required",
        )
    assert isinstance(retrieval, Mapping)
    assert isinstance(ranking, Mapping)
    assert isinstance(citation, Mapping)
    if (
        retrieval.get("contractVersion") != TWO_STAGE_RANKING_CONTRACT_VERSION
        or retrieval.get("candidatePoolCount") != len(candidate_pool_ids)
        or retrieval.get("authoritativeFactCount") != len(candidate_pool_ids)
    ):
        raise TwoStageRankingContractError(
            "retrieval_trace_mismatch",
            "retrievalTrace does not describe the authoritative candidate pool",
        )
    if (
        ranking.get("contractVersion") != TWO_STAGE_RANKING_CONTRACT_VERSION
        or ranking.get("inputCandidateCount") != len(candidate_pool_ids)
        or ranking.get("rankedItemCount") != len(ranked_item_ids)
        or ranking.get("tieBreak") != RANKING_TIE_BREAK
        or ranking.get("formula") != RANKING_FORMULA
    ):
        raise TwoStageRankingContractError(
            "ranking_trace_mismatch",
            "rankingTrace does not describe the deterministic reranker output",
        )
    if (
        citation.get("contractVersion") != TWO_STAGE_RANKING_CONTRACT_VERSION
        or citation.get("sourceTool") != "search_products"
        or citation.get("rankedItemIds") != list(ranked_item_ids)
        or citation.get("evidenceRefCount") != len(detail.get("evidenceRefs", []))
        or citation.get("binding") != "current_successful_tool_call_ranked_items_only"
    ):
        raise TwoStageRankingContractError(
            "citation_trace_mismatch",
            "citationTrace is not bound to this ranked result",
        )


def normalize_search_products_detail(
    detail: object, *, requirements: object = None, category: object = None,
) -> TwoStageRankingOutput:
    """Validate a successful search_products detail payload."""

    if not isinstance(detail, Mapping):
        raise TwoStageRankingContractError(
            "invalid_ranking_detail", "search_products detail must be an object"
        )
    _require_version(detail)
    pool = _id_list(
        detail.get("candidatePoolIds"),
        field="candidatePoolIds",
        maximum=MAX_CANDIDATE_POOL_ITEMS,
    )
    ranked = _id_list(
        detail.get("rankedItemIds"),
        field="rankedItemIds",
        maximum=MAX_RANKED_ITEMS,
        allow_empty=True,
    )
    if not set(ranked).issubset(pool):
        raise TwoStageRankingContractError(
            "ranked_item_outside_candidate_pool",
            "rankedItemIds must be an ordered subset of candidatePoolIds",
        )
    if detail.get("candidateIds") != list(ranked):
        raise TwoStageRankingContractError(
            "candidate_alias_mismatch",
            "candidateIds must exactly equal rankedItemIds",
        )
    rows = _validate_candidate_rows(detail.get("candidates"), ranked)
    citation_refs = _validate_evidence(detail, rows, ranked)
    _validate_traces(detail, pool, ranked)
    return TwoStageRankingOutput(
        pool, ranked, citation_refs,
        _candidate_support(
            rows, ranked, detail=detail, requirements=requirements, category=category,
        ),
    )


def project_validated_search_product_presentations(
    detail: object,
    *,
    requirements: object = None,
    category: object = None,
    limit: int = MAX_RANKED_ITEMS,
) -> tuple[TwoStageRankingOutput, list[dict[str, Any]]]:
    """Return an expanded UI projection after revalidating the raw tool detail.

    The persisted ranking/Validator contract remains compact (Top 3).  This
    projection is built only for the browser response and therefore does not
    enlarge FinalAnswerContextView or duplicate the candidate pool in model
    context.
    """

    normalized = normalize_search_products_detail(
        detail,
        requirements=requirements,
        category=category,
    )
    assert isinstance(detail, Mapping)
    rows = _validate_candidate_rows(
        detail.get("candidates"), normalized.ranked_item_ids
    )
    fully_supported = normalized.candidate_support.get(
        "fullySupportedProductIds", []
    )
    if not isinstance(fully_supported, list):
        raise TwoStageRankingContractError(
            "candidate_support_mismatch",
            "fully supported product identities must be a list",
        )
    presentations = _product_presentations(
        rows,
        normalized.ranked_item_ids,
        detail=detail,
        fully_supported_ids=set(fully_supported),
        limit=min(limit, len(normalized.ranked_item_ids)),
    ) if normalized.ranked_item_ids else []
    return normalized, presentations


def normalize_persisted_ranking_values(
    values: object,
) -> TwoStageRankingOutput:
    """Validate the compact contract persisted in TaskState.stepOutputs."""

    if not isinstance(values, Mapping) or set(values) != NORMALIZED_RANKING_OUTPUT_FIELDS:
        raise TwoStageRankingContractError(
            "normalized_ranking_fields_mismatch",
            "normalized ranking output fields do not match the registered contract",
        )
    _require_version(values)
    pool = _id_list(
        values.get("candidatePoolIds"),
        field="candidatePoolIds",
        maximum=MAX_CANDIDATE_POOL_ITEMS,
    )
    ranked = _id_list(
        values.get("rankedItemIds"),
        field="rankedItemIds",
        maximum=MAX_RANKED_ITEMS,
        allow_empty=True,
    )
    if not set(ranked).issubset(pool):
        raise TwoStageRankingContractError(
            "ranked_item_outside_candidate_pool",
            "rankedItemIds must be an ordered subset of candidatePoolIds",
        )
    if values.get("productIds") != list(ranked):
        raise TwoStageRankingContractError(
            "product_ids_alias_mismatch",
            "productIds must exactly equal rankedItemIds",
        )
    evidence_refs = values.get("evidenceRefs")
    if (
        not isinstance(evidence_refs, list)
        or len(evidence_refs) != len(ranked)
        or len(evidence_refs) != len(set(evidence_refs))
    ):
        raise TwoStageRankingContractError(
            "normalized_evidence_refs_mismatch",
            "evidenceRefs must contain exactly one unique citation per ranked item",
        )
    for ranked_id, ref in zip(ranked, evidence_refs, strict=True):
        if _evidence_product_id(ref) != ranked_id:
            raise TwoStageRankingContractError(
                "normalized_evidence_refs_mismatch",
                "each normalized citation must belong to its ranked item",
            )
    support = _validated_candidate_support(values.get("candidateSupport"), ranked)
    return TwoStageRankingOutput(pool, ranked, tuple(evidence_refs), support)


def _validate_scope_rerank_common(
    scope_id: object,
    input_ids: tuple[int, ...],
    ranked: tuple[int, ...],
    product_ids: object,
    no_full_search: object,
    ranking_signal: object,
    degraded: object,
) -> None:
    if not isinstance(scope_id, str) or not scope_id.strip():
        raise TwoStageRankingContractError(
            "scope_rerank_identity_missing",
            "scopeId must be a non-empty string",
        )
    if not set(ranked).issubset(input_ids):
        raise TwoStageRankingContractError(
            "scope_rerank_outside_input",
            "rankedItemIds must be an ordered subset of inputProductIds",
        )
    if product_ids != list(ranked):
        raise TwoStageRankingContractError(
            "scope_rerank_product_alias_mismatch",
            "productIds must exactly equal rankedItemIds",
        )
    if no_full_search is not True:
        raise TwoStageRankingContractError(
            "scope_rerank_full_search_forbidden",
            "scope rerank must never run a full catalog search",
        )
    if not isinstance(ranking_signal, str) or not ranking_signal.strip():
        raise TwoStageRankingContractError(
            "scope_rerank_signal_missing",
            "rankingSignal must be a non-empty string",
        )
    if (
        not isinstance(degraded, list)
        or any(not isinstance(item, dict) for item in degraded)
    ):
        raise TwoStageRankingContractError(
            "scope_rerank_degraded_invalid",
            "degraded must be a list of objects",
        )


def _validate_scope_rerank_evidence_refs(
    evidence_refs: object,
    ranked: tuple[int, ...],
) -> tuple[str, ...]:
    if (
        not isinstance(evidence_refs, list)
        or len(evidence_refs) != len(ranked)
        or len(evidence_refs) != len(set(evidence_refs))
    ):
        raise TwoStageRankingContractError(
            "normalized_scope_rerank_evidence_refs_mismatch",
            "evidenceRefs must contain exactly one unique citation per ranked item",
        )
    for ranked_id, ref in zip(ranked, evidence_refs, strict=True):
        if _evidence_product_id(ref) != ranked_id:
            raise TwoStageRankingContractError(
                "normalized_scope_rerank_evidence_refs_mismatch",
                "each normalized citation must belong to its ranked item",
            )
    return tuple(evidence_refs)


def normalize_scope_rerank_detail(
    detail: object, *, requirements: object = None, category: object = None,
) -> ScopeRerankOutput:
    """Validate a successful rerank_products_in_scope detail payload.

    The rerank contract re-runs the existing hard requirement gate over the
    scope's ranked candidates only; it must never touch the full catalog and its
    citationTrace must bind to ``rerank_products_in_scope`` rather than a new
    ``search_products``.
    """

    if not isinstance(detail, Mapping):
        raise TwoStageRankingContractError(
            "invalid_scope_rerank_detail",
            "rerank_products_in_scope detail must be an object",
        )
    version = detail.get("contractVersion")
    if version != SCOPE_RERANK_CONTRACT_VERSION:
        raise TwoStageRankingContractError(
            "unsupported_scope_rerank_contract_version",
            "missing or unknown scope-rerank contractVersion",
        )
    input_ids = _id_list(
        detail.get("inputProductIds"),
        field="inputProductIds",
        maximum=MAX_RANKED_ITEMS,
    )
    ranked = _id_list(
        detail.get("rankedItemIds"),
        field="rankedItemIds",
        maximum=MAX_RANKED_ITEMS,
    )
    _validate_scope_rerank_common(
        detail.get("scopeId"),
        input_ids,
        ranked,
        detail.get("productIds"),
        detail.get("noFullSearch"),
        detail.get("rankingSignal"),
        detail.get("degraded"),
    )
    assert isinstance(detail.get("scopeId"), str)
    assert isinstance(detail.get("rankingSignal"), str)
    rows = _validate_candidate_rows(detail.get("candidates"), ranked)
    citation_refs = _validate_evidence(detail, rows, ranked)
    citation = detail.get("citationTrace")
    if (
        not isinstance(citation, Mapping)
        or citation.get("contractVersion") != SCOPE_RERANK_CONTRACT_VERSION
        or citation.get("sourceTool") != "rerank_products_in_scope"
        or citation.get("rankedItemIds") != list(ranked)
        or citation.get("evidenceRefCount") != len(detail.get("evidenceRefs", []))
        or citation.get("binding") != "current_successful_scope_rerank_only"
    ):
        raise TwoStageRankingContractError(
            "scope_rerank_citation_trace_mismatch",
            "citationTrace is not bound to this in-scope rerank result",
        )
    return ScopeRerankOutput(
        detail["scopeId"],
        input_ids,
        ranked,
        detail["rankingSignal"],
        tuple(detail.get("degraded", [])),
        citation_refs,
        _candidate_support(
            rows, ranked, detail=detail, requirements=requirements, category=category,
        ),
    )


def normalize_persisted_scope_rerank_values(
    values: object,
) -> ScopeRerankOutput:
    """Validate the compact scope-rerank contract persisted in TaskState.stepOutputs."""

    if not isinstance(values, Mapping) or set(values) != SCOPE_RERANK_OUTPUT_FIELDS:
        raise TwoStageRankingContractError(
            "normalized_scope_rerank_fields_mismatch",
            "normalized scope-rerank output fields do not match the registered contract",
        )
    version = values.get("contractVersion")
    if version != SCOPE_RERANK_CONTRACT_VERSION:
        raise TwoStageRankingContractError(
            "unsupported_scope_rerank_contract_version",
            "missing or unknown scope-rerank contractVersion",
        )
    input_ids = _id_list(
        values.get("inputProductIds"),
        field="inputProductIds",
        maximum=MAX_RANKED_ITEMS,
    )
    ranked = _id_list(
        values.get("rankedItemIds"),
        field="rankedItemIds",
        maximum=MAX_RANKED_ITEMS,
    )
    _validate_scope_rerank_common(
        values.get("scopeId"),
        input_ids,
        ranked,
        values.get("productIds"),
        values.get("noFullSearch"),
        values.get("rankingSignal"),
        values.get("degraded"),
    )
    assert isinstance(values.get("scopeId"), str)
    assert isinstance(values.get("rankingSignal"), str)
    evidence_refs = _validate_scope_rerank_evidence_refs(
        values.get("evidenceRefs"),
        ranked,
    )
    support = _validated_candidate_support(values.get("candidateSupport"), ranked)
    return ScopeRerankOutput(
        values["scopeId"],
        input_ids,
        ranked,
        values["rankingSignal"],
        tuple(values.get("degraded", [])),
        evidence_refs,
        support,
    )
