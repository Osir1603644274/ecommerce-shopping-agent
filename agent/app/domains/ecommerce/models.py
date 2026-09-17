"""Typed state and deterministic comparison logic for the ecommerce domain."""

import math
import re
from collections import Counter
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from .used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
    USED_PHONE_ATTRIBUTE_REGISTRY,
    USED_PHONE_ATTRIBUTE_RULESET_VERSION,
    observe_used_phone_attributes,
)
from .synthetic_prices import synthetic_price_value

ProductCategory = Literal["phone", "laptop", "headphones"]
RequirementPriority = Literal["hard", "soft"]
RequirementOperator = Literal["eq", "lte", "gte", "in", "not_in"]

CATEGORY_LABELS = {"phone": "手机", "laptop": "笔记本", "headphones": "耳机"}
CATEGORY_ALIASES = {
    "phone": (
        "手机", "智能机", "5g手机", "二手机", "二手手机",
        "iphone", "苹果手机", "安卓机",
    ),
    "laptop": ("笔记本", "轻薄本", "游戏本"),
    "headphones": ("耳机", "耳麦", "降噪耳机"),
}
SPEC_REGISTRY: dict[str, dict[str, tuple[str, str, tuple[str, ...]]]] = {
    "phone": {
        "brand": ("text", "text", ("eq", "in", "not_in")),
        "price_minor": ("number", "CNY_MINOR", ("lte", "gte", "eq")),
        "memory_gb": ("number", "GB", ("lte", "gte", "eq")),
        "storage_gb": ("number", "GB", ("lte", "gte", "eq")),
        "battery_mah": ("number", "mAh", ("lte", "gte", "eq")),
        "supports_5g": ("boolean", "bool", ("eq",)),
    },
    "laptop": {
        "brand": ("text", "text", ("eq", "in", "not_in")),
        "price_minor": ("number", "CNY_MINOR", ("lte", "gte", "eq")),
        "memory_gb": ("number", "GB", ("lte", "gte", "eq")),
        "storage_gb": ("number", "GB", ("lte", "gte", "eq")),
        "weight_kg": ("number", "kg", ("lte", "gte", "eq")),
        "screen_inch": ("number", "inch", ("lte", "gte", "eq")),
    },
    "headphones": {
        "brand": ("text", "text", ("eq", "in", "not_in")),
        "price_minor": ("number", "CNY_MINOR", ("lte", "gte", "eq")),
        "battery_hours": ("number", "hour", ("lte", "gte", "eq")),
        "noise_cancelling": ("boolean", "bool", ("eq",)),
        "wireless": ("boolean", "bool", ("eq",)),
        "weight_g": ("number", "g", ("lte", "gte", "eq")),
    },
}
for _key, _spec in USED_PHONE_ATTRIBUTE_REGISTRY.items():
    SPEC_REGISTRY["phone"][_key] = (
        _spec.type,
        _spec.unit,
        _spec.operators,
    )

# Immutable evidence/requirement key closure shared by producers and
# downstream validators.  SPEC_REGISTRY remains a compatibility mapping, but
# later caller mutation cannot expand the accepted canonical key universe.
CANONICAL_ECOMMERCE_ATTRIBUTE_CODES: frozenset[str] = frozenset(
    key for category_specs in SPEC_REGISTRY.values() for key in category_specs
)

ENUM_VALUE_REGISTRY: dict[str, frozenset[str]] = {
    key: frozenset(spec.allowed_values)
    for key, spec in USED_PHONE_ATTRIBUTE_REGISTRY.items()
}

_BRAND_CANONICAL_ALIASES: dict[str, frozenset[str]] = {
    "apple": frozenset({"apple", "iphone", "苹果"}),
    "samsung": frozenset({"samsung", "三星"}),
    "huawei": frozenset({"huawei", "华为"}),
    "honor": frozenset({"honor", "荣耀"}),
    "xiaomi": frozenset({"xiaomi", "mi", "小米"}),
    "redmi": frozenset({"redmi", "hongmi", "红米"}),
    "oppo": frozenset({"oppo"}),
    "vivo": frozenset({"vivo"}),
    "iqoo": frozenset({"iqoo"}),
    "oneplus": frozenset({"oneplus", "一加"}),
    "realme": frozenset({"realme", "真我"}),
    "nubia": frozenset({"nubia", "努比亚"}),
    "blackshark": frozenset({"blackshark", "黑鲨"}),
    "hi": frozenset({"hi", "畅享"}),
}

DOMESTIC_PHONE_BRANDS: tuple[str, ...] = (
    "huawei", "honor", "xiaomi", "redmi", "oppo", "vivo", "iqoo",
    "oneplus", "realme", "nubia", "blackshark", "hi",
)


def canonicalize_brand(value: str) -> str:
    """Map a display brand or user alias to one comparison identity."""

    normalized = re.sub(r"\s+", "", value).casefold()
    tokens = {
        token
        for token in re.split(r"[/|,，;；]+", normalized)
        if token
    }
    tokens.add(normalized)
    for canonical, aliases in _BRAND_CANONICAL_ALIASES.items():
        if tokens & aliases:
            return canonical
    return normalized


def product_brand_aliases(canonical: str) -> tuple[str, ...]:
    """Return stable aliases for one canonical brand identity."""

    return tuple(sorted(
        _BRAND_CANONICAL_ALIASES.get(canonicalize_brand(canonical), frozenset()),
        key=lambda value: (-len(value), value),
    ))


def detect_product_brands(text: str) -> list[str]:
    """Return unambiguous canonical brands explicitly named in user text."""

    normalized = text.casefold()
    found: list[str] = []
    for canonical, aliases in _BRAND_CANONICAL_ALIASES.items():
        matched = any(
            alias in normalized
            if re.search(r"[\u4e00-\u9fff]", alias)
            else re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized)
            is not None
            for alias in aliases
        )
        if matched:
            found.append(canonical)
    return found


class ShoppingRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    operator: RequirementOperator
    value: float | bool | str | list[str]
    unit: str
    priority: RequirementPriority
    source: str = Field(min_length=1, max_length=500)


class BrandAvoidance(BaseModel):
    """Server-owned negative brand preference extracted from user language."""

    model_config = ConfigDict(extra="forbid")

    values: list[str] = Field(min_length=1)
    strength: RequirementPriority
    source: Literal["user"] = "user"

    @model_validator(mode="after")
    def validate_values(self):
        canonical = [canonicalize_brand(value) for value in self.values]
        if any(not value for value in canonical):
            raise ValueError("brand avoidance values cannot be empty")
        if len(canonical) != len(set(canonical)):
            raise ValueError("brand avoidance values must be unique")
        self.values = canonical
        return self


class ShoppingGuideState(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")
    mode: Literal["recommend", "compare"] = "recommend"
    category: ProductCategory | None = None
    use_cases: list[str] = Field(default_factory=list, alias="useCases")
    requirements: list[ShoppingRequirement] = Field(default_factory=list)
    brand_avoidances: list[BrandAvoidance] = Field(
        default_factory=list,
        alias="brandAvoidances",
    )
    candidate_ids: list[int] = Field(default_factory=list, alias="candidateIds")
    compared_ids: list[int] = Field(default_factory=list, alias="comparedIds")
    evidence_status: Literal["missing", "partial", "complete"] = Field(
        default="missing", alias="evidenceStatus"
    )

    @model_validator(mode="after")
    def validate_contract(self):
        if self.requirements and self.category is None:
            raise ValueError("category is required when requirements are present")
        if self.category:
            validate_requirements(self.category, self.requirements)
        if self.brand_avoidances and self.category != "phone":
            raise ValueError("brand avoidances are currently supported only for phone")
        strengths_by_brand: dict[str, RequirementPriority] = {}
        for avoidance in self.brand_avoidances:
            for brand in avoidance.values:
                previous = strengths_by_brand.setdefault(brand, avoidance.strength)
                if previous != avoidance.strength:
                    raise ValueError(
                        "the same avoided brand cannot be both hard and soft"
                    )
        return self


def compiled_shopping_requirements(
    guide: ShoppingGuideState,
) -> list[ShoppingRequirement]:
    """Compile server-owned avoidances into the existing tool requirement ABI."""

    compiled = [item.model_copy(deep=True) for item in guide.requirements]
    for avoidance in guide.brand_avoidances:
        compiled.append(ShoppingRequirement(
            key="brand",
            operator="not_in",
            value=list(avoidance.values),
            unit="text",
            priority=avoidance.strength,
            source=avoidance.source,
        ))
    if guide.category is None:
        raise ValueError("category is required to compile shopping requirements")
    validate_requirements(guide.category, compiled)
    return compiled


def _strict_positive_int_list(value: object, *, field: str) -> list[int]:
    """Reject bool/string/0/negative/duplicate IDs in a strict list contract."""

    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    seen: set[int] = set()
    result: list[int] = []
    for item in value:
        if type(item) is not int or item <= 0:
            raise ValueError(f"{field} must contain only positive integer IDs")
        if item in seen:
            raise ValueError(f"{field} must not contain duplicate IDs")
        seen.add(item)
        result.append(item)
    return result


class CandidateScope(BaseModel):
    """Server-owned, auditable, invalidatable frozen candidate range.

    A ``CandidateScope`` is materialized ONLY from the current Task/Plan/Step
    successful ``search_products`` normalized output plus the passed Validator
    summary.  It is never accepted from a ToolTrace.detail raw body, LLM output,
    or user-submitted IDs.  ``scopeId`` is generated by the server; consumers may
    only reference it.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    scope_id: str = Field(alias="scopeId")
    task_id: str = Field(alias="taskId")
    source_revision: int = Field(alias="sourceRevision")
    source_plan_id: str = Field(alias="sourcePlanId")
    source_step_id: str = Field(alias="sourceStepId")
    source_query: str | None = Field(default=None, alias="sourceQuery", max_length=2000)
    category: ProductCategory
    candidate_pool_ids: list[int] = Field(alias="candidatePoolIds")
    ranked_item_ids: list[int] = Field(alias="rankedItemIds")
    visible_product_ids: list[int] = Field(alias="visibleProductIds")
    requirements_snapshot: list[ShoppingRequirement] = Field(
        alias="requirementsSnapshot",
    )
    brand_avoidances_snapshot: list[BrandAvoidance] = Field(
        alias="brandAvoidancesSnapshot",
    )
    evidence_refs: list[str] = Field(alias="evidenceRefs")
    created_at: str = Field(alias="createdAt")
    status: Literal["active", "invalidated"] = "active"
    invalidation_reason: str | None = Field(default=None, alias="invalidationReason")

    @field_validator(
        "candidate_pool_ids",
        "ranked_item_ids",
        "visible_product_ids",
        mode="before",
    )
    @classmethod
    def _validate_strict_positive_int_ids(
        cls,
        value: object,
        info: ValidationInfo,
    ) -> object:
        return _strict_positive_int_list(value, field=info.field_name or "ids")

    @model_validator(mode="after")
    def validate_scope_contract(self):
        if not isinstance(self.scope_id, str) or not self.scope_id.strip():
            raise ValueError("scopeId must be a non-empty string")
        if self.source_revision < 1:
            raise ValueError("sourceRevision must be a positive integer")
        if not self.ranked_item_ids:
            raise ValueError("rankedItemIds must not be empty")
        if not set(self.ranked_item_ids).issubset(self.candidate_pool_ids):
            raise ValueError("rankedItemIds must be an ordered subset of candidatePoolIds")
        if not self.visible_product_ids:
            raise ValueError("visibleProductIds must not be empty")
        if not set(self.visible_product_ids).issubset(self.ranked_item_ids):
            raise ValueError("visibleProductIds must belong to rankedItemIds")
        if not self.evidence_refs:
            raise ValueError("evidenceRefs must not be empty")
        return self


class ScopeRerankRequest(BaseModel):
    """Server-owned, one-turn instruction to rerank inside a valid CandidateScope.

    Only the deterministic scope-preserving decision may persist a request; the
    Planner merely copies the exact scopeId/rankedItemIds/rankingIntent, never
    invents them.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    scope_id: str = Field(alias="scopeId")
    ranking_intent: Literal["camera_title_claim", "gaming_title_claim", "evidence_comparison"] = Field(
        alias="rankingIntent",
    )
    created_at: str = Field(alias="createdAt")

    @model_validator(mode="after")
    def validate_scope_rerank_request(self):
        if not isinstance(self.scope_id, str) or not self.scope_id.strip():
            raise ValueError("scopeId must be a non-empty string")
        return self


def detect_product_category(text: str) -> ProductCategory | None:
    normalized = text.lower()
    for category, aliases in CATEGORY_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return category  # type: ignore[return-value]
    return None


def is_ecommerce_message(text: str) -> bool:
    return detect_product_category(text) is not None and any(
        token in text for token in (
            "推荐", "选", "买", "找", "预算", "对比", "导购", "适合",
        )
    )


def is_unsafe_shopping_use(text: str) -> bool:
    return any(token in text for token in (
        "窃听", "监听别人", "偷拍", "破解", "未经授权", "绕过家长控制",
        "绕过监护", "跟踪别人",
    ))


def validate_requirements(
    category: ProductCategory, requirements: list[ShoppingRequirement]
) -> None:
    registry = SPEC_REGISTRY[category]
    for requirement in requirements:
        if requirement.key not in CANONICAL_ECOMMERCE_ATTRIBUTE_CODES:
            raise ValueError(f"unsupported requirement key: {requirement.key}")
        spec = registry.get(requirement.key)
        if spec is None:
            raise ValueError(f"unsupported requirement key: {requirement.key}")
        _, unit, operators = spec
        if requirement.unit != unit:
            raise ValueError(f"invalid unit for {requirement.key}: expected {unit}")
        if requirement.operator not in operators:
            raise ValueError(f"invalid operator for {requirement.key}")
        allowed_values = ENUM_VALUE_REGISTRY.get(requirement.key)
        if allowed_values is not None:
            if requirement.operator == "eq":
                valid_enum = (
                    isinstance(requirement.value, str)
                    and requirement.value in allowed_values
                )
            else:
                valid_enum = (
                    isinstance(requirement.value, list)
                    and bool(requirement.value)
                    and len(requirement.value) == len(set(requirement.value))
                    and all(
                        isinstance(value, str) and value in allowed_values
                        for value in requirement.value
                    )
                )
            if not valid_enum:
                raise ValueError(
                    f"invalid enum value for {requirement.key}: "
                    f"expected one of {sorted(allowed_values)}"
                )
        if requirement.priority == "hard" and requirement.source.startswith("inferred:"):
            raise ValueError("inferred requirements cannot be hard constraints")


def reciprocal_rank_fusion(
    ranked_lists: list[list[int]], *, k: int = 60
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        seen: set[int] = set()
        for rank, product_id in enumerate(ranked, 1):
            if product_id in seen:
                continue
            seen.add(product_id)
            scores[product_id] = scores.get(product_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


def tokenize_product_text(text: str) -> list[str]:
    lowered = text.lower()
    latin = re.findall(r"[a-z0-9]+", lowered)
    chinese = re.findall(r"[\u4e00-\u9fff]", lowered)
    return latin + chinese + [
        chinese[index] + chinese[index + 1] for index in range(len(chinese) - 1)
    ]


DEFAULT_BM25_FIELD_WEIGHTS: dict[str, float] = {
    "title": 1.0,
    "attributeText": 0.45,
    "brand": 0.25,
}

PRODUCT_QUERY_EXPANSIONS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("打游戏", "游戏", "电竞", "竞技", "吃鸡", "和平精英", "王者荣耀", "手游"),
        ("游戏", "竞技", "和平精英"),
    ),
    (
        ("拍照", "摄影", "相机", "摄像"),
        ("拍照", "相机", "摄像头"),
    ),
    (
        ("大电池", "续航", "电量"),
        ("大电池", "续航", "电池", "mah"),
    ),
)
PRODUCT_QUERY_EXPANSION_INTENT_CUES = (
    "适合", "有没有", "有没", "推荐", "想要", "哪款", "哪个好", "求", "需要",
)


def expand_product_query(query: str) -> tuple[str, list[str]]:
    """Add small, audited title-language synonyms for broad use-case recall.

    These are recall hints, never structured facts or hard constraints.  The
    returned terms are exposed in the retrieval trace so expansion remains
    explainable and regression-testable.
    """

    lowered = query.casefold()
    compact = re.sub(r"\s+", "", lowered)
    broad_use_case = (
        len(compact) <= 14
        or any(cue in compact for cue in PRODUCT_QUERY_EXPANSION_INTENT_CUES)
    )
    if not broad_use_case:
        return query, []
    added: list[str] = []
    for triggers, expansions in PRODUCT_QUERY_EXPANSIONS:
        if not any(trigger.casefold() in lowered for trigger in triggers):
            continue
        for term in expansions:
            if term.casefold() not in lowered and term not in added:
                added.append(term)
    return (" ".join((query, *added)).strip(), added)


def product_embedding_text(product: Mapping[str, Any]) -> str:
    """Build the audited semantic document from the seller title only.

    Brand and attribute values remain independent BM25/structured channels.
    Keeping them out of this embedding prevents the long attribute blob from
    diluting natural-language use-case phrases in ``title``.
    """

    return str(product.get("title") or "")


def _bm25_scores(
    query_tokens: list[str],
    documents: list[list[str]],
) -> list[float]:
    """Return BM25 scores for one field without mixing field lengths."""

    if not documents:
        return []
    doc_frequency = Counter()
    for document in documents:
        doc_frequency.update(set(document))
    average_length = sum(map(len, documents)) / max(len(documents), 1)
    scores: list[float] = []
    for document in documents:
        frequencies = Counter(document)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if not frequency:
                continue
            inverse = math.log(
                1
                + (len(documents) - doc_frequency[token] + 0.5)
                / (doc_frequency[token] + 0.5)
            )
            denominator = frequency + 1.2 * (
                0.25 + 0.75 * len(document) / max(average_length, 1)
            )
            score += inverse * frequency * 2.2 / denominator
        scores.append(score)
    return scores


def bm25_rank(
    query: str,
    products: list[dict[str, Any]],
    *,
    field_weights: Mapping[str, float] | None = None,
) -> list[int]:
    """Rank products with field-aware BM25 over the useful catalog fields.

    Title, attribute value text, and brand are scored independently so a long
    seller/category blob cannot dilute a title match.  ``field_weights`` is
    exposed for frozen offline ablations; production uses the audited default.
    """

    query_tokens = tokenize_product_text(query)
    if not query_tokens:
        return [int(product["id"]) for product in products]
    weights = dict(DEFAULT_BM25_FIELD_WEIGHTS if field_weights is None else field_weights)
    unknown = set(weights) - {"title", "attributeText", "brand"}
    if unknown:
        raise ValueError(f"unsupported BM25 fields: {sorted(unknown)}")
    if not weights or any(type(value) not in {int, float} or value < 0 for value in weights.values()):
        raise ValueError("BM25 field weights must be non-negative numbers")
    combined = [0.0] * len(products)
    for field, weight in weights.items():
        if not weight:
            continue
        field_documents = [
            tokenize_product_text(str(product.get(field) or ""))
            for product in products
        ]
        for index, score in enumerate(_bm25_scores(query_tokens, field_documents)):
            combined[index] += float(weight) * score
    scored = [
        (int(product["id"]), combined[index])
        for index, product in enumerate(products)
    ]
    return [item[0] for item in sorted(scored, key=lambda item: (-item[1], item[0]))]


def bm25_rank_legacy_concat(query: str, products: list[dict[str, Any]]) -> list[int]:
    """Frozen pre-2026-08-16 baseline used only by offline ablation."""

    query_tokens = tokenize_product_text(query)
    if not query_tokens:
        return [int(product["id"]) for product in products]
    documents = [
        tokenize_product_text(" ".join(
            str(product.get(field) or "") for field in (
                "title", "brand", "seller", "categoryL1", "categoryL2",
                "categoryL3", "attributeText",
            )
        ))
        for product in products
    ]
    scores = _bm25_scores(query_tokens, documents)
    return [
        item[0]
        for item in sorted(
            (
                (int(product["id"]), scores[index])
                for index, product in enumerate(products)
            ),
            key=lambda item: (-item[1], item[0]),
        )
    ]


def _summary_requirement_value(product: dict[str, Any], key: str) -> Any:
    """Read only public summary facts suitable for candidate recall.

    Controlled used-phone attributes retain the exact-token observer's
    unknown/conflict semantics.  This intentionally does not trust arbitrary
    normalized fields or infer facts from title, brand, or seller text.
    """
    if key == "brand":
        value = product.get("brand")
        return value if value not in (None, "") else None
    if key == "price_minor":
        if product.get("priceStatus") == "verified":
            return product.get("snapshotPriceMinor")
        value, _ = synthetic_price_value(product, allow_budget=True)
        return value
    if key not in USED_PHONE_ATTRIBUTE_REGISTRY:
        return None
    raw_value = product.get("attributeText")
    if not isinstance(raw_value, str):
        return None
    observation = observe_used_phone_attributes(raw_value)[key]
    if observation.status != "known" or observation.fact is None:
        return None
    return observation.fact.value


def soft_preference_match_score(
    actual: Any,
    requirement: ShoppingRequirement,
    status: str,
) -> float:
    """Score an explicitly ordered soft preference without inventing facts.

    A soft ``in`` list is an ordered preference list: the first accepted value
    is strongest, the second is weaker, and so on.  Hard ``in`` requirements
    remain ordinary set-membership gates.  Other passing soft requirements keep
    the existing binary score.
    """

    if requirement.priority != "soft" or status != "pass":
        return 0.0
    expected = requirement.value
    if requirement.operator != "in" or not isinstance(expected, list) or not expected:
        return 1.0

    comparison_actual = actual
    comparison_expected = list(expected)
    if requirement.key == "brand" and isinstance(actual, str):
        comparison_actual = canonicalize_brand(actual)
        comparison_expected = [
            canonicalize_brand(item) if isinstance(item, str) else item
            for item in expected
        ]
    for index, candidate in enumerate(comparison_expected):
        matches = (
            comparison_actual.casefold() == candidate.casefold()
            if isinstance(comparison_actual, str) and isinstance(candidate, str)
            else comparison_actual == candidate
        )
        if matches:
            return (len(comparison_expected) - index) / len(comparison_expected)
    return 0.0


def structured_requirement_rank(
    category: ProductCategory,
    products: list[dict[str, Any]],
    requirements: list[ShoppingRequirement],
    lexical_rank: list[int] | None = None,
) -> list[int]:
    """Build a generic structured recall channel over public catalog facts.

    Hard failures are absent from this *additional* channel; the lexical
    channel remains intact.  Unknown and conflicting evidence is retained
    behind confirmed matches, never promoted to a fact.  Soft requirements
    order pass before unknown before fail without changing hard eligibility.
    """
    validate_requirements(category, requirements)
    recall_requirements = [
        requirement for requirement in requirements
        if requirement.key in {"brand", "price_minor"}
        or (
            category == "phone"
            and requirement.key in USED_PHONE_ATTRIBUTE_REGISTRY
        )
    ]
    if not recall_requirements:
        return []
    hard_max_budget = next((
        requirement for requirement in recall_requirements
        if requirement.key == "price_minor"
        and requirement.operator == "lte"
        and requirement.priority == "hard"
    ), None)
    lexical_position = {
        product_id: index for index, product_id in enumerate(lexical_rank or [])
    }
    ranked: list[tuple[int, float, int, int, int, int, int, int]] = []
    for product in products:
        product_id = int(product["id"])
        hard_unknowns = 0
        hard_failed = False
        soft_passes = 0
        soft_preference_points = 0.0
        soft_failures = 0
        soft_unknowns = 0
        for requirement in recall_requirements:
            status = _evaluate(
                _summary_requirement_value(product, requirement.key), requirement
            )
            if requirement.priority == "hard":
                hard_failed = hard_failed or status == "fail"
                hard_unknowns += int(status == "unknown")
            else:
                soft_passes += int(status == "pass")
                soft_preference_points += soft_preference_match_score(
                    _summary_requirement_value(product, requirement.key),
                    requirement,
                    status,
                )
                soft_failures += int(status == "fail")
                soft_unknowns += int(status == "unknown")
        if hard_failed:
            continue
        budget_price = (
            _summary_requirement_value(product, "price_minor")
            if hard_max_budget is not None else None
        )
        budget_order = (
            -int(budget_price)
            if type(budget_price) in {int, float} else 0
        )
        ranked.append((
            hard_unknowns,
            -soft_preference_points,
            budget_order,
            -soft_passes,
            soft_failures,
            soft_unknowns,
            lexical_position.get(product_id, len(products)),
            product_id,
        ))
    return [row[-1] for row in sorted(ranked)]


def _attribute_value(product: dict[str, Any], key: str) -> tuple[Any, dict | None]:
    if key == "brand":
        value = product.get("brand")
        if value is None or value == "":
            return None, None
        return value, {
            "ref": f"product:{product['id']}:brand",
            "field": "brand",
            "rawValue": value,
            "source": product.get("source"),
            "provenanceUrl": product.get("provenanceUrl"),
        }
    if key == "price_minor":
        if product.get("priceStatus") == "verified":
            value = product.get("snapshotPriceMinor")
            return value, {
                "ref": f"product:{product['id']}:snapshotPriceMinor",
                "field": "snapshotPriceMinor", "rawValue": value,
                "priceStatus": product.get("priceStatus"),
                "dataNature": product.get("dataNature"),
                "pricePolicy": "verified_snapshot_only",
            }
        value, synthetic = synthetic_price_value(product, allow_budget=True)
        if value is None or synthetic is None:
            return None, None
        return value, {
            "ref": f"product:{product['id']}:syntheticReferencePriceMinor",
            "field": "syntheticReferencePriceMinor",
            "rawValue": value,
            "priceStatus": "synthetic",
            "dataNature": "synthetic",
            "pricePolicy": synthetic["policy"],
            "rulesetVersion": synthetic["rulesetVersion"],
            "rulesetSha256": synthetic["rulesetSha256"],
            "sourceCatalogSha256": synthetic["sourceCatalogSha256"],
            "seed": synthetic["seed"],
            "disclosureZh": synthetic["disclosureZh"],
        }
    for attribute in product.get("attributes") or []:
        if attribute.get("key") != key:
            continue
        if key in USED_PHONE_ATTRIBUTE_REGISTRY:
            raw_value = attribute.get("rawValue")
            observation = (
                observe_used_phone_attributes(raw_value).get(key)
                if isinstance(raw_value, str)
                else None
            )
            normalized = attribute.get("normalizedText")
            if (
                observation is None
                or observation.status != "known"
                or observation.fact is None
                or observation.fact.value != normalized
                or attribute.get("normalizedNumber") is not None
                or attribute.get("normalizedBoolean") is not None
                or attribute.get("evidenceField")
                != USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD
                or attribute.get("extractionMethod")
                != USED_PHONE_ATTRIBUTE_RULESET_VERSION
            ):
                return None, None
            return normalized, {
                "ref": f"product:{product['id']}:attribute:{key}",
                "field": attribute.get("evidenceField"),
                "rawValue": raw_value,
                "method": attribute.get("extractionMethod"),
                "confidence": attribute.get("confidence"),
            }
        value = attribute.get("normalizedNumber")
        if value is None:
            value = attribute.get("normalizedBoolean")
        if value is None:
            value = attribute.get("normalizedText")
        return value, {
            "ref": f"product:{product['id']}:attribute:{key}",
            "field": attribute.get("evidenceField"),
            "rawValue": attribute.get("rawValue"),
            "method": attribute.get("extractionMethod"),
            "confidence": attribute.get("confidence"),
        }
    return None, None


def _evaluate(actual: Any, requirement: ShoppingRequirement) -> str:
    if actual is None:
        return "unknown"
    expected = requirement.value
    if requirement.key == "brand" and isinstance(actual, str):
        actual = canonicalize_brand(actual)
        if isinstance(expected, str):
            expected = canonicalize_brand(expected)
        elif isinstance(expected, list):
            expected = [
                canonicalize_brand(item) if isinstance(item, str) else item
                for item in expected
            ]
    if requirement.operator == "eq":
        if isinstance(actual, str) and isinstance(expected, str):
            return "pass" if actual.casefold() == expected.casefold() else "fail"
        return "pass" if actual == expected else "fail"
    if requirement.operator == "lte":
        return "pass" if float(actual) <= float(expected) else "fail"
    if requirement.operator == "gte":
        return "pass" if float(actual) >= float(expected) else "fail"
    if requirement.operator == "in":
        return "pass" if actual in expected else "fail"
    if requirement.operator == "not_in":
        return "pass" if actual not in expected else "fail"
    return "unknown"


def _fact_evidence(product: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build a display view exclusively from fields returned by the Java fact API."""
    product_id = int(product["id"])
    source = product.get("source")
    provenance = product.get("provenanceUrl")
    evidence: list[dict[str, Any]] = []

    def fact(field: str, value: Any, *, verified: bool = True) -> Any:
        if value is None or value == "" or not verified:
            return "未知"
        evidence.append({
            "ref": f"product:{product_id}:{field}",
            "field": field,
            "rawValue": value,
            "source": source,
            "provenanceUrl": provenance,
        })
        return value

    attributes: dict[str, Any] = {}
    for attribute in product.get("attributes") or []:
        key = attribute.get("key")
        if not key:
            continue
        if key in USED_PHONE_ATTRIBUTE_REGISTRY:
            value, citation = _attribute_value(product, str(key))
            attributes[str(key)] = value if citation is not None else "未知"
            if citation is not None:
                evidence.append(citation)
            continue
        value = attribute.get("normalizedNumber")
        if value is None:
            value = attribute.get("normalizedBoolean")
        if value is None:
            value = attribute.get("normalizedText")
        attributes[str(key)] = fact(f"attribute:{key}", value)

    if product.get("priceStatus") == "verified":
        price_value = fact(
            "snapshotPriceMinor", product.get("snapshotPriceMinor"), verified=True
        )
        price_status = "verified"
    else:
        price_value, synthetic = synthetic_price_value(product, allow_budget=False)
        if price_value is not None and synthetic is not None:
            evidence.append({
                "ref": f"product:{product_id}:syntheticReferencePriceMinor",
                "field": "syntheticReferencePriceMinor",
                "rawValue": price_value,
                "priceStatus": "synthetic",
                "dataNature": "synthetic",
                "pricePolicy": synthetic["policy"],
                "rulesetVersion": synthetic["rulesetVersion"],
                "rulesetSha256": synthetic["rulesetSha256"],
                "sourceCatalogSha256": synthetic["sourceCatalogSha256"],
                "seed": synthetic["seed"],
                "disclosureZh": synthetic["disclosureZh"],
            })
            price_status = "synthetic"
        else:
            price_value, price_status = "未知", "unverified"

    facts = {
        "productId": product_id,
        "title": fact("title", product.get("title")),
        "brand": fact("brand", product.get("brand")),
        "priceMinor": price_value,
        "priceStatus": price_status,
        "currency": fact("currency", product.get("currency")),
        "specifications": attributes,
        "description": fact("description", product.get("attributeText")),
        "source": fact("source", source),
        "provenanceUrl": fact("provenanceUrl", provenance),
    }
    return facts, evidence


def rule_rerank_candidates(
    category: ProductCategory,
    products: list[dict[str, Any]],
    requirements: list[ShoppingRequirement],
    retrieval_scores: dict[int, float] | None = None,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    """Apply the deterministic Top-50 rule reranker over authoritative facts."""
    validate_requirements(category, requirements)
    raw_scores = retrieval_scores or {}
    maximum = max(raw_scores.values(), default=0.0)
    soft_count = sum(item.priority == "soft" for item in requirements)
    hard_max_budget = next((
        requirement for requirement in requirements
        if requirement.key == "price_minor"
        and requirement.operator == "lte"
        and requirement.priority == "hard"
    ), None)
    rows: list[dict[str, Any]] = []
    eliminated: list[dict[str, Any]] = []

    for product in products[:50]:
        product_id = int(product["id"])
        checks: list[dict[str, Any]] = []
        check_evidence: list[dict[str, Any]] = []
        soft_passes = 0
        soft_preference_points = 0.0
        hard_failures = 0
        hard_unknowns = 0
        budget_price_minor: int | None = None
        category_values = [
            product.get(field) for field in ("categoryL1", "categoryL2", "categoryL3")
            if product.get(field)
        ]
        expected_terms = (*CATEGORY_ALIASES[category], CATEGORY_LABELS[category])
        if not category_values:
            hard_unknowns += 1
            category_status = "unknown"
            category_evidence = None
        elif any(
            term in str(value)
            for value in category_values for term in expected_terms
        ):
            category_status = "pass"
            matched_field = next(
                field for field in ("categoryL1", "categoryL2", "categoryL3")
                if product.get(field)
                and any(term in str(product[field]) for term in expected_terms)
            )
            category_evidence = {
                "ref": f"product:{product_id}:{matched_field}",
                "field": matched_field,
                "rawValue": product[matched_field],
                "source": product.get("source"),
                "provenanceUrl": product.get("provenanceUrl"),
            }
            check_evidence.append(category_evidence)
        else:
            hard_failures += 1
            category_status = "fail"
            category_evidence = None
        for requirement in requirements:
            actual, citation = _attribute_value(product, requirement.key)
            if (
                requirement is hard_max_budget
                and type(actual) in {int, float}
            ):
                budget_price_minor = int(actual)
            status = _evaluate(actual, requirement)
            if citation:
                check_evidence.append(citation)
            if requirement.priority == "hard":
                hard_failures += int(status == "fail")
                hard_unknowns += int(status == "unknown")
            else:
                soft_passes += int(status == "pass")
                soft_preference_points += soft_preference_match_score(
                    actual, requirement, status
                )
            checks.append({
                "key": requirement.key,
                "operator": requirement.operator,
                "expected": requirement.value,
                "unit": requirement.unit,
                "priority": requirement.priority,
                "source": requirement.source,
                "actual": actual,
                "displayValue": actual if actual is not None else "未知",
                "status": status,
                "evidenceRef": citation["ref"] if citation else None,
            })

        if hard_failures:
            eliminated.append({
                "productId": product_id,
                "reason": (
                    "category_violation" if category_status == "fail"
                    else "explicit_hard_constraint_violation"
                ),
                "checks": [
                    check for check in checks
                    if check["priority"] == "hard" and check["status"] == "fail"
                ],
            })
            continue

        facts, fact_evidence = _fact_evidence(product)
        deduped = {
            item["ref"]: item for item in [*fact_evidence, *check_evidence]
        }
        recall_score = raw_scores.get(product_id, 0.0) / maximum if maximum else 0.0
        soft_score = soft_preference_points / soft_count if soft_count else 0.0
        evidence_slots = 5 + len(requirements)
        present_slots = sum(
            facts[key] != "未知"
            for key in ("title", "brand", "priceMinor", "description", "source")
        ) + sum(check["evidenceRef"] is not None for check in checks)
        evidence_score = present_slots / evidence_slots if evidence_slots else 0.0
        final_score = 0.55 * recall_score + 0.30 * soft_score + 0.15 * evidence_score
        rows.append({
            "product": product,
            "facts": facts,
            "checks": checks,
            "fullyMatched": hard_failures == 0 and hard_unknowns == 0,
            "hardFailures": hard_failures,
            "hardUnknowns": hard_unknowns,
            "categoryStatus": category_status,
            "categoryEvidenceRef": (
                category_evidence["ref"] if category_evidence else None
            ),
            "softScore": soft_passes,
            "softPreferenceScore": round(soft_score, 8),
            "selectionType": (
                "full_match" if hard_unknowns == 0 else "closest_alternative"
            ),
            "scoreBreakdown": {
                "normalizedRecall": round(recall_score, 8),
                "softRequirementMatch": round(soft_score, 8),
                "evidenceCompleteness": round(evidence_score, 8),
                "weights": {"recall": 0.55, "soft": 0.30, "evidence": 0.15},
                "final": round(final_score, 8),
            },
            "budgetSortPriceMinor": budget_price_minor,
            "evidenceRefs": list(deduped),
            "evidence": list(deduped.values()),
        })

    rows.sort(key=lambda row: (
        row["hardUnknowns"] > 0,
        -float(row.get("softPreferenceScore", 0.0)),
        -row["budgetSortPriceMinor"]
        if hard_max_budget is not None
        and type(row.get("budgetSortPriceMinor")) is int
        else 0,
        -row["scoreBreakdown"]["final"],
        int(row["product"]["id"]),
    ))
    selected = rows[:limit]
    return {
        "products": selected,
        "eliminated": eliminated,
        "rankingTrace": {
            "inputCandidateCount": min(len(products), 50),
            "eligibleCandidateCount": len(rows),
            "eliminatedHardViolationCount": len(eliminated),
            "confirmedBeforeUnknown": True,
            "softPreferenceOrdering": "ordered_soft_in_before_budget_and_relevance",
            "budgetOrdering": (
                "price_desc_within_hard_lte"
                if hard_max_budget is not None else "inactive"
            ),
            "budgetCeilingMinor": (
                hard_max_budget.value if hard_max_budget is not None else None
            ),
            "tieBreak": "productId_ascending",
            "formula": (
                "0.55*normalized_recall + 0.30*soft_requirement_match + "
                "0.15*evidence_completeness"
            ),
        },
        "evidence": list({
            item["ref"]: item
            for row in selected for item in row["evidence"]
        }.values()),
    }


def compare_product_details(
    category: ProductCategory,
    products: list[dict[str, Any]],
    requirements: list[ShoppingRequirement],
) -> dict[str, Any]:
    reranked = rule_rerank_candidates(
        category,
        products[:5],
        requirements,
        {
            int(product["id"]): float(5 - index)
            for index, product in enumerate(products[:5])
        },
        limit=5,
    )
    finalists = reranked["products"][:3]
    below_top_three = [
        {
            "productId": row["product"]["id"],
            "reason": "below_top3_after_rule_rerank",
            "checks": row["checks"],
        }
        for row in reranked["products"][3:]
    ]
    return {
        "category": category, "categoryLabel": CATEGORY_LABELS[category],
        "requirements": [item.model_dump() for item in requirements],
        "products": finalists,
        "comparisonMatrix": [
            {"productId": row["product"]["id"], "checks": row["checks"]}
            for row in finalists
        ],
        "eliminated": [*reranked["eliminated"], *below_top_three],
        "evidence": reranked["evidence"],
        "evidenceRefs": [item["ref"] for item in reranked["evidence"]],
        "rankingTrace": reranked["rankingTrace"],
        "snapshotNotice": "历史公开数据快照，非实时价格/库存；未验证价格不展示且不参与预算判断。",
        "hasCompleteMatch": any(row["fullyMatched"] for row in finalists),
    }
