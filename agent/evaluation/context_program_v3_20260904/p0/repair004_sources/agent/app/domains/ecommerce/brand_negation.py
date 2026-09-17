"""Pure, deterministic parsing for explicitly negated product brands.

This module deliberately stops before TaskState mutation or retrieval.  It
answers one narrow question: which explicitly named brands are controlled by a
negative cue, and how strong is that cue?  Callers can then prevent those
brands from being inverted into positive requirements while later phases
decide how to persist or rank negative preferences.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import detect_product_brands, product_brand_aliases


NegationStrength = Literal["hard", "soft"]


class BrandNegationTarget(BaseModel):
    """One negative cue bound to one or more canonical brand identities."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    key: Literal["brand"] = "brand"
    values: list[str] = Field(min_length=1)
    strength: NegationStrength
    cue: str = Field(min_length=1)
    source_text: str = Field(alias="sourceText", min_length=1)


class BrandNegationParseResult(BaseModel):
    """Auditable parser output; unresolved cues must never become positives."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    targets: list[BrandNegationTarget] = Field(default_factory=list)
    unresolved_cues: list[str] = Field(default_factory=list, alias="unresolvedCues")
    guarded_brands: list[str] = Field(default_factory=list, alias="guardedBrands")
    released_brands: list[str] = Field(default_factory=list, alias="releasedBrands")

    @property
    def negated_brands(self) -> frozenset[str]:
        return frozenset(
            value
            for target in self.targets
            for value in target.values
        )


_NEGATION_CUES: tuple[tuple[str, NegationStrength], ...] = (
    ("绝对不要", "hard"),
    ("坚决不要", "hard"),
    ("不能接受", "hard"),
    ("不接受", "hard"),
    ("都不要", "hard"),
    ("别推荐", "hard"),
    ("排除掉", "hard"),
    ("排除", "hard"),
    ("不想要", "hard"),
    ("不考虑", "hard"),
    ("不要", "hard"),
    ("尽量不要", "soft"),
    ("不太想要", "soft"),
    ("都不感兴趣", "soft"),
    ("不太感兴趣", "soft"),
    ("不感兴趣", "soft"),
    ("都不喜欢", "soft"),
    ("不太喜欢", "soft"),
    ("不喜欢", "soft"),
    ("不优先", "soft"),
)

# Longest alternatives must win before an embedded shorter cue (for example,
# ``尽量不要`` before ``不要``).
_CUE_STRENGTH = dict(_NEGATION_CUES)
_CUE_RE = re.compile(
    "|".join(
        re.escape(cue)
        for cue, _strength in sorted(
            _NEGATION_CUES,
            key=lambda item: len(item[0]),
            reverse=True,
        )
    )
)
_CLAUSE_SPLIT_RE = re.compile(
    r"[，,；;。.!！?？]+|(?:但是|不过|而是|只是|但)"
)
_POSITIVE_BOUNDARY_RE = re.compile(
    r"(?:更喜欢|比较喜欢|偏好|优先|想要|要(?!(?:不|排除))|可以|也行)"
)
_NEUTRALIZED_NEGATION_RE = re.compile(
    r"(?:不是|并非)(?:真的)?(?:绝对不要|坚决不要|不要|排除|不喜欢|不感兴趣|不优先)"
)
_ACCEPTANCE_RE = re.compile(r"(?:不介意|可以接受|能接受|也可以|也行)")
_NEGATION_REMOVAL_RE = re.compile(
    r"(?:取消|撤掉|去掉|删掉|解除|不再保留)"
    r"(?:刚才|之前|原来|先前)?(?:的)?"
    r"(?:绝对不要|坚决不要|不要|不想要|排除|不考虑|不喜欢)"
)


def _neutralized_ranges(clause: str) -> list[tuple[int, int]]:
    return [match.span() for match in _NEUTRALIZED_NEGATION_RE.finditer(clause)]


def _inside_ranges(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def _target_span(
    clause: str,
    matches: list[re.Match[str]],
    index: int,
) -> str:
    match = matches[index]
    next_start = matches[index + 1].start() if index + 1 < len(matches) else len(clause)
    after = clause[match.end():next_start]
    boundary = _POSITIVE_BOUNDARY_RE.search(after)
    if boundary is not None:
        after = after[:boundary.start()]
    if detect_product_brands(after):
        return after

    previous_end = matches[index - 1].end() if index else 0
    return clause[previous_end:match.start()]


def parse_brand_negations(message: str) -> BrandNegationParseResult:
    """Bind explicit Chinese negative cues to named brands without guessing.

    Both prefix and postfix forms are supported, including one cue governing
    multiple coordinated brands.  A detected cue with no named brand is
    reported as unresolved so downstream code can clarify instead of silently
    turning a nearby entity into a positive requirement.
    """

    targets: list[BrandNegationTarget] = []
    unresolved: list[str] = []
    guarded: list[str] = []
    released: list[str] = []
    seen: set[tuple[tuple[str, ...], NegationStrength, str, str]] = set()

    for raw_clause in _CLAUSE_SPLIT_RE.split(message):
        clause = re.sub(r"\s+", "", raw_clause).casefold()
        if not clause:
            continue
        neutralized = _neutralized_ranges(clause)
        if neutralized or _ACCEPTANCE_RE.search(clause):
            accepted = detect_product_brands(clause)
            guarded.extend(accepted)
            released.extend(accepted)
        if _NEGATION_REMOVAL_RE.search(clause):
            released_in_clause = detect_product_brands(clause)
            guarded.extend(released_in_clause)
            released.extend(released_in_clause)
            continue

        # ``非苹果`` is a productive Chinese prefix form, not an ordinary cue
        # that can be matched safely as a bare ``非`` (for example, ``非常喜欢``
        # is positive).  Bind it only when ``非`` is immediately followed by a
        # known brand alias.  This keeps the rule deterministic and prevents
        # the positive brand extractor from inverting "non-Apple" into Apple.
        for brand in detect_product_brands(clause):
            if not any(
                re.search(rf"非{re.escape(alias)}(?![a-z0-9])", clause)
                for alias in product_brand_aliases(brand)
            ):
                continue
            guarded.append(brand)
            identity = ((brand,), "hard", "非", clause)
            if identity not in seen:
                seen.add(identity)
                targets.append(BrandNegationTarget(
                    values=[brand],
                    strength="hard",
                    cue="非",
                    sourceText=clause,
                ))
        matches = [
            match
            for match in _CUE_RE.finditer(clause)
            if not _inside_ranges(match.start(), neutralized)
            # ``不要求拍照`` means "photography is not required". The
            # embedded ``不要`` must not become an unresolved brand rejection.
            and not (
                match.group(0) == "不要"
                and clause[match.end():].startswith(("求", "动", "变", "修改", "放宽", "放松", "更改", "调整", "擅自"))
            )
        ]
        for index, match in enumerate(matches):
            cue = match.group(0)
            brands = detect_product_brands(_target_span(clause, matches, index))
            if not brands:
                unresolved.append(cue)
                continue
            guarded.extend(brands)
            strength = _CUE_STRENGTH[cue]
            identity = (tuple(brands), strength, cue, clause)
            if identity in seen:
                continue
            seen.add(identity)
            targets.append(BrandNegationTarget(
                values=brands,
                strength=strength,
                cue=cue,
                sourceText=clause,
            ))

    return BrandNegationParseResult(
        targets=targets,
        unresolvedCues=list(dict.fromkeys(unresolved)),
        guardedBrands=list(dict.fromkeys(guarded)),
        releasedBrands=list(dict.fromkeys(released)),
    )
