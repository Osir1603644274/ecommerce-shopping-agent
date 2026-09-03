"""Build a pinned, deterministic MySQL row plan for the frozen used-phone catalog.

This module never connects to MySQL.  It validates the frozen v2 catalog and
emits the exact ``product`` and ``product_attribute`` column values required by
a later, separately gated loader.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from app.domains.ecommerce.used_phone_attributes import (
    USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
    USED_PHONE_ATTRIBUTE_REGISTRY,
    USED_PHONE_ATTRIBUTE_RULESET_VERSION,
    materialize_used_phone_product_attributes,
    observe_used_phone_attributes,
    used_phone_attribute_ruleset_sha256,
)

SCHEMA_VERSION = "used-phone-catalog-seed-plan-v1"
MANIFEST_SCHEMA_VERSION = "used-phone-catalog-seed-plan-manifest-v1"
AUDIT_SCHEMA_VERSION = "used-phone-catalog-seed-plan-audit-v1"
DATASET_ID = "benchen4395/KuaiSearch"
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
SOURCE_URL = "https://huggingface.co/datasets/benchen4395/KuaiSearch"
SOURCE_LICENSE = "MIT"
TARGET_CATEGORY_KEY = "46/133/185"
TARGET_CATEGORY_PATH = ("二手", "二手手机通讯", "二手手机")
FROZEN_CATALOG_ARTIFACT = (
    "data/processed/ecommerce/kuaisearch_used_phone_complex_benchmark_v1/"
    f"{DATASET_REVISION}/used_phone_attribute_contract_v2/catalog.jsonl"
)
EXPECTED_CATALOG_SHA256 = (
    "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50"
)
EXPECTED_RULESET_SHA256 = (
    "c4f7400d9697a8c5246b4c56740031e8239b7642b494d3508db6f02f7f205b1a"
)
EXPECTED_PRODUCT_COUNT = 252
EXPECTED_STATUS_COUNTS: Mapping[str, Mapping[str, int]] = {
    "battery_health": {"known": 220, "conflict": 0, "unknown": 32},
    "battery_originality": {"known": 224, "conflict": 0, "unknown": 28},
    "motherboard_repair": {"known": 227, "conflict": 0, "unknown": 25},
    "os": {"known": 204, "conflict": 1, "unknown": 47},
    "scratch_level": {"known": 224, "conflict": 0, "unknown": 28},
    "screen_originality": {"known": 224, "conflict": 0, "unknown": 28},
    "shell_condition": {"known": 223, "conflict": 0, "unknown": 29},
}
ZERO_SHA256 = "0" * 64


class UsedPhoneCatalogSeedPlanError(ValueError):
    """Raised before output publication when a pinned invariant fails."""


_MAX_SIGNED_BIGINT = 9_223_372_036_854_775_807


def _strict_positive_int(value: object, *, label: str) -> int:
    if type(value) is not int or value < 1 or value > _MAX_SIGNED_BIGINT:
        raise UsedPhoneCatalogSeedPlanError(f"invalid {label}")
    return value


def _canonical_source_item_id(value: object, *, catalog_line: int) -> tuple[str, int]:
    if not isinstance(value, str):
        raise UsedPhoneCatalogSeedPlanError(
            f"invalid numeric itemId at catalog line {catalog_line}"
        )
    if (
        not value.isascii()
        or not value.isdecimal()
        or str(int(value)) != value
        or int(value) < 1
        or int(value) > _MAX_SIGNED_BIGINT
    ):
        raise UsedPhoneCatalogSeedPlanError(
            f"invalid numeric itemId at catalog line {catalog_line}"
        )
    return value, int(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_line(value: Any) -> bytes:
    return (_canonical_json(value) + "\n").encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_ref(
    value: Any,
    *,
    item_id: str,
    group: str,
    catalog_line: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise UsedPhoneCatalogSeedPlanError(
            f"non-object evidence ref at catalog line {catalog_line} group {group}"
        )
    required = {"source", "field", "lineNumber", "rawValue", "matchedRawTokens"}
    if not required.issubset(value):
        raise UsedPhoneCatalogSeedPlanError(
            f"incomplete evidence ref for item {item_id} group {group}"
        )
    if value["source"] != "relevance" or value["field"] != "attr_value":
        raise UsedPhoneCatalogSeedPlanError(
            f"untrusted evidence source for item {item_id} group {group}"
        )
    try:
        line_number = _strict_positive_int(
            value["lineNumber"],
            label=f"evidence line for item {item_id} group {group}",
        )
    except UsedPhoneCatalogSeedPlanError as exc:
        raise UsedPhoneCatalogSeedPlanError(str(exc)) from None
    if not isinstance(value["rawValue"], str) or not value["rawValue"]:
        raise UsedPhoneCatalogSeedPlanError(
            f"invalid raw evidence for item {item_id} group {group}"
        )
    if not isinstance(value["matchedRawTokens"], list) or not all(
        isinstance(token, str) and token for token in value["matchedRawTokens"]
    ):
        raise UsedPhoneCatalogSeedPlanError(
            f"invalid matched tokens for item {item_id} group {group}"
        )
    return {
        "field": "attr_value",
        "lineNumber": line_number,
        "matchedRawTokens": list(value["matchedRawTokens"]),
        "rawValue": value["rawValue"],
        "source": "relevance",
    }


def _product_plan(
    row: Mapping[str, Any],
    *,
    catalog_line: int,
    line_owners: dict[int, tuple[str, str]],
    audit_line_owners: dict[int, str],
) -> tuple[dict[str, Any], list[dict[str, Any]], Counter[str]]:
    item_id, product_id = _canonical_source_item_id(
        row.get("itemId"), catalog_line=catalog_line
    )
    if row.get("schemaVersion") != "used-phone-attribute-contract-v2":
        raise UsedPhoneCatalogSeedPlanError(f"wrong schemaVersion for item {item_id}")
    if row.get("datasetRevision") != DATASET_REVISION:
        raise UsedPhoneCatalogSeedPlanError(f"wrong revision for item {item_id}")
    if row.get("categoryKey") != TARGET_CATEGORY_KEY or tuple(
        row.get("categoryPath") or []
    ) != TARGET_CATEGORY_PATH:
        raise UsedPhoneCatalogSeedPlanError(f"wrong category for item {item_id}")
    for field in ("title", "brand", "seller"):
        if not isinstance(row.get(field), str) or not row[field].strip():
            raise UsedPhoneCatalogSeedPlanError(
                f"missing {field} for item {item_id}"
            )
    if (
        len(row["title"]) > 512
        or len(row["brand"]) > 128
        or len(row["seller"]) > 255
        or len(item_id) > 128
    ):
        raise UsedPhoneCatalogSeedPlanError(
            f"database text width exceeded for item {item_id}"
        )
    provenance = row.get("provenance")
    if not isinstance(provenance, dict):
        raise UsedPhoneCatalogSeedPlanError(f"missing provenance for item {item_id}")
    audit_line = provenance.get("evidenceProductAuditLineNumber")
    source_ref_count = provenance.get("sourceRefCount")
    unique_ref_count = provenance.get("uniqueRefCount")
    try:
        audit_line = _strict_positive_int(
            audit_line, label=f"audit line for item {item_id}"
        )
        source_ref_count = _strict_positive_int(
            source_ref_count, label=f"source ref count for item {item_id}"
        )
        unique_ref_count = _strict_positive_int(
            unique_ref_count, label=f"unique ref count for item {item_id}"
        )
    except UsedPhoneCatalogSeedPlanError:
        raise UsedPhoneCatalogSeedPlanError(
            f"invalid provenance identity for item {item_id}"
        ) from None
    if source_ref_count < unique_ref_count:
        raise UsedPhoneCatalogSeedPlanError(
            f"invalid provenance identity for item {item_id}"
        )
    audit_owner = audit_line_owners.setdefault(audit_line, item_id)
    if audit_owner != item_id:
        raise UsedPhoneCatalogSeedPlanError(
            f"evidence product audit line {audit_line} is bound to multiple items"
        )

    attributes = row.get("attributes")
    expected_groups = set(USED_PHONE_ATTRIBUTE_REGISTRY)
    if not isinstance(attributes, dict) or set(attributes) != expected_groups:
        raise UsedPhoneCatalogSeedPlanError(
            f"seven-group mismatch for item {item_id}"
        )

    unique_refs: dict[tuple[int, str], dict[str, Any]] = {}
    expected_by_group: dict[str, tuple[str, str | None]] = {}
    status_counts: Counter[str] = Counter()
    for group in sorted(expected_groups):
        observation = attributes[group]
        if not isinstance(observation, dict) or observation.get("key") != group:
            raise UsedPhoneCatalogSeedPlanError(
                f"invalid observation for item {item_id} group {group}"
            )
        if (
            observation.get("semanticStatus")
            != "controlled_interpretation_not_source_ground_truth"
            or observation.get("humanConfirmed") is not False
        ):
            raise UsedPhoneCatalogSeedPlanError(
                f"invalid semantic boundary for item {item_id} group {group}"
            )
        matched_tokens = observation.get("matchedRawTokens")
        if not isinstance(matched_tokens, list) or not all(
            isinstance(token, str) and token for token in matched_tokens
        ):
            raise UsedPhoneCatalogSeedPlanError(
                f"invalid observation tokens for item {item_id} group {group}"
            )
        status = observation.get("status")
        value = observation.get("value")
        if status not in {"known", "conflict", "unknown"}:
            raise UsedPhoneCatalogSeedPlanError(
                f"invalid status for item {item_id} group {group}"
            )
        if status == "known":
            if value not in USED_PHONE_ATTRIBUTE_REGISTRY[group].allowed_values:
                raise UsedPhoneCatalogSeedPlanError(
                    f"invalid canonical value for item {item_id} group {group}"
                )
        elif value is not None:
            raise UsedPhoneCatalogSeedPlanError(
                f"non-known observation carries value for item {item_id} group {group}"
            )
        refs_value = observation.get("evidenceRefs")
        if not isinstance(refs_value, list):
            raise UsedPhoneCatalogSeedPlanError(
                f"invalid evidenceRefs for item {item_id} group {group}"
            )
        refs = [
            _validated_ref(
                ref,
                item_id=item_id,
                group=group,
                catalog_line=catalog_line,
            )
            for ref in refs_value
        ]
        ref_tokens = {
            token for ref in refs for token in ref["matchedRawTokens"]
        }
        if ref_tokens != set(matched_tokens):
            raise UsedPhoneCatalogSeedPlanError(
                f"observation/ref token mismatch for item {item_id} group {group}"
            )
        if status == "unknown" and refs:
            raise UsedPhoneCatalogSeedPlanError(
                f"unknown observation has evidence for item {item_id} group {group}"
            )
        if status != "unknown" and not refs:
            raise UsedPhoneCatalogSeedPlanError(
                f"known/conflict observation lacks evidence for item {item_id} group {group}"
            )
        for ref in refs:
            identity = (ref["lineNumber"], ref["rawValue"])
            unique_refs.setdefault(identity, ref)
            owner = line_owners.setdefault(
                ref["lineNumber"], (item_id, ref["rawValue"])
            )
            if owner != (item_id, ref["rawValue"]):
                raise UsedPhoneCatalogSeedPlanError(
                    f"evidence line {ref['lineNumber']} is bound to multiple products"
                )
        expected_by_group[group] = (status, value)
        status_counts[f"{group}:{status}"] += 1

    ordered_refs = [unique_refs[key] for key in sorted(unique_refs)]
    if ordered_refs and len(ordered_refs) != unique_ref_count:
        raise UsedPhoneCatalogSeedPlanError(
            f"controlled evidence ref count mismatch for item {item_id}"
        )
    raw_value = ",".join(ref["rawValue"] for ref in ordered_refs)
    if len(raw_value) > 512:
        raise UsedPhoneCatalogSeedPlanError(
            f"product_attribute.raw_value exceeds 512 characters for item {item_id}"
        )
    observed = observe_used_phone_attributes(raw_value)
    for group in sorted(expected_groups):
        fact_value = observed[group].fact.value if observed[group].fact else None
        if (observed[group].status, fact_value) != expected_by_group[group]:
            raise UsedPhoneCatalogSeedPlanError(
                f"frozen/production observation mismatch for item {item_id} group {group}"
            )
        if set(observed[group].matched_raw_tokens) != set(
            attributes[group]["matchedRawTokens"]
        ):
            raise UsedPhoneCatalogSeedPlanError(
                f"frozen/production token mismatch for item {item_id} group {group}"
            )

    materialized = list(materialize_used_phone_product_attributes(raw_value))
    materialized_by_key = {str(value["key"]): value for value in materialized}
    expected_materialized = {
        group for group, (status, _) in expected_by_group.items() if status != "unknown"
    }
    if set(materialized_by_key) != expected_materialized:
        raise UsedPhoneCatalogSeedPlanError(
            f"materialized attribute mismatch for item {item_id}"
        )
    _strict_positive_int(product_id, label=f"product id for item {item_id}")
    attribute_rows = []
    for group in sorted(materialized_by_key):
        value = materialized_by_key[group]
        attribute_rows.append(
            {
                "attribute_key": group,
                "confidence": value["confidence"],
                "evidence_field": value["evidenceField"],
                "extraction_method": value["extractionMethod"],
                "normalized_boolean": None,
                "normalized_number": None,
                "normalized_text": value["normalizedText"],
                "product_id": product_id,
                "raw_value": value["rawValue"],
                "unit": value["unit"],
                "value_type": value["valueType"],
            }
        )

    product_row = {
        "attribute_text": raw_value,
        "brand": row["brand"].strip(),
        "category_l1": TARGET_CATEGORY_PATH[0],
        "category_l2": TARGET_CATEGORY_PATH[1],
        "category_l3": TARGET_CATEGORY_PATH[2],
        "currency": None,
        "data_nature": "historical_dataset_snapshot",
        "dataset_revision": DATASET_REVISION,
        "id": product_id,
        "price_status": "unverified",
        "provenance_url": SOURCE_URL,
        "seller": row["seller"].strip(),
        "snapshot_price_minor": None,
        "source": "kuaisearch",
        "source_item_id": item_id,
        "source_license": SOURCE_LICENSE,
        "title": row["title"].strip(),
    }
    return product_row, attribute_rows, status_counts


def _manifest(
    *,
    catalog_sha: str,
    product_sha: str,
    attribute_sha: str,
    audit_sha: str,
    product_count: int,
    attribute_count: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "boundaries": {
            "databaseConnected": False,
            "databaseWritten": False,
            "modelUsed": False,
            "networkUsed": False,
            "requiresDedicatedBenchmarkDatabase": True,
            "titleBrandSellerUsedForAttributeFact": False,
            "unknownProducesAttributeRow": False,
            "conflictProducesAssertion": False,
        },
        "dataset": {
            "categoryKey": TARGET_CATEGORY_KEY,
            "id": DATASET_ID,
            "revision": DATASET_REVISION,
            "sourceUrl": SOURCE_URL,
        },
        "input": {
            "artifactRelativePath": FROZEN_CATALOG_ARTIFACT,
            "basename": "catalog.jsonl",
            "locationBoundary": "upstream_independent_worktree_frozen_artifact",
            "sha256": catalog_sha,
        },
        "loadContract": {
            "cacheInvalidation": "after_successful_commit_only",
            "product": "upsert_by_primary_and_source_item_identity",
            "productAttribute": "replace_for_planned_product_ids_only",
            "rollbackOnAnyFailure": True,
            "transaction": "single_dedicated_benchmark_database_transaction",
        },
        "outputs": {
            "audit.json": {"sha256": audit_sha},
            "manifest.json": {
                "hashConvention": "canonical_manifest_with_self_sha_zeroed",
                "sha256": ZERO_SHA256,
            },
            "product.jsonl": {"rowCount": product_count, "sha256": product_sha},
            "product_attribute.jsonl": {
                "rowCount": attribute_count,
                "sha256": attribute_sha,
            },
        },
        "ruleset": {
            "evidenceField": USED_PHONE_ATTRIBUTE_EVIDENCE_FIELD,
            "sha256": used_phone_attribute_ruleset_sha256(),
            "version": USED_PHONE_ATTRIBUTE_RULESET_VERSION,
        },
        "schemaVersion": MANIFEST_SCHEMA_VERSION,
    }
    payload["outputs"]["manifest.json"]["sha256"] = _sha256_bytes(
        _canonical_line(payload)
    )
    return payload


def build_used_phone_catalog_seed_plan(
    *, catalog_path: Path, output_dir: Path
) -> dict[str, Any]:
    """Validate the pinned catalog and atomically publish a DB row plan."""

    catalog_path = Path(catalog_path)
    output_dir = Path(output_dir)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    if catalog_path.name != "catalog.jsonl":
        raise UsedPhoneCatalogSeedPlanError("input must be named catalog.jsonl")
    catalog_sha = _sha256_file(catalog_path)
    if catalog_sha != EXPECTED_CATALOG_SHA256:
        raise UsedPhoneCatalogSeedPlanError(
            f"catalog SHA mismatch: {catalog_sha}"
        )
    if used_phone_attribute_ruleset_sha256() != EXPECTED_RULESET_SHA256:
        raise UsedPhoneCatalogSeedPlanError("production ruleset SHA mismatch")

    parent = output_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=parent))
    try:
        products: list[dict[str, Any]] = []
        product_attributes: list[dict[str, Any]] = []
        seen_items: set[str] = set()
        line_owners: dict[int, tuple[str, str]] = {}
        audit_line_owners: dict[int, str] = {}
        status_counts: Counter[str] = Counter()
        with catalog_path.open(encoding="utf-8") as stream:
            for catalog_line, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                row = json.loads(line)
                item_id = str(row.get("itemId") or "")
                if item_id in seen_items:
                    raise UsedPhoneCatalogSeedPlanError(
                        f"duplicate itemId {item_id}"
                    )
                seen_items.add(item_id)
                product, attributes, counts = _product_plan(
                    row,
                    catalog_line=catalog_line,
                    line_owners=line_owners,
                    audit_line_owners=audit_line_owners,
                )
                products.append(product)
                product_attributes.extend(attributes)
                status_counts.update(counts)

        if len(products) != EXPECTED_PRODUCT_COUNT:
            raise UsedPhoneCatalogSeedPlanError(
                f"expected {EXPECTED_PRODUCT_COUNT} products, got {len(products)}"
            )
        actual_status_counts = {
            group: {
                status: status_counts[f"{group}:{status}"]
                for status in ("known", "conflict", "unknown")
            }
            for group in sorted(USED_PHONE_ATTRIBUTE_REGISTRY)
        }
        expected_counts = {
            group: {
                status: int(EXPECTED_STATUS_COUNTS[group].get(status, 0))
                for status in ("known", "conflict", "unknown")
            }
            for group in sorted(EXPECTED_STATUS_COUNTS)
        }
        if actual_status_counts != expected_counts:
            raise UsedPhoneCatalogSeedPlanError(
                "seven-group status counts do not match the frozen contract"
            )

        products.sort(key=lambda row: row["id"])
        product_attributes.sort(
            key=lambda row: (row["product_id"], row["attribute_key"])
        )
        product_path = stage / "product.jsonl"
        attribute_path = stage / "product_attribute.jsonl"
        with product_path.open("wb") as stream:
            for row in products:
                stream.write(_canonical_line(row))
        with attribute_path.open("wb") as stream:
            for row in product_attributes:
                stream.write(_canonical_line(row))

        audit = {
            "attributeRowCount": len(product_attributes),
            "catalogSha256": catalog_sha,
            "databaseConnected": False,
            "databaseWritten": False,
            "evidenceProductAuditLineOwnerCount": len(audit_line_owners),
            "evidenceLineOwnerCount": len(line_owners),
            "productCount": len(products),
            "rulesetSha256": used_phone_attribute_ruleset_sha256(),
            "schemaVersion": AUDIT_SCHEMA_VERSION,
            "statusCounts": actual_status_counts,
        }
        audit_path = stage / "audit.json"
        audit_path.write_bytes(_canonical_line(audit))
        manifest = _manifest(
            catalog_sha=catalog_sha,
            product_sha=_sha256_file(product_path),
            attribute_sha=_sha256_file(attribute_path),
            audit_sha=_sha256_file(audit_path),
            product_count=len(products),
            attribute_count=len(product_attributes),
        )
        manifest_path = stage / "manifest.json"
        manifest_path.write_bytes(_canonical_line(manifest))
        stage.replace(output_dir)
        return {
            "audit": audit,
            "manifest": manifest,
            "manifestFileSha256": _sha256_file(output_dir / "manifest.json"),
        }
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


__all__ = [
    "FROZEN_CATALOG_ARTIFACT",
    "UsedPhoneCatalogSeedPlanError",
    "build_used_phone_catalog_seed_plan",
]
