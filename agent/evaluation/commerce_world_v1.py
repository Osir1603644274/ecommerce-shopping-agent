"""Deterministic, offline CommerceWorld-CN-V1 builder.

The builder deliberately separates a *world specification* from a materialized
world.  A small JSONL fixture is enough to exercise the source-claim contract;
the independent controlled synthetic builder materializes the formal 12,000
row world without making any market-data claim.  No network access or model
call is performed.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

WORLD_SCHEMA_VERSION = "commerce-world-manifest-v1"
BUILDER_VERSION = "commerce-world-builder-v1"
TARGET_COUNTS = {
    "products": 12_000,
    "merchants": 48,
    "policies": 192,
    "coupons": 144,
    "knowledge": 120,
}
CATEGORIES = (
    "used_phone",
    "phone_accessory",
    "earbuds",
    "smartwatch",
    "tshirt",
    "jeans",
    "sneakers",
    "handbag",
    "drinkware",
    "cleaning",
    "stationery",
    "books",
)
ENVIRONMENTS = ("E0", "E1", "E2", "E3")


class CommerceWorldError(ValueError):
    """A world cannot be materialized or fails a deterministic invariant."""


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl(path: Path | str) -> list[Mapping[str, Any]]:
    records: list[Mapping[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CommerceWorldError(f"{path}:{line_no}: invalid JSON") from exc
            if not isinstance(value, Mapping):
                raise CommerceWorldError(f"{path}:{line_no}: record must be an object")
            records.append(value)
    return records


def _write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(canonical_bytes(record).decode("utf-8"))
            count += 1
    return count


def _first(record: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return default


def _digest_number(seed: str, low: int, high: int) -> int:
    if high < low:
        raise ValueError("high must be >= low")
    number = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12], 16)
    return low + number % (high - low + 1)


def _normalize_category(value: Any, index: int) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "手机": "used_phone", "二手手机": "used_phone", "phone": "used_phone",
        "耳机": "earbuds", "耳麦": "earbuds", "手表": "smartwatch",
        "上衣": "tshirt", "牛仔裤": "jeans", "鞋": "sneakers",
        "包": "handbag", "水杯": "drinkware", "清洁": "cleaning",
        "文具": "stationery", "图书": "books",
    }
    if text in CATEGORIES:
        return text
    if text in aliases:
        return aliases[text]
    # A benchmark world must never manufacture a category from row position.
    # Round-robin assignment makes an unrelated or malformed source look like
    # a balanced catalogue and can silently change the oracle universe.
    raise CommerceWorldError(f"unknown or missing product category: {value!r}")


def _fact(value: Any, source_ref: str, tier: str = "source_claim") -> Mapping[str, Any]:
    if value is None or value == "":
        return {"known": False, "value": None, "sourceRef": source_ref, "factTier": tier}
    return {"known": True, "value": value, "sourceRef": source_ref, "factTier": tier}


def _canonical_raw_fact(value: Any, source_ref: str) -> Mapping[str, Any]:
    """Strip caller-controlled provenance from every source fact.

    A source adapter may provide either a scalar value or a ``known/value``
    envelope.  Its sourceRef/factTier are never trusted; the fixture ledger
    and field path are the only provenance accepted by the benchmark.
    """
    if isinstance(value, Mapping) and "known" in value:
        known = value.get("known") is True
        if known and "value" not in value:
            raise CommerceWorldError("known fact is missing its value")
        fact_value = value.get("value") if known else None
    else:
        known = value not in (None, "")
        fact_value = value if known else None
    return {"known": known, "value": fact_value, "sourceRef": source_ref, "factTier": "source_claim"}


@dataclass(frozen=True)
class WorldBuildResult:
    output_dir: Path
    manifest: Mapping[str, Any]
    products: tuple[Mapping[str, Any], ...]


def _normalise_product(raw: Mapping[str, Any], index: int, source_ref: str) -> Mapping[str, Any]:
    raw_id = _first(raw, "productId", "product_id", "id", default=None)
    if raw_id is None or not str(raw_id).strip():
        raise CommerceWorldError(f"product row {index}: missing productId")
    product_id = str(raw_id).strip()
    raw_title = _first(raw, "title", "item_title", "name", default=None)
    if raw_title is None or not str(raw_title).strip():
        raise CommerceWorldError(f"product {product_id}: missing title")
    title = str(raw_title).strip()
    category = _normalize_category(_first(raw, "category", "catalog", "categoryName"), index)
    source_merchant = _first(raw, "merchantId", "merchant_id", "seller", "seller_name")
    # Raw seller identities are source claims, never the benchmark merchant
    # universe.  The caller deterministically maps every row to one of exactly
    # 48 synthetic merchants.
    merchant_id = f"merchant-{index % TARGET_COUNTS['merchants'] + 1:03d}"
    raw_price = _first(raw, "price", "salePrice", "priceCny")
    try:
        if raw_price is None or isinstance(raw_price, bool) or str(raw_price).strip() == "":
            raise ValueError("missing price")
        price = round(float(raw_price), 2)
    except (TypeError, ValueError) as exc:
        raise CommerceWorldError(f"product {product_id}: price is missing or non-numeric") from exc
    if not math.isfinite(price) or price < 0:
        raise CommerceWorldError(f"product {product_id}: price must be finite and non-negative")
    facts: dict[str, Mapping[str, Any]] = {}
    raw_facts = raw.get("facts")
    if isinstance(raw_facts, Mapping):
        for field, value in sorted(raw_facts.items(), key=lambda item: str(item[0])):
            field_name = str(field)
            facts[field_name] = _canonical_raw_fact(value, f"{source_ref}#product={product_id}&field={field_name}")
    for field in ("brand", "condition", "platform", "color", "size", "storage", "battery", "camera", "material"):
        if field in raw and field not in facts:
            facts[field] = _canonical_raw_fact(raw[field], f"{source_ref}#product={product_id}&field={field}")
    # These are the minimum fields that can participate in a deterministic
    # oracle.  They are overwritten with exact source-bound facts even if a
    # loosely shaped upstream ``facts`` object attempted to disagree.
    for field, value in (("category", category), ("title", title), ("price", price)):
        facts[field] = _fact(value, f"{source_ref}#product={product_id}&field={field}", "source_claim")
    result = {
        "productId": product_id,
        "category": category,
        "title": title,
        "merchantId": merchant_id,
        "price": price,
        "facts": facts,
        "factTier": "source_claim",
        "sourceRef": source_ref,
    }
    if source_merchant not in (None, ""):
        result["sourceMerchantClaim"] = str(source_merchant)
    return result


def _environment_rows(products: Sequence[Mapping[str, Any]], revision: str) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for index, product in enumerate(products):
        base = float(product["price"])
        if revision == "E0":
            price, stock = base, 3 + _digest_number(str(product["productId"]), 0, 20)
            mutation = "baseline"
        elif revision == "E1":
            price, stock = round(base * 1.05, 2), 3 + _digest_number(str(product["productId"]), 0, 20)
            mutation = "price_increase"
        elif revision == "E2":
            price, stock = base, 0 if index % 7 == 0 else 3 + _digest_number(str(product["productId"]), 0, 20)
            mutation = "stock_change"
        else:
            price, stock = round(base * 0.95, 2), 3 + _digest_number(str(product["productId"]), 0, 20)
            mutation = "price_decrease"
        rows.append({
            "productId": product["productId"],
            "merchantId": product["merchantId"],
            "price": price,
            "stock": stock,
            "environmentRevision": revision,
            "factTier": "synthetic_fixture",
            "sourceRef": f"synthetic://offer/{revision}/{product['productId']}",
            "mutation": mutation,
        })
    return rows


def build_world(
    fixture_path: Path | str,
    output_dir: Path | str,
    *,
    world_id: str = "COMMERCE-WORLD-CN-V1-fixture",
    source_license: str = "UNVERIFIED",
    source_license_status: str = "pending_review",
) -> WorldBuildResult:
    """Build a deterministic small world from JSONL without network access.

    The formal target counts are always recorded in the manifest.  A world is
    ``READY`` only when all targets are truly materialized; otherwise the
    manifest is explicitly ``PENDING_DATA_SCALE``.
    """
    fixture_path = Path(fixture_path)
    output_dir = Path(output_dir)
    if not fixture_path.is_file():
        raise CommerceWorldError(f"fixture not found: {fixture_path}")
    raw_records = _jsonl(fixture_path)
    products = tuple(_normalise_product(row, index, str(fixture_path)) for index, row in enumerate(raw_records))
    if len({row["productId"] for row in products}) != len(products):
        raise CommerceWorldError("duplicate productId after deterministic normalization")

    merchants = [f"merchant-{index:03d}" for index in range(1, TARGET_COUNTS["merchants"] + 1)]
    merchant_rows = [{"merchantId": merchant, "name": merchant, "factTier": "synthetic_fixture", "sourceRef": f"synthetic://merchant/{merchant}"} for merchant in merchants]
    policies = [
        {"policyId": f"policy-{merchant}-{slot:02d}", "merchantId": merchant, "topic": topic, "text": f"{merchant} {topic} policy", "factTier": "synthetic_fixture", "sourceRef": f"synthetic://policy/{merchant}/{slot}"}
        for merchant in merchants for slot, topic in enumerate(("returns", "shipping", "warranty", "service"), 1)
    ]
    coupons = [
        {"couponId": f"coupon-{merchant}-{slot:02d}", "merchantId": merchant, "kind": "fixed" if slot % 2 else "percent", "threshold": float(200 * slot), "amount": 10.0 if slot % 2 else 0.10, "cap": 80.0, "environmentRevision": "E0", "stackable": False, "factTier": "synthetic_fixture", "sourceRef": f"synthetic://coupon/{merchant}/{slot}"}
        for merchant in merchants for slot in range(1, 4)
    ]
    knowledge = [
        {"knowledgeId": f"knowledge-{category}-{slot:02d}", "category": category, "topic": "care_and_selection", "attribute": "category", "value": category, "text": f"{category} selection note {slot}", "factTier": "synthetic_fixture", "sourceRef": f"synthetic://knowledge/{category}/{slot}"}
        for category in CATEGORIES for slot in range(1, 11)
    ]

    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []

    def materialize(name: str, records: Iterable[Mapping[str, Any]], tier: str) -> None:
        path = output_dir / name
        count = _write_jsonl(path, records)
        artifacts.append({"path": name, "sha256": sha256_file(path), "recordCount": count, "factTier": tier})

    materialize("products.jsonl", products, "source_claim")
    materialize("merchants.jsonl", merchant_rows, "synthetic_fixture")
    materialize("policies.jsonl", policies, "synthetic_fixture")
    materialize("coupons.jsonl", coupons, "synthetic_fixture")
    materialize("knowledge.jsonl", knowledge, "synthetic_fixture")
    for revision in ENVIRONMENTS:
        materialize(f"environments/{revision}.jsonl", _environment_rows(products, revision), "synthetic_fixture")

    ledger = [{"sourceId": "fixture-products", "sourceUri": str(fixture_path), "license": source_license, "licenseStatus": source_license_status, "sha256": sha256_file(fixture_path), "recordCount": len(products)}]
    ledger_path = output_dir / "license-ledger.jsonl"
    _write_jsonl(ledger_path, ledger)
    ledger_sha = sha256_file(ledger_path)
    actuals = {"products": len(products), "merchants": len(merchants), "policies": len(policies), "coupons": len(coupons), "knowledge": len(knowledge)}
    category_counts = {category: 0 for category in CATEGORIES}
    for product in products:
        category_counts[product["category"]] += 1
    category_coverage = [{"category": category, "target": 1000, "actual": category_counts[category]} for category in CATEGORIES]
    license_gate = "verified" if source_license_status == "verified" else ("synthetic_fixture" if source_license_status == "synthetic_fixture" else "pending_review")
    exact_shape = actuals == TARGET_COUNTS and all(row["actual"] == row["target"] for row in category_coverage)
    status = "READY" if exact_shape and license_gate in {"verified", "synthetic_fixture"} and len(ENVIRONMENTS) == 4 else "PENDING_DATA_SCALE"
    catalog_revision = "catalog-" + sha256_bytes(canonical_bytes({"products": [p["productId"] for p in products], "source": sha256_file(fixture_path)}))[:16]
    provenance_sha = sha256_bytes(canonical_bytes({"catalogRevision": catalog_revision, "ledgerSha256": ledger_sha, "artifacts": artifacts, "environmentRevisions": ENVIRONMENTS}))
    manifest = {
        "worldId": world_id,
        "schemaVersion": WORLD_SCHEMA_VERSION,
        "status": status,
        "builderVersion": BUILDER_VERSION,
        "catalogRevision": catalog_revision,
        "environmentRevisions": list(ENVIRONMENTS),
        "targets": dict(TARGET_COUNTS),
        "actuals": actuals,
        "sources": ledger,
        "artifacts": artifacts,
        "licenseGate": license_gate,
        "manifestAudit": "verified",
        "categoryCoverage": category_coverage,
        "licenseLedgerSha256": ledger_sha,
        "provenanceSha256": provenance_sha,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_bytes(canonical_bytes(manifest))
    return WorldBuildResult(output_dir=output_dir, manifest=manifest, products=products)


def _count_jsonl(path: Path) -> tuple[int, list[Mapping[str, Any]]]:
    rows = _jsonl(path)
    return len(rows), rows


def load_world_manifest(path: Path | str) -> Mapping[str, Any]:
    """Load a manifest and re-audit every referenced artifact and relation.

    Merely having the right schema/version is insufficient: every artifact SHA
    and record count, the license ledger, exact E0-E3 coverage, merchant
    references, product uniqueness and the provenance digest are recomputed.
    """
    manifest_path = Path(path)
    with open(manifest_path, encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, Mapping) or value.get("schemaVersion") != WORLD_SCHEMA_VERSION:
        raise CommerceWorldError("not a commerce-world-manifest-v1")
    schema_path = Path(__file__).resolve().parent / "schemas" / "commerce_world_manifest_v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(value))
    if errors:
        raise CommerceWorldError(f"manifest schema invalid: {errors[0].message}")
    root = manifest_path.parent
    artifact_rows: dict[str, list[Mapping[str, Any]]] = {}
    for artifact in value["artifacts"]:
        artifact_path = root / str(artifact["path"])
        if not artifact_path.is_file():
            raise CommerceWorldError(f"manifest artifact missing: {artifact['path']}")
        if sha256_file(artifact_path) != artifact["sha256"]:
            raise CommerceWorldError(f"manifest artifact SHA mismatch: {artifact['path']}")
        count, rows = _count_jsonl(artifact_path)
        if count != artifact["recordCount"]:
            raise CommerceWorldError(f"manifest artifact recordCount mismatch: {artifact['path']}")
        artifact_rows[str(artifact["path"])] = rows
    ledger_path = root / "license-ledger.jsonl"
    if not ledger_path.is_file() or sha256_file(ledger_path) != value.get("licenseLedgerSha256"):
        raise CommerceWorldError("license ledger missing or SHA mismatch")
    ledger = _jsonl(ledger_path)
    if len(ledger) != len(value["sources"]):
        raise CommerceWorldError("license ledger record count mismatch")
    if canonical_bytes(ledger) != canonical_bytes(value["sources"]):
        raise CommerceWorldError("license ledger contents do not match manifest sources")
    ledger_source_uris = {str(source.get("sourceUri")) for source in ledger}
    if not ledger_source_uris:
        raise CommerceWorldError("license ledger has no source URI")
    if tuple(value["environmentRevisions"]) != ENVIRONMENTS:
        raise CommerceWorldError("environment revisions must be exactly E0,E1,E2,E3")
    expected_files = {"products.jsonl", "merchants.jsonl", "policies.jsonl", "coupons.jsonl", "knowledge.jsonl", *(f"environments/{revision}.jsonl" for revision in ENVIRONMENTS)}
    if set(artifact_rows) != expected_files:
        raise CommerceWorldError("manifest artifact set is incomplete")
    counts = {"products": len(artifact_rows["products.jsonl"]), "merchants": len(artifact_rows["merchants.jsonl"]), "policies": len(artifact_rows["policies.jsonl"]), "coupons": len(artifact_rows["coupons.jsonl"]), "knowledge": len(artifact_rows["knowledge.jsonl"])}
    if counts != dict(value["actuals"]):
        raise CommerceWorldError("manifest actual counts do not match artifacts")
    if len({str(row.get("productId")) for row in artifact_rows["products.jsonl"]}) != counts["products"]:
        raise CommerceWorldError("product IDs are not unique")
    merchant_ids = {str(row.get("merchantId")) for row in artifact_rows["merchants.jsonl"]}
    if merchant_ids != {f"merchant-{index:03d}" for index in range(1, 49)}:
        raise CommerceWorldError("merchant universe is not exactly 48 synthetic merchants")
    if any(str(row.get("merchantId")) not in merchant_ids for row in artifact_rows["products.jsonl"]):
        raise CommerceWorldError("product references unknown merchant")
    if value.get("worldKind") == "controlled_synthetic":
        return _audit_controlled_manifest(manifest_path, value, artifact_rows, ledger, ledger_source_uris)
    # Re-audit every product's oracle-relevant source facts.  Counts and an
    # artifact SHA alone cannot catch a source row whose category/price was
    # guessed or whose provenance was dropped during normalization.
    for row in artifact_rows["products.jsonl"]:
        product_id = str(row.get("productId", ""))
        if not product_id or str(row.get("category", "")) not in CATEGORIES:
            raise CommerceWorldError(f"product {product_id or '<missing>'} has unknown category")
        title = row.get("title")
        price = row.get("price")
        if not isinstance(title, str) or not title.strip():
            raise CommerceWorldError(f"product {product_id}: missing title")
        if not isinstance(price, (int, float)) or isinstance(price, bool) or not math.isfinite(float(price)) or float(price) < 0:
            raise CommerceWorldError(f"product {product_id}: invalid price")
        source_ref = str(row.get("sourceRef", ""))
        if not source_ref:
            raise CommerceWorldError(f"product {product_id}: missing sourceRef")
        if source_ref not in ledger_source_uris:
            raise CommerceWorldError(f"product {product_id}: sourceRef is outside the fixture ledger")
        facts = row.get("facts")
        if not isinstance(facts, Mapping):
            raise CommerceWorldError(f"product {product_id}: missing facts")
        for field, fact in facts.items():
            if not isinstance(fact, Mapping) or not isinstance(fact.get("known"), bool):
                raise CommerceWorldError(f"product {product_id}: malformed {field} fact")
            expected_ref = f"{source_ref}#product={product_id}&field={field}"
            if str(fact.get("sourceRef", "")) != expected_ref or fact.get("factTier") != "source_claim":
                raise CommerceWorldError(f"product {product_id}: {field} fact provenance/value audit failed")
            if fact.get("known") is False and fact.get("value") is not None:
                raise CommerceWorldError(f"product {product_id}: unknown {field} fact carries a value")
        for field, expected_value in (("category", row["category"]), ("title", title), ("price", price)):
            fact = facts.get(field)
            expected_ref = f"{source_ref}#product={product_id}&field={field}"
            if not isinstance(fact, Mapping) or fact.get("known") is not True or fact.get("value") != expected_value or str(fact.get("sourceRef", "")) != expected_ref or fact.get("factTier") != "source_claim":
                raise CommerceWorldError(f"product {product_id}: {field} fact provenance/value audit failed")
    for revision in ENVIRONMENTS:
        env_rows = artifact_rows[f"environments/{revision}.jsonl"]
        if len(env_rows) != counts["products"] or any(row.get("environmentRevision") != revision for row in env_rows):
            raise CommerceWorldError(f"environment {revision} is not a complete product projection")
    coverage = {str(row["category"]): int(row["actual"]) for row in value.get("categoryCoverage", [])}
    actual_category_counts: dict[str, int] = {category: 0 for category in CATEGORIES}
    for row in artifact_rows["products.jsonl"]:
        category = str(row.get("category"))
        if category not in actual_category_counts:
            raise CommerceWorldError(f"unknown product category: {category}")
        actual_category_counts[category] += 1
    if coverage != actual_category_counts:
        raise CommerceWorldError("category coverage does not match products artifact")
    expected_provenance = sha256_bytes(canonical_bytes({"catalogRevision": value["catalogRevision"], "ledgerSha256": value["licenseLedgerSha256"], "artifacts": value["artifacts"], "environmentRevisions": ENVIRONMENTS}))
    if expected_provenance != value["provenanceSha256"]:
        raise CommerceWorldError("manifest provenance SHA mismatch")
    license_ok = value["licenseGate"] in {"verified", "synthetic_fixture"} and all(source.get("licenseStatus") in {"verified", "synthetic_fixture"} for source in value["sources"])
    exact_ready_shape = dict(value["actuals"]) == TARGET_COUNTS and all(int(row["actual"]) == int(row["target"]) == 1000 for row in value.get("categoryCoverage", []))
    expected_status = "READY" if exact_ready_shape and license_ok else "PENDING_DATA_SCALE"
    if value["status"] != expected_status:
        raise CommerceWorldError(f"manifest status lies about materialization: expected {expected_status}")
    return value


def _audit_controlled_manifest(
    manifest_path: Path,
    value: Mapping[str, Any],
    artifact_rows: Mapping[str, Sequence[Mapping[str, Any]]],
    ledger: Sequence[Mapping[str, Any]],
    ledger_source_uris: set[str],
) -> Mapping[str, Any]:
    """Audit the materialized controlled synthetic world.

    This branch deliberately remains in the world loader rather than in the
    Pilot generator: the authoritative scorer must be able to re-audit a
    controlled world without trusting a caller-provided product list.  The
    category-specific type vocabulary is imported lazily to avoid a module
    cycle with the controlled-world builder.
    """
    from .commerce_controlled_world_v1 import CATEGORY_SPECS, CONTROLLED_GENERATOR_ID, CONTROLLED_WORLD_KIND, CONTROLLED_SEED

    if value.get("worldKind") != CONTROLLED_WORLD_KIND:
        raise CommerceWorldError("controlled manifest worldKind is invalid")
    if value.get("builderVersion") != CONTROLLED_GENERATOR_ID:
        raise CommerceWorldError("controlled manifest builder identity drift")
    if value.get("generatorIdentity") != CONTROLLED_GENERATOR_ID:
        raise CommerceWorldError("controlled manifest generator identity drift")
    if value.get("generationSeed") != CONTROLLED_SEED:
        raise CommerceWorldError("controlled manifest generation seed drift")
    if value.get("status") != "READY":
        raise CommerceWorldError("controlled world must be READY after materialization")
    if dict(value.get("targets", {})) != TARGET_COUNTS or dict(value.get("actuals", {})) != TARGET_COUNTS:
        raise CommerceWorldError("controlled world counts are not exactly the formal target")
    coverage = {str(row.get("category")): (int(row.get("target", -1)), int(row.get("actual", -1))) for row in value.get("categoryCoverage", [])}
    if coverage != {category: (1000, 1000) for category in CATEGORIES}:
        raise CommerceWorldError("controlled category coverage is not exactly 12x1000")
    if value.get("licenseGate") != "synthetic_fixture" or len(ledger) != 1:
        raise CommerceWorldError("controlled world license gate is not synthetic_fixture")
    source = ledger[0]
    expected_source_uri = f"synthetic://commerce-world-cn-v1/controlled/{CONTROLLED_SEED}"
    if source.get("sourceUri") != expected_source_uri or source.get("licenseStatus") != "synthetic_fixture" or source.get("license") != "controlled_synthetic_non_market_data" or int(source.get("recordCount", -1)) != TARGET_COUNTS["products"] or source.get("generatorIdentity") != CONTROLLED_GENERATOR_ID or source.get("generationSeed") != CONTROLLED_SEED or source.get("reproducible") is not True or source.get("nonMarketData") is not True:
        raise CommerceWorldError("controlled source/license ledger is not stable synthetic metadata")
    descriptor = {"generatorIdentity": CONTROLLED_GENERATOR_ID, "generationSeed": CONTROLLED_SEED, "worldKind": CONTROLLED_WORLD_KIND, "sourceUri": expected_source_uri}
    if source.get("sha256") != sha256_bytes(canonical_bytes(descriptor)):
        raise CommerceWorldError("controlled source ledger generator descriptor SHA mismatch")
    if not ledger_source_uris or expected_source_uri not in ledger_source_uris:
        raise CommerceWorldError("controlled source URI is absent from the ledger")
    if any(str(artifact.get("factTier")) != "synthetic_fixture" for artifact in value.get("artifacts", [])):
        raise CommerceWorldError("controlled artifact has a non-synthetic fact tier")
    expected_provenance = sha256_bytes(canonical_bytes({"worldKind": CONTROLLED_WORLD_KIND, "generationSeed": CONTROLLED_SEED, "generatorIdentity": CONTROLLED_GENERATOR_ID, "catalogRevision": value["catalogRevision"], "ledgerSha256": value["licenseLedgerSha256"], "artifacts": value["artifacts"], "environmentRevisions": ENVIRONMENTS, "diversity": value.get("diversity")}))
    if expected_provenance != value.get("provenanceSha256"):
        raise CommerceWorldError("controlled manifest provenance SHA mismatch")

    products = list(artifact_rows["products.jsonl"])
    product_ids = {str(row.get("productId")) for row in products}
    merchant_categories: dict[str, set[str]] = {merchant: set() for merchant in {f"merchant-{index:03d}" for index in range(1, 49)}}
    merchants = list(artifact_rows["merchants.jsonl"])
    if len(merchants) != 48 or any(row.get("factTier") != "synthetic_fixture" or not isinstance(row.get("name"), str) or set(row.get("coverage", [])) != set(CATEGORIES) or not str(row.get("sourceRef", "")).startswith(f"{expected_source_uri}/merchants/") for row in merchants):
        raise CommerceWorldError("controlled merchant artifact is not synthetic, covered and meaningful")
    product_by_id: dict[str, Mapping[str, Any]] = {}
    diversity_accumulators: dict[str, dict[str, Any]] = {
        category: {"titles": set(), "vectors": set(), "fields": {field: set() for field in CATEGORY_SPECS[category]["fields"]}}
        for category in CATEGORIES
    }
    for row in products:
        product_id = str(row.get("productId", ""))
        category = str(row.get("category", ""))
        if not product_id or category not in CATEGORY_SPECS or str(row.get("factTier")) != "synthetic_fixture":
            raise CommerceWorldError(f"controlled product {product_id or '<missing>'} has invalid category or fact tier")
        if str(row.get("sourceRef", "")) != f"{expected_source_uri}/products":
            raise CommerceWorldError(f"controlled product {product_id}: sourceRef drift")
        if product_id in product_by_id:
            raise CommerceWorldError(f"controlled product {product_id}: duplicate identity")
        product_by_id[product_id] = row
        merchant_id = str(row.get("merchantId", ""))
        if merchant_id not in merchant_categories:
            raise CommerceWorldError(f"controlled product {product_id}: merchant FK drift")
        merchant_categories[merchant_id].add(category)
        facts = row.get("facts")
        if not isinstance(facts, Mapping):
            raise CommerceWorldError(f"controlled product {product_id}: facts are missing")
        spec = CATEGORY_SPECS[category]
        required_fields = tuple(spec["fields"])
        if len(required_fields) < 6 or not set(required_fields).issubset(facts):
            raise CommerceWorldError(f"controlled product {product_id}: typed attribute coverage is incomplete")
        for field, fact in facts.items():
            if not isinstance(fact, Mapping) or not isinstance(fact.get("known"), bool):
                raise CommerceWorldError(f"controlled product {product_id}: malformed {field} fact")
            expected_ref = f"{expected_source_uri}/products#product={product_id}&field={field}"
            if str(fact.get("sourceRef", "")) != expected_ref or fact.get("factTier") != "synthetic_fixture":
                raise CommerceWorldError(f"controlled product {product_id}: {field} provenance/tier drift")
            if fact.get("known") is False and fact.get("value") is not None:
                raise CommerceWorldError(f"controlled product {product_id}: unknown fact carries a value")
        for field, descriptor_field in spec["fields"].items():
            fact = facts[field]
            if fact.get("known") is not True:
                raise CommerceWorldError(f"controlled product {product_id}: required {field} is unknown")
            actual = fact.get("value")
            expected_type = descriptor_field["type"]
            if expected_type == "string" and not isinstance(actual, str):
                raise CommerceWorldError(f"controlled product {product_id}: {field} is not a string")
            if expected_type == "integer" and (not isinstance(actual, int) or isinstance(actual, bool)):
                raise CommerceWorldError(f"controlled product {product_id}: {field} is not an integer")
            if expected_type == "number" and (not isinstance(actual, (int, float)) or isinstance(actual, bool) or not math.isfinite(float(actual))):
                raise CommerceWorldError(f"controlled product {product_id}: {field} is not numeric")
            if expected_type == "boolean" and not isinstance(actual, bool):
                raise CommerceWorldError(f"controlled product {product_id}: {field} is not boolean")
            if descriptor_field.get("choices") and actual not in descriptor_field["choices"]:
                raise CommerceWorldError(f"controlled product {product_id}: {field} is outside typed choices")
            if descriptor_field.get("minimum") is not None and float(actual) < float(descriptor_field["minimum"]):
                raise CommerceWorldError(f"controlled product {product_id}: {field} is below minimum")
            if descriptor_field.get("maximum") is not None and float(actual) > float(descriptor_field["maximum"]):
                raise CommerceWorldError(f"controlled product {product_id}: {field} is above maximum")
        if facts.get("category", {}).get("value") != category or facts.get("title", {}).get("value") != row.get("title") or facts.get("price", {}).get("value") != row.get("price") or facts.get("optionalSellerNote", {}).get("known") is not False:
            raise CommerceWorldError(f"controlled product {product_id}: top-level facts or explicit unknown drift")
        for title_field in spec["titleFields"]:
            if str(facts[title_field].get("value")) not in str(row.get("title", "")):
                raise CommerceWorldError(f"controlled product {product_id}: title does not match {title_field}")
        title = str(row.get("title", ""))
        if product_id in title or product_id.replace("-", " ") in title:
            raise CommerceWorldError(f"controlled product {product_id}: title uses identifier-only uniqueness")
        accumulator = diversity_accumulators[category]
        accumulator["titles"].add(title)
        accumulator["vectors"].add(canonical_bytes([facts[field].get("value") for field in spec["fields"]]))
        for field in spec["fields"]:
            accumulator["fields"][field].add(canonical_bytes(facts[field].get("value")))
    computed_diversity = {
        "perCategory": {
            category: {
                "uniqueTitles": len(data["titles"]),
                "uniqueFactVectors": len(data["vectors"]),
                "fieldCoverage": {field: len(values) for field, values in data["fields"].items()},
            }
            for category, data in diversity_accumulators.items()
        }
    }
    if value.get("diversity") != computed_diversity:
        raise CommerceWorldError("controlled diversity metrics do not match audited product facts")
    if any(
        int(metrics["uniqueTitles"]) < 750 or int(metrics["uniqueFactVectors"]) < 750
        for metrics in computed_diversity["perCategory"].values()
    ):
        raise CommerceWorldError("controlled catalog diversity is below the 750-per-category floor")
    for category, metrics in computed_diversity["perCategory"].items():
        for field, descriptor in CATEGORY_SPECS[category]["fields"].items():
            observed = int(metrics["fieldCoverage"][field])
            choices = descriptor.get("choices")
            if choices and observed != len(choices):
                raise CommerceWorldError(f"controlled catalog choice coverage is incomplete for {category}.{field}")
            if not choices and observed < 2:
                raise CommerceWorldError(f"controlled catalog range coverage is incomplete for {category}.{field}")
    if any(len(categories) != len(CATEGORIES) for categories in merchant_categories.values()):
        raise CommerceWorldError("controlled merchants do not cover all product categories")

    policies = list(artifact_rows["policies.jsonl"])
    policy_topics = {"returns", "shipping", "warranty", "service"}
    policy_specs: dict[str, dict[str, dict[str, Any]]] = {
        "returns": {
            "returnDays": {"type": "integer", "minimum": 0, "maximum": 365},
            "restockingFeePct": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "shipping": {
            "cutoffHour": {"type": "integer", "minimum": 0, "maximum": 23},
            "freeShippingThreshold": {"type": "number", "minimum": 0, "maximum": 1_000_000},
        },
        "warranty": {
            "warrantyMonths": {"type": "integer", "minimum": 0, "maximum": 120},
            "proofRequired": {"type": "boolean"},
        },
        "service": {
            "responseHours": {"type": "integer", "minimum": 0, "maximum": 720},
            "repairWindowDays": {"type": "integer", "minimum": 0, "maximum": 365},
        },
    }
    by_merchant_policy: dict[str, set[str]] = {merchant: set() for merchant in merchant_categories}
    policy_ids: set[str] = set()
    for row in policies:
        merchant_id = str(row.get("merchantId", "")); topic = str(row.get("topic", ""))
        policy_id = str(row.get("policyId", ""))
        if not policy_id or policy_id in policy_ids or merchant_id not in by_merchant_policy or topic not in policy_topics or topic in by_merchant_policy[merchant_id]:
            raise CommerceWorldError("controlled policy merchant/topic set is invalid")
        policy_ids.add(policy_id)
        terms = row.get("terms")
        expected_terms = policy_specs.get(topic, {})
        if not isinstance(terms, Mapping) or set(str(key) for key in row.get("keyFields", [])) != set(expected_terms) or set(str(key) for key in terms) != set(expected_terms):
            raise CommerceWorldError("controlled policy terms/keyFields are not exact")
        for field, descriptor in expected_terms.items():
            actual = terms.get(field)
            expected_type = descriptor["type"]
            if expected_type == "integer" and (not isinstance(actual, int) or isinstance(actual, bool)):
                raise CommerceWorldError("controlled policy term type is invalid")
            if expected_type == "number" and (not isinstance(actual, (int, float)) or isinstance(actual, bool) or not math.isfinite(float(actual))):
                raise CommerceWorldError("controlled policy term type is invalid")
            if expected_type == "boolean" and not isinstance(actual, bool):
                raise CommerceWorldError("controlled policy term type is invalid")
            if descriptor.get("minimum") is not None and float(actual) < float(descriptor["minimum"]):
                raise CommerceWorldError("controlled policy term is below range")
            if descriptor.get("maximum") is not None and float(actual) > float(descriptor["maximum"]):
                raise CommerceWorldError("controlled policy term is above range")
        text = row.get("text")
        if not isinstance(row.get("version"), str) or not row["version"] or not isinstance(row.get("effectiveFrom"), str) or not isinstance(row.get("effectiveTo"), str) or not isinstance(row.get("keyFields"), list) or len(row["keyFields"]) < 2 or not isinstance(text, str) or topic not in text or any(f"{field}={terms[field]}" not in text for field in expected_terms) or row.get("factTier") != "synthetic_fixture" or not str(row.get("sourceRef", "")).startswith(f"{expected_source_uri}/policies/"):
            raise CommerceWorldError("controlled policy semantic shape is invalid")
        by_merchant_policy[merchant_id].add(topic)
    if len(policies) != TARGET_COUNTS["policies"] or any(topics != policy_topics for topics in by_merchant_policy.values()):
        raise CommerceWorldError("controlled policies are not exactly four meaningful topics per merchant")

    coupons = list(artifact_rows["coupons.jsonl"])
    by_merchant_coupon: dict[str, list[Mapping[str, Any]]] = {merchant: [] for merchant in merchant_categories}
    coupon_ids: set[str] = set()
    for row in coupons:
        merchant_id = str(row.get("merchantId", ""))
        coupon_id = str(row.get("couponId", ""))
        if not coupon_id or coupon_id in coupon_ids or merchant_id not in by_merchant_coupon or row.get("factTier") != "synthetic_fixture" or row.get("environmentRevision") != "E0" or row.get("kind") not in {"fixed", "percent"} or row.get("scope") not in {"merchant", "platform"} or not isinstance(row.get("stackable"), bool) or not isinstance(row.get("threshold"), (int, float)) or float(row["threshold"]) < 0 or not isinstance(row.get("cap"), (int, float)) or float(row["cap"]) < 0 or not str(row.get("sourceRef", "")).startswith(f"{expected_source_uri}/coupons/"):
            raise CommerceWorldError("controlled coupon semantic shape is invalid")
        coupon_ids.add(coupon_id)
        if row.get("kind") == "fixed" and (not isinstance(row.get("amount"), (int, float)) or float(row["amount"]) <= 0):
            raise CommerceWorldError("controlled fixed coupon amount is invalid")
        if row.get("kind") == "percent" and (not isinstance(row.get("amount"), (int, float)) or not 0 < float(row["amount"]) <= 1):
            raise CommerceWorldError("controlled percent coupon amount is invalid")
        by_merchant_coupon[merchant_id].append(row)
    if len(coupons) != TARGET_COUNTS["coupons"] or any(len(rows) != 3 for rows in by_merchant_coupon.values()) or {str(row.get("kind")) for row in coupons} != {"fixed", "percent"} or {str(row.get("scope")) for row in coupons} != {"merchant", "platform"} or {bool(row.get("stackable")) for row in coupons} != {True, False}:
        raise CommerceWorldError("controlled coupon coverage is not a meaningful 48x3 matrix")

    knowledge = list(artifact_rows["knowledge.jsonl"])
    knowledge_counts: dict[str, int] = {category: 0 for category in CATEGORY_SPECS}
    knowledge_ids: set[str] = set()
    for row in knowledge:
        category = str(row.get("category")); field = str(row.get("attributeField")); value_for_rule = row.get("attributeValue")
        knowledge_id = str(row.get("knowledgeId", ""))
        if not knowledge_id or knowledge_id in knowledge_ids or category not in CATEGORY_SPECS or field not in CATEGORY_SPECS[category]["fields"] or row.get("factTier") != "synthetic_fixture" or not str(row.get("sourceRef", "")).startswith(f"{expected_source_uri}/knowledge/"):
            raise CommerceWorldError("controlled knowledge category/field provenance is invalid")
        knowledge_ids.add(knowledge_id)
        rule = row.get("selectionRule")
        if not isinstance(rule, Mapping) or rule.get("field") != field or rule.get("operator") != "EQ" or rule.get("value") != value_for_rule or rule.get("unknownPolicy") != "fail":
            raise CommerceWorldError("controlled knowledge selection rule is not bound")
        if not any(facts.get(field, {}).get("known") is True and facts[field].get("value") == value_for_rule for product in products if str(product.get("category")) == category for facts in [product.get("facts", {})]):
            raise CommerceWorldError("controlled knowledge value does not occur in product facts")
        if not isinstance(row.get("useCase"), str) or not row["useCase"] or not isinstance(row.get("text"), str) or not row["text"]:
            raise CommerceWorldError("controlled knowledge document is not meaningful")
        knowledge_counts[category] += 1
    if len(knowledge) != TARGET_COUNTS["knowledge"] or any(count != 10 for count in knowledge_counts.values()):
        raise CommerceWorldError("controlled knowledge coverage is not exactly ten documents per category")

    previous_by_revision: dict[str, dict[str, Mapping[str, Any]]] = {}
    expected_previous = {"E0": None, "E1": "E0", "E2": "E1", "E3": "E2"}
    for revision in ENVIRONMENTS:
        rows = list(artifact_rows[f"environments/{revision}.jsonl"])
        if len(rows) != TARGET_COUNTS["products"]:
            raise CommerceWorldError(f"controlled environment {revision} is not complete")
        current: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            product_id = str(row.get("productId", ""))
            if product_id not in product_ids or product_id in current or row.get("environmentRevision") != revision or row.get("factTier") != "synthetic_fixture" or not isinstance(row.get("promotionEligible"), bool) or not isinstance(row.get("mutation"), Mapping) or row.get("mutation", {}).get("kind") not in {"baseline", "noop", "price_update", "stock_update", "promotion_update", "mixed_update"} or not str(row.get("sourceRef", "")).startswith(f"{expected_source_uri}/offers/{revision}/"):
                raise CommerceWorldError(f"controlled environment {revision} identity/tier/source drift")
            if not isinstance(row.get("price"), (int, float)) or isinstance(row.get("price"), bool) or float(row["price"]) < 0 or not isinstance(row.get("stock"), int) or isinstance(row.get("stock"), bool) or row["stock"] < 0:
                raise CommerceWorldError(f"controlled environment {revision} price/stock type drift")
            if row.get("previousRevision") != expected_previous[revision]:
                raise CommerceWorldError(f"controlled environment {revision} previousRevision drift")
            if str(row.get("merchantId")) != str(product_by_id[product_id].get("merchantId")):
                raise CommerceWorldError(f"controlled environment {revision} merchant FK drift")
            current[product_id] = row
        if set(current) != product_ids:
            raise CommerceWorldError(f"controlled environment {revision} product coverage drift")
        if revision == "E0":
            if any(row.get("previousPrice") is not None or row.get("previousStock") is not None or row.get("mutation", {}).get("kind") != "baseline" or row.get("mutation", {}).get("changedFields") != [] for row in current.values()):
                raise CommerceWorldError("controlled E0 baseline relation is invalid")
        else:
            previous = previous_by_revision[expected_previous[revision]]
            if any(row.get("previousPrice") != previous[product_id].get("price") or row.get("previousStock") != previous[product_id].get("stock") for product_id, row in current.items()):
                raise CommerceWorldError(f"controlled environment {revision} previous offer relation drift")
            for product_id, row in current.items():
                prior = previous[product_id]
                actual_changed = [
                    field for field, current_value, previous_value in (
                        ("price", row.get("price"), prior.get("price")),
                        ("stock", row.get("stock"), prior.get("stock")),
                        ("promotionEligible", row.get("promotionEligible"), prior.get("promotionEligible")),
                    )
                    if current_value != previous_value
                ]
                declared_changed = row.get("mutation", {}).get("changedFields")
                if not isinstance(declared_changed, list) or declared_changed != actual_changed:
                    raise CommerceWorldError(f"controlled environment {revision} changedFields is not an exact diff for {product_id}")
                expected_kind = (
                    "noop" if not actual_changed else
                    "price_update" if actual_changed == ["price"] else
                    "stock_update" if actual_changed == ["stock"] else
                    "promotion_update" if actual_changed == ["promotionEligible"] else
                    "mixed_update"
                )
                if row.get("mutation", {}).get("kind") != expected_kind:
                    raise CommerceWorldError(f"controlled environment {revision} mutation kind is not an exact diff for {product_id}")
            if not any(row.get("price") != row.get("previousPrice") or row.get("stock") != row.get("previousStock") or row.get("promotionEligible") != previous[product_id].get("promotionEligible") for product_id, row in current.items()):
                raise CommerceWorldError(f"controlled environment {revision} has no observable mutation")
        previous_by_revision[revision] = current
    if not any(row.get("stock") == 0 for row in previous_by_revision["E2"].values()):
        raise CommerceWorldError("controlled E2 has no deterministic sold-out witness")
    if not any(row.get("promotionEligible") != previous_by_revision["E2"][product_id].get("promotionEligible") for product_id, row in previous_by_revision["E3"].items()):
        raise CommerceWorldError("controlled E3 has no coupon/promotion witness")
    return value


def load_world_products(path: Path | str) -> list[Mapping[str, Any]]:
    return _jsonl(path)
