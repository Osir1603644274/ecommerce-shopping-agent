"""Fast-preview analysis for used-phone shopping turns (先快后完整).

Single semantic source: this module orchestrates the existing deterministic
used-phone parsers in ``app.llm`` rather than maintaining a second keyword
vocabulary that could drift.  It is pure, deterministic and read-only — it
never writes TaskState, candidate ids, Validator state or step outputs, and it
never claims the Validator has approved anything.

The pipeline shape is:

1. ``analyze_used_phone_fast`` decides whether this used-phone turn can run
   without the TaskManager/TaskState models (``full``), can preview products
   while a background model completes (``safe_partial``), must stay on the
   normal model path (``unsafe``), or is outside the used-phone contract
   (``not_applicable``).  Every safety gate fails closed.
2. ``build_preview_cache_key`` / ``PreviewCandidateCache`` keep bounded deep
   copies of normalized candidates keyed by catalog revision + normalized
   query/useCase + full merged requirement identity.
3. ``build_provisional_guide_result`` projects only rule-level ``full_match``
   Top-3 candidates into the same card shape the browser already renders,
   explicitly labelled provisional.

``app.llm`` imports ``app.domains.ecommerce`` at module top, so this module
lazy-imports the llm parser functions inside the analyzer to avoid an import
cycle.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ...settings import settings
from .brand_negation import _CUE_RE, _POSITIVE_BOUNDARY_RE
from .models import (
    BrandAvoidance,
    ShoppingGuideState,
    ShoppingRequirement,
    canonicalize_brand,
    compiled_shopping_requirements,
    detect_product_brands,
    detect_product_category,
)
from .ranking_contract import TWO_STAGE_RANKING_CONTRACT_VERSION
from .used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_REGISTRY,
    used_phone_attribute_ruleset_sha256,
)

FastPreviewStatus = Literal["full", "safe_partial", "unsafe", "not_applicable"]

# A bare number that is not bound to a complete unit+operator must never be
# turned into a guess.  ``iPhone 12`` is ignored (no spec context); ``3000以内``
# is bound by its unit; ``预算3000`` is left unbound and fail-closes.
_BARE_NUMBER_RE = re.compile(r"(?<![a-z0-9])\d+(?:\.\d+)?(?![a-z0-9])")
_NUMERIC_SPEC_CUES = (
    "价格", "预算", "价位", "以内", "以下", "以上", "左右", "大约", "差不多",
    "内存", "存储", "容量", "电池", "健康", "成", "%", "元", "¥", "千", "万",
    "mah", "毫安", "gb", "英寸", "寸",
)
_NUMERIC_UNIT_ANCHORS = (
    "元", "¥", "以内", "以下", "以上", "成", "%", "mah", "毫安", "gb",
    "千", "万", "寸", "英寸", "块",
)

_CANCEL_EXPRESSION_RE = re.compile(
    r"(?:算了|不买了|不要了|取消(?:当前)?(?:任务|导购)?|"
    r"停止(?:当前)?(?:任务|导购)?|结束(?:当前)?(?:任务|导购)?)"
)
_NEW_TASK_EXPRESSION_RE = re.compile(r"(?:新任务|另一个任务|另外一个任务|重新开始)")


class FastAnalysis(BaseModel):
    """Read-only verdict for one used-phone turn."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    status: FastPreviewStatus
    route: str
    reason: str
    mentioned_keys: list[str] = Field(default_factory=list)
    covered_keys: list[str] = Field(default_factory=list)
    uncovered_keys: list[str] = Field(default_factory=list)
    requirements: list[ShoppingRequirement] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    unresolved_cues: list[str] = Field(default_factory=list)
    risk_codes: list[str] = Field(default_factory=list)
    allow_task_manager_bypass: bool = False
    allow_task_state_bypass: bool = False
    allow_product_preview: bool = False
    preview_requirements: list[ShoppingRequirement] = Field(default_factory=list)


def _no_used_phone_signal(
    message: str,
    mentioned_keys: set[str],
    covered_keys: set[str],
) -> FastAnalysis:
    return FastAnalysis(
        status="not_applicable",
        route="fast_analyzer",
        reason="no_used_phone_signal",
        mentioned_keys=sorted(mentioned_keys),
        covered_keys=sorted(covered_keys),
        uncovered_keys=sorted(mentioned_keys - covered_keys),
        allow_product_preview=False,
    )


def _unsafe_analysis(
    message: str,
    *,
    reason: str,
    risk_codes: list[str],
    mentioned_keys: set[str],
    covered_keys: set[str],
    unresolved_cues: list[str] | None = None,
) -> FastAnalysis:
    return FastAnalysis(
        status="unsafe",
        route="fast_analyzer",
        reason=reason,
        mentioned_keys=sorted(mentioned_keys),
        covered_keys=sorted(covered_keys),
        uncovered_keys=sorted(mentioned_keys - covered_keys),
        unresolved_cues=list(unresolved_cues or []),
        risk_codes=list(dict.fromkeys(risk_codes)),
        allow_task_manager_bypass=False,
        allow_task_state_bypass=False,
        allow_product_preview=False,
    )


def _unbound_numeric_units(message: str) -> list[str]:
    """Return bare numbers that look like a price/capacity/percentage spec but
    lack a complete unit/operator and are not otherwise bound."""
    normalized = re.sub(r"\s+", "", message).casefold()
    unbound: list[str] = []
    for match in _BARE_NUMBER_RE.finditer(normalized):
        window = normalized[max(0, match.start() - 8): match.end() + 8]
        if not any(cue in window for cue in _NUMERIC_SPEC_CUES):
            continue
        if any(anchor in window for anchor in _NUMERIC_UNIT_ANCHORS):
            continue
        unbound.append(match.group(0))
    return unbound


def _same_turn_value_conflict(
    explicit: dict[str, ShoppingRequirement],
    exclusions: dict[str, list[str]],
) -> bool:
    """Detect a same-turn accept+reject of the same controlled value."""
    for key, requirement in explicit.items():
        allowed = USED_PHONE_ATTRIBUTE_REGISTRY.get(key)
        if allowed is None:
            continue
        if requirement.operator in {"eq", "in"}:
            constrained = (
                list(requirement.value) if isinstance(requirement.value, list)
                else [requirement.value]
            )
            if len(constrained) != len(set(constrained)):
                return True
            excluded = set(exclusions.get(key, []))
            if any(value in excluded for value in constrained):
                return True
            # A finite domain fully constrained by positives alone.
            if len(constrained) >= len(set(allowed.allowed_values)):
                return True
    return False


# --- Unified fail-closed safety verdict -----------------------------------
#
# A single, testable scan owns every safety decision for one used-phone turn:
# ANY unbound negation target, bare ordinal, demonstrative reference or
# "换一个" remaining in the turn — even next to a fully-resolved brand/os/screen
# condition — must force every bypass/preview flag off and status=unsafe.  The
# keyword tables below live here and only here; ``app.llm``'s scoped
# negation/ordinal parsers remain the authority for what counts as *bound*.

# Unified ordinal/quantity grammar for the fail-closed scan.  Any ``第N…``,
# ``前N…`` or ``头N…`` reference — Arabic or Chinese numeral, any rank — is an
# unbound reference unless the turn's ordinals are server-bound to Validator
# display ids (``ordinal_status == "bound"``); only the surfaces
# ``_ordinal_comparison_binding`` actually binds (explicit first two, or
# ``这两个`` against exactly two trusted ids).  Every other surface must fail
# closed here.
_CHINESE_DIGITS = "零〇一二两三四五六七八九十百千万"
_ORDINAL_SURFACE_RE = re.compile(
    r"(?:第|前|头)"
    r"(?:[0-9０-９]+|[" + _CHINESE_DIGITS + r"]+)"
    r"(?:个|款|台|部)"
)
# Demonstrative two-item references the deterministic path only binds against
# exactly two trusted Validator ids.
_DEMONSTRATIVE_TWO_CUES = ("这两个", "这两款", "这两台", "这两部")

# The exact ordinal surfaces ``_ordinal_comparison_binding`` can server-bind to
# Validator display ids.  Under ``ordinal_status == "bound"`` ONLY these are
# exempt; any other ordinal surface co-occurring in the same turn (第3个, 前3个,
# 头两个, 这两台, …) is a reference the deterministic path never binds and must
# fail closed even when the turn otherwise resolves to a bound compare.
_BOUND_COMPARISON_SURFACES = (
    "第一个", "第一款", "第二个", "第二款",
    "前两个", "前两款", "这两个", "这两款",
)

# Demonstrative product references and unbounded substitution requests the
# deterministic path never binds.  ``换一款``/``换掉当前``/``继续找`` etc. are
# legitimate bound substitutions and are deliberately NOT listed here.
_NONORDINAL_REFERENCE_CUES = (
    "换一个", "换个", "换台", "换部",
    "另一个", "另外一个", "另一款", "另一台",
    "另外那个", "另外这款", "另外那款",
    "这个", "那个", "这台", "那台", "这款", "那款", "这部", "那部",
    "这些", "那些",
)

# Same-turn accept+reject of the same controlled value: a positive surface and
# a negation of the same surface in one message.  Single location (this
# verdict); the scoped negation parser stays the authority for what *counts*
# as an exclusion.
_SAME_TURN_VALUE_SURFACES: dict[str, tuple[str, ...]] = {
    "screen_originality": ("原装屏", "原厂屏", "非原装屏", "非原厂屏"),
    "battery_originality": ("原装电池", "原厂电池", "非原装电池", "非原厂电池"),
    "os": ("安卓系统", "ios系统", "安卓", "ios", "android"),
}
_NEGATION_PREFIXES = ("不想要", "不要", "不能要", "不再要")


def _unresolved_reference_cues(message: str, ordinal_status: str) -> list[str]:
    """Return bare ordinals/pronouns/"换一个" that are not server-bound.

    ``ordinal_status == "bound"`` means the turn resolves to trusted Validator
    ids (compare mode); the exact surfaces ``_ordinal_comparison_binding`` can
    bind are exempt, but a demonstrative reference or "换一个" in the same turn
    is never exempt, and any ordinal surface the deterministic path never binds
    (第3个, 前3个, 头两个, …) still fails closed.
    """
    normalized = re.sub(r"\s+", "", message).casefold()
    found: list[str] = []
    for cue in _NONORDINAL_REFERENCE_CUES:
        if cue in normalized:
            found.append(cue)
    for cue in _DEMONSTRATIVE_TWO_CUES:
        if cue in normalized and (
            ordinal_status != "bound" or cue not in _BOUND_COMPARISON_SURFACES
        ):
            found.append(cue)
    for match in _ORDINAL_SURFACE_RE.finditer(normalized):
        surface = match.group(0)
        if surface in _BOUND_COMPARISON_SURFACES and ordinal_status == "bound":
            continue
        found.append(surface)
    return list(dict.fromkeys(found))


def _same_turn_accept_and_reject_same_value(message: str) -> bool:
    """Detect a positive request and a negation of the same controlled value
    even when the negation parser swallows the positive clause (``我要原装屏但也
    不要原装屏`` parses as a bare screen exclusion and loses the "要原装屏" half).
    The negation prefixes are masked before the positive scan so a negation's
    own 要 never counts as a request.
    """
    normalized = re.sub(r"\s+", "", message).casefold()
    masked = normalized
    for prefix in _NEGATION_PREFIXES:
        masked = masked.replace(prefix, "#")
    for surfaces in _SAME_TURN_VALUE_SURFACES.values():
        for surface in surfaces:
            positive = (
                f"要{surface}" in masked or f"{surface}要" in masked
            )
            if not positive:
                continue
            negative = (
                any(
                    f"{prefix}{surface}" in normalized
                    for prefix in _NEGATION_PREFIXES
                )
                or f"{surface}不要" in normalized
                or f"{surface}不想要" in normalized
            )
            if negative:
                return True
    return False


# Coordination boundaries and trailing particles that stop a negation cue's
# postfix/before span from reaching back across a sibling clause to re-claim an
# earlier cue's target (Codex defect #1: ``不要苹果而且贴膜也不要`` must leave the
# second cue unbound — its 贴膜 cannot bind to the first cue's 苹果 because the
# coordination boundary 而且 is never crossed).  This mirrors the consumed-span
# accounting used by ``app.llm``'s scoped-negation primitives.
_NEGATION_COORDINATION_BOUNDARIES = (
    "而且", "并且", "但是", "不过", "而是", "只是", "同时", "还要",
    "另外", "再加上", "然后", "接着", "还有", "以及", "或者", "或是",
    "还是", "因为", "所以", "不仅", "不但", "和", "与", "及", "而", "但",
)
_NEGATION_TRAILING_PARTICLES = ("也", "都", "又", "还", "再", "就")


def _negation_after_span(clause: str, cue: Any, next_cue_start: int) -> str:
    """Postfix span of ``cue`` up to the next negation cue (or clause end).

    A positive boundary inside the span truncates it (``不要苹果，安卓优先``:
    the 不要 binds only 苹果, the positive preference is its own clause).
    """
    after = clause[cue.end():next_cue_start]
    boundary = _POSITIVE_BOUNDARY_RE.search(after)
    if boundary is not None:
        after = after[:boundary.start()]
    return after


def _negation_before_span(clause: str, cue: Any, prev_cue_end: int) -> str:
    """Prefix span of ``cue`` bounded by the last coordination boundary.

    Consumed-span accounting: the span starts after the nearest coordination
    boundary so a cue can never reach back into a sibling clause.  Trailing
    particles (也/都/又/还/再/就) that belong to the *previous* cue's postfix are
    trimmed so ``苹果也不要``'s 不要 does not re-consume the trailing 也.
    """
    start = prev_cue_end
    last_end = start
    for boundary in _NEGATION_COORDINATION_BOUNDARIES:
        pos = clause.rfind(boundary, start, cue.start())
        if pos != -1:
            candidate = pos + len(boundary)
            if candidate > last_end:
                last_end = candidate
    before = clause[last_end:cue.start()]
    while before and before[-1] in _NEGATION_TRAILING_PARTICLES:
        before = before[:-1]
    return before


def _cue_targets_bound(clause: str, cue: Any, matches: list[Any], index: int) -> bool:
    """Does this negation cue bind to a brand or controlled-attribute exclusion?

    Prefix forms bind a descriptor/anchor immediately after the cue
    (``不要苹果`` brand, ``不要非原装的屏幕`` scoped anchor+value).  Middle forms
    carry the anchor before the cue and the value after (``屏幕不要非原装的``).
    Postfix forms carry the target before the cue (``苹果也不要``, ``非原装屏不要``);
    an embedded value bound by the scoped parser in the before-span also counts.
    """
    from app.llm import (
        _scope_anchor_and_value,
        _scope_anchor_key,
        _scope_excluded_values,
    )

    next_start = matches[index + 1].start() if index + 1 < len(matches) else len(clause)
    prev_end = matches[index - 1].end() if index else 0
    after = _negation_after_span(clause, cue, next_start)
    before = _negation_before_span(clause, cue, prev_end)

    if detect_product_brands(after):
        return True
    key, values = _scope_anchor_and_value(after)
    if key is not None and values:
        return True
    if not before:
        return False

    key = _scope_anchor_key(before)
    if key is not None:
        values = _scope_excluded_values(key, after)
        if values:
            return True
    if detect_product_brands(before):
        return True
    if key is not None:
        values = _scope_excluded_values(key, before)
        if values:
            return True
    return False


def _unbound_negation_clauses(message: str) -> list[str]:
    """Return clauses carrying a negation cue that binds to no brand and no
    controlled-attribute exclusion.

    Per-cue target binding with consumed-span accounting: every cue in a clause
    must independently bind (or be covered by the first-cue-inherits-exclusion
    rule), so a bound brand in one position never masks a later negation that
    resolves to nothing (``不要苹果而且贴膜也不要``, ``不要苹果但贴膜也不要``).  The
    first cue in a clause whose message-level exclusion scan already carried a
    scoped exclusion (``屏幕不要非原装的``, ``非原装屏不要``) inherits that bound
    exclusion; every later cue must self-bind.
    """
    from app.llm import _explicit_used_phone_exclusions, _used_phone_clauses

    unbound: list[str] = []
    for clause in _used_phone_clauses(message):
        matches = [
            match
            for match in _CUE_RE.finditer(clause)
            if not (
                match.group(0) == "不要"
                and clause[match.end():].startswith("求")
            )
        ]
        if not matches:
            continue
        clause_has_exclusion = bool(_explicit_used_phone_exclusions(clause))
        for index, cue in enumerate(matches):
            if index == 0 and clause_has_exclusion:
                continue
            if _cue_targets_bound(clause, cue, matches, index):
                continue
            unbound.append(clause)
            break
    return unbound


def _fast_safety_verdict(
    message: str,
    state: Any,
    *,
    explicit: dict[str, Any],
    brand_negations: Any,
    exclusions: dict[str, list[str]],
) -> tuple[list[str], list[str], str]:
    """Unified fail-closed safety scan for one used-phone turn.

    Returns ``(risk_codes, unresolved_cues, ordinal_status)``.  ANY unresolved
    negation target, bare ordinal, demonstrative reference or "换一个" in the
    turn — even alongside a fully-resolved brand/os/screen condition — produces
    a risk code, so every bypass/preview flag ends False and the status is
    unsafe.  Server-bound comparisons (``ordinal_status == "bound"``) are exempt
    from the ordinal scan but never search-preview.
    """
    from app.llm import _ordinal_comparison_binding, _unsupported_used_phone_capability

    ordinal_status, _ordinal_ids = _ordinal_comparison_binding(state, message)
    risk_codes: list[str] = []
    unresolved_cues: list[str] = []

    unbound_negation = _unbound_negation_clauses(message)
    if unbound_negation:
        risk_codes.append("unbound_negation_target")
        unresolved_cues.extend(unbound_negation)

    if brand_negations.negated_brands & frozenset(brand_negations.released_brands):
        risk_codes.append("same_turn_accept_reject")
    if _same_turn_value_conflict(explicit, exclusions):
        risk_codes.append("same_turn_accept_reject")
    if _same_turn_accept_and_reject_same_value(message):
        risk_codes.append("same_turn_accept_reject")

    if ordinal_status in {"ambiguous", "missing_or_out_of_range"}:
        risk_codes.append("unbound_ordinal_reference")
        unresolved_cues.append(ordinal_status)
    references = _unresolved_reference_cues(message, ordinal_status)
    if references:
        risk_codes.append("unbound_ordinal_reference")
        unresolved_cues.extend(references)

    if _unbound_numeric_units(message):
        risk_codes.append("unbound_numeric_unit")
    if _unsupported_used_phone_capability(message) is not None:
        risk_codes.append("unsupported_capability_boundary")

    return risk_codes, unresolved_cues, ordinal_status


def _project_preview_requirements(
    guide: ShoppingGuideState,
    message: str,
) -> list[ShoppingRequirement]:
    """Project the requirements the finalized TaskState will compile.

    Baseline is the persisted state (so multi-turn A/B/C survive); this turn's
    deterministic explicit/exclusion/removal/priority operations overwrite only
    the keys they address, and brand negations are projected through the same
    hard/soft replacement rule the persistence boundary uses.  The projection
    is preview-only and never persisted.
    """
    from app.llm import (
        _explicit_used_phone_exclusions,
        _explicit_used_phone_priority_changes,
        _explicit_used_phone_requirement_removals,
        _explicit_used_phone_requirements,
        parse_brand_negations,
    )

    explicit = _explicit_used_phone_requirements(message)
    exclusions = _explicit_used_phone_exclusions(message)
    removals = _explicit_used_phone_requirement_removals(message)
    priority_changes = _explicit_used_phone_priority_changes(message)
    brand_negations = parse_brand_negations(message)

    by_key = {item.key: item for item in guide.requirements}
    for key in removals:
        by_key.pop(key, None)
    for key, requirement in explicit.items():
        by_key[key] = requirement
    for key, values in exclusions.items():
        by_key[key] = ShoppingRequirement(
            key=key,
            operator="not_in",
            value=values,
            unit="enum",
            priority="hard",
            source="user",
        )
    for key, priority in priority_changes.items():
        current = by_key.get(key)
        if current is not None:
            by_key[key] = current.model_copy(update={"priority": priority})
    requirements = list(by_key.values())

    strength_by_brand: dict[str, str] = {
        brand: avoidance.strength
        for avoidance in guide.brand_avoidances
        for brand in avoidance.values
    }
    for brand in brand_negations.released_brands:
        strength_by_brand.pop(brand, None)
    positive_brand = explicit.get("brand")
    if positive_brand is not None:
        raw = (
            positive_brand.value
            if isinstance(positive_brand.value, list)
            else [positive_brand.value]
        )
        for value in raw:
            strength_by_brand.pop(canonicalize_brand(str(value)), None)
    for target in brand_negations.targets:
        for brand in target.values:
            strength_by_brand[brand] = target.strength
    avoidances = [
        BrandAvoidance(
            values=sorted(
                brand for brand, strength in strength_by_brand.items()
                if strength == level
            ),
            strength=level,
            source="user",
        )
        for level in ("hard", "soft")
        if any(strength == level for strength in strength_by_brand.values())
    ]
    projected = guide.model_copy(update={
        "requirements": requirements,
        "brand_avoidances": avoidances,
    })
    return compiled_shopping_requirements(projected)


def _coerce_shopping_requirements(
    requirements: list[ShoppingRequirement],
) -> list[ShoppingRequirement]:
    """Rebuild every requirement against this module's ShoppingRequirement class.

    The demo Agent is launched as ``uvicorn agent.app.main:app`` while modules
    use absolute ``app.*`` imports, so ``app`` and ``agent.app`` can exist as two
    module trees (same physical files, distinct module objects) and ``app.llm``
    yields a *sibling* ``ShoppingRequirement`` class object that pydantic rejects
    with a ``model_type`` error.  Instances that are not this module's class are
    re-validated from their ``model_dump()`` wire values; wire semantics are
    preserved, class identity is canonicalised.  Read-only — no mutation.
    """
    rebuilt: list[ShoppingRequirement] = []
    for requirement in requirements:
        if isinstance(requirement, ShoppingRequirement):
            rebuilt.append(requirement)
        else:
            rebuilt.append(ShoppingRequirement.model_validate(requirement.model_dump()))
    return rebuilt


def analyze_used_phone_fast(
    message: str,
    state: Any | None,
) -> FastAnalysis:
    """Return a read-only fast analysis for a used-phone shopping turn.

    ``state`` is the active ``TaskState`` (or ``None``).  Every safety gate
    fail-closes: unbound negation/ordinal/number, same-turn accept+reject,
    unsupported capability boundaries, category/task switches and identity
    uncertainty all suppress the bypass and the preview.
    """
    if state is None or getattr(state, "task_type", None) != "ecommerce_guide":
        return FastAnalysis(status="not_applicable", route="fast_analyzer", reason="not_ecommerce_task")

    raw_guide = (state.domain_state or {}).get("shoppingGuide")
    if not isinstance(raw_guide, dict):
        return FastAnalysis(status="not_applicable", route="fast_analyzer", reason="no_shopping_guide")
    try:
        guide = ShoppingGuideState.model_validate(raw_guide)
    except Exception:
        return FastAnalysis(status="not_applicable", route="fast_analyzer", reason="unparseable_shopping_guide")
    if guide.category != "phone":
        return FastAnalysis(status="not_applicable", route="fast_analyzer", reason="non_phone_category")

    normalized = re.sub(r"\s+", "", message).strip("。！!？?")
    compact = normalized
    if _CANCEL_EXPRESSION_RE.fullmatch(compact):
        return FastAnalysis(status="not_applicable", route="fast_analyzer", reason="cancel_expression")
    if _NEW_TASK_EXPRESSION_RE.search(compact):
        return FastAnalysis(status="not_applicable", route="fast_analyzer", reason="explicit_new_task")

    requested_category = detect_product_category(message)
    if requested_category not in {None, "phone"}:
        return FastAnalysis(
            status="unsafe",
            route="fast_analyzer",
            reason="category_switch",
            risk_codes=["category_switch"],
            allow_task_manager_bypass=False,
            allow_task_state_bypass=False,
            allow_product_preview=False,
        )

    # Lazy import keeps the module import graph acyclic.
    from app.llm import (
        _deterministic_used_phone_task_state_decision,
        _explicit_used_phone_exclusions,
        _explicit_used_phone_priority_changes,
        _explicit_used_phone_requirement_removals,
        _explicit_used_phone_requirements,
        _explicit_used_phone_retained_requirements,
        _nonbinding_used_phone_acceptance_mentions,
        _nonbinding_used_phone_ambiguous_mentions,
        _used_phone_controlled_mentions,
        _used_phone_text_claim_discovery,
        parse_brand_negations,
    )

    explicit = _explicit_used_phone_requirements(message)
    brand_negations = parse_brand_negations(message)
    exclusions = _explicit_used_phone_exclusions(message)
    removals = _explicit_used_phone_requirement_removals(message)
    priority_changes = _explicit_used_phone_priority_changes(message)
    retained = _explicit_used_phone_retained_requirements(message)
    mentioned_keys = _used_phone_controlled_mentions(message)
    covered_keys = (
        set(explicit)
        | set(exclusions)
        | set(removals)
        | set(priority_changes)
        | retained
        | _nonbinding_used_phone_acceptance_mentions(message)
        | _nonbinding_used_phone_ambiguous_mentions(message)
    ) & set(USED_PHONE_ATTRIBUTE_REGISTRY)
    if (
        "brand" in explicit
        or "brand" in removals
        or brand_negations.targets
        or brand_negations.released_brands
    ):
        mentioned_keys.add("brand")
        covered_keys.add("brand")
    text_claim = _used_phone_text_claim_discovery(message)

    risk_codes, unresolved_cues, ordinal_status = _fast_safety_verdict(
        message,
        state,
        explicit=explicit,
        brand_negations=brand_negations,
        exclusions=exclusions,
    )
    if risk_codes:
        return _unsafe_analysis(
            message,
            reason="safety_gate_fail_closed",
            risk_codes=risk_codes,
            mentioned_keys=mentioned_keys,
            covered_keys=covered_keys,
            unresolved_cues=unresolved_cues,
        )

    arguments, extraction_observation = _deterministic_used_phone_task_state_decision(
        state,
        message,
    )
    reason = (
        (extraction_observation or {}).get("reason")
        if isinstance(extraction_observation, dict)
        else None
    )
    route = (
        (extraction_observation or {}).get("route")
        if isinstance(extraction_observation, dict)
        else None
    )

    use_cases = list(guide.use_cases)
    if text_claim is not None and text_claim not in use_cases:
        use_cases.append(text_claim)

    this_turn_requirements: list[ShoppingRequirement] = []
    for requirement in explicit.values():
        this_turn_requirements.append(requirement)
    for key, values in exclusions.items():
        this_turn_requirements.append(ShoppingRequirement(
            key=key, operator="not_in", value=values, unit="enum",
            priority="hard", source="user",
        ))
    for target in brand_negations.targets:
        this_turn_requirements.append(ShoppingRequirement(
            key="brand", operator="not_in", value=list(target.values),
            unit="text", priority=target.strength, source="user",
        ))

    preview_requirements = _project_preview_requirements(guide, message)

    # Cross-tree class-identity guard: the demo's dual ``app``/``agent.app``
    # import layout can hand us sibling-class ShoppingRequirement instances
    # (e.g. the os=android soft requirement from ``app.llm``).  Rebuild every
    # requirement against this module's class before FastAnalysis construction,
    # otherwise pydantic rejects the list with a model_type error and the whole
    # SSE stream fails closed on an otherwise full-controlled turn.
    this_turn_requirements = _coerce_shopping_requirements(this_turn_requirements)
    preview_requirements = _coerce_shopping_requirements(preview_requirements)

    if arguments is not None:
        if arguments.get("status") != "ready":
            return _unsafe_analysis(
                message,
                reason="deterministic_clarification",
                risk_codes=["identity_uncertain"],
                mentioned_keys=mentioned_keys,
                covered_keys=covered_keys,
            )
        # A bound comparison resolves server-owned Validator ids
        # (``_boundComparedIds`` → ``compare_products``); it must never re-run a
        # product search for a provisional preview, and it carries no new
        # requirements to preview.
        is_compare = (
            ordinal_status == "bound"
            or (
                (arguments.get("domainStatePatch") or {})
                .get("shoppingGuide") or {}
            ).get("mode") == "compare"
        )
        return FastAnalysis(
            status="full",
            route=str(route or "deterministic_complete"),
            reason=str(reason or "complete_controlled_coverage"),
            mentioned_keys=sorted(mentioned_keys),
            covered_keys=sorted(covered_keys),
            uncovered_keys=sorted(mentioned_keys - covered_keys),
            requirements=this_turn_requirements,
            use_cases=use_cases,
            allow_task_manager_bypass=True,
            allow_task_state_bypass=True,
            allow_product_preview=not is_compare,
            preview_requirements=[] if is_compare else preview_requirements,
        )

    if mentioned_keys or covered_keys:
        return FastAnalysis(
            status="safe_partial",
            route=str(route or "fast_analyzer"),
            reason=str(reason or "partial_or_unknown_semantics"),
            mentioned_keys=sorted(mentioned_keys),
            covered_keys=sorted(covered_keys),
            uncovered_keys=sorted(mentioned_keys - covered_keys),
            requirements=this_turn_requirements,
            use_cases=use_cases,
            allow_task_manager_bypass=True,
            allow_task_state_bypass=False,
            allow_product_preview=True,
            preview_requirements=preview_requirements,
        )

    return _no_used_phone_signal(message, mentioned_keys, covered_keys)


def catalog_revision() -> str:
    """Return a stable token for the catalog/dataset identity the preview cache
    keys on.  Any setting that changes catalog facts, ranking or evidence rules
    automatically invalidates cached previews."""
    payload = {
        "retrievalMode": settings.product_retrieval_mode,
        "vectorBackend": settings.product_vector_backend,
        "syntheticPricePolicy": settings.used_phone_synthetic_price_policy,
        "syntheticPriceDir": settings.used_phone_synthetic_price_dir,
        "rankingContract": TWO_STAGE_RANKING_CONTRACT_VERSION,
        "attributeRuleset": used_phone_attribute_ruleset_sha256(),
        "dataset": settings.used_phone_fast_preview_catalog_revision,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _requirement_identity(requirement: ShoppingRequirement) -> tuple[str, ...]:
    value = tuple(requirement.value) if isinstance(requirement.value, list) else requirement.value
    return (
        requirement.key,
        requirement.operator,
        requirement.unit,
        requirement.priority,
        str(value),
        requirement.source,
    )


def build_preview_cache_key(
    *,
    catalog_revision: str,
    query: str,
    use_cases: list[str] | None = None,
    requirements: list[ShoppingRequirement] | None = None,
) -> str:
    """Return the exact cache identity for a provisional preview.

    Sensitive to catalog revision, normalized Query/useCase and the full merged
    requirement identity (key/operator/value/priority/source).  Different
    hard/soft, operators or values never share a key.
    """
    payload = {
        "revision": catalog_revision,
        "query": re.sub(r"\s+", "", query or "").casefold(),
        "useCases": sorted(set(use_cases or [])),
        "requirements": sorted(
            _requirement_identity(requirement)
            for requirement in (requirements or [])
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class PreviewCandidateCache:
    """Bounded, thread-safe, deep-copy cache for normalized preview candidates.

    Only normalized candidates/traces are stored — never final natural-language
    answers.  Entries are invalidated automatically when the catalog revision
    changes, and can be cleared explicitly or purged to a target revision.
    """

    def __init__(self, max_entries: int = 256) -> None:
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.RLock()
        self._entries: dict[str, tuple[str, Any]] = {}
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                self._misses += 1
                return None
            revision, value = item
            if revision != catalog_revision():
                self._entries.pop(key, None)
                self._misses += 1
                return None
            self._hits += 1
            return deepcopy(value)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._entries[key] = (catalog_revision(), deepcopy(value))
            while len(self._entries) > self._max_entries:
                self._entries.pop(next(iter(self._entries)))

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def invalidate_catalog(self, revision: str | None = None) -> int:
        target = revision or catalog_revision()
        with self._lock:
            before = len(self._entries)
            self._entries = {
                key: item for key, item in self._entries.items()
                if item[0] == target
            }
            return before - len(self._entries)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                "maxEntries": self._max_entries,
                "size": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
            }


_preview_cache: PreviewCandidateCache | None = None
_preview_cache_lock = threading.Lock()


def get_preview_candidate_cache() -> PreviewCandidateCache:
    global _preview_cache
    if _preview_cache is None:
        with _preview_cache_lock:
            if _preview_cache is None:
                _preview_cache = PreviewCandidateCache(
                    max_entries=settings.used_phone_fast_preview_cache_max_entries
                )
    return _preview_cache


def set_preview_candidate_cache(cache: PreviewCandidateCache | None) -> None:
    global _preview_cache
    with _preview_cache_lock:
        _preview_cache = cache


def _preview_product_card(product: dict[str, Any]) -> dict[str, Any]:
    product_id = product.get("id")
    return {
        "id": str(product_id) if type(product_id) in {int, str} else None,
        "title": product.get("title"),
        "brand": product.get("brand"),
        "snapshotPriceMinor": (
            product.get("snapshotPriceMinor")
            if product.get("priceStatus") == "verified" else None
        ),
        "syntheticReferencePriceMinor": (
            product.get("syntheticReferencePriceMinor")
            if product.get("priceStatus") == "synthetic" else None
        ),
        "currency": product.get("currency"),
        "priceStatus": product.get("priceStatus"),
        "priceDataNature": product.get("priceDataNature"),
        "pricePolicy": product.get("pricePolicy"),
        "priceDisclosure": product.get("priceDisclosure"),
    }


def _preview_attributes(facts: Any) -> list[dict[str, Any]]:
    specifications = (
        facts.get("specifications") if isinstance(facts, dict) else None
    )
    if not isinstance(specifications, dict):
        return []
    attributes: list[dict[str, Any]] = []
    for key, value in specifications.items():
        if value is None or value == "未知":
            continue
        attributes.append({"key": key, "value": value, "status": "known"})
    return attributes


def build_provisional_guide_result(
    candidates: list[dict[str, Any]] | None,
    *,
    limit: int = 3,
) -> dict[str, Any]:
    """Project only rule-level ``full_match`` Top-3 candidates into a preview.

    Candidates that fail or leave a hard requirement unknown are never shown as
    ``full_match``; when nothing is trustworthy the preview carries zero product
    cards and lets the caller render the state-only notice.
    """
    top = [
        row
        for row in (candidates or [])
        if row.get("selectionType") == "full_match"
    ][:max(1, int(limit))]
    products: list[dict[str, Any]] = []
    for row in top:
        product = row.get("product") if isinstance(row.get("product"), dict) else row
        products.append({
            "product": _preview_product_card(product),
            "attributes": _preview_attributes(row.get("facts")),
            "selectionType": "full_match",
            "evidenceRefs": [
                ref for ref in (row.get("evidenceRefs") or [])
                if isinstance(ref, str)
            ],
        })
    return {
        "contractVersion": "provisional-product-preview-v1",
        "status": "provisional",
        "category": "phone",
        "products": products,
        "hasCompleteMatch": bool(products),
        "rankedItemCount": len(candidates or []),
        "snapshotNotice": "初步匹配，最终结果可能调整。",
    }


__all__ = [
    "FastAnalysis",
    "FastPreviewStatus",
    "PreviewCandidateCache",
    "_fast_safety_verdict",
    "analyze_used_phone_fast",
    "build_preview_cache_key",
    "build_provisional_guide_result",
    "catalog_revision",
    "get_preview_candidate_cache",
    "set_preview_candidate_cache",
]
