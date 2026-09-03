"""Build a query-independent exact product-aspect sidecar for Memory V14."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation import audit_shopping_companion_catalog_v14_2 as identity


SOURCE_REVISION = "9a8a2a1c13f0d88de070238352bcf71f98ca851f"
EXPECTED_SOURCE_BYTES = 20_030_340_587
EXPECTED_SOURCE_ROWS = 1_298_797
EXPECTED_SOURCE_SHA256 = "95b42f63e458d62337fc21294b148e489275ede8c283db1ff1061173a843721f"
IDENTITY_SOURCE_SHA256 = "27a17305f36b8fbc24f28853e3a09aa2af0eeafb67fd8df868e3fb4b19873b7d"


class AspectBuildError(RuntimeError):
    """Fail-closed aspect sidecar construction error."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def product_aspects(product: dict[str, Any], shapes: Counter[str]) -> set[tuple[str, str]]:
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
    output: set[tuple[str, str]] = set()
    for source in sources:
        for raw_key, raw_values in source.items():
            values = identity.scalar_values(raw_values, shapes)
            if len(values) > 1:
                shapes["multiValuedField"] += 1
            key = identity.attribute_key(raw_key)
            output.update((key, identity.slug(value, limit=128)) for value in values)
    return output


def _open_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA cache_size=-131072")
    connection.executescript("""
        CREATE TABLE product_aspect (
            product_rowid INTEGER NOT NULL,
            attribute_key TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            PRIMARY KEY(product_rowid,attribute_key,normalized_value)
        ) WITHOUT ROWID;
        CREATE TABLE build_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    return connection


def build_sidecar(
    catalog_path: Path,
    database_path: Path,
    *,
    expected_bytes: int = EXPECTED_SOURCE_BYTES,
    expected_rows: int = EXPECTED_SOURCE_ROWS,
    expected_sha256: str = EXPECTED_SOURCE_SHA256,
    minimum_free_bytes: int = 20 * 1024 * 1024 * 1024,
    progress_every: int = 100_000,
) -> dict[str, Any]:
    if database_path.exists():
        raise FileExistsError(f"refusing to overwrite {database_path}")
    dependency_path = Path(identity.__file__).resolve()
    if sha256(dependency_path) != IDENTITY_SOURCE_SHA256:
        raise AspectBuildError("catalog identity dependency hash mismatch")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    free_before = shutil.disk_usage(database_path.parent).free
    if free_before < minimum_free_bytes:
        raise AspectBuildError("insufficient aspect sidecar volume space")

    started = time.perf_counter()
    digest = hashlib.sha256()
    source_bytes = 0
    source_rows = 0
    aspect_rows = 0
    products_with_aspects = 0
    shapes: Counter[str] = Counter()
    batch: list[tuple[int, str, str]] = []
    connection = _open_database(database_path)
    try:
        connection.execute("BEGIN")
        with catalog_path.open("rb") as stream:
            for source_rows, raw in enumerate(stream, 1):
                digest.update(raw)
                source_bytes += len(raw)
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AspectBuildError(f"invalid catalog JSON at line {source_rows}") from exc
                if type(row) is not dict or type(row.get("product")) is not dict:
                    raise AspectBuildError(f"invalid catalog product at line {source_rows}")
                aspects = product_aspects(row["product"], shapes)
                products_with_aspects += bool(aspects)
                aspect_rows += len(aspects)
                batch.extend((source_rows, key, value) for key, value in sorted(aspects))
                if len(batch) >= 100_000:
                    connection.executemany(
                        "INSERT INTO product_aspect(product_rowid,attribute_key,normalized_value) VALUES(?,?,?)",
                        batch,
                    )
                    batch.clear()
                if progress_every > 0 and source_rows % progress_every == 0:
                    if batch:
                        connection.executemany(
                            "INSERT INTO product_aspect(product_rowid,attribute_key,normalized_value) VALUES(?,?,?)",
                            batch,
                        )
                        batch.clear()
                    connection.commit()
                    connection.execute("BEGIN")
                    print(json.dumps({"phase": "ASPECT_ROWS", "products": source_rows, "aspects": aspect_rows}), flush=True)
        if batch:
            connection.executemany(
                "INSERT INTO product_aspect(product_rowid,attribute_key,normalized_value) VALUES(?,?,?)",
                batch,
            )
        connection.commit()
        identity_gates = {
            "sourceBytesExact": source_bytes == expected_bytes,
            "sourceRowsExact": source_rows == expected_rows,
            "sourceSha256Exact": digest.hexdigest() == expected_sha256,
            "invalidShapesZero": not any(
                shapes[key] for key in (
                    "invalidAttributesShape", "invalidOptionsShape", "invalidOptionShape", "unsupportedNested"
                )
            ),
        }
        if not all(identity_gates.values()):
            raise AspectBuildError(f"source or aspect shape gate failed: {identity_gates}")
        actual_rows = connection.execute("SELECT count(*) FROM product_aspect").fetchone()[0]
        if actual_rows != aspect_rows:
            raise AspectBuildError("aspect row cardinality mismatch")
        print(json.dumps({"phase": "LOOKUP_INDEX", "aspectRows": aspect_rows}), flush=True)
        connection.execute(
            "CREATE INDEX aspect_lookup ON product_aspect(attribute_key,normalized_value,product_rowid)"
        )
        metadata = {
            "schemaVersion": "shopping-memory-v14-aspect-sidecar-v1",
            "sourceRevision": SOURCE_REVISION,
            "sourceSha256": digest.hexdigest(),
            "sourceRows": str(source_rows),
            "aspectRows": str(aspect_rows),
            "identitySourceSha256": IDENTITY_SOURCE_SHA256,
        }
        connection.executemany(
            "INSERT INTO build_metadata(key,value) VALUES(?,?)", sorted(metadata.items())
        )
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise AspectBuildError(f"SQLite quick_check failed: {quick_check}")
    finally:
        connection.close()

    return {
        "schemaVersion": "shopping-memory-v14-aspect-sidecar-build-report-v1",
        "decision": "FULL_CATALOG_ASPECT_SIDECAR_ACCEPT",
        "source": {
            "path": str(catalog_path), "revision": SOURCE_REVISION,
            "bytes": source_bytes, "rows": source_rows, "sha256": digest.hexdigest(),
        },
        "sidecar": {
            "path": str(database_path), "bytes": database_path.stat().st_size,
            "sha256": sha256(database_path), "aspectRows": aspect_rows,
            "productsWithAspects": products_with_aspects,
            "productsWithoutAspects": source_rows - products_with_aspects,
            "quickCheck": "ok", "freeBytesBefore": free_before,
        },
        "shapeCounters": dict(sorted(shapes.items())),
        "identityDependency": {"path": str(dependency_path), "sha256": IDENTITY_SOURCE_SHA256},
        "elapsedSeconds": time.perf_counter() - started,
        "explicitBoundaries": {
            "queryExecution": False, "qualityRun": False,
            "validationRead": False, "sealedRead": False,
            "productionSwitchAuthority": False,
        },
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def materialize(
    final_database_path: Path,
    receipt_dir: Path,
    report: dict[str, Any],
    *,
    builder_path: Path,
    contract_path: Path,
) -> Path:
    if final_database_path.exists():
        raise FileExistsError(f"refusing to overwrite {final_database_path}")
    if receipt_dir.exists():
        raise FileExistsError(f"refusing to overwrite {receipt_dir}")
    building_path = Path(report["sidecar"]["path"])
    os.replace(building_path, final_database_path)
    report["sidecar"]["path"] = str(final_database_path)
    receipt_dir.mkdir(parents=True)
    report_path = receipt_dir / "report.json"
    _write_json(report_path, report)
    receipt = {
        "schemaVersion": "shopping-memory-v14-aspect-sidecar-build-receipt-v1",
        "decision": report["decision"],
        "sourceSha256": report["source"]["sha256"],
        "databaseSha256": report["sidecar"]["sha256"],
        "builderSha256": sha256(builder_path),
        "identitySourceSha256": IDENTITY_SOURCE_SHA256,
        "contractSha256": sha256(contract_path),
        "reportSha256": sha256(report_path),
        "sealedAccess": "NONE",
    }
    receipt_path = receipt_dir / "receipt.json"
    _write_json(receipt_path, receipt)
    (receipt_dir / "SHA256SUMS.txt").write_text(
        f"{sha256(report_path)}  report.json\n{sha256(receipt_path)}  receipt.json\n", encoding="ascii"
    )
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--building-database", type=Path, required=True)
    parser.add_argument("--final-database", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    report = build_sidecar(args.catalog, args.building_database)
    print(materialize(
        args.final_database, args.receipt_dir, report,
        builder_path=Path(__file__), contract_path=args.contract,
    ))


if __name__ == "__main__":
    main()
