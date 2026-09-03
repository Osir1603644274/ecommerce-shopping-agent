"""Freeze all synthetic used-phone reference prices before price-based cases."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from agent.evaluation.used_phone_synthetic_price_v1 import (
    DATA_NATURE,
    DEFAULT_SEED,
    EXPECTED_SOURCE_SHA256,
    PRICE_STATUS,
    RULESET_VERSION,
    SCHEMA_VERSION,
    build_rows,
    canonical_json_bytes,
    load_catalog,
    ruleset_sha256,
    sha256_file,
    validate_bundle,
)


def _write_new(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise RuntimeError(f"refusing to overwrite divergent frozen file: {path}")
        return
    path.write_bytes(payload)


def freeze(*, catalog: Path, output: Path) -> dict:
    catalog_rows = load_catalog(catalog)
    prices = build_rows(catalog_rows)
    prices_payload = b"".join(canonical_json_bytes(row) for row in prices)
    prices_path = output / "prices.jsonl"
    _write_new(prices_path, prices_payload)
    generator_sha = sha256_file(Path(__file__).resolve())
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "dataNature": DATA_NATURE,
        "priceStatus": PRICE_STATUS,
        "sourceCatalog": {
            "pathHint": "frozen-public-used-phone-attribute-contract-v2/catalog.jsonl",
            "sha256": EXPECTED_SOURCE_SHA256,
            "rowCount": len(catalog_rows),
            "containsObservedPrice": False,
        },
        "ruleset": {"version": RULESET_VERSION, "sha256": ruleset_sha256()},
        "seed": DEFAULT_SEED,
        "prompt": {
            "used": False,
            "sha256": None,
            "status": "not_used_deterministic_python_rules",
        },
        "generator": {
            "path": "agent/scripts/freeze_used_phone_synthetic_prices_v1.py",
            "sha256": generator_sha,
        },
        "output": {
            "file": "prices.jsonl",
            "sha256": hashlib.sha256(prices_payload).hexdigest(),
            "rowCount": len(prices),
        },
        "policyBoundary": {
            "verifiedSnapshotField": "snapshotPriceMinor",
            "syntheticReferenceField": "syntheticReferencePriceMinor",
            "mayPopulateVerifiedSnapshot": False,
            "maySupportBudgetFilteringOnlyWhenPolicy": "budget_and_ranking",
            "displayDisclosureZh": "模拟参考价 / AI 合成，非真实报价",
        },
    }
    _write_new(output / "manifest.json", canonical_json_bytes(manifest))
    validate_bundle(output)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = freeze(catalog=args.catalog, output=args.output)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
