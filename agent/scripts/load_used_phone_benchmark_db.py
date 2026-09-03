"""Transactionally load the pinned used-phone seed plan into an isolated DB.

This loader is deliberately narrow.  It accepts one frozen seed-plan identity,
one dedicated database name, and a caller-pinned MySQL server UUID.  It never
deletes products.  A second run is a read/compare no-op when every persisted
field is already identical; any foreign or divergent row fails closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote, urlparse


DATABASE_NAME = "used_phone_benchmark_v1_09807c7"
CATALOG_VERSION = (
    "used-phone-benchmark-v1-"
    "09807c773ce67360ed8df30842e372182fcf7ad9"
)
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
CATALOG_SHA256 = "a79986121375d09bdc9c34b8e6ba3811bbe70a96e2aa21398981898a4c201c50"
PRODUCT_SHA256 = "d60cdf433f035df73b2530a8b28e7352a3f92c7b4db2af5a18ad43fd4b8a1d00"
ATTRIBUTE_SHA256 = "e917ad1457f83f71e452d470ad1b79efeb53e379bc869e440c1010a31a588018"
AUDIT_SHA256 = "bd591ab9318d20228699a5ebcad25577964b58c8e3d54c0835111a0d2a9958f0"
MANIFEST_FILE_SHA256 = (
    "7a0ea602cc64da2b8fa8195213ce9bb0a93b8c1937b0a2a44f183a5de1c8a0b7"
)
MANIFEST_SELF_SHA256 = (
    "20f3f6e442c575f0612a8655dfc227a9e89a729a1b6ee7e7cbe6c014819fa737"
)
EXPECTED_PRODUCT_COUNT = 252
EXPECTED_ATTRIBUTE_COUNT = 1547
DETAIL_CACHE_PREFIX = "local-life:product:detail:v1:"
LOCK_CACHE_PREFIX = "local-life:lock:product:detail:"
REDIS_IDENTITY_KEY = "used-phone-benchmark:instance-identity"
REDIS_IDENTITY = (
    "used-phone-benchmark-stage3-20260811:redis-v1:"
    "5dc0f693-1a62-4e7a-a386-1a52b99a4afb"
)
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 16379
REDIS_DB = 0


class UsedPhoneBenchmarkLoadError(RuntimeError):
    """Raised before commit when an isolation or data invariant fails."""


class FailureProbeTriggered(RuntimeError):
    """Test-only failure injected after product inserts and before commit."""


@dataclass(frozen=True)
class SeedPlan:
    products: list[dict[str, Any]]
    attributes: list[dict[str, Any]]
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_line(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise UsedPhoneBenchmarkLoadError(
                    f"non-object row in {path.name}:{line_number}"
                )
            rows.append(value)
    return rows


def load_seed_plan(seed_dir: Path) -> SeedPlan:
    seed_dir = Path(seed_dir)
    expected_files = {
        "product.jsonl": PRODUCT_SHA256,
        "product_attribute.jsonl": ATTRIBUTE_SHA256,
        "audit.json": AUDIT_SHA256,
        "manifest.json": MANIFEST_FILE_SHA256,
    }
    if not seed_dir.is_dir():
        raise UsedPhoneBenchmarkLoadError("seed plan directory is missing")
    actual_names = {item.name for item in seed_dir.iterdir() if item.is_file()}
    if actual_names != set(expected_files):
        raise UsedPhoneBenchmarkLoadError("seed plan file set mismatch")
    for name, expected_sha in expected_files.items():
        actual_sha = _sha256(seed_dir / name)
        if actual_sha != expected_sha:
            raise UsedPhoneBenchmarkLoadError(
                f"seed plan SHA mismatch for {name}: {actual_sha}"
            )

    manifest = json.loads((seed_dir / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise UsedPhoneBenchmarkLoadError("manifest must be an object")
    declared_self = manifest.get("outputs", {}).get("manifest.json", {}).get("sha256")
    if declared_self != MANIFEST_SELF_SHA256:
        raise UsedPhoneBenchmarkLoadError("manifest self SHA pin mismatch")
    self_payload = json.loads(json.dumps(manifest))
    self_payload["outputs"]["manifest.json"]["sha256"] = "0" * 64
    if hashlib.sha256(_canonical_line(self_payload)).hexdigest() != declared_self:
        raise UsedPhoneBenchmarkLoadError("manifest canonical self hash mismatch")
    if (
        manifest.get("dataset", {}).get("revision") != DATASET_REVISION
        or manifest.get("input", {}).get("sha256") != CATALOG_SHA256
        or manifest.get("outputs", {}).get("product.jsonl", {}).get("sha256")
        != PRODUCT_SHA256
        or manifest.get("outputs", {}).get("product_attribute.jsonl", {}).get("sha256")
        != ATTRIBUTE_SHA256
        or manifest.get("outputs", {}).get("product.jsonl", {}).get("rowCount")
        != EXPECTED_PRODUCT_COUNT
        or manifest.get("outputs", {})
        .get("product_attribute.jsonl", {})
        .get("rowCount")
        != EXPECTED_ATTRIBUTE_COUNT
    ):
        raise UsedPhoneBenchmarkLoadError("manifest pinned identity mismatch")

    products = _read_jsonl(seed_dir / "product.jsonl")
    attributes = _read_jsonl(seed_dir / "product_attribute.jsonl")
    if len(products) != EXPECTED_PRODUCT_COUNT or len(attributes) != EXPECTED_ATTRIBUTE_COUNT:
        raise UsedPhoneBenchmarkLoadError("seed row count mismatch")
    product_ids = [row.get("id") for row in products]
    if (
        not all(type(value) is int and value > 0 for value in product_ids)
        or product_ids != sorted(product_ids)
        or len(set(product_ids)) != len(product_ids)
    ):
        raise UsedPhoneBenchmarkLoadError("product identity/order mismatch")
    allowed_ids = set(product_ids)
    attribute_keys = [(row.get("product_id"), row.get("attribute_key")) for row in attributes]
    if (
        not all(type(product_id) is int and product_id in allowed_ids for product_id, _ in attribute_keys)
        or attribute_keys != sorted(attribute_keys)
        or len(set(attribute_keys)) != len(attribute_keys)
    ):
        raise UsedPhoneBenchmarkLoadError("attribute identity/order mismatch")
    if any(row.get("dataset_revision") != DATASET_REVISION for row in products):
        raise UsedPhoneBenchmarkLoadError("product revision mismatch")
    return SeedPlan(products=products, attributes=attributes, manifest=manifest)


def _mysql_connection(mysql_url: str):
    import pymysql

    parsed = urlparse(mysql_url)
    database = parsed.path.lstrip("/")
    if parsed.scheme != "mysql" or database != DATABASE_NAME:
        raise UsedPhoneBenchmarkLoadError("refusing non-benchmark database target")
    if not parsed.hostname or not parsed.username or parsed.password is None:
        raise UsedPhoneBenchmarkLoadError("incomplete MySQL target")
    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=unquote(parsed.username),
        password=unquote(parsed.password),
        database=database,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _cache_client(
    redis_url: str,
    *,
    expected_host: str,
    expected_port: int,
    expected_db: int,
    expected_identity: str,
):
    import redis

    parsed = urlparse(redis_url)
    try:
        parsed_db = int((parsed.path or "/0").removeprefix("/"))
    except ValueError:
        raise UsedPhoneBenchmarkLoadError("Redis DB is not canonical") from None
    if (
        parsed.scheme != "redis"
        or parsed.hostname != expected_host
        or (parsed.port or 6379) != expected_port
        or parsed_db != expected_db
        or expected_host != REDIS_HOST
        or expected_port != REDIS_PORT
        or expected_db != REDIS_DB
        or expected_identity != REDIS_IDENTITY
    ):
        raise UsedPhoneBenchmarkLoadError("Redis endpoint/identity pin mismatch")
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    client.ping()
    actual_identity = client.get(REDIS_IDENTITY_KEY)
    if actual_identity != expected_identity:
        raise UsedPhoneBenchmarkLoadError("Redis persistent identity mismatch")
    info = client.info(section="server")
    run_id = info.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise UsedPhoneBenchmarkLoadError("Redis run_id is unavailable")
    return client, run_id


def _owned_detail_cache_keys(client, product_ids: set[int]) -> list[str]:
    """Return only proven seed detail keys; reject every other Redis key.

    Locks are never deleted or interpreted by this loader.  Their presence,
    including a lock for a seed product, means another process may be active,
    so the operation fails closed without consulting or guessing the TTL.
    """

    expected = {f"{DETAIL_CACHE_PREFIX}{product_id}" for product_id in product_ids}
    owned: list[str] = []
    for raw_key in client.scan_iter(match="*", count=500):
        key = str(raw_key)
        if key == REDIS_IDENTITY_KEY:
            continue
        if key.startswith(LOCK_CACHE_PREFIX):
            raise UsedPhoneBenchmarkLoadError("Redis contains an active product lock")
        if not key.startswith(DETAIL_CACHE_PREFIX):
            raise UsedPhoneBenchmarkLoadError("dedicated Redis contains a foreign key")
        suffix = key.removeprefix(DETAIL_CACHE_PREFIX)
        if (
            not suffix
            or not suffix.isascii()
            or not suffix.isdecimal()
            or str(int(suffix)) != suffix
            or int(suffix) < 1
        ):
            raise UsedPhoneBenchmarkLoadError("Redis detail key has a noncanonical ID")
        if key not in expected:
            raise UsedPhoneBenchmarkLoadError("Redis detail key is outside the seed catalog")
        owned.append(key)
    return sorted(owned)


PRODUCT_COLUMNS = (
    "attribute_text", "brand", "category_l1", "category_l2", "category_l3",
    "currency", "data_nature", "dataset_revision", "id", "price_status",
    "provenance_url", "seller", "snapshot_price_minor", "source",
    "source_item_id", "source_license", "title",
)
ATTRIBUTE_COLUMNS = (
    "attribute_key", "confidence", "evidence_field", "extraction_method",
    "normalized_boolean", "normalized_number", "normalized_text", "product_id",
    "raw_value", "unit", "value_type",
)


def _canonical_db_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def _rows_equal(actual: list[dict[str, Any]], expected: list[dict[str, Any]], columns: tuple[str, ...]) -> bool:
    normalized_actual = [
        {column: _canonical_db_value(row.get(column)) for column in columns}
        for row in actual
    ]
    normalized_expected = [
        {column: _canonical_db_value(row.get(column)) for column in columns}
        for row in expected
    ]
    return normalized_actual == normalized_expected


def load_database(
    *,
    seed_dir: Path,
    mysql_url: str,
    redis_url: str,
    expected_server_uuid: str,
    expected_redis_host: str,
    expected_redis_port: int,
    expected_redis_db: int,
    expected_redis_identity: str,
    failure_probe_after_product_inserts: bool = False,
) -> dict[str, Any]:
    seed = load_seed_plan(seed_dir)
    seed_product_ids = {int(row["id"]) for row in seed.products}
    redis_client, redis_run_id = _cache_client(
        redis_url,
        expected_host=expected_redis_host,
        expected_port=expected_redis_port,
        expected_db=expected_redis_db,
        expected_identity=expected_redis_identity,
    )
    cache_keys_before = _owned_detail_cache_keys(redis_client, seed_product_ids)
    connection = _mysql_connection(mysql_url)
    inserted = False
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT DATABASE() AS database_name, @@server_uuid AS server_uuid")
            identity = cursor.fetchone()
            if (
                identity["database_name"] != DATABASE_NAME
                or identity["server_uuid"] != expected_server_uuid
            ):
                raise UsedPhoneBenchmarkLoadError("MySQL isolation identity mismatch")
            cursor.execute(
                """
                SELECT TABLE_NAME AS table_name
                FROM information_schema.TABLES
                WHERE TABLE_SCHEMA=%s AND TABLE_NAME IN
                    ('product','product_attribute','catalog_state')
                ORDER BY TABLE_NAME
                """,
                (DATABASE_NAME,),
            )
            if [row["table_name"] for row in cursor.fetchall()] != [
                "catalog_state", "product", "product_attribute"
            ]:
                raise UsedPhoneBenchmarkLoadError("required schema is missing")
            cursor.execute("SELECT COUNT(*) AS count FROM product")
            product_count_before = int(cursor.fetchone()["count"])
            cursor.execute("SELECT COUNT(*) AS count FROM product_attribute")
            attribute_count_before = int(cursor.fetchone()["count"])
            cursor.execute("SELECT COUNT(*) AS count FROM catalog_state")
            catalog_count_before = int(cursor.fetchone()["count"])
            allowed_count_states = {
                (0, 0, 0),
                (EXPECTED_PRODUCT_COUNT, EXPECTED_ATTRIBUTE_COUNT, 1),
            }
            if (
                product_count_before,
                attribute_count_before,
                catalog_count_before,
            ) not in allowed_count_states:
                raise UsedPhoneBenchmarkLoadError(
                    "database is neither empty nor the exact benchmark catalog"
                )

            connection.begin()
            if product_count_before == 0:
                product_sql = (
                    "INSERT INTO product (" + ",".join(PRODUCT_COLUMNS) + ") VALUES ("
                    + ",".join(["%s"] * len(PRODUCT_COLUMNS)) + ")"
                )
                cursor.executemany(
                    product_sql,
                    [tuple(row[column] for column in PRODUCT_COLUMNS) for row in seed.products],
                )
                inserted = True
                if failure_probe_after_product_inserts:
                    raise FailureProbeTriggered("failure probe after product inserts")
                attribute_sql = (
                    "INSERT INTO product_attribute ("
                    + ",".join(ATTRIBUTE_COLUMNS)
                    + ") VALUES ("
                    + ",".join(["%s"] * len(ATTRIBUTE_COLUMNS))
                    + ")"
                )
                cursor.executemany(
                    attribute_sql,
                    [tuple(row[column] for column in ATTRIBUTE_COLUMNS) for row in seed.attributes],
                )
                cursor.execute(
                    """
                    INSERT INTO catalog_state
                        (catalog_version, product_count, content_hash)
                    VALUES (%s, %s, %s)
                    """,
                    (CATALOG_VERSION, EXPECTED_PRODUCT_COUNT, PRODUCT_SHA256),
                )

            cursor.execute(
                "SELECT " + ",".join(PRODUCT_COLUMNS) + " FROM product ORDER BY id"
            )
            actual_products = cursor.fetchall()
            cursor.execute(
                "SELECT " + ",".join(ATTRIBUTE_COLUMNS)
                + " FROM product_attribute ORDER BY product_id, attribute_key"
            )
            actual_attributes = cursor.fetchall()
            cursor.execute(
                """
                SELECT catalog_version, product_count, content_hash
                FROM catalog_state ORDER BY published_at DESC
                """
            )
            actual_catalog = cursor.fetchall()
            if not _rows_equal(actual_products, seed.products, PRODUCT_COLUMNS):
                raise UsedPhoneBenchmarkLoadError("persisted product rows differ from seed")
            if not _rows_equal(actual_attributes, seed.attributes, ATTRIBUTE_COLUMNS):
                raise UsedPhoneBenchmarkLoadError("persisted attribute rows differ from seed")
            if actual_catalog != [{
                "catalog_version": CATALOG_VERSION,
                "product_count": EXPECTED_PRODUCT_COUNT,
                "content_hash": PRODUCT_SHA256,
            }]:
                raise UsedPhoneBenchmarkLoadError("catalog_state mismatch")
            connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    owned_detail_keys_after = _owned_detail_cache_keys(
        redis_client, seed_product_ids
    )
    deleted_cache_keys = 0
    if owned_detail_keys_after:
        deleted_cache_keys = int(redis_client.delete(*owned_detail_keys_after))
    return {
        "attributeCount": EXPECTED_ATTRIBUTE_COUNT,
        "backendRestartRequiredForLocalL1": True,
        "cacheKeysBefore": len(cache_keys_before),
        "catalogContentHash": PRODUCT_SHA256,
        "catalogVersion": CATALOG_VERSION,
        "database": DATABASE_NAME,
        "databaseWritten": inserted,
        "deletedCacheKeys": deleted_cache_keys,
        "productCount": EXPECTED_PRODUCT_COUNT,
        "redisDb": expected_redis_db,
        "redisEndpoint": f"{expected_redis_host}:{expected_redis_port}",
        "redisIdentity": expected_redis_identity,
        "redisRunId": redis_run_id,
        "serverUuid": expected_server_uuid,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed-dir", type=Path, required=True)
    parser.add_argument("--mysql-url", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--expected-server-uuid", required=True)
    parser.add_argument("--expected-redis-host", required=True)
    parser.add_argument("--expected-redis-port", type=int, required=True)
    parser.add_argument("--expected-redis-db", type=int, required=True)
    parser.add_argument("--expected-redis-identity", required=True)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.apply:
        raise UsedPhoneBenchmarkLoadError("--apply is required; no implicit write mode")
    result = load_database(
        seed_dir=args.seed_dir,
        mysql_url=args.mysql_url,
        redis_url=args.redis_url,
        expected_server_uuid=args.expected_server_uuid,
        expected_redis_host=args.expected_redis_host,
        expected_redis_port=args.expected_redis_port,
        expected_redis_db=args.expected_redis_db,
        expected_redis_identity=args.expected_redis_identity,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
