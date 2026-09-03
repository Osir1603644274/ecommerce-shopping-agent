"""Verify every Java product projection against the pinned DB seed plan."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.load_used_phone_benchmark_db import (
    CATALOG_VERSION,
    EXPECTED_ATTRIBUTE_COUNT,
    EXPECTED_PRODUCT_COUNT,
    PRODUCT_SHA256,
    load_seed_plan,
)


PRODUCT_FIELD_MAP = {
    "attribute_text": "attributeText",
    "brand": "brand",
    "category_l1": "categoryL1",
    "category_l2": "categoryL2",
    "category_l3": "categoryL3",
    "currency": "currency",
    "data_nature": "dataNature",
    "dataset_revision": "datasetRevision",
    "id": "id",
    "price_status": "priceStatus",
    "provenance_url": "provenanceUrl",
    "seller": "seller",
    "snapshot_price_minor": "snapshotPriceMinor",
    "source": "source",
    "source_item_id": "sourceItemId",
    "source_license": "sourceLicense",
    "title": "title",
}
ATTRIBUTE_FIELD_MAP = {
    "attribute_key": "key",
    "confidence": "confidence",
    "evidence_field": "evidenceField",
    "extraction_method": "extractionMethod",
    "normalized_boolean": "normalizedBoolean",
    "normalized_number": "normalizedNumber",
    "normalized_text": "normalizedText",
    "raw_value": "rawValue",
    "unit": "unit",
    "value_type": "valueType",
}


class JavaCatalogVerificationError(RuntimeError):
    """Raised when the Java API does not match the frozen seed plan."""


def _json_get(url: str) -> dict[str, Any]:
    with urlopen(url, timeout=10) as response:
        return json.load(response)


def _json_post(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        return json.load(response)


def expected_java_products(seed_dir: Path) -> dict[int, dict[str, Any]]:
    seed = load_seed_plan(seed_dir)
    attributes: dict[int, list[dict[str, Any]]] = {
        int(product["id"]): [] for product in seed.products
    }
    for row in seed.attributes:
        attributes[int(row["product_id"])].append(
            {java_key: row[seed_key] for seed_key, java_key in ATTRIBUTE_FIELD_MAP.items()}
        )
    return {
        int(row["id"]): {
            **{java_key: row[seed_key] for seed_key, java_key in PRODUCT_FIELD_MAP.items()},
            "attributes": attributes[int(row["id"])],
        }
        for row in seed.products
    }


def verify_java_catalog(*, seed_dir: Path, base_url: str) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    expected = expected_java_products(seed_dir)
    manifest_response = _json_get(f"{base_url}/internal/catalog/manifest")
    manifest = manifest_response.get("data") if manifest_response.get("success") else None
    if not isinstance(manifest, dict) or {
        "catalogVersion": manifest.get("catalogVersion"),
        "productCount": manifest.get("productCount"),
        "contentHash": manifest.get("contentHash"),
    } != {
        "catalogVersion": CATALOG_VERSION,
        "productCount": EXPECTED_PRODUCT_COUNT,
        "contentHash": PRODUCT_SHA256,
    }:
        raise JavaCatalogVerificationError("Java catalog manifest mismatch")

    paged_ids: list[int] = []
    after_id: int | None = None
    while True:
        query = {"catalogVersion": CATALOG_VERSION, "limit": 200}
        if after_id is not None:
            query["afterId"] = after_id
        page_response = _json_get(
            f"{base_url}/internal/catalog/products?{urlencode(query)}"
        )
        page = page_response.get("data") if page_response.get("success") else None
        if not isinstance(page, dict):
            raise JavaCatalogVerificationError("Java catalog page failed")
        paged_ids.extend(int(value) for value in page.get("items", []))
        if page.get("complete"):
            break
        after_id = page.get("nextAfterId")
        if type(after_id) is not int:
            raise JavaCatalogVerificationError("Java catalog pagination stalled")
    if paged_ids != sorted(expected):
        raise JavaCatalogVerificationError("Java catalog item identities mismatch")

    actual: dict[int, dict[str, Any]] = {}
    for index in range(0, len(paged_ids), 10):
        batch = paged_ids[index : index + 10]
        response = _json_post(
            f"{base_url}/api/products/resolve", {"productIds": batch}
        )
        rows = response.get("data") if response.get("success") else None
        if not isinstance(rows, list) or len(rows) != len(batch):
            raise JavaCatalogVerificationError("Java resolve batch is incomplete")
        for row in rows:
            if not isinstance(row, dict) or type(row.get("id")) is not int:
                raise JavaCatalogVerificationError("Java resolve identity is invalid")
            normalized = {key: row.get(key) for key in PRODUCT_FIELD_MAP.values()}
            normalized["attributes"] = [
                {key: attribute.get(key) for key in ATTRIBUTE_FIELD_MAP.values()}
                for attribute in row.get("attributes", [])
            ]
            actual[int(row["id"])] = normalized
    if actual != expected:
        mismatches = [product_id for product_id in expected if actual.get(product_id) != expected[product_id]]
        raise JavaCatalogVerificationError(
            f"Java product/attribute projection mismatch: {mismatches[:5]}"
        )
    payload = json.dumps(
        [actual[product_id] for product_id in sorted(actual)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "attributeCount": sum(len(row["attributes"]) for row in actual.values()),
        "catalogVersion": CATALOG_VERSION,
        "javaProjectionSha256": hashlib.sha256(payload).hexdigest(),
        "productCount": len(actual),
        "resolveBatchCount": (len(actual) + 9) // 10,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = verify_java_catalog(seed_dir=args.seed_dir, base_url=args.base_url)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
