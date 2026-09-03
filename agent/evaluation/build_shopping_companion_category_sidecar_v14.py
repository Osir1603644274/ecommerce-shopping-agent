"""Build a query-independent breadcrumb category sidecar for V14 retrieval."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from evaluation.audit_shopping_companion_catalog_v14_2 import category_ids


SOURCE_REVISION = "9a8a2a1c13f0d88de070238352bcf71f98ca851f"
EXPECTED_SOURCE_BYTES = 20_030_340_587
EXPECTED_SOURCE_ROWS = 1_298_797
EXPECTED_SOURCE_SHA256 = "95b42f63e458d62337fc21294b148e489275ede8c283db1ff1061173a843721f"
EXPECTED_FTS_SHA256 = "c23bf88d9116c91d36bef6603a295e2cf921fafb544912a83ae8bb0f545686cb"


class CategoryBuildError(RuntimeError):
    """Fail-closed category sidecar error."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_sidecar(
    catalog_path: Path,
    fts_path: Path,
    database_path: Path,
    *,
    expected_source_bytes: int = EXPECTED_SOURCE_BYTES,
    expected_source_rows: int = EXPECTED_SOURCE_ROWS,
    expected_source_sha256: str = EXPECTED_SOURCE_SHA256,
    expected_fts_sha256: str = EXPECTED_FTS_SHA256,
    minimum_free_bytes: int = 20 * 1024 * 1024 * 1024,
    progress_every: int = 100_000,
) -> dict[str, Any]:
    if database_path.exists():
        raise FileExistsError(f"refusing to overwrite {database_path}")
    if sha256(fts_path) != expected_fts_sha256:
        raise CategoryBuildError("FTS index hash mismatch")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    free_before = shutil.disk_usage(database_path.parent).free
    if free_before < minimum_free_bytes:
        raise CategoryBuildError("insufficient category sidecar disk space")

    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-65536")
    connection.executescript("""
        CREATE TABLE product_category (
            category_id TEXT NOT NULL,
            product_rowid INTEGER NOT NULL,
            PRIMARY KEY(category_id, product_rowid)
        ) WITHOUT ROWID;
        CREATE TABLE build_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    digest = hashlib.sha256()
    source_bytes = 0
    source_rows = 0
    memberships = 0
    batch: list[tuple[str, int]] = []
    started = time.perf_counter()
    try:
        connection.execute("BEGIN")
        with catalog_path.open("rb") as stream:
            for raw in stream:
                digest.update(raw)
                source_bytes += len(raw)
                source_rows += 1
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CategoryBuildError(f"invalid source JSON at line {source_rows}") from exc
                product = row.get("product") if type(row) is dict else None
                category = product.get("category") if type(product) is dict else None
                if type(category) is not str or not category.strip():
                    raise CategoryBuildError(f"missing category at line {source_rows}")
                categories = category_ids(category)
                if not categories:
                    raise CategoryBuildError(f"empty category identity at line {source_rows}")
                for category_id in categories:
                    batch.append((category_id, source_rows))
                memberships += len(categories)
                if len(batch) >= 10_000:
                    connection.executemany(
                        "INSERT INTO product_category(category_id,product_rowid) VALUES(?,?)",
                        batch,
                    )
                    batch.clear()
                if progress_every > 0 and source_rows % progress_every == 0:
                    if batch:
                        connection.executemany(
                            "INSERT INTO product_category(category_id,product_rowid) VALUES(?,?)",
                            batch,
                        )
                        batch.clear()
                    connection.commit()
                    connection.execute("BEGIN")
                    print(json.dumps({"phase": "CATEGORY_INDEXING", "rows": source_rows, "memberships": memberships}), flush=True)
        if batch:
            connection.executemany(
                "INSERT INTO product_category(category_id,product_rowid) VALUES(?,?)",
                batch,
            )
        connection.commit()
        source_identity = {
            "bytes": source_bytes == expected_source_bytes,
            "rows": source_rows == expected_source_rows,
            "sha256": digest.hexdigest() == expected_source_sha256,
        }
        if not all(source_identity.values()):
            raise CategoryBuildError(f"source identity mismatch: {source_identity}")
        stored_memberships = connection.execute("SELECT count(*) FROM product_category").fetchone()[0]
        distinct_rows = connection.execute("SELECT count(DISTINCT product_rowid) FROM product_category").fetchone()[0]
        if stored_memberships != memberships or distinct_rows != source_rows:
            raise CategoryBuildError("category membership cardinality mismatch")
        metadata = {
            "schemaVersion": "shopping-memory-v14-category-sidecar-v1",
            "sourceRevision": SOURCE_REVISION,
            "sourceSha256": digest.hexdigest(),
            "ftsSha256": expected_fts_sha256,
            "productRows": str(source_rows),
            "categoryMemberships": str(memberships),
            "categoryIdentity": "all breadcrumb segments",
        }
        connection.executemany("INSERT INTO build_metadata(key,value) VALUES(?,?)", sorted(metadata.items()))
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise CategoryBuildError(f"SQLite quick_check failed: {quick_check}")
    finally:
        connection.close()
    return {
        "schemaVersion": "shopping-memory-v14-category-sidecar-build-report-v1",
        "decision": "FULL_CATALOG_CATEGORY_SIDECAR_ACCEPT",
        "source": {
            "revision": SOURCE_REVISION,
            "rows": source_rows,
            "bytes": source_bytes,
            "sha256": digest.hexdigest(),
        },
        "ftsSha256": expected_fts_sha256,
        "sidecar": {
            "path": str(database_path),
            "bytes": database_path.stat().st_size,
            "sha256": sha256(database_path),
            "productRows": source_rows,
            "categoryMemberships": memberships,
            "averageMembershipsPerProduct": memberships / source_rows,
            "quickCheck": "ok",
            "freeBytesBefore": free_before,
        },
        "elapsedSeconds": time.perf_counter() - started,
        "explicitBoundaries": {
            "queryExecution": False,
            "qualityRun": False,
            "validationRead": False,
            "sealedRead": False,
            "productionSwitchAuthority": False,
        },
    }


def materialize(
    final_path: Path,
    receipt_dir: Path,
    report: dict[str, Any],
    *,
    builder_path: Path,
    category_identity_path: Path,
    contract_path: Path,
) -> Path:
    if final_path.exists():
        raise FileExistsError(f"refusing to overwrite {final_path}")
    if receipt_dir.exists():
        raise FileExistsError(f"refusing to overwrite {receipt_dir}")
    building_path = Path(report["sidecar"]["path"])
    os.replace(building_path, final_path)
    report["sidecar"]["path"] = str(final_path)
    receipt_dir.mkdir(parents=True)
    report_path = receipt_dir / "report.json"
    _write_json(report_path, report)
    receipt = {
        "schemaVersion": "shopping-memory-v14-category-sidecar-build-receipt-v1",
        "decision": report["decision"],
        "sourceSha256": report["source"]["sha256"],
        "ftsSha256": report["ftsSha256"],
        "sidecarSha256": report["sidecar"]["sha256"],
        "builderSha256": sha256(builder_path),
        "categoryIdentitySha256": sha256(category_identity_path),
        "contractSha256": sha256(contract_path),
        "reportSha256": sha256(report_path),
        "sealedAccess": "NONE",
    }
    receipt_path = receipt_dir / "receipt.json"
    _write_json(receipt_path, receipt)
    (receipt_dir / "SHA256SUMS.txt").write_text(
        f"{sha256(report_path)}  report.json\n{sha256(receipt_path)}  receipt.json\n",
        encoding="ascii",
    )
    return report_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--fts-index", type=Path, required=True)
    parser.add_argument("--building-sidecar", type=Path, required=True)
    parser.add_argument("--final-sidecar", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    report = build_sidecar(args.catalog, args.fts_index, args.building_sidecar)
    print(materialize(
        args.final_sidecar,
        args.receipt_dir,
        report,
        builder_path=Path(__file__),
        category_identity_path=Path(__file__).with_name("audit_shopping_companion_catalog_v14_2.py"),
        contract_path=args.contract,
    ))


if __name__ == "__main__":
    main()
