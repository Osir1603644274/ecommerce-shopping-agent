"""Deterministic used-phone model evidence routing.

This module deliberately does *not* browse the web, copy third-party page
content, or verify the physical identity of a listing.  It turns an untrusted
seller-title model claim into bounded reference locators from a frozen registry
and fails closed for listing-specific condition claims.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator


TITLE_CLAIM_RULESET_VERSION = "used-phone-title-model-claim-rules-v2"
MODEL_EVIDENCE_POLICY_VERSION = "used-phone-model-evidence-policy-v1"

TitleClaimStatus = Literal[
    "TITLE_CLAIM_SINGLE",
    "TITLE_CLAIM_AMBIGUOUS",
    "TITLE_CLAIM_UNRESOLVED",
]
EvidenceGapKey = Literal[
    "official_model_reference",
    "independent_performance_reference",
    "physical_device_condition",
]
EvidenceResolutionStatus = Literal["REFERENCE_AVAILABLE", "UNKNOWN", "CONFLICT"]
EvidenceAuthority = Literal[
    "SELLER_TITLE_CLAIM_ONLY",
    "OFFICIAL_MODEL_REFERENCE",
    "INDEPENDENT_MODEL_TEST_REFERENCE",
    "NO_TRUSTED_DEVICE_BINDING",
]


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


@dataclass(frozen=True)
class _Rule:
    family: str
    rule_id: str
    pattern: re.Pattern[str]


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# This ruleset is intentionally the same conservative ruleset used by the
# sealed Phase 1B V2 title-claim audit.  It extracts seller claims, not truth.
_RULES: tuple[_Rule, ...] = (
    _Rule("apple", "apple-iphone", _rx(r"(?:iphone|苹果)\s*(?P<model>(?:1[1-6])\s*(?:pro\s*max|promax|pro|plus|mini|迷你|pm|p)?|x\s*s\s*max|xsmax|x\s*s|xs|x\s*r|xr|x|se\s*[123]?|[6-8]\s*(?:s\s*plus|sp|s|plus|p)?)")),
    _Rule("huawei", "huawei-mate", _rx(r"(?P<model>mate\s*\d{1,3}\s*(?:rs|pro\s*\+|pro\+|pro|e)?)")),
    _Rule("huawei", "huawei-pura", _rx(r"(?P<model>pura\s*\d{1,3}\s*(?:pro\s*\+|pro\+|pro|ultra)?)")),
    _Rule("huawei", "huawei-nova", _rx(r"(?P<model>nova\s*\d{1,3}\s*(?:pro|se|e)?)")),
    _Rule("huawei", "huawei-enjoy", _rx(r"(?P<model>畅享\s*\d{1,3}\s*(?:plus|pro|se|s|x)?)")),
    _Rule("huawei", "huawei-maimang", _rx(r"(?P<model>麦芒\s*\d{1,3}\s*(?:plus|pro)?)")),
    _Rule("huawei", "huawei-p-series", _rx(r"(?<![a-z0-9])(?P<model>p\s*[2-9]\d\s*(?:pro\s*\+|pro\+|pro|lite)?)")),
    _Rule("redmi", "redmi-note", _rx(r"(?:redmi|红米)?\s*(?P<model>note\s*\d{1,2}\s*(?:t\s*pro|pro\s*\+|pro\+|pro|t|r|se)?)")),
    _Rule("redmi", "redmi-k", _rx(r"(?:redmi|红米|小米红米|小米)?\s*(?P<model>k\s*\d{2}\s*(?:pro|ultra|至尊版|游戏增强版)?)")),
    _Rule("redmi", "redmi-number", _rx(r"(?:redmi|红米)\s*(?P<model>\d{1,2}\s*(?:pro|c|r|x)?)")),
    _Rule("xiaomi", "xiaomi-mix", _rx(r"(?P<model>mix\s*(?:fold\s*)?\d{1,2})")),
    _Rule("xiaomi", "xiaomi-number", _rx(r"(?:xiaomi|小米)\s*(?P<model>\d{1,2}\s*(?:ultra|pro|lite|s|x)?)")),
    _Rule("vivo", "vivo-series", _rx(r"vivo\s*(?P<model>[xysvz]\s*\d{1,3}\s*(?:pro\s*\+|pro\+|pro|e|i|s)?)")),
    _Rule("iqoo", "iqoo-neo", _rx(r"iqoo\s*(?P<model>neo\s*\d{1,2}\s*(?:pro|se|竞速版)?)")),
    _Rule("iqoo", "iqoo-z-u-number", _rx(r"iqoo\s*(?P<model>(?:z|u)\s*\d{1,2}\s*(?:turbo\s*\+|turbo\+|turbo|x)?|\d{1,2}\s*(?:pro|s)?)")),
    _Rule("oppo", "oppo-find", _rx(r"oppo\s*(?P<model>find\s*x\s*\d{1,2}\s*(?:ultra|pro)?)")),
    _Rule("oppo", "oppo-reno", _rx(r"oppo\s*(?P<model>reno\s*\d{1,2}\s*(?:pro\s*\+|pro\+|pro)?)")),
    _Rule("oppo", "oppo-a-k-r", _rx(r"oppo\s*(?P<model>[akr]\s*\d{1,3}\s*[a-z]?)")),
    _Rule("honor", "honor-magic", _rx(r"(?:honor|荣耀)\s*(?P<model>magic\s*(?:v\s*\d?|vs\s*\d?|flip|\d{1,2})\s*(?:pro|至臻版)?)")),
    _Rule("honor", "honor-x-play-enjoy", _rx(r"(?:honor|荣耀)\s*(?P<model>x\s*\d{1,3}\s*(?:pro|gt)?|play\s*\d{1,2}|畅玩\s*\d{1,3}\s*(?:plus)?)")),
    _Rule("honor", "honor-number", _rx(r"(?:honor|荣耀)\s*(?P<model>\d{1,3}\s*(?:pro|se|青春版|lite|i)?)")),
    _Rule("samsung", "samsung-fold-flip", _rx(r"(?:samsung|三星|galaxy)?\s*(?P<model>(?:galaxy\s*)?z\s*(?:fold|flip|filp)\s*\d{1,2})")),
    _Rule("samsung", "samsung-s-note-a-w", _rx(r"(?:samsung|三星|galaxy)\s*(?P<model>(?:galaxy\s*)?(?:s|note|a|w)\s*\d{1,3}\s*(?:ultra|fe|\+|flip)?)")),
    _Rule("oneplus", "oneplus-number", _rx(r"(?:oneplus|一加)\s*(?P<model>\d{1,2}\s*(?:pro|t|r)?)")),
    _Rule("nubia", "nubia-series", _rx(r"(?:nubia|努比亚)\s*(?P<model>(?:z|redmagic|红魔)\s*\d{1,3}\s*(?:pro|s)?)")),
    _Rule("realme", "realme-series", _rx(r"(?:realme|真我)\s*(?P<model>(?:gt|q|v)\s*\d{1,3}\s*(?:pro|neo|s)?)")),
    _Rule("blackshark", "blackshark-number", _rx(r"(?:黑鲨|black\s*shark)\s*(?P<model>\d{1,2}\s*(?:pro|s)?)")),
)


_FAMILY_CONTEXT_RULES: dict[str, tuple[_Rule, ...]] = {
    "apple": (
        _Rule("apple", "apple-context-variant", _rx(
            r"(?<![a-z0-9])(?P<model>(?:1[1-6])\s*(?:pro\s*max|promax|pro|plus|mini|迷你|pm|p)|x\s*s\s*max|xsmax|x\s*s|xs|x\s*r|xr)(?![a-z0-9])"
        )),
    ),
    "oppo": (
        _Rule("oppo", "oppo-context-model", _rx(
            r"(?<![a-z0-9])(?P<model>find\s*x\s*\d{1,2}\s*(?:ultra|pro)?|reno\s*\d{1,2}\s*(?:pro\s*\+|pro\+|pro)?|[akr]\s*\d{1,3}\s*[a-z]?)(?![a-z0-9])"
        )),
    ),
    "vivo": (
        _Rule("vivo", "vivo-context-model", _rx(r"(?<![a-z0-9])(?P<model>[xysvz]\s*\d{2,3}\s*(?:pro\s*\+|pro\+|pro|e|i|s)?)(?![a-z0-9])")),
    ),
    "iqoo": (
        _Rule("iqoo", "iqoo-context-model", _rx(
            r"(?<![a-z0-9])(?P<model>neo\s*\d{1,2}\s*(?:pro|se|竞速版)?|(?:z|u)\s*\d{1,2}\s*(?:turbo\s*\+|turbo\+|turbo|x)?)(?![a-z0-9])"
        )),
    ),
    "honor": (
        _Rule("honor", "honor-context-model", _rx(
            r"(?<![a-z0-9])(?P<model>magic\s*(?:v\s*\d?|vs\s*\d?|flip|\d{1,2})\s*(?:pro|至臻版)?|x\s*\d{1,3}\s*(?:pro|gt)?|play\s*\d{1,2}|畅玩\s*\d{1,3}\s*(?:plus)?)(?![a-z0-9])"
        )),
    ),
    "xiaomi": (
        _Rule("xiaomi", "xiaomi-context-qualified-model", _rx(r"(?<![a-z0-9])(?P<model>\d{1,2}\s*(?:ultra|pro|lite))(?![a-z0-9])")),
    ),
    "samsung": (
        _Rule("samsung", "samsung-context-model", _rx(
            r"(?<![a-z0-9])(?P<model>(?:galaxy\s*)?z\s*(?:fold|flip|filp)\s*\d{1,2}|(?:s|note|a|w)\s*\d{1,3}\s*(?:ultra|fe|\+|flip)?)(?![a-z0-9])"
        )),
    ),
}

_APPLE_PRO_SLASH_MAX = _rx(r"(?:iphone|苹果)?\s*(?P<number>1[1-6])\s*pro\s*[/／]\s*max")


def _compact_model(raw: str) -> str:
    value = raw.lower()
    for old, new in {
        "至尊版": "ultra",
        "竞速版": "racing",
        "游戏增强版": "gaming",
        "青春版": "youth",
        "至臻版": "ultimate",
        "迷你": "mini",
        "filp": "flip",
    }.items():
        value = value.replace(old, new)
    return re.sub(r"[\s_\-/·•]+", "", value)


def catalog_brand_family_v1(brand: str) -> str:
    value = brand.lower()
    checks = (
        ("apple", ("apple", "苹果", "iphone")),
        ("huawei", ("huawei", "华为", "畅享")),
        ("redmi", ("redmi", "红米", "hongmi")),
        ("xiaomi", ("xiaomi", "小米", "mi")),
        ("iqoo", ("iqoo",)),
        ("vivo", ("vivo",)),
        ("oppo", ("oppo", "鸥铂")),
        ("honor", ("honor", "荣耀")),
        ("samsung", ("samsung", "三星")),
        ("oneplus", ("oneplus", "一加")),
        ("nubia", ("nubia", "努比亚")),
        ("realme", ("realme", "真我")),
        ("blackshark", ("黑鲨", "black shark")),
    )
    for family, aliases in checks:
        if any(alias in value for alias in aliases):
            return family
    return "unknown"


def _extract_claims(title: str, catalog_family: str) -> list[dict[str, Any]]:
    claims: dict[str, dict[str, Any]] = {}
    for rule in _RULES + _FAMILY_CONTEXT_RULES.get(catalog_family, ()):
        for match in rule.pattern.finditer(title):
            raw = match.group("model")
            canonical = f"{rule.family}:{_compact_model(raw)}"
            observation = {
                "canonicalModelClaim": canonical,
                "family": rule.family,
                "matchedText": match.group(0),
                "modelText": raw,
                "ruleId": rule.rule_id,
                "span": (match.start(), match.end()),
            }
            existing = claims.get(canonical)
            if existing is None or observation["span"] < existing["span"]:
                claims[canonical] = observation

    if catalog_family == "apple":
        for match in _APPLE_PRO_SLASH_MAX.finditer(title):
            number = match.group("number")
            for suffix in ("pro", "promax"):
                canonical = f"apple:{number}{suffix}"
                claims[canonical] = {
                    "canonicalModelClaim": canonical,
                    "family": "apple",
                    "matchedText": match.group(0),
                    "modelText": f"{number}{suffix}",
                    "ruleId": "apple-pro-slash-max-alternatives",
                    "span": (match.start(), match.end()),
                }

    observations = list(claims.values())
    drop: set[str] = set()
    for shorter in observations:
        short_id = str(shorter["canonicalModelClaim"])
        short_span = tuple(shorter["span"])
        for longer in observations:
            long_id = str(longer["canonicalModelClaim"])
            long_span = tuple(longer["span"])
            if short_id == long_id or shorter["family"] != longer["family"]:
                continue
            if short_span == long_span:
                continue
            overlaps = short_span[0] < long_span[1] and long_span[0] < short_span[1]
            specializes = long_id.startswith(short_id) and len(long_id) > len(short_id)
            if overlaps and specializes and (long_span[1] - long_span[0]) > (short_span[1] - short_span[0]):
                drop.add(short_id)
    for canonical in drop:
        claims.pop(canonical, None)
    return sorted(
        claims.values(), key=lambda row: (row["canonicalModelClaim"], row["span"])
    )


class ModelClaimV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    canonical_model_claim: str = Field(alias="canonicalModelClaim", min_length=3)
    family: str = Field(min_length=1)
    matched_text: str = Field(alias="matchedText", min_length=1)
    model_text: str = Field(alias="modelText", min_length=1)
    rule_id: str = Field(alias="ruleId", min_length=1)
    span: tuple[int, int]

    @model_validator(mode="after")
    def validate_claim(self) -> "ModelClaimV1":
        if not self.canonical_model_claim.startswith(f"{self.family}:"):
            raise ValueError("canonicalModelClaim family mismatch")
        if self.span[0] < 0 or self.span[1] <= self.span[0]:
            raise ValueError("invalid claim span")
        return self


class TitleClaimObservationV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["title-claim-observation-v1"] = Field(
        default="title-claim-observation-v1", alias="schemaVersion"
    )
    ruleset_version: Literal["used-phone-title-model-claim-rules-v2"] = Field(
        default=TITLE_CLAIM_RULESET_VERSION, alias="rulesetVersion"
    )
    item_id: str = Field(alias="itemId", min_length=1)
    title: str = Field(min_length=1)
    title_sha256: str = Field(alias="titleSha256", pattern=r"^[0-9a-f]{64}$")
    catalog_brand: str = Field(alias="catalogBrand")
    catalog_brand_family: str = Field(alias="catalogBrandFamily", min_length=1)
    status: TitleClaimStatus
    authority: Literal["SELLER_TITLE_CLAIM_ONLY"] = "SELLER_TITLE_CLAIM_ONLY"
    listing_identity_verified: Literal[False] = Field(
        default=False, alias="listingIdentityVerified"
    )
    claims: tuple[ModelClaimV1, ...]

    @model_validator(mode="after")
    def validate_status(self) -> "TitleClaimObservationV1":
        expected = (
            "TITLE_CLAIM_UNRESOLVED"
            if not self.claims
            else "TITLE_CLAIM_SINGLE"
            if len(self.claims) == 1
            else "TITLE_CLAIM_AMBIGUOUS"
        )
        if self.status != expected:
            raise ValueError("title claim status does not match claims")
        return self


def normalize_title_model_claim_v1(
    *, item_id: str | int, title: str, catalog_brand: str
) -> TitleClaimObservationV1:
    normalized_title = title.strip()
    if not normalized_title:
        raise ValueError("title must not be blank")
    family = catalog_brand_family_v1(catalog_brand)
    claims = tuple(ModelClaimV1.model_validate(row) for row in _extract_claims(normalized_title, family))
    status: TitleClaimStatus = (
        "TITLE_CLAIM_UNRESOLVED"
        if not claims
        else "TITLE_CLAIM_SINGLE"
        if len(claims) == 1
        else "TITLE_CLAIM_AMBIGUOUS"
    )
    return TitleClaimObservationV1(
        itemId=str(item_id),
        title=normalized_title,
        titleSha256=_sha256_bytes(normalized_title.encode("utf-8")),
        catalogBrand=catalog_brand,
        catalogBrandFamily=family,
        status=status,
        claims=claims,
    )


class ModelSourceLocatorV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    source_role: Literal["OFFICIAL", "INDEPENDENT_TEST"] = Field(alias="sourceRole")
    status: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")
    stable_document_id: str = Field(alias="stableDocumentId", min_length=1)
    license_profile: str = Field(alias="licenseProfile", min_length=1)
    last_verified_date: str = Field(alias="lastVerifiedDate", pattern=r"^\d{4}-\d{2}-\d{2}$")
    automated_extraction_authorized: Literal[False] = Field(
        default=False, alias="automatedExtractionAuthorized"
    )
    snapshot_redistribution_authorized: Literal[False] = Field(
        default=False, alias="snapshotRedistributionAuthorized"
    )


@dataclass(frozen=True)
class ModelReferenceRecordV1:
    canonical_model_claim: str
    family: str
    official: ModelSourceLocatorV1 | None
    independent: tuple[ModelSourceLocatorV1, ...]
    pilot_binding_sha256: str


def _reject_copied_content(value: Any, *, path: str = "root") -> None:
    forbidden = {"body", "content", "html", "quote", "rawtext", "pagecontent"}
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold().replace("_", "") in forbidden:
                raise ValueError(f"copied third-party content is forbidden at {path}.{key}")
            _reject_copied_content(nested, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_copied_content(nested, path=f"{path}[{index}]")


class FrozenModelEvidenceRegistryV1:
    """Validated URL-only registry built from a frozen audit artifact."""

    def __init__(
        self,
        *,
        records: dict[str, ModelReferenceRecordV1],
        source_sha256: str,
        pilot_binding_sha256: str,
    ) -> None:
        self._records = dict(records)
        self.source_sha256 = source_sha256
        self.pilot_binding_sha256 = pilot_binding_sha256

    @classmethod
    def from_jsonl(
        cls, path: str | Path, *, expected_sha256: str | None = None
    ) -> "FrozenModelEvidenceRegistryV1":
        source_path = Path(path)
        source_bytes = source_path.read_bytes()
        source_sha256 = _sha256_bytes(source_bytes)
        if expected_sha256 is not None and source_sha256 != expected_sha256:
            raise ValueError("model evidence registry hash mismatch")

        records: dict[str, ModelReferenceRecordV1] = {}
        bindings: set[str] = set()
        for line_number, raw_line in enumerate(source_bytes.decode("utf-8").splitlines(), start=1):
            if not raw_line.strip():
                continue
            raw = json.loads(raw_line)
            _reject_copied_content(raw, path=f"line[{line_number}]")
            if raw.get("schemaVersion") != "phase1b-source-coverage-row-v2":
                raise ValueError("unsupported source coverage schema")
            if raw.get("listingIdentityVerified") is not False:
                raise ValueError("registry must not assert listing identity")
            if raw.get("trustedPhysicalDeviceJoinKeyAvailable") is not False:
                raise ValueError("registry must not contain a trusted device join")
            if raw.get("agentEligible") is not False:
                raise ValueError("registry must not elevate locator rows to Agent tasks")

            canonical = str(raw["canonicalModelClaim"])
            if canonical in records:
                raise ValueError("duplicate canonical model claim")
            binding = str(raw["pilotBindingSha256"])
            if not re.fullmatch(r"[0-9a-f]{64}", binding):
                raise ValueError("invalid pilot binding")
            bindings.add(binding)

            official_raw = raw.get("official") or {}
            official = None
            if official_raw.get("url"):
                official = ModelSourceLocatorV1(
                    provider=official_raw["provider"],
                    publisher=official_raw["publisher"],
                    sourceRole="OFFICIAL",
                    status=official_raw["status"],
                    url=official_raw["url"],
                    stableDocumentId=official_raw["stableDocumentId"],
                    licenseProfile=official_raw["licenseProfile"],
                    lastVerifiedDate=official_raw["lastVerifiedDate"],
                )
            independent = tuple(
                ModelSourceLocatorV1(
                    provider=item["provider"],
                    publisher=item["publisher"],
                    sourceRole="INDEPENDENT_TEST",
                    status=item["status"],
                    url=item["url"],
                    stableDocumentId=item["stableDocumentId"],
                    licenseProfile=item["licenseProfile"],
                    lastVerifiedDate=item["lastVerifiedDate"],
                )
                for item in (raw.get("independentPerformance") or [])
                if item.get("url")
            )
            records[canonical] = ModelReferenceRecordV1(
                canonical_model_claim=canonical,
                family=str(raw["family"]),
                official=official,
                independent=independent,
                pilot_binding_sha256=binding,
            )
        if not records or len(bindings) != 1:
            raise ValueError("registry must contain one non-empty frozen pilot binding")
        return cls(
            records=records,
            source_sha256=source_sha256,
            pilot_binding_sha256=next(iter(bindings)),
        )

    def lookup(self, canonical_model_claim: str) -> ModelReferenceRecordV1 | None:
        return self._records.get(canonical_model_claim)

    @property
    def record_count(self) -> int:
        return len(self._records)


class ModelEvidenceFindingV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    evidence_gap_key: EvidenceGapKey = Field(alias="evidenceGapKey")
    status: EvidenceResolutionStatus
    authority: EvidenceAuthority
    canonical_model_claim: str | None = Field(default=None, alias="canonicalModelClaim")
    source_locators: tuple[ModelSourceLocatorV1, ...] = Field(
        default=(), alias="sourceLocators"
    )
    reason_code: str = Field(alias="reasonCode", min_length=1)
    can_prove: tuple[str, ...] = Field(default=(), alias="canProve")
    cannot_prove: tuple[str, ...] = Field(default=(), alias="cannotProve")

    @model_validator(mode="after")
    def validate_locator_status(self) -> "ModelEvidenceFindingV1":
        if self.status == "REFERENCE_AVAILABLE" and not self.source_locators:
            raise ValueError("REFERENCE_AVAILABLE requires a source locator")
        if self.evidence_gap_key == "physical_device_condition" and self.status != "UNKNOWN":
            raise ValueError("physical device condition must fail closed")
        return self


class ModelEvidenceResultV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["used-phone-model-evidence-result-v1"] = Field(
        default="used-phone-model-evidence-result-v1", alias="schemaVersion"
    )
    policy_version: Literal["used-phone-model-evidence-policy-v1"] = Field(
        default=MODEL_EVIDENCE_POLICY_VERSION, alias="policyVersion"
    )
    observation: TitleClaimObservationV1
    findings: tuple[ModelEvidenceFindingV1, ...]
    registry_sha256: str = Field(alias="registrySha256", pattern=r"^[0-9a-f]{64}$")
    pilot_binding_sha256: str = Field(alias="pilotBindingSha256", pattern=r"^[0-9a-f]{64}$")
    trace: tuple[str, ...]
    network_calls: Literal[0] = Field(default=0, alias="networkCalls")
    model_calls: Literal[0] = Field(default=0, alias="modelCalls")
    stop_reason: str = Field(alias="stopReason", min_length=1)


_PHYSICAL_CANNOT_PROVE = (
    "physical_listing_identity",
    "battery_health",
    "repair_history",
    "part_originality",
    "device_specific_performance",
)


def resolve_model_evidence_v1(
    *,
    item_id: str | int,
    title: str,
    catalog_brand: str,
    evidence_gap_keys: Sequence[EvidenceGapKey],
    registry: FrozenModelEvidenceRegistryV1,
) -> ModelEvidenceResultV1:
    requested = tuple(dict.fromkeys(evidence_gap_keys))
    if not requested:
        raise ValueError("evidence_gap_keys must not be empty")
    observation = normalize_title_model_claim_v1(
        item_id=item_id,
        title=title,
        catalog_brand=catalog_brand,
    )
    trace = ["normalize_seller_title_claim"]
    claim = (
        observation.claims[0].canonical_model_claim
        if observation.status == "TITLE_CLAIM_SINGLE"
        else None
    )
    record = registry.lookup(claim) if claim is not None else None
    trace.append("lookup_frozen_model_reference_registry")

    findings: list[ModelEvidenceFindingV1] = []
    for gap in requested:
        if gap == "physical_device_condition":
            findings.append(ModelEvidenceFindingV1(
                evidenceGapKey=gap,
                status="UNKNOWN",
                authority="NO_TRUSTED_DEVICE_BINDING",
                canonicalModelClaim=claim,
                reasonCode="TRUSTED_DEVICE_JOIN_KEY_ABSENT",
                cannotProve=_PHYSICAL_CANNOT_PROVE,
            ))
            continue
        if observation.status == "TITLE_CLAIM_UNRESOLVED":
            findings.append(ModelEvidenceFindingV1(
                evidenceGapKey=gap,
                status="UNKNOWN",
                authority="SELLER_TITLE_CLAIM_ONLY",
                reasonCode="TITLE_MODEL_UNRESOLVED",
                cannotProve=_PHYSICAL_CANNOT_PROVE,
            ))
            continue
        if observation.status == "TITLE_CLAIM_AMBIGUOUS":
            findings.append(ModelEvidenceFindingV1(
                evidenceGapKey=gap,
                status="CONFLICT",
                authority="SELLER_TITLE_CLAIM_ONLY",
                reasonCode="TITLE_MODEL_AMBIGUOUS",
                cannotProve=_PHYSICAL_CANNOT_PROVE,
            ))
            continue
        if record is None:
            findings.append(ModelEvidenceFindingV1(
                evidenceGapKey=gap,
                status="UNKNOWN",
                authority="SELLER_TITLE_CLAIM_ONLY",
                canonicalModelClaim=claim,
                reasonCode="MODEL_NOT_IN_FROZEN_REGISTRY",
                cannotProve=_PHYSICAL_CANNOT_PROVE,
            ))
            continue

        if gap == "official_model_reference":
            source = record.official
            if source is not None and source.status == "VERIFIED_EXACT_MODEL":
                findings.append(ModelEvidenceFindingV1(
                    evidenceGapKey=gap,
                    status="REFERENCE_AVAILABLE",
                    authority="OFFICIAL_MODEL_REFERENCE",
                    canonicalModelClaim=claim,
                    sourceLocators=(source,),
                    reasonCode="EXACT_OFFICIAL_MODEL_LOCATOR",
                    canProve=("official_reference_for_seller_stated_model",),
                    cannotProve=_PHYSICAL_CANNOT_PROVE,
                ))
            else:
                findings.append(ModelEvidenceFindingV1(
                    evidenceGapKey=gap,
                    status="UNKNOWN",
                    authority="SELLER_TITLE_CLAIM_ONLY",
                    canonicalModelClaim=claim,
                    reasonCode="EXACT_OFFICIAL_MODEL_LOCATOR_ABSENT",
                    cannotProve=_PHYSICAL_CANNOT_PROVE,
                ))
            continue

        controlled = tuple(
            locator
            for locator in record.independent
            if locator.status == "OWN_CONTROLLED_TEST"
        )
        if controlled:
            findings.append(ModelEvidenceFindingV1(
                evidenceGapKey=gap,
                status="REFERENCE_AVAILABLE",
                authority="INDEPENDENT_MODEL_TEST_REFERENCE",
                canonicalModelClaim=claim,
                sourceLocators=controlled,
                reasonCode="CONTROLLED_MODEL_TEST_LOCATOR_AVAILABLE",
                canProve=("publisher_test_reference_for_seller_stated_model",),
                cannotProve=_PHYSICAL_CANNOT_PROVE,
            ))
        else:
            findings.append(ModelEvidenceFindingV1(
                evidenceGapKey=gap,
                status="UNKNOWN",
                authority="SELLER_TITLE_CLAIM_ONLY",
                canonicalModelClaim=claim,
                reasonCode="CONTROLLED_MODEL_TEST_LOCATOR_ABSENT",
                cannotProve=_PHYSICAL_CANNOT_PROVE,
            ))

    if observation.status == "TITLE_CLAIM_AMBIGUOUS":
        stop_reason = "TITLE_CONFLICT"
    elif any(item.status == "REFERENCE_AVAILABLE" for item in findings):
        stop_reason = "COMPLETE_REFERENCE_ROUTING"
    else:
        stop_reason = "FAIL_CLOSED_UNKNOWN"
    trace.append("apply_authority_and_license_guard")
    trace.append("stop_without_network_or_model")
    return ModelEvidenceResultV1(
        observation=observation,
        findings=tuple(findings),
        registrySha256=registry.source_sha256,
        pilotBindingSha256=registry.pilot_binding_sha256,
        trace=tuple(trace),
        stopReason=stop_reason,
    )


__all__ = [
    "EvidenceGapKey",
    "FrozenModelEvidenceRegistryV1",
    "MODEL_EVIDENCE_POLICY_VERSION",
    "ModelClaimV1",
    "ModelEvidenceFindingV1",
    "ModelEvidenceResultV1",
    "ModelSourceLocatorV1",
    "TITLE_CLAIM_RULESET_VERSION",
    "TitleClaimObservationV1",
    "catalog_brand_family_v1",
    "normalize_title_model_claim_v1",
    "resolve_model_evidence_v1",
]
