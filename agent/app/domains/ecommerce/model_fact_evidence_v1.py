"""Small, manually verified development slice of normalized model-level facts.

Facts remain model-level observations under the named publisher protocol.  They
must not be joined to physical-device condition, and cross-review comparisons
remain diagnostic because browser/test details can differ across review dates.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .model_evidence_v1 import TitleClaimObservationV1, normalize_title_model_claim_v1


class StructuredModelFactV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["used-phone-model-fact-v1"] = Field(
        default="used-phone-model-fact-v1", alias="schemaVersion"
    )
    fact_id: str = Field(alias="factId", min_length=1)
    canonical_model_claim: str = Field(alias="canonicalModelClaim", min_length=3)
    fact_key: Literal["wifi_websurfing_runtime_minutes"] = Field(alias="factKey")
    value: int = Field(gt=0)
    unit: Literal["minute"] = "minute"
    fact_authority: Literal["INDEPENDENT_MODEL_TEST"] = Field(alias="factAuthority")
    publisher: Literal["Notebookcheck"] = "Notebookcheck"
    source_locator: str = Field(alias="sourceLocator", pattern=r"^https://www\.notebookcheck\.(?:net|org)/")
    source_section: Literal["Battery Runtime / WiFi Websurfing"] = Field(alias="sourceSection")
    license_profile: Literal["SHORT_QUOTATION_REFERENCE_ONLY"] = Field(alias="licenseProfile")
    protocol_family: Literal["NOTEBOOKCHECK_WIFI_V1_3"] = Field(alias="protocolFamily")
    browser: str | None = None
    test_brightness_cd_m2: int | None = Field(default=None, alias="testBrightnessCdM2", gt=0)
    comparison_scope: Literal[
        "DIAGNOSTIC_SAME_PUBLISHER_PROTOCOL_FAMILY"
    ] = Field(alias="comparisonScope")
    listing_identity_verified: Literal[False] = Field(alias="listingIdentityVerified")
    production_ranking_authorized: Literal[False] = Field(alias="productionRankingAuthorized")
    observed_at: str = Field(alias="observedAt", pattern=r"^\d{4}-\d{2}-\d{2}$")


class FrozenModelFactRegistryV1:
    def __init__(self, *, facts: tuple[StructuredModelFactV1, ...], source_sha256: str) -> None:
        self.facts = facts
        self.source_sha256 = source_sha256
        self._by_claim: dict[str, tuple[StructuredModelFactV1, ...]] = {}
        for fact in facts:
            self._by_claim[fact.canonical_model_claim] = (
                *self._by_claim.get(fact.canonical_model_claim, ()),
                fact,
            )

    @classmethod
    def from_jsonl(
        cls, path: str | Path, *, expected_sha256: str | None = None
    ) -> "FrozenModelFactRegistryV1":
        source = Path(path).read_bytes()
        digest = hashlib.sha256(source).hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ValueError("model fact registry hash mismatch")
        facts = tuple(
            StructuredModelFactV1.model_validate(json.loads(line))
            for line in source.decode("utf-8").splitlines()
            if line.strip()
        )
        if not facts:
            raise ValueError("model fact registry must not be empty")
        fact_ids = [fact.fact_id for fact in facts]
        keys = [(fact.canonical_model_claim, fact.fact_key) for fact in facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("duplicate factId")
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate model fact key")
        return cls(facts=facts, source_sha256=digest)

    def facts_for(self, canonical_model_claim: str) -> tuple[StructuredModelFactV1, ...]:
        return self._by_claim.get(canonical_model_claim, ())


class ModelFactResolutionV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["used-phone-model-fact-resolution-v1"] = Field(
        default="used-phone-model-fact-resolution-v1", alias="schemaVersion"
    )
    observation: TitleClaimObservationV1
    status: Literal["FACTS_AVAILABLE", "UNKNOWN", "CONFLICT"]
    facts: tuple[StructuredModelFactV1, ...] = ()
    reason_code: str = Field(alias="reasonCode", min_length=1)
    fact_registry_sha256: str = Field(alias="factRegistrySha256", pattern=r"^[0-9a-f]{64}$")
    listing_identity_verified: Literal[False] = Field(
        default=False, alias="listingIdentityVerified"
    )
    production_ranking_authorized: Literal[False] = Field(
        default=False, alias="productionRankingAuthorized"
    )
    network_calls: Literal[0] = Field(default=0, alias="networkCalls")
    model_calls: Literal[0] = Field(default=0, alias="modelCalls")

    @model_validator(mode="after")
    def validate_resolution(self) -> "ModelFactResolutionV1":
        if self.status == "FACTS_AVAILABLE" and not self.facts:
            raise ValueError("FACTS_AVAILABLE requires facts")
        if self.status != "FACTS_AVAILABLE" and self.facts:
            raise ValueError("unresolved model must not expose facts")
        return self


def resolve_model_facts_v1(
    *,
    item_id: str | int,
    title: str,
    catalog_brand: str,
    registry: FrozenModelFactRegistryV1,
) -> ModelFactResolutionV1:
    observation = normalize_title_model_claim_v1(
        item_id=item_id,
        title=title,
        catalog_brand=catalog_brand,
    )
    if observation.status == "TITLE_CLAIM_AMBIGUOUS":
        return ModelFactResolutionV1(
            observation=observation,
            status="CONFLICT",
            reasonCode="TITLE_MODEL_AMBIGUOUS",
            factRegistrySha256=registry.source_sha256,
        )
    if observation.status == "TITLE_CLAIM_UNRESOLVED":
        return ModelFactResolutionV1(
            observation=observation,
            status="UNKNOWN",
            reasonCode="TITLE_MODEL_UNRESOLVED",
            factRegistrySha256=registry.source_sha256,
        )
    canonical = observation.claims[0].canonical_model_claim
    facts = registry.facts_for(canonical)
    return ModelFactResolutionV1(
        observation=observation,
        status="FACTS_AVAILABLE" if facts else "UNKNOWN",
        facts=facts,
        reasonCode="MODEL_FACTS_FOUND" if facts else "MODEL_FACTS_NOT_IN_DEV_REGISTRY",
        factRegistrySha256=registry.source_sha256,
    )


class DiagnosticFactComparisonV1(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid", frozen=True)

    schema_version: Literal["used-phone-model-fact-comparison-v1"] = Field(
        default="used-phone-model-fact-comparison-v1", alias="schemaVersion"
    )
    fact_key: Literal["wifi_websurfing_runtime_minutes"] = Field(alias="factKey")
    ordered_model_claims: tuple[str, ...] = Field(alias="orderedModelClaims", min_length=2)
    values_by_model: dict[str, int] = Field(alias="valuesByModel")
    comparison_scope: Literal[
        "DIAGNOSTIC_SAME_PUBLISHER_PROTOCOL_FAMILY"
    ] = Field(alias="comparisonScope")
    production_ranking_authorized: Literal[False] = Field(
        default=False, alias="productionRankingAuthorized"
    )
    caveat: Literal[
        "Browser version, review date, firmware and test-device state may differ."
    ] = "Browser version, review date, firmware and test-device state may differ."


def compare_wifi_runtime_diagnostic_v1(
    facts: tuple[StructuredModelFactV1, ...],
) -> DiagnosticFactComparisonV1:
    if len(facts) < 2:
        raise ValueError("at least two facts are required")
    if any(fact.fact_key != "wifi_websurfing_runtime_minutes" for fact in facts):
        raise ValueError("facts must share the WiFi runtime key")
    if len({fact.canonical_model_claim for fact in facts}) != len(facts):
        raise ValueError("one fact per model is required")
    ordered = tuple(
        fact.canonical_model_claim
        for fact in sorted(facts, key=lambda item: (-item.value, item.canonical_model_claim))
    )
    return DiagnosticFactComparisonV1(
        factKey="wifi_websurfing_runtime_minutes",
        orderedModelClaims=ordered,
        valuesByModel={fact.canonical_model_claim: fact.value for fact in facts},
        comparisonScope="DIAGNOSTIC_SAME_PUBLISHER_PROTOCOL_FAMILY",
    )


__all__ = [
    "DiagnosticFactComparisonV1",
    "FrozenModelFactRegistryV1",
    "ModelFactResolutionV1",
    "StructuredModelFactV1",
    "compare_wifi_runtime_diagnostic_v1",
    "resolve_model_facts_v1",
]
