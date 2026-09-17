"""Exact-token observations for controlled used-phone evidence.

The current production contract mirrors the frozen seven-field v2 evidence
ruleset.  It never infers facts from titles, brands, sellers, substrings, token
positions, or unbound percentages.  The explicit v1 exports keep the already
frozen three-field builder reproducible after production moves to v2.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping

ObservationStatus = Literal["known", "conflict", "unknown"]
RequirementOperator = Literal["eq", "in", "not_in"]

USED_PHONE_ATTRIBUTE_RULESET_VERSION_V1 = "used-phone-controlled-alias-v1"
USED_PHONE_ATTRIBUTE_RULESET_VERSION = "used-phone-exact-token-seven-field-v2"
USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD = "relevance.attr_value"


@dataclass(frozen=True)
class UsedPhoneAttributeSpec:
    """Public requirement metadata for one controlled interpretation."""

    key: str
    type: Literal["enum"]
    unit: Literal["enum"]
    operators: tuple[RequirementOperator, ...]
    allowed_values: tuple[str, ...]
    semantic_status: Literal["controlled_interpretation_not_source_ground_truth"]


@dataclass(frozen=True)
class UsedPhoneAttributeFact:
    key: str
    value: str
    type: Literal["enum"] = "enum"
    unit: Literal["enum"] = "enum"


@dataclass(frozen=True)
class UsedPhoneAttributeObservation:
    key: str
    status: ObservationStatus
    fact: UsedPhoneAttributeFact | None
    matched_raw_tokens: tuple[str, ...]


_SEMANTIC_STATUS = "controlled_interpretation_not_source_ground_truth"

# Canonical value -> exact source spellings.  This is intentionally shaped like
# the frozen v2 manifest so its canonical payload can be hash-compared directly.
_CANONICAL_ALIASES_V2: Mapping[str, Mapping[str, tuple[str, ...]]] = (
    MappingProxyType(
        {
            "battery_health": MappingProxyType(
                {
                    "lt70": ("70%以下",),
                    "70_80": ("70%-80%",),
                    "80_90": ("80%-90%",),
                    "90_plus": ("90%+",),
                }
            ),
            "battery_originality": MappingProxyType(
                {
                    "original": ("原装电池",),
                    "non_original": ("非原装电池",),
                }
            ),
            "motherboard_repair": MappingProxyType(
                {
                    "not_repaired": ("主板未维修",),
                    "repaired": ("主板有过维修",),
                }
            ),
            "os": MappingProxyType(
                {
                    "android": ("Android/安卓", "安卓"),
                    "ios": ("iOS",),
                }
            ),
            "scratch_level": MappingProxyType(
                {
                    "none": ("无划痕",),
                    "light": ("轻微划痕",),
                    "obvious": ("明显划痕",),
                }
            ),
            "screen_originality": MappingProxyType(
                {
                    "original": ("原装屏",),
                    "non_original": ("非原装内屏/外屏",),
                }
            ),
            "shell_condition": MappingProxyType(
                {
                    "normal": ("外壳正常",),
                    "damaged": ("外壳有磕碰", "外壳有缺失"),
                }
            ),
        }
    )
)

_V1_FIELD_KEYS = ("os", "battery_health", "screen_originality")


def _spec(key: str, values: tuple[str, ...]) -> UsedPhoneAttributeSpec:
    return UsedPhoneAttributeSpec(
        key=key,
        type="enum",
        unit="enum",
        # eq is the existing scalar API form; in/not_in are the lowercase
        # runtime projections of frozen benchmark IN/NOT_IN atoms.
        operators=("eq", "in", "not_in"),
        allowed_values=values,
        semantic_status=_SEMANTIC_STATUS,
    )


USED_PHONE_ATTRIBUTE_REGISTRY: Mapping[str, UsedPhoneAttributeSpec] = MappingProxyType(
    {
        "os": _spec("os", ("ios", "android")),
        "battery_health": _spec(
            "battery_health", ("lt70", "70_80", "80_90", "90_plus")
        ),
        "screen_originality": _spec(
            "screen_originality", ("original", "non_original")
        ),
        "motherboard_repair": _spec(
            "motherboard_repair", ("not_repaired", "repaired")
        ),
        "battery_originality": _spec(
            "battery_originality", ("original", "non_original")
        ),
        "scratch_level": _spec("scratch_level", ("none", "light", "obvious")),
        "shell_condition": _spec("shell_condition", ("normal", "damaged")),
    }
)

# The legacy registry is an explicit immutable snapshot, not a view over the
# current registry.  The v1 builder must continue emitting its original hashes.
USED_PHONE_ATTRIBUTE_REGISTRY_V1: Mapping[str, UsedPhoneAttributeSpec] = (
    MappingProxyType(
        {
            key: UsedPhoneAttributeSpec(
                key=key,
                type="enum",
                unit="enum",
                operators=("eq",),
                allowed_values=USED_PHONE_ATTRIBUTE_REGISTRY[key].allowed_values,
                semantic_status=_SEMANTIC_STATUS,
            )
            for key in _V1_FIELD_KEYS
        }
    )
)


def _normalized_token(token: str) -> str:
    return re.sub(r"\s+", " ", token).strip().casefold()


def _alias_index(
    canonical_aliases: Mapping[str, Mapping[str, tuple[str, ...]]],
) -> Mapping[str, Mapping[str, str]]:
    return MappingProxyType(
        {
            key: MappingProxyType(
                {
                    _normalized_token(alias): canonical
                    for canonical, aliases in values.items()
                    for alias in aliases
                }
            )
            for key, values in canonical_aliases.items()
        }
    )


_ALIASES_V2 = _alias_index(_CANONICAL_ALIASES_V2)
_ALIASES_V1: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {key: _ALIASES_V2[key] for key in _V1_FIELD_KEYS}
)


def _split_raw_tokens(raw_value: str | None) -> tuple[tuple[str, str], ...]:
    if not raw_value:
        return ()
    tokens = []
    for raw_token in re.split(r"[,，]", raw_value):
        stripped = raw_token.strip()
        normalized = _normalized_token(stripped)
        if normalized:
            tokens.append((normalized, stripped))
    return tuple(tokens)


def _observe(
    raw_value: str | None,
    *,
    registry: Mapping[str, UsedPhoneAttributeSpec],
    aliases_by_key: Mapping[str, Mapping[str, str]],
) -> Mapping[str, UsedPhoneAttributeObservation]:
    raw_tokens = _split_raw_tokens(raw_value)
    observations: dict[str, UsedPhoneAttributeObservation] = {}

    for key in sorted(registry):
        aliases = aliases_by_key[key]
        matches = tuple(
            sorted(
                (
                    (aliases[normalized], normalized, raw_token)
                    for normalized, raw_token in raw_tokens
                    if normalized in aliases
                ),
                key=lambda item: (item[0], item[1], item[2]),
            )
        )
        values = {canonical for canonical, _, _ in matches}
        matched_raw_tokens = tuple(raw_token for _, _, raw_token in matches)

        if len(values) == 1:
            value = next(iter(values))
            observation = UsedPhoneAttributeObservation(
                key=key,
                status="known",
                fact=UsedPhoneAttributeFact(key=key, value=value),
                matched_raw_tokens=matched_raw_tokens,
            )
        elif len(values) > 1:
            observation = UsedPhoneAttributeObservation(
                key=key,
                status="conflict",
                fact=None,
                matched_raw_tokens=matched_raw_tokens,
            )
        else:
            observation = UsedPhoneAttributeObservation(
                key=key,
                status="unknown",
                fact=None,
                matched_raw_tokens=(),
            )
        observations[key] = observation

    return MappingProxyType(observations)


def observe_used_phone_attributes(
    raw_value: str | None,
) -> Mapping[str, UsedPhoneAttributeObservation]:
    """Observe all seven current fields from complete comma-delimited tokens."""

    return _observe(
        raw_value,
        registry=USED_PHONE_ATTRIBUTE_REGISTRY,
        aliases_by_key=_ALIASES_V2,
    )


def observe_used_phone_attributes_v1(
    raw_value: str | None,
) -> Mapping[str, UsedPhoneAttributeObservation]:
    """Observe the immutable three-field v1 contract for legacy rebuilds."""

    return _observe(
        raw_value,
        registry=USED_PHONE_ATTRIBUTE_REGISTRY_V1,
        aliases_by_key=_ALIASES_V1,
    )


def materialize_used_phone_product_attributes(
    raw_value: str | None,
) -> tuple[dict[str, object], ...]:
    """Create Java product-snapshot rows without upgrading unknowns to facts.

    Known values receive a normalized enum.  Conflicts retain their raw source
    row with a null normalized value so downstream code can re-observe the
    conflict.  Pure unknown fields create no synthetic attribute row.
    """

    if not isinstance(raw_value, str) or not raw_value.strip():
        return ()
    rows: list[dict[str, object]] = []
    for key, observation in observe_used_phone_attributes(raw_value).items():
        if observation.status == "unknown":
            continue
        normalized = observation.fact.value if observation.fact is not None else None
        rows.append(
            {
                "key": key,
                "valueType": "enum",
                "rawValue": raw_value,
                "normalizedText": normalized,
                "normalizedNumber": None,
                "normalizedBoolean": None,
                "unit": "enum",
                "evidenceField": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
                "extractionMethod": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
                "confidence": 1.0 if normalized is not None else 0.0,
            }
        )
    return tuple(rows)


def used_phone_attribute_ruleset_payload() -> dict[str, object]:
    """Return the exact canonical evidence payload pinned by frozen v2."""

    return {
        "excludedFields": ["condition_grade", "region"],
        "fields": {
            key: {
                "aliases": {
                    canonical: list(aliases)
                    for canonical, aliases in sorted(values.items())
                },
                "allowedValues": sorted(values),
                "operators": ["IN", "NOT_IN"],
                "semanticStatus": _SEMANTIC_STATUS,
            }
            for key, values in sorted(_CANONICAL_ALIASES_V2.items())
        },
        "matching": "exact_comma_delimited_token_casefold_whitespace_normalized",
        "titleBrandSellerUsedForFact": False,
        "unboundPercentageUsedForFact": False,
        "version": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
    }


def used_phone_attribute_ruleset_payload_v1() -> dict[str, object]:
    """Return the byte-compatible payload used by the frozen v1 builder."""

    return {
        "version": USED_PHONE_ATTRIBUTE_RULESET_VERSION_V1,
        "matching": "exact_comma_delimited_token_casefold_whitespace_normalized",
        "titleUsedForFact": False,
        "brandUsedForFact": False,
        "fields": {
            key: {
                "type": spec.type,
                "unit": spec.unit,
                "operators": list(spec.operators),
                "allowedValues": list(spec.allowed_values),
                "semanticStatus": spec.semantic_status,
                "aliases": dict(sorted(_ALIASES_V1[key].items())),
            }
            for key, spec in sorted(USED_PHONE_ATTRIBUTE_REGISTRY_V1.items())
        },
    }


def _payload_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def used_phone_attribute_ruleset_sha256() -> str:
    return _payload_sha256(used_phone_attribute_ruleset_payload())


def used_phone_attribute_ruleset_sha256_v1() -> str:
    return _payload_sha256(used_phone_attribute_ruleset_payload_v1())
