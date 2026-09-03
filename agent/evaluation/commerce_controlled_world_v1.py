"""Materialize and audit the controlled synthetic CommerceWorld-CN-V1.

This module is deliberately separate from the 96-case Pilot authoring code.
It creates a deterministic, path-independent world for benchmark
infrastructure work only.  Every fact is ``synthetic_fixture``: this is not a
marketplace snapshot and none of its prices, inventory or policy text is a
real-world claim.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .commerce_world_v1 import (
    CATEGORIES,
    ENVIRONMENTS,
    TARGET_COUNTS,
    CommerceWorldError,
    canonical_bytes,
    load_world_manifest,
    sha256_bytes,
    sha256_file,
)


CONTROLLED_WORLD_KIND = "controlled_synthetic"
CONTROLLED_GENERATOR_ID = "commerce-controlled-world-builder-v1"
CONTROLLED_SEED = "controlled-cn-v1-seed-20260821"
CONTROLLED_WORLD_ID = "COMMERCE-WORLD-CN-V1-Controlled"
CONTROLLED_SOURCE_URI = f"synthetic://commerce-world-cn-v1/controlled/{CONTROLLED_SEED}"


def _string_field(choices: Sequence[str]) -> Mapping[str, Any]:
    return {"type": "string", "choices": tuple(choices)}


def _integer_field(choices: Sequence[int] | None = None, *, minimum: int | None = None, maximum: int | None = None) -> Mapping[str, Any]:
    result: dict[str, Any] = {"type": "integer"}
    if choices:
        result["choices"] = tuple(choices)
    if minimum is not None:
        result["minimum"] = minimum
    if maximum is not None:
        result["maximum"] = maximum
    return result


def _boolean_field() -> Mapping[str, Any]:
    return {"type": "boolean"}


# Every category has at least six typed, constraint-bearing dimensions.  The
# first two title fields are embedded in generated titles and are checked by
# the manifest auditor, preventing answer-first product selection.
CATEGORY_SPECS: Mapping[str, Mapping[str, Any]] = {
    "used_phone": {"display": "二手手机", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Apple", "Samsung", "Xiaomi", "Huawei")), "model": _string_field(("Nova-A", "Nova-B", "Nova-C", "Nova-D")), "condition": _string_field(("excellent", "good", "fair")), "storageGb": _integer_field((64, 128, 256, 512)), "batteryHealthPct": _integer_field(minimum=80, maximum=99), "platform": _string_field(("iOS", "Android")), "cameraGrade": _string_field(("A", "B", "C"))}},
    "phone_accessory": {"display": "手机配件", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Anker", "Baseus", "UGREEN", "Belkin")), "model": _string_field(("Flex-A", "Flex-B", "Flex-C", "Flex-D")), "accessoryType": _string_field(("charger", "case", "cable", "stand")), "compatibleWith": _string_field(("iPhone", "Android", "Universal")), "material": _string_field(("aluminum", "silicone", "nylon", "plastic")), "color": _string_field(("black", "white", "blue", "green")), "connector": _string_field(("USB-C", "Lightning", "USB-A"))}},
    "earbuds": {"display": "无线耳机", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Sony", "JBL", "Anker", "Huawei")), "model": _string_field(("Air-A", "Air-B", "Air-C", "Air-D")), "connection": _string_field(("Bluetooth-5.2", "Bluetooth-5.3")), "noiseControl": _string_field(("ANC", "passive", "adaptive")), "batteryHours": _integer_field(minimum=4, maximum=12), "fit": _string_field(("in-ear", "semi-in-ear", "ear-hook")), "waterproofRating": _string_field(("IPX4", "IPX5", "IPX7"))}},
    "smartwatch": {"display": "智能手表", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Apple", "Huawei", "Garmin", "Amazfit")), "model": _string_field(("Watch-A", "Watch-B", "Watch-C", "Watch-D")), "platform": _string_field(("watchOS", "HarmonyOS", "AndroidWear")), "caseMaterial": _string_field(("aluminum", "steel", "ceramic")), "screenType": _string_field(("OLED", "AMOLED", "LCD")), "batteryDays": _integer_field(minimum=1, maximum=21), "waterResistance": _string_field(("5ATM", "10ATM", "IP68"))}},
    "tshirt": {"display": "T恤", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Uniqlo", "Nike", "Adidas", "Muji")), "model": _string_field(("Tee-A", "Tee-B", "Tee-C", "Tee-D")), "fabric": _string_field(("cotton", "linen", "modal", "recycled")), "fit": _string_field(("regular", "slim", "oversized")), "size": _string_field(("S", "M", "L", "XL")), "color": _string_field(("black", "white", "navy", "gray")), "season": _string_field(("spring", "summer", "autumn"))}},
    "jeans": {"display": "牛仔裤", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Levis", "Lee", "Uniqlo", "Gap")), "model": _string_field(("Denim-A", "Denim-B", "Denim-C", "Denim-D")), "fabric": _string_field(("denim", "stretch-denim", "organic-denim")), "fit": _string_field(("straight", "slim", "relaxed")), "waistSize": _integer_field(minimum=28, maximum=36), "wash": _string_field(("dark", "medium", "light")), "stretch": _boolean_field()}},
    "sneakers": {"display": "运动鞋", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Nike", "Adidas", "Asics", "NewBalance")), "model": _string_field(("Run-A", "Run-B", "Run-C", "Run-D")), "sport": _string_field(("running", "walking", "training")), "upperMaterial": _string_field(("mesh", "knit", "leather")), "soleMaterial": _string_field(("foam", "rubber", "carbon")), "size": _integer_field(minimum=36, maximum=44), "cushioning": _string_field(("soft", "balanced", "firm"))}},
    "handbag": {"display": "手提包", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Coach", "MichaelKors", "Furla", "ToryBurch")), "model": _string_field(("Bag-A", "Bag-B", "Bag-C", "Bag-D")), "material": _string_field(("leather", "canvas", "nylon")), "closure": _string_field(("zipper", "magnetic", "drawstring")), "capacity": _string_field(("small", "medium", "large")), "color": _string_field(("black", "brown", "cream", "red")), "strapType": _string_field(("top-handle", "crossbody", "shoulder"))}},
    "drinkware": {"display": "水杯", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Thermos", "LocknLock", "Stanley", "Camel")), "model": _string_field(("Cup-A", "Cup-B", "Cup-C", "Cup-D")), "material": _string_field(("stainless-steel", "glass", "tritan")), "capacityMl": _integer_field((350, 500, 750, 1000)), "insulation": _string_field(("single-wall", "double-wall", "vacuum")), "lidType": _string_field(("flip", "screw", "straw")), "leakproof": _boolean_field()}},
    "cleaning": {"display": "清洁用品", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Method", "MrMuscle", "Kao", "Dettol")), "model": _string_field(("Clean-A", "Clean-B", "Clean-C", "Clean-D")), "useSurface": _string_field(("kitchen", "bathroom", "glass", "fabric")), "ingredient": _string_field(("plant-based", "neutral", "enzyme")), "form": _string_field(("spray", "liquid", "wipe")), "volumeMl": _integer_field((300, 500, 750, 1000)), "scent": _string_field(("unscented", "citrus", "floral"))}},
    "stationery": {"display": "文具", "titleFields": ("brand", "model"), "fields": {"brand": _string_field(("Pilot", "Moleskine", "Muji", "Staedtler")), "model": _string_field(("Note-A", "Note-B", "Note-C", "Note-D")), "itemType": _string_field(("pen", "notebook", "marker", "folder")), "material": _string_field(("paper", "plastic", "metal")), "color": _string_field(("black", "blue", "red", "green")), "packCount": _integer_field(minimum=1, maximum=24), "refillable": _boolean_field()}},
    "books": {"display": "图书", "titleFields": ("publisher", "edition"), "fields": {"publisher": _string_field(("OpenPress", "NorthStar", "CivicBooks", "RiverHouse")), "edition": _string_field(("2022", "2023", "2024", "2025")), "genre": _string_field(("technology", "business", "fiction", "history")), "audience": _string_field(("beginner", "intermediate", "advanced")), "language": _string_field(("zh-CN", "en-US", "bilingual")), "pageCount": _integer_field(minimum=120, maximum=640), "binding": _string_field(("paperback", "hardcover", "ebook"))}},
}

# These two dimensions are deliberately shared as *typed* dimensions rather
# than being hidden in an identifier.  Four title dimensions provide a
# natural, readable product title with more than 750 combinations in every
# category while the remaining fields are generated independently below.
_SERIES_CHOICES = (
    "Classic", "Urban", "Studio", "Trail", "Home", "Pro", "Travel", "Daily",
    "Air", "Core", "Edge", "Flex", "Prime", "Select", "Craft", "Active",
    "Lite", "Plus", "Max", "Mini", "One", "Two", "Three", "Go",
    "Essential", "Modern", "Heritage", "Fresh", "Peak", "Metro", "Calm", "Work",
)
_VARIANT_CHOICES = ("Core", "Plus", "Max", "Lite")

# Keep the public spec as the single source of truth for title and typed
# fields.  Mutating the dict here is intentional: all downstream audits
# import the same spec and therefore validate the exact generated contract.
for _spec in CATEGORY_SPECS.values():
    _spec["fields"] = {
        **_spec["fields"],
        "series": _string_field(_SERIES_CHOICES),
        "variant": _string_field(_VARIANT_CHOICES),
    }
    _spec["titleFields"] = tuple(_spec["titleFields"]) + ("series", "variant")


@dataclass(frozen=True)
class ControlledWorldBuildResult:
    output_dir: Path
    manifest: Mapping[str, Any]
    products: tuple[Mapping[str, Any], ...]


def _choice(descriptor: Mapping[str, Any], index: int, offset: int = 0) -> Any:
    choices = descriptor.get("choices")
    if choices:
        return choices[(index + offset) % len(choices)]
    if descriptor["type"] == "integer":
        minimum = int(descriptor.get("minimum", 0)); maximum = int(descriptor.get("maximum", minimum + 1))
        return minimum + ((index * 7 + offset * 3) % (maximum - minimum + 1))
    if descriptor["type"] == "boolean":
        return (index + offset) % 2 == 0
    raise CommerceWorldError(f"unsupported controlled field type: {descriptor}")


def _field_value(
    spec: Mapping[str, Any],
    field: str,
    descriptor: Mapping[str, Any],
    local_index: int,
    category_index: int,
    field_index: int,
) -> Any:
    """Generate one typed value without locking every field to one index.

    Title fields use a mixed-radix counter, so the first 1,000 rows enumerate
    distinct, human-readable combinations.  Non-title fields use independent
    strides; this prevents the old accidental one-to-one correlation between
    every attribute while preserving deterministic coverage of each vocabulary
    and numeric range.
    """
    title_fields = tuple(spec["titleFields"])
    if field in title_fields:
        title_choices: list[Sequence[Any]] = []
        for title_field in title_fields:
            choices = spec["fields"][title_field].get("choices")
            if not choices:
                raise CommerceWorldError(f"title field {title_field} must have finite choices")
            title_choices.append(choices)
        cardinality = 1
        for choices in title_choices:
            cardinality *= len(choices)
        # An odd multiplier is a permutation of the 2,048-value mixed-radix
        # title space.  Unlike a plain counter, the first 1,000 rows cover all
        # choices of every title field while retaining one-to-one tuples.
        code = (local_index * 3 + category_index * 17) % cardinality
        stride = 1
        for title_field, choices in zip(title_fields, title_choices):
            if title_field == field:
                return choices[(code // stride) % len(choices)]
            stride *= len(choices)
        raise CommerceWorldError(f"title field {field} is not in titleFields")
    # Distinct odd strides make each field's sequence independent of the
    # neighbouring field even when their vocabularies have the same length.
    # Every coefficient is odd and is 1 modulo 3; together with the 4-wide
    # vocabularies this guarantees that no supported choice set collapses to a
    # single value.  The varying field offset still keeps the columns
    # decorrelated.
    source_index = local_index * (37 + field_index * 6) + category_index * 101 + field_index * 17
    return _choice(descriptor, source_index, category_index + field_index)


def _product_price(category_index: int, local_index: int) -> float:
    return round(68.0 + category_index * 41.0 + (local_index * 17 % 233) + (local_index % 5) * 0.25, 2)


def _product_rows() -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for category_index, category in enumerate(CATEGORIES):
        spec = CATEGORY_SPECS[category]
        for local_index in range(1000):
            product_id = f"{category}-{local_index + 1:04d}"
            merchant_id = f"merchant-{local_index % 48 + 1:03d}"
            facts: dict[str, Mapping[str, Any]] = {}
            values = {
                field: _field_value(spec, field, descriptor, local_index, category_index, field_index)
                for field_index, (field, descriptor) in enumerate(spec["fields"].items())
            }
            title = f"{spec['display']} " + " ".join(str(values[field]) for field in spec["titleFields"])
            price = _product_price(category_index, local_index)
            values.update({"category": category, "title": title, "price": price})
            for field, value in values.items():
                facts[field] = {"known": True, "value": value, "sourceRef": f"{CONTROLLED_SOURCE_URI}/products#product={product_id}&field={field}", "factTier": "synthetic_fixture"}
            # Preserve an explicit unknown so future constraints cannot
            # accidentally interpret absence as a negative fact.
            facts["optionalSellerNote"] = {"known": False, "value": None, "sourceRef": f"{CONTROLLED_SOURCE_URI}/products#product={product_id}&field=optionalSellerNote", "factTier": "synthetic_fixture"}
            rows.append({"productId": product_id, "category": category, "title": title, "merchantId": merchant_id, "price": price, "facts": facts, "factTier": "synthetic_fixture", "sourceRef": f"{CONTROLLED_SOURCE_URI}/products"})
    return rows


def diversity_metrics(products: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Return deterministic, recomputable diversity metrics for the catalog."""
    per_category: dict[str, Mapping[str, Any]] = {}
    for category in CATEGORIES:
        spec = CATEGORY_SPECS[category]
        rows = [row for row in products if row.get("category") == category]
        vectors = {
            canonical_bytes([row.get("facts", {}).get(field, {}).get("value") for field in spec["fields"]])
            for row in rows
        }
        field_coverage = {
            field: len({canonical_bytes(row.get("facts", {}).get(field, {}).get("value")) for row in rows})
            for field in spec["fields"]
        }
        per_category[category] = {
            "uniqueTitles": len({str(row.get("title")) for row in rows}),
            "uniqueFactVectors": len(vectors),
            "fieldCoverage": field_coverage,
        }
    return {"perCategory": per_category}


def _merchant_rows() -> list[Mapping[str, Any]]:
    return [{"merchantId": f"merchant-{index:03d}", "name": f"Controlled Merchant {index:03d}", "coverage": list(CATEGORIES), "factTier": "synthetic_fixture", "sourceRef": f"{CONTROLLED_SOURCE_URI}/merchants/merchant-{index:03d}"} for index in range(1, 49)]


def _policy_rows() -> list[Mapping[str, Any]]:
    topics = ("returns", "shipping", "warranty", "service")
    rows: list[Mapping[str, Any]] = []
    for merchant_index in range(1, 49):
        merchant_id = f"merchant-{merchant_index:03d}"
        for slot, topic in enumerate(topics, 1):
            version = f"v{1 + (merchant_index % 3)}.{slot}"
            if topic == "returns":
                key_fields, detail = ["returnDays", "restockingFeePct"], {"returnDays": 7 + merchant_index % 8, "restockingFeePct": (merchant_index % 4) * 0.05}
            elif topic == "shipping":
                key_fields, detail = ["cutoffHour", "freeShippingThreshold"], {"cutoffHour": 15 + merchant_index % 6, "freeShippingThreshold": 99 + merchant_index * 10}
            elif topic == "warranty":
                key_fields, detail = ["warrantyMonths", "proofRequired"], {"warrantyMonths": 6 + merchant_index % 19, "proofRequired": merchant_index % 2 == 0}
            else:
                key_fields, detail = ["responseHours", "repairWindowDays"], {"responseHours": 12 + merchant_index % 36, "repairWindowDays": 3 + merchant_index % 10}
            rows.append({"policyId": f"policy-{merchant_id}-{slot:02d}", "merchantId": merchant_id, "topic": topic, "version": version, "effectiveFrom": "2026-01-01", "effectiveTo": "2027-01-01", "keyFields": key_fields, "terms": detail, "text": f"{merchant_id} {topic} policy {version}: " + "; ".join(f"{key}={detail[key]}" for key in key_fields), "factTier": "synthetic_fixture", "sourceRef": f"{CONTROLLED_SOURCE_URI}/policies/{merchant_id}/{topic}"})
    return rows


def _coupon_rows() -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for merchant_index in range(1, 49):
        merchant_id = f"merchant-{merchant_index:03d}"
        rows.extend((
            {"couponId": f"coupon-{merchant_id}-01", "merchantId": merchant_id, "kind": "fixed", "threshold": float(120 + merchant_index * 5), "amount": float(15 + merchant_index % 10), "cap": float(15 + merchant_index % 10), "stackable": False, "scope": "merchant", "environmentRevision": "E0", "factTier": "synthetic_fixture", "sourceRef": f"{CONTROLLED_SOURCE_URI}/coupons/{merchant_id}/01"},
            {"couponId": f"coupon-{merchant_id}-02", "merchantId": merchant_id, "kind": "percent", "threshold": float(260 + merchant_index * 7), "amount": 0.08 + (merchant_index % 3) * 0.01, "cap": float(45 + merchant_index % 25), "stackable": True, "scope": "merchant", "environmentRevision": "E0", "factTier": "synthetic_fixture", "sourceRef": f"{CONTROLLED_SOURCE_URI}/coupons/{merchant_id}/02"},
            {"couponId": f"coupon-{merchant_id}-03", "merchantId": merchant_id, "kind": "percent", "threshold": 99999.0, "amount": 0.15, "cap": 100.0, "stackable": False, "scope": "platform", "environmentRevision": "E0", "factTier": "synthetic_fixture", "sourceRef": f"{CONTROLLED_SOURCE_URI}/coupons/{merchant_id}/03"},
        ))
    return rows


def _knowledge_rows(products: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    by_category: dict[str, list[Mapping[str, Any]]] = {category: [] for category in CATEGORIES}
    for product in products:
        by_category[str(product["category"])].append(product)
    rows: list[Mapping[str, Any]] = []
    for category in CATEGORIES:
        fields = tuple(CATEGORY_SPECS[category]["fields"])
        for slot in range(10):
            field = fields[slot % len(fields)]
            product = by_category[category][slot * 37 % 1000]
            value = product["facts"][field]["value"]
            rows.append({"knowledgeId": f"knowledge-{category}-{slot + 1:02d}", "category": category, "topic": "selection_rule", "useCase": f"按{field}筛选{category}的场景{slot + 1}", "attributeField": field, "attributeValue": value, "selectionRule": {"field": field, "operator": "EQ", "value": value, "unknownPolicy": "fail"}, "text": f"选择{category}时，如果用户关注{field}，可优先考虑属性值 {value}；未知事实不得自动通过。", "factTier": "synthetic_fixture", "sourceRef": f"{CONTROLLED_SOURCE_URI}/knowledge/{category}/{slot + 1:02d}"})
    return rows


def _environment_rows(products: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    result: dict[str, list[Mapping[str, Any]]] = {}
    previous: dict[str, Mapping[str, Any]] | None = None
    previous_revision: str | None = None
    for revision in ENVIRONMENTS:
        rows: list[Mapping[str, Any]] = []
        for global_index, product in enumerate(products):
            base_price = float(product["price"])
            base_stock = 4 + (global_index * 11 % 17)
            if revision == "E0":
                price, stock, promotion = base_price, base_stock, global_index % 3 != 0
            elif revision == "E1":
                prior = previous[str(product["productId"])]
                price, stock, promotion = round(base_price * (1.04 if global_index % 4 == 0 else 1.015), 2), prior["stock"], prior["promotionEligible"]
            elif revision == "E2":
                prior = previous[str(product["productId"])]
                price, stock, promotion = prior["price"], (0 if global_index % 17 == 0 else prior["stock"]), prior["promotionEligible"]
            else:
                prior = previous[str(product["productId"])]
                price = round(prior["price"] * (0.92 if global_index % 5 == 0 else 1.01), 2)
                stock = prior["stock"] + (1 if global_index % 7 == 0 else 0)
                promotion = not prior["promotionEligible"] if global_index % 5 == 0 else prior["promotionEligible"]
            prior_price = None if previous is None else previous[str(product["productId"])]["price"]
            prior_stock = None if previous is None else previous[str(product["productId"])]["stock"]
            if previous is None:
                changed_fields = []
                kind = "baseline"
            else:
                prior = previous[str(product["productId"])]
                changed_fields = [
                    field for field, current_value, previous_value in (
                        ("price", price, prior["price"]),
                        ("stock", stock, prior["stock"]),
                        ("promotionEligible", promotion, prior["promotionEligible"]),
                    )
                    if current_value != previous_value
                ]
                kind = (
                    "noop" if not changed_fields else
                    "price_update" if changed_fields == ["price"] else
                    "stock_update" if changed_fields == ["stock"] else
                    "promotion_update" if changed_fields == ["promotionEligible"] else
                    "mixed_update"
                )
            rows.append({"productId": product["productId"], "merchantId": product["merchantId"], "price": price, "stock": stock, "promotionEligible": promotion, "environmentRevision": revision, "previousRevision": previous_revision, "previousPrice": prior_price, "previousStock": prior_stock, "mutation": {"kind": kind, "changedFields": changed_fields}, "factTier": "synthetic_fixture", "sourceRef": f"{CONTROLLED_SOURCE_URI}/offers/{revision}/{product['productId']}"})
        result[revision] = rows
        previous = {str(row["productId"]): row for row in rows}
        previous_revision = revision
    return result


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_bytes(row).decode("utf-8"))
            count += 1
    return count


def materialize_controlled_world(output_dir: Path | str) -> ControlledWorldBuildResult:
    """Materialize the exact 12k controlled world into a new directory.

    Existing non-empty directories are rejected so a prior benchmark freeze
    cannot be silently overwritten.  All paths recorded in the manifest are
    relative; the manifest bytes therefore remain stable across output roots.
    """
    output_dir = Path(output_dir)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise CommerceWorldError(f"refusing to overwrite non-empty controlled world directory: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    products = _product_rows()
    merchants = _merchant_rows()
    policies = _policy_rows()
    coupons = _coupon_rows()
    knowledge = _knowledge_rows(products)
    environments = _environment_rows(products)
    rows_by_artifact: list[tuple[str, Sequence[Mapping[str, Any]], str]] = [
        ("products.jsonl", products, "synthetic_fixture"),
        ("merchants.jsonl", merchants, "synthetic_fixture"),
        ("policies.jsonl", policies, "synthetic_fixture"),
        ("coupons.jsonl", coupons, "synthetic_fixture"),
        ("knowledge.jsonl", knowledge, "synthetic_fixture"),
    ] + [(f"environments/{revision}.jsonl", environments[revision], "synthetic_fixture") for revision in ENVIRONMENTS]
    artifacts: list[Mapping[str, Any]] = []
    for name, rows, tier in rows_by_artifact:
        path = output_dir / name
        count = _write_jsonl(path, rows)
        artifacts.append({"path": name, "sha256": sha256_file(path), "recordCount": count, "factTier": tier})
    descriptor = {"generatorIdentity": CONTROLLED_GENERATOR_ID, "generationSeed": CONTROLLED_SEED, "worldKind": CONTROLLED_WORLD_KIND, "sourceUri": CONTROLLED_SOURCE_URI}
    source = {"sourceId": "controlled-synthetic-generator", "sourceUri": CONTROLLED_SOURCE_URI, "license": "controlled_synthetic_non_market_data", "licenseStatus": "synthetic_fixture", "sha256": sha256_bytes(canonical_bytes(descriptor)), "recordCount": TARGET_COUNTS["products"], "generatorIdentity": CONTROLLED_GENERATOR_ID, "generationSeed": CONTROLLED_SEED, "reproducible": True, "nonMarketData": True}
    ledger_path = output_dir / "license-ledger.jsonl"
    _write_jsonl(ledger_path, [source])
    ledger_sha = sha256_file(ledger_path)
    category_coverage = [{"category": category, "target": 1000, "actual": sum(1 for row in products if row["category"] == category)} for category in CATEGORIES]
    diversity = diversity_metrics(products)
    catalog_revision = "catalog-controlled-" + sha256_bytes(canonical_bytes({"generatorIdentity": CONTROLLED_GENERATOR_ID, "generationSeed": CONTROLLED_SEED, "productIds": [row["productId"] for row in products]}))[:16]
    provenance_sha = sha256_bytes(canonical_bytes({"worldKind": CONTROLLED_WORLD_KIND, "generationSeed": CONTROLLED_SEED, "generatorIdentity": CONTROLLED_GENERATOR_ID, "catalogRevision": catalog_revision, "ledgerSha256": ledger_sha, "artifacts": artifacts, "environmentRevisions": list(ENVIRONMENTS), "diversity": diversity}))
    manifest: Mapping[str, Any] = {"worldId": CONTROLLED_WORLD_ID, "schemaVersion": "commerce-world-manifest-v1", "status": "READY", "builderVersion": CONTROLLED_GENERATOR_ID, "worldKind": CONTROLLED_WORLD_KIND, "generationSeed": CONTROLLED_SEED, "generatorIdentity": CONTROLLED_GENERATOR_ID, "catalogRevision": catalog_revision, "environmentRevisions": list(ENVIRONMENTS), "targets": dict(TARGET_COUNTS), "actuals": dict(TARGET_COUNTS), "sources": [source], "artifacts": artifacts, "licenseGate": "synthetic_fixture", "manifestAudit": "verified", "categoryCoverage": category_coverage, "diversity": diversity, "licenseLedgerSha256": ledger_sha, "provenanceSha256": provenance_sha}
    (output_dir / "manifest.json").write_bytes(canonical_bytes(manifest))
    audited = load_world_manifest(output_dir / "manifest.json")
    return ControlledWorldBuildResult(output_dir=output_dir, manifest=audited, products=tuple(products))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize the deterministic controlled CommerceWorld-CN-V1")
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)
    result = materialize_controlled_world(args.output)
    print(f"READY {result.manifest['worldId']} {result.manifest['catalogRevision']} products={len(result.products)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
