"""Build a query-independent SQLite FTS5 index for Shopping Memory V14."""
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


SOURCE_REVISION = "9a8a2a1c13f0d88de070238352bcf71f98ca851f"
EXPECTED_SOURCE_BYTES = 20_030_340_587
EXPECTED_SOURCE_ROWS = 1_298_797
EXPECTED_SOURCE_SHA256 = "95b42f63e458d62337fc21294b148e489275ede8c283db1ff1061173a843721f"
TOKENIZER = "unicode61 remove_diacritics 2"
SCHEMA_VERSION = "shopping-memory-v14-sqlite-fts-index-v1"


class IndexBuildError(RuntimeError):
    """Fail-closed index construction error."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _open_build_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA page_size=4096")
    connection.execute("PRAGMA journal_mode=OFF")
    connection.execute("PRAGMA synchronous=OFF")
    connection.execute("PRAGMA locking_mode=EXCLUSIVE")
    connection.execute("PRAGMA temp_store=MEMORY")
    connection.execute("PRAGMA cache_size=-65536")
    connection.executescript(f"""
        CREATE TABLE product_locator (
            rowid INTEGER PRIMARY KEY,
            product_id TEXT NOT NULL UNIQUE,
            byte_offset INTEGER NOT NULL,
            byte_length INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE product_fts USING fts5(
            product_id UNINDEXED,
            contents,
            tokenize='{TOKENIZER}'
        );
        CREATE TABLE build_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    return connection


def build_index(
    catalog_path: Path,
    database_path: Path,
    *,
    expected_bytes: int = EXPECTED_SOURCE_BYTES,
    expected_rows: int = EXPECTED_SOURCE_ROWS,
    expected_sha256: str = EXPECTED_SOURCE_SHA256,
    progress_every: int = 100_000,
    minimum_free_bytes: int = 40 * 1024 * 1024 * 1024,
) -> dict[str, Any]:
    if database_path.exists():
        raise FileExistsError(f"refusing to overwrite {database_path}")
    database_path.parent.mkdir(parents=True, exist_ok=True)
    free_bytes_before = shutil.disk_usage(database_path.parent).free
    if free_bytes_before < minimum_free_bytes:
        raise IndexBuildError(
            f"insufficient index volume space: {free_bytes_before} < {minimum_free_bytes}"
        )
    started = time.perf_counter()
    digest = hashlib.sha256()
    total_bytes = 0
    row_count = 0
    empty_contents = 0
    connection = _open_build_database(database_path)
    locator_batch: list[tuple[int, str, int, int]] = []
    fts_batch: list[tuple[int, str, str]] = []
    try:
        connection.execute("BEGIN")
        with catalog_path.open("rb") as stream:
            while True:
                byte_offset = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                digest.update(raw)
                total_bytes += len(raw)
                row_count += 1
                try:
                    row = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise IndexBuildError(f"invalid catalog JSON at line {row_count}") from exc
                if type(row) is not dict:
                    raise IndexBuildError(f"catalog row is not an object at line {row_count}")
                product_id = row.get("id")
                contents = row.get("contents")
                product = row.get("product")
                if type(product_id) not in {str, int} or not str(product_id):
                    raise IndexBuildError(f"invalid product id at line {row_count}")
                if type(contents) is not str:
                    raise IndexBuildError(f"invalid contents at line {row_count}")
                if type(product) is not dict:
                    raise IndexBuildError(f"invalid product object at line {row_count}")
                if not contents.strip():
                    empty_contents += 1
                normalized_id = str(product_id)
                locator_batch.append((row_count, normalized_id, byte_offset, len(raw)))
                fts_batch.append((row_count, normalized_id, contents))
                if len(locator_batch) >= 5_000:
                    connection.executemany(
                        "INSERT INTO product_locator(rowid,product_id,byte_offset,byte_length) VALUES(?,?,?,?)",
                        locator_batch,
                    )
                    connection.executemany(
                        "INSERT INTO product_fts(rowid,product_id,contents) VALUES(?,?,?)",
                        fts_batch,
                    )
                    locator_batch.clear()
                    fts_batch.clear()
                if progress_every > 0 and row_count % progress_every == 0:
                    connection.commit()
                    connection.execute("BEGIN")
                    print(json.dumps({
                        "phase": "INDEXING",
                        "rows": row_count,
                        "bytes": total_bytes,
                    }), flush=True)
        if locator_batch:
            connection.executemany(
                "INSERT INTO product_locator(rowid,product_id,byte_offset,byte_length) VALUES(?,?,?,?)",
                locator_batch,
            )
            connection.executemany(
                "INSERT INTO product_fts(rowid,product_id,contents) VALUES(?,?,?)",
                fts_batch,
            )
        connection.commit()
        actual_sha256 = digest.hexdigest()
        identity = {
            "sourceBytesExact": total_bytes == expected_bytes,
            "sourceRowsExact": row_count == expected_rows,
            "sourceSha256Exact": actual_sha256 == expected_sha256,
        }
        if not all(identity.values()):
            raise IndexBuildError(f"source identity mismatch: {identity}")
        locator_count = connection.execute("SELECT count(*) FROM product_locator").fetchone()[0]
        fts_count = connection.execute("SELECT count(*) FROM product_fts").fetchone()[0]
        if locator_count != row_count or fts_count != row_count:
            raise IndexBuildError("index cardinality mismatch")
        metadata = {
            "schemaVersion": SCHEMA_VERSION,
            "sourceRevision": SOURCE_REVISION,
            "sourceSha256": actual_sha256,
            "sourceBytes": str(total_bytes),
            "sourceRows": str(row_count),
            "tokenizer": TOKENIZER,
            "queryField": "contents",
            "candidateDepth": "50",
        }
        connection.executemany(
            "INSERT INTO build_metadata(key,value) VALUES(?,?)",
            sorted(metadata.items()),
        )
        connection.execute("INSERT INTO product_fts(product_fts) VALUES('optimize')")
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        if quick_check != "ok":
            raise IndexBuildError(f"SQLite quick_check failed: {quick_check}")
    finally:
        connection.close()
    elapsed = time.perf_counter() - started
    return {
        "schemaVersion": "shopping-memory-v14-fts-build-report-v1",
        "decision": "FULL_CATALOG_FTS_BUILD_ACCEPT",
        "source": {
            "path": str(catalog_path),
            "revision": SOURCE_REVISION,
            "bytes": total_bytes,
            "rows": row_count,
            "sha256": digest.hexdigest(),
        },
        "index": {
            "path": str(database_path),
            "bytes": database_path.stat().st_size,
            "sha256": sha256(database_path),
            "locatorRows": row_count,
            "ftsRows": row_count,
            "emptyContents": empty_contents,
            "tokenizer": TOKENIZER,
            "quickCheck": "ok",
            "freeBytesBefore": free_bytes_before,
        },
        "elapsedSeconds": elapsed,
        "explicitBoundaries": {
            "queryExecution": False,
            "qualityRun": False,
            "validationRead": False,
            "sealedRead": False,
            "productionSwitchAuthority": False,
        },
    }


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
    building_path = Path(report["index"]["path"])
    os.replace(building_path, final_database_path)
    report["index"]["path"] = str(final_database_path)
    receipt_dir.mkdir(parents=True)
    report_path = receipt_dir / "report.json"
    _write_json(report_path, report)
    receipt = {
        "schemaVersion": "shopping-memory-v14-fts-build-receipt-v1",
        "decision": report["decision"],
        "sourceRevision": SOURCE_REVISION,
        "sourceSha256": report["source"]["sha256"],
        "databaseSha256": report["index"]["sha256"],
        "builderSha256": sha256(builder_path),
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
    parser.add_argument("--building-database", type=Path, required=True)
    parser.add_argument("--final-database", type=Path, required=True)
    parser.add_argument("--receipt-dir", type=Path, required=True)
    parser.add_argument("--contract", type=Path, required=True)
    args = parser.parse_args()
    report = build_index(args.catalog, args.building_database)
    output = materialize(
        args.final_database,
        args.receipt_dir,
        report,
        builder_path=Path(__file__),
        contract_path=args.contract,
    )
    print(output)


if __name__ == "__main__":
    main()
