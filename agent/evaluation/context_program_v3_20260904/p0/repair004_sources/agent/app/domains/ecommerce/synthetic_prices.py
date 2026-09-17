"""Runtime adapter for the frozen synthetic used-phone price sidecar."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "used-phone-synthetic-reference-price-v1"
RULESET_VERSION = "brand-base-seven-defect-penalties-title-tier-v1"
RULESET_SHA256 = "0e2568c9248dd67fd0d90578b5ee4c0a909c3d011f60d783f8f0a4fcf898a3ad"
EXPECTED_SEED = "used-phone-synthetic-reference-price-v1-seed-20260815"
SYNTHETIC_PRICE_POLICIES = frozenset({"disabled", "display_only", "budget_and_ranking"})
SYNTHETIC_PRICE_DISCLOSURE_ZH = "模拟参考价 / AI 合成，非真实报价"

# Runtime bundles remain explicitly allow-listed.  The 439 bundle preserves the
# frozen 252 rows and applies the same deterministic pricing rules to the 187
# catalog-expansion rows; it is not a market-price snapshot.
SUPPORTED_BUNDLE_IDENTITIES = {
    "used-phone-synthetic-reference-price-v1": {
        "source_sha256": "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50",
        "item_count": 252,
        "manifest_name": "manifest.json",
    },
    "used-phone-synthetic-reference-price-expansion-v1": {
        "source_sha256": "725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75",
        "item_count": 439,
        "manifest_name": "price_manifest.json",
    },
}
SUPPORTED_SOURCE_SHA256 = frozenset(
    identity["source_sha256"] for identity in SUPPORTED_BUNDLE_IDENTITIES.values()
)


class SyntheticPriceRuntimeError(ValueError):
    """Fail-closed runtime sidecar/configuration error."""


@lru_cache(maxsize=4)
def _load_bundle(directory_text: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        directory = Path(directory_text)
        price_manifest_path = directory / "price_manifest.json"
        manifest_path = price_manifest_path if price_manifest_path.is_file() else directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        prices_path = directory / "prices.jsonl"
        digest = hashlib.sha256(prices_path.read_bytes()).hexdigest()
        identity = (
            SUPPORTED_BUNDLE_IDENTITIES.get(str(manifest.get("schemaVersion") or ""))
            if isinstance(manifest, dict)
            else None
        )
        if (
            not isinstance(manifest, dict)
            or identity is None
            or manifest_path.name != identity["manifest_name"]
            or manifest.get("dataNature") != "synthetic"
            or manifest.get("priceStatus") != "synthetic"
            or manifest.get("sourceCatalog", {}).get("sha256") != identity["source_sha256"]
            or manifest.get("ruleset", {}).get("version") != RULESET_VERSION
            or manifest.get("ruleset", {}).get("sha256") != RULESET_SHA256
            or manifest.get("seed") != EXPECTED_SEED
            or manifest.get("prompt", {}).get("used") is not False
            or manifest.get("output", {}).get("sha256") != digest
            or manifest.get("output", {}).get("rowCount") != identity["item_count"]
        ):
            raise SyntheticPriceRuntimeError("synthetic price manifest identity mismatch")
        by_id: dict[str, dict[str, Any]] = {}
        with prices_path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                item_id = str(row.get("itemId") or "") if isinstance(row, dict) else ""
                if (
                    not isinstance(row, dict)
                    or row.get("schemaVersion") != SCHEMA_VERSION
                    or row.get("dataNature") != "synthetic"
                    or row.get("priceStatus") != "synthetic"
                    or type(row.get("referencePriceMinor")) is not int
                    or row["referencePriceMinor"] < 0
                    or not item_id.isascii()
                    or not item_id.isdecimal()
                    or item_id in by_id
                ):
                    raise SyntheticPriceRuntimeError("invalid synthetic price row")
                by_id[item_id] = row
        if len(by_id) != identity["item_count"]:
            raise SyntheticPriceRuntimeError("synthetic price bundle row count mismatch")
        return manifest, by_id
    except (OSError, ValueError) as exc:
        if isinstance(exc, SyntheticPriceRuntimeError):
            raise
        raise SyntheticPriceRuntimeError(str(exc)) from exc


def clear_synthetic_price_cache() -> None:
    _load_bundle.cache_clear()


def synthetic_price_value(product: Mapping[str, Any], *, allow_budget: bool) -> tuple[int | None, dict[str, Any] | None]:
    value = product.get("syntheticReferencePrice")
    if not isinstance(value, Mapping):
        return None, None
    required = {
        "schemaVersion", "referencePriceMinor", "currency", "dataNature",
        "priceStatus", "sourceCatalogSha256", "rulesetVersion", "rulesetSha256",
        "seed", "policy", "labelZh", "disclosureZh",
    }
    if (
        set(value) != required
        or value.get("schemaVersion") != SCHEMA_VERSION
        or value.get("dataNature") != "synthetic"
        or value.get("priceStatus") != "synthetic"
        or value.get("sourceCatalogSha256") not in SUPPORTED_SOURCE_SHA256
        or value.get("rulesetVersion") != RULESET_VERSION
        or value.get("rulesetSha256") != RULESET_SHA256
        or value.get("seed") != EXPECTED_SEED
        or value.get("currency") != "CNY"
        or value.get("disclosureZh") != "AI 合成，非真实报价"
        or value.get("policy") not in SYNTHETIC_PRICE_POLICIES - {"disabled"}
        or type(value.get("referencePriceMinor")) is not int
        or value["referencePriceMinor"] < 0
    ):
        raise SyntheticPriceRuntimeError("invalid synthetic price runtime projection")
    if allow_budget and value.get("policy") != "budget_and_ranking":
        return None, None
    return int(value["referencePriceMinor"]), dict(value)


def apply_synthetic_prices(
    products: list[dict[str, Any]], *, directory: str, policy: str,
) -> list[dict[str, Any]]:
    """Return copied Java rows with an explicit, separately named sidecar.

    ``snapshotPriceMinor`` and Java ``priceStatus`` are never changed.
    """

    if policy not in SYNTHETIC_PRICE_POLICIES:
        raise SyntheticPriceRuntimeError("unknown synthetic price policy")
    if policy == "disabled":
        return [deepcopy(product) for product in products]
    manifest, by_id = _load_bundle(str(Path(directory).resolve()))
    result: list[dict[str, Any]] = []
    for product in products:
        copied = deepcopy(product)
        product_id = copied.get("id")
        item_id = str(product_id) if type(product_id) is int else ""
        row = by_id.get(item_id)
        if row is None:
            raise SyntheticPriceRuntimeError(f"missing synthetic price for product {item_id or '?'}")
        if copied.get("priceStatus") == "verified" and copied.get("snapshotPriceMinor") is not None:
            # A real verified snapshot remains authoritative; the sidecar is not
            # allowed to shadow it even when enabled.
            result.append(copied)
            continue
        copied["syntheticReferencePrice"] = {
            "schemaVersion": SCHEMA_VERSION,
            "referencePriceMinor": row["referencePriceMinor"],
            "currency": row["currency"],
            "dataNature": row["dataNature"],
            "priceStatus": row["priceStatus"],
            "sourceCatalogSha256": manifest["sourceCatalog"]["sha256"],
            "rulesetVersion": manifest["ruleset"]["version"],
            "rulesetSha256": manifest["ruleset"]["sha256"],
            "seed": manifest["seed"],
            "policy": policy,
            "labelZh": row["labelZh"],
            "disclosureZh": row["disclosureZh"],
        }
        result.append(copied)
    return result


def canonical_synthetic_price_value(
    product_id: int,
    *,
    directory: str,
    policy: str,
    allow_budget: bool,
) -> tuple[int | None, dict[str, Any] | None]:
    """Resolve one price from the allow-listed bundle, independent of tool output."""

    if type(product_id) is not int or product_id <= 0:
        raise SyntheticPriceRuntimeError("invalid synthetic price product id")
    projected = apply_synthetic_prices(
        [{"id": product_id, "priceStatus": "unverified", "snapshotPriceMinor": None}],
        directory=directory,
        policy=policy,
    )[0]
    return synthetic_price_value(projected, allow_budget=allow_budget)
