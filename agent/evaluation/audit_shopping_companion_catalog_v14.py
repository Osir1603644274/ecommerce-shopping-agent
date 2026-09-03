"""Audit the pinned Shopping Companion catalog before any V14 quality run.

The auditor is deliberately query-independent.  It reads only the full product
catalog plus the public train-derived V13 catalog references.  It never reads a
validation, test, sealed, mapping, ranking, or oracle file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Iterable


SOURCE_REVISION = "9a8a2a1c13f0d88de070238352bcf71f98ca851f"
CATALOG_REVISION = f"shopping-companion-{SOURCE_REVISION[:12]}"
EXPECTED_CATALOG_BYTES = 20_030_340_587
EXPECTED_CATALOG_ROWS = 1_298_797
EXPECTED_CATALOG_SHA256 = "95b42f63e458d62337fc21294b148e489275ede8c283db1ff1061173a843721f"
EXPECTED_CATALOG_VALUES_SHA256 = "ceea5d32202ba533fa336535047e74b59e105508d6dd1dcac80cf6912c27b8e6"
EXPECTED_TRAIN_TARGETS_SHA256 = "f74d743d2ba3718fbcf6cedcf2253f14325953d806ec1bfe747c20c005f65252"

TupleKey = tuple[str, str, str]


class CatalogAuditError(RuntimeError):
    """Fail-closed input or materialization error."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slug(value: object, *, limit: int) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    if not text:
        text = "unknown-" + hashlib.sha256(str(value).encode()).hexdigest()[:12]
    return text[:limit].rstrip("-")


def attribute_key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    if not text or not text[0].isalpha():
        text = "attr_" + hashlib.sha256(str(value).encode()).hexdigest()[:12]
    return text[:64].rstrip("_")


def category_id(value: object) -> str:
    """Match benchmark leaf categories to full-catalog breadcrumb categories."""
    parts = [part.strip() for part in str(value).split(" - ") if part.strip()]
    return slug(parts[-1] if parts else value, limit=64)


def jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CatalogAuditError(f"invalid JSONL at {path}:{number}") from exc
            if type(value) is not dict:
                raise CatalogAuditError(f"JSONL row is not an object at {path}:{number}")
            yield value


def load_references(
    catalog_values_path: Path,
    train_targets_path: Path,
    *,
    enforce_pinned_hashes: bool = True,
) -> tuple[set[TupleKey], set[TupleKey], set[str]]:
    if enforce_pinned_hashes:
        if sha256(catalog_values_path) != EXPECTED_CATALOG_VALUES_SHA256:
            raise CatalogAuditError("public catalog-values hash mismatch")
        if sha256(train_targets_path) != EXPECTED_TRAIN_TARGETS_SHA256:
            raise CatalogAuditError("public train-target-catalog hash mismatch")

    preferences: set[TupleKey] = set()
    for row in jsonl(catalog_values_path):
        if row.get("catalogRevision") != CATALOG_REVISION:
            raise CatalogAuditError("catalog-values revision mismatch")
        preferences.add((
            str(row.get("categoryId")),
            str(row.get("attributeKey")),
            str(row.get("normalizedValue")),
        ))

    target_aspects: set[TupleKey] = set()
    target_ids: set[str] = set()
    for row in jsonl(train_targets_path):
        category_id = str(row.get("categoryId"))
        product_id = str(row.get("productId"))
        aspects = row.get("aspects")
        if not category_id or not product_id or type(aspects) is not list:
            raise CatalogAuditError("invalid public train target")
        target_ids.add(product_id)
        for aspect in aspects:
            if type(aspect) is not dict:
                raise CatalogAuditError("invalid public train target aspect")
            target_aspects.add((
                category_id,
                str(aspect.get("attributeKey")),
                str(aspect.get("normalizedValue")),
            ))
    if not preferences or not target_aspects or not target_ids:
        raise CatalogAuditError("empty public references")
    return preferences, target_aspects, target_ids


def scalar_values(raw: object, shapes: Counter[str]) -> list[object]:
    if raw is None:
        shapes["null"] += 1
        return []
    values = raw if type(raw) is list else [raw]
    shapes["list" if type(raw) is list else "scalar"] += 1
    result: list[object] = []
    for value in values:
        if type(value) in {str, int, float, bool}:
            result.append(value)
        elif value is None:
            shapes["nestedNull"] += 1
        else:
            shapes["unsupportedNested"] += 1
    return result


def product_tuples(product: dict[str, Any], shapes: Counter[str]) -> set[TupleKey]:
    category = product.get("category")
    if type(category) is not str or not category.strip():
        shapes["missingCategory"] += 1
        return set()
    normalized_category = category_id(category)
    sources: list[dict[str, Any]] = []
    attributes = product.get("attributes")
    if type(attributes) is dict:
        sources.append(attributes)
        shapes["attributesObject"] += 1
    elif attributes is not None:
        shapes["invalidAttributesShape"] += 1

    options = product.get("options")
    if type(options) is list:
        shapes["optionsList"] += 1
        for option in options:
            if type(option) is dict:
                sources.append(option)
            else:
                shapes["invalidOptionShape"] += 1
    elif options is not None:
        shapes["invalidOptionsShape"] += 1

    result: set[TupleKey] = set()
    for source in sources:
        for raw_key, raw_values in source.items():
            values = scalar_values(raw_values, shapes)
            if len(values) > 1:
                shapes["multiValuedField"] += 1
            key = attribute_key(raw_key)
            for value in values:
                result.add((normalized_category, key, slug(value, limit=128)))
    return result


def audit_catalog(
    catalog_path: Path,
    preferences: set[TupleKey],
    target_aspects: set[TupleKey],
    target_ids: set[str],
    *,
    expected_bytes: int,
    expected_rows: int,
    expected_sha256: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    required = preferences | target_aspects
    support = {key: 0 for key in required}
    seen_target_ids: set[str] = set()
    shapes: Counter[str] = Counter()
    digest = hashlib.sha256()
    row_count = 0
    byte_count = 0
    parse_errors: list[dict[str, object]] = []

    with catalog_path.open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            digest.update(raw)
            byte_count += len(raw)
            row_count += 1
            try:
                row = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if len(parse_errors) < 20:
                    parse_errors.append({"line": number, "error": type(exc).__name__})
                continue
            if type(row) is not dict:
                shapes["rowNotObject"] += 1
                continue
            product_id = str(row.get("id"))
            if product_id in target_ids:
                seen_target_ids.add(product_id)
            product = row.get("product")
            if type(product) is not dict:
                shapes["productNotObject"] += 1
                continue
            embedded_id = product.get("product_id")
            if embedded_id is not None and str(embedded_id) != product_id:
                shapes["productIdMismatch"] += 1
            tuples = product_tuples(product, shapes)
            for key in tuples & required:
                support[key] += 1

    actual_sha256 = digest.hexdigest()
    preference_covered = sum(support[key] > 0 for key in preferences)
    target_covered = sum(support[key] > 0 for key in target_aspects)
    preference_shared_five = sum(support[key] >= 5 for key in preferences)
    gates = {
        "sourceBytesExact": byte_count == expected_bytes,
        "sourceRowsExact": row_count == expected_rows,
        "sourceSha256Exact": actual_sha256 == expected_sha256,
        "jsonParseErrorsZero": not parse_errors,
        "rowShapeErrorsZero": shapes["rowNotObject"] == 0 and shapes["productNotObject"] == 0,
        "trainTargetIdsPresent": len(seen_target_ids) == len(target_ids),
        "trainTargetAspectCoverageOne": target_covered == len(target_aspects),
        "publicPreferenceCoverageAtLeast095": preference_covered / len(preferences) >= 0.95,
    }
    decision = "CATALOG_STRUCTURE_ACCEPT" if all(gates.values()) else "HOLD_CATALOG_STRUCTURE"
    report = {
        "schemaVersion": "shopping-memory-v14-catalog-structure-audit-v1",
        "decision": decision,
        "scope": "query-independent public-train structural audit; no validation/test/sealed access",
        "source": {
            "revision": SOURCE_REVISION,
            "catalogRevision": CATALOG_REVISION,
            "path": str(catalog_path),
            "bytes": byte_count,
            "rows": row_count,
            "sha256": actual_sha256,
        },
        "references": {
            "preferenceTuples": len(preferences),
            "trainTargetAspectTuples": len(target_aspects),
            "trainTargetProductIds": len(target_ids),
        },
        "coverage": {
            "preferenceTuplesPresent": preference_covered,
            "preferenceTupleRate": preference_covered / len(preferences),
            "preferenceTuplesSharedByAtLeastFiveProducts": preference_shared_five,
            "preferenceSharedByAtLeastFiveRate": preference_shared_five / len(preferences),
            "trainTargetAspectTuplesPresent": target_covered,
            "trainTargetAspectRate": target_covered / len(target_aspects),
            "trainTargetProductIdsPresent": len(seen_target_ids),
            "trainTargetProductIdRate": len(seen_target_ids) / len(target_ids),
        },
        "shapeCounters": dict(sorted(shapes.items())),
        "parseErrors": parse_errors,
        "gates": gates,
        "explicitBoundaries": {
            "qualityRun": False,
            "sealedRead": False,
            "productionSwitchAuthority": False,
            "globalUniqueProductIdAudit": "NOT_EVALUATED_SOURCE_HASH_BINDS_OFFICIAL_SNAPSHOT",
        },
    }
    support_rows = []
    for key in sorted(required):
        roles = []
        if key in preferences:
            roles.append("publicPreference")
        if key in target_aspects:
            roles.append("trainTargetAspect")
        support_rows.append({
            "categoryId": key[0],
            "attributeKey": key[1],
            "normalizedValue": key[2],
            "productSupport": support[key],
            "referenceRoles": roles,
        })
    return report, support_rows


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def materialize(
    output_dir: Path,
    report: dict[str, Any],
    support_rows: list[dict[str, Any]],
    *,
    catalog_values_path: Path,
    train_targets_path: Path,
) -> Path:
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite {output_dir}")
    output_dir.mkdir(parents=True)
    report_path = output_dir / "report.json"
    support_path = output_dir / "tuple-support.jsonl"
    write_json(report_path, report)
    with support_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in support_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    receipt = {
        "schemaVersion": "shopping-memory-v14-catalog-audit-receipt-v1",
        "decision": report["decision"],
        "sourceRevision": SOURCE_REVISION,
        "catalogSha256": report["source"]["sha256"],
        "catalogValuesSha256": sha256(catalog_values_path),
        "trainTargetCatalogSha256": sha256(train_targets_path),
        "reportSha256": sha256(report_path),
        "tupleSupportSha256": sha256(support_path),
        "sealedAccess": "NONE",
    }
    receipt_path = output_dir / "receipt.json"
    write_json(receipt_path, receipt)
    files = [report_path, support_path, receipt_path]
    (output_dir / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in files),
        encoding="ascii",
    )
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--catalog-values", type=Path, required=True)
    parser.add_argument("--train-target-catalog", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    preferences, targets, target_ids = load_references(
        args.catalog_values,
        args.train_target_catalog,
        enforce_pinned_hashes=True,
    )
    report, support_rows = audit_catalog(
        args.catalog,
        preferences,
        targets,
        target_ids,
        expected_bytes=EXPECTED_CATALOG_BYTES,
        expected_rows=EXPECTED_CATALOG_ROWS,
        expected_sha256=EXPECTED_CATALOG_SHA256,
    )
    path = materialize(
        args.output_dir,
        report,
        support_rows,
        catalog_values_path=args.catalog_values,
        train_targets_path=args.train_target_catalog,
    )
    print(path)


if __name__ == "__main__":
    main()
