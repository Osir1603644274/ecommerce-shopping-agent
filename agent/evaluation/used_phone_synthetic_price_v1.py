"""Deterministic synthetic reference prices for the frozen used-phone catalog.

The source catalog contains no price observations.  Values produced here are
derived examples, never market observations, verified snapshots, or transaction
prices.  The module is I/O-light so generation and runtime validation share the
same fail-closed contract.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = "used-phone-synthetic-reference-price-v1"
RULESET_VERSION = "brand-base-seven-defect-penalties-title-tier-v1"
DATA_NATURE = "synthetic"
PRICE_STATUS = "synthetic"
CURRENCY = "CNY"
DEFAULT_SEED = "used-phone-synthetic-reference-price-v1-seed-20260815"
EXPECTED_SOURCE_SHA256 = (
    "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50"
)
EXPECTED_ITEM_COUNT = 252

# The brand baseline is the primary positive signal.  Title tiers only scale
# that baseline; the seven controlled attributes never add value.
BRAND_BASE_MINOR = {
    "苹果": 350_000,
    "apple": 350_000,
    "华为": 220_000,
    "huawei": 220_000,
    "三星": 180_000,
    "samsung": 180_000,
    "一加": 160_000,
    "oneplus": 160_000,
    "小米": 140_000,
    "xiaomi": 140_000,
    "荣耀": 130_000,
    "honor": 130_000,
    "oppo": 120_000,
    "vivo": 120_000,
    "魅族": 100_000,
    "meizu": 100_000,
    "红米": 90_000,
    "redmi": 90_000,
    "realme": 90_000,
}
DEFAULT_BRAND_BASE_MINOR = 80_000

TITLE_TIERS: tuple[tuple[str, float, tuple[str, ...]], ...] = (
    ("current_flagship", 1.80, (r"iphone\s*1[56]\s*(pro|max|plus)?", r"mate\s*(6|7|8)0", r"pura\s*70", r"s2[345]\s*(ultra|plus)?", r"find\s*x[78]", r"xiaomi\s*1[45]", r"小米\s*1[45]")),
    ("recent_premium", 1.35, (r"iphone\s*1[234]", r"mate\s*(4|5)0", r"p50", r"s2[012]", r"find\s*x[3-6]", r"xiaomi\s*1[123]", r"小米\s*1[123]")),
    ("mid_generation", 1.00, (r"iphone\s*(x|11)", r"mate\s*(2|3)0", r"p(3|4)0", r"s(9|10|20)", r"xiaomi\s*(9|10)", r"小米\s*(9|10)")),
    ("legacy", 0.58, (r"iphone\s*[4-8]", r"苹果\s*[4-8]", r"mate\s*(7|8|9|10|20)", r"p(8|9|10|20)", r"小米\s*[1-8]")),
)

ATTRIBUTE_PENALTIES: dict[str, dict[str, float]] = {
    "battery_health": {
        "95_100": 0.00, "90_95": 0.03, "80_90": 0.08,
        "70_80": 0.15, "below_70": 0.25,
    },
    "screen_originality": {"original": 0.00, "non_original": 0.18},
    "motherboard_repair": {"not_repaired": 0.00, "repaired": 0.30},
    "battery_originality": {"original": 0.00, "non_original": 0.08},
    "scratch_level": {"none": 0.00, "light": 0.04, "moderate": 0.10, "heavy": 0.18},
    "shell_condition": {"normal": 0.00, "worn": 0.08, "damaged": 0.18, "missing": 0.25},
    "os": {"ios": 0.00, "android": 0.00, "other": 0.00},
}
UNKNOWN_PENALTIES = {
    "battery_health": 0.10,
    "screen_originality": 0.08,
    "motherboard_repair": 0.10,
    "battery_originality": 0.04,
    "scratch_level": 0.04,
    "shell_condition": 0.05,
    "os": 0.02,
}
CONFLICT_PENALTIES = {
    key: min(0.35, value + 0.04) for key, value in UNKNOWN_PENALTIES.items()
}
CRITICAL_PENALTY_THRESHOLD = 0.18
PERTURBATION_FRACTION = 0.04
MIN_PRICE_MINOR = 10_000


class SyntheticPriceContractError(ValueError):
    """Raised when a source or derived price violates the frozen contract."""


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def ruleset_sha256() -> str:
    return hashlib.sha256(canonical_json_bytes(ruleset_payload())).hexdigest()


def ruleset_payload() -> dict[str, Any]:
    return {
        "rulesetVersion": RULESET_VERSION,
        "brandBaseMinor": BRAND_BASE_MINOR,
        "defaultBrandBaseMinor": DEFAULT_BRAND_BASE_MINOR,
        "titleTiers": TITLE_TIERS,
        "attributePenalties": ATTRIBUTE_PENALTIES,
        "unknownPenalties": UNKNOWN_PENALTIES,
        "conflictPenalties": CONFLICT_PENALTIES,
        "criticalPenaltyThreshold": CRITICAL_PENALTY_THRESHOLD,
        "perturbationFraction": PERTURBATION_FRACTION,
        "criticalPositivePerturbationCap": 0.0,
        "minimumPriceMinor": MIN_PRICE_MINOR,
    }


def _brand_baseline(brand: object) -> tuple[int, str]:
    text = str(brand or "").casefold()
    matches = [(len(token), token, value) for token, value in BRAND_BASE_MINOR.items() if token in text]
    if not matches:
        return DEFAULT_BRAND_BASE_MINOR, "default"
    _, token, value = max(matches)
    return value, token


def _title_tier(title: object) -> tuple[str, float, str | None]:
    text = str(title or "").casefold()
    for name, multiplier, patterns in TITLE_TIERS:
        for pattern in patterns:
            if re.search(pattern, text, flags=re.IGNORECASE):
                return name, multiplier, pattern
    return "unclassified", 0.82, None


def _attribute_value(attribute: object) -> tuple[str, object]:
    if not isinstance(attribute, Mapping):
        return "unknown", None
    status = attribute.get("status")
    value = attribute.get("value")
    if value is None:
        fact = attribute.get("fact")
        if isinstance(fact, Mapping):
            value = fact.get("value")
    return str(status or "unknown"), value


def _controlled_penalties(attributes: object) -> tuple[list[dict[str, Any]], float, bool]:
    source = attributes if isinstance(attributes, Mapping) else {}
    rows: list[dict[str, Any]] = []
    total = 0.0
    critical = False
    for key in ATTRIBUTE_PENALTIES:
        status, value = _attribute_value(source.get(key))
        if status == "conflict":
            penalty = CONFLICT_PENALTIES[key]
        elif status != "known" or value not in ATTRIBUTE_PENALTIES[key]:
            penalty = UNKNOWN_PENALTIES[key]
            status, value = "unknown", None
        else:
            penalty = ATTRIBUTE_PENALTIES[key][str(value)]
        total += penalty
        critical = critical or penalty >= CRITICAL_PENALTY_THRESHOLD
        rows.append({"key": key, "status": status, "value": value, "penaltyFraction": penalty})
    return rows, min(total, 0.72), critical


def _perturbation(seed: str, item_id: str, *, critical: bool) -> float:
    raw = hashlib.sha256(f"{seed}:{item_id}".encode("utf-8")).digest()
    unit = int.from_bytes(raw[:8], "big") / ((1 << 64) - 1)
    value = (unit * 2.0 - 1.0) * PERTURBATION_FRACTION
    return min(value, 0.0) if critical else value


def derive_price(catalog_row: Mapping[str, Any], *, seed: str = DEFAULT_SEED) -> dict[str, Any]:
    item_id = str(catalog_row.get("itemId") or "")
    if not item_id.isascii() or not item_id.isdecimal() or str(int(item_id)) != item_id:
        raise SyntheticPriceContractError("catalog itemId is not canonical")
    baseline, brand_rule = _brand_baseline(catalog_row.get("brand"))
    tier, tier_multiplier, tier_pattern = _title_tier(catalog_row.get("title"))
    penalties, penalty_fraction, critical = _controlled_penalties(catalog_row.get("attributes"))
    pre_perturb = max(MIN_PRICE_MINOR, round(baseline * tier_multiplier * (1.0 - penalty_fraction)))
    perturbation = _perturbation(seed, item_id, critical=critical)
    final_minor = max(MIN_PRICE_MINOR, int(round(pre_perturb * (1.0 + perturbation) / 100.0) * 100))
    return {
        "schemaVersion": SCHEMA_VERSION,
        "itemId": item_id,
        "referencePriceMinor": final_minor,
        "currency": CURRENCY,
        "dataNature": DATA_NATURE,
        "priceStatus": PRICE_STATUS,
        "labelZh": "模拟参考价",
        "disclosureZh": "AI 合成，非真实报价",
        "derivation": {
            "brandBaselineMinor": baseline,
            "brandRule": brand_rule,
            "titleTier": tier,
            "titleTierMultiplier": tier_multiplier,
            "titleInferencePattern": tier_pattern,
            "titleInferenceStatus": "synthetic_inference_not_source_fact",
            "attributePenalties": penalties,
            "totalPenaltyFraction": round(penalty_fraction, 6),
            "prePerturbationMinor": pre_perturb,
            "perturbationFraction": round(perturbation, 8),
            "criticalDefectPositivePerturbationCapped": critical,
        },
    }


def load_catalog(path: Path, *, expected_sha256: str = EXPECTED_SOURCE_SHA256) -> list[dict[str, Any]]:
    path = Path(path)
    if sha256_file(path) != expected_sha256:
        raise SyntheticPriceContractError("source catalog SHA-256 mismatch")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise SyntheticPriceContractError(f"non-object catalog row at line {line_number}")
            rows.append(value)
    ids = [str(row.get("itemId") or "") for row in rows]
    if len(rows) != EXPECTED_ITEM_COUNT or len(ids) != len(set(ids)):
        raise SyntheticPriceContractError("source catalog count or identity mismatch")
    return rows


def build_rows(catalog_rows: Iterable[Mapping[str, Any]], *, seed: str = DEFAULT_SEED) -> list[dict[str, Any]]:
    rows = [derive_price(row, seed=seed) for row in catalog_rows]
    rows.sort(key=lambda row: int(row["itemId"]))
    if len(rows) != EXPECTED_ITEM_COUNT:
        raise SyntheticPriceContractError("derived row count mismatch")
    return rows


def validate_derived_row(row: Mapping[str, Any]) -> None:
    required = {
        "schemaVersion", "itemId", "referencePriceMinor", "currency",
        "dataNature", "priceStatus", "labelZh", "disclosureZh", "derivation",
    }
    if set(row) != required:
        raise SyntheticPriceContractError("synthetic price row fields mismatch")
    if (
        row.get("schemaVersion") != SCHEMA_VERSION
        or row.get("dataNature") != DATA_NATURE
        or row.get("priceStatus") != PRICE_STATUS
        or row.get("currency") != CURRENCY
        or type(row.get("referencePriceMinor")) is not int
        or row["referencePriceMinor"] < MIN_PRICE_MINOR
    ):
        raise SyntheticPriceContractError("synthetic price row identity/value mismatch")
    derivation = row.get("derivation")
    if not isinstance(derivation, Mapping) or derivation.get("titleInferenceStatus") != "synthetic_inference_not_source_fact":
        raise SyntheticPriceContractError("synthetic derivation provenance is incomplete")


def validate_bundle(directory: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    prices_path = directory / "prices.jsonl"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SyntheticPriceContractError("manifest must be an object")
    if (
        manifest.get("schemaVersion") != SCHEMA_VERSION
        or manifest.get("dataNature") != DATA_NATURE
        or manifest.get("priceStatus") != PRICE_STATUS
        or manifest.get("sourceCatalog", {}).get("sha256") != EXPECTED_SOURCE_SHA256
        or manifest.get("ruleset", {}).get("sha256") != ruleset_sha256()
        or manifest.get("seed") != DEFAULT_SEED
        or manifest.get("prompt", {}).get("used") is not False
        or manifest.get("output", {}).get("sha256") != sha256_file(prices_path)
        or manifest.get("output", {}).get("rowCount") != EXPECTED_ITEM_COUNT
    ):
        raise SyntheticPriceContractError("synthetic price manifest identity mismatch")
    by_id: dict[str, dict[str, Any]] = {}
    with prices_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            validate_derived_row(row)
            item_id = str(row["itemId"])
            if item_id in by_id:
                raise SyntheticPriceContractError("duplicate synthetic price itemId")
            by_id[item_id] = row
    if len(by_id) != EXPECTED_ITEM_COUNT:
        raise SyntheticPriceContractError("synthetic price bundle row count mismatch")
    return manifest, by_id
