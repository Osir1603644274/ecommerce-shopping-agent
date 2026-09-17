from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

import scripts.load_used_phone_benchmark_db as loader


def _line(value) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _redis_identity_kwargs() -> dict:
    return {
        "expected_redis_host": loader.REDIS_HOST,
        "expected_redis_port": loader.REDIS_PORT,
        "expected_redis_db": loader.REDIS_DB,
        "expected_redis_identity": loader.REDIS_IDENTITY,
    }


def _fixture_seed(tmp_path: Path, monkeypatch) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    product = {
        "attribute_text": "90%+",
        "brand": "brand",
        "category_l1": "二手",
        "category_l2": "二手手机通讯",
        "category_l3": "二手手机",
        "currency": None,
        "data_nature": "historical_dataset_snapshot",
        "dataset_revision": loader.DATASET_REVISION,
        "id": 1001,
        "price_status": "unverified",
        "provenance_url": "https://example.test",
        "seller": "seller",
        "snapshot_price_minor": None,
        "source": "kuaisearch",
        "source_item_id": "1001",
        "source_license": "MIT",
        "title": "phone",
    }
    attribute = {
        "attribute_key": "battery_health",
        "confidence": 1.0,
        "evidence_field": "relevance.attr_value",
        "extraction_method": "used-phone-exact-token-seven-field-v2",
        "normalized_boolean": None,
        "normalized_number": None,
        "normalized_text": "90_plus",
        "product_id": 1001,
        "raw_value": "90%+",
        "unit": "enum",
        "value_type": "enum",
    }
    (seed / "product.jsonl").write_bytes(_line(product))
    (seed / "product_attribute.jsonl").write_bytes(_line(attribute))
    (seed / "audit.json").write_bytes(_line({"fixture": True}))
    monkeypatch.setattr(loader, "EXPECTED_PRODUCT_COUNT", 1)
    monkeypatch.setattr(loader, "EXPECTED_ATTRIBUTE_COUNT", 1)
    monkeypatch.setattr(loader, "PRODUCT_SHA256", _sha(seed / "product.jsonl"))
    monkeypatch.setattr(loader, "ATTRIBUTE_SHA256", _sha(seed / "product_attribute.jsonl"))
    monkeypatch.setattr(loader, "AUDIT_SHA256", _sha(seed / "audit.json"))
    manifest = {
        "dataset": {"revision": loader.DATASET_REVISION},
        "input": {"sha256": loader.CATALOG_SHA256},
        "outputs": {
            "audit.json": {"sha256": loader.AUDIT_SHA256},
            "manifest.json": {"sha256": "0" * 64},
            "product.jsonl": {
                "rowCount": 1,
                "sha256": loader.PRODUCT_SHA256,
            },
            "product_attribute.jsonl": {
                "rowCount": 1,
                "sha256": loader.ATTRIBUTE_SHA256,
            },
        },
    }
    self_sha = hashlib.sha256(_line(manifest)).hexdigest()
    manifest["outputs"]["manifest.json"]["sha256"] = self_sha
    (seed / "manifest.json").write_bytes(_line(manifest))
    monkeypatch.setattr(loader, "MANIFEST_SELF_SHA256", self_sha)
    monkeypatch.setattr(loader, "MANIFEST_FILE_SHA256", _sha(seed / "manifest.json"))
    return seed


def test_fixture_seed_is_hash_and_identity_bound(tmp_path, monkeypatch):
    seed = _fixture_seed(tmp_path, monkeypatch)
    loaded = loader.load_seed_plan(seed)
    assert [row["id"] for row in loaded.products] == [1001]
    assert [(row["product_id"], row["attribute_key"]) for row in loaded.attributes] == [
        (1001, "battery_health")
    ]


def test_fixture_seed_tamper_fails_closed(tmp_path, monkeypatch):
    seed = _fixture_seed(tmp_path, monkeypatch)
    with (seed / "product.jsonl").open("ab") as stream:
        stream.write(b"\n")
    with pytest.raises(loader.UsedPhoneBenchmarkLoadError, match="SHA mismatch"):
        loader.load_seed_plan(seed)


def test_extra_seed_file_is_rejected(tmp_path, monkeypatch):
    seed = _fixture_seed(tmp_path, monkeypatch)
    (seed / "unexpected.txt").write_text("no", encoding="utf-8")
    with pytest.raises(loader.UsedPhoneBenchmarkLoadError, match="file set mismatch"):
        loader.load_seed_plan(seed)


def test_non_benchmark_database_name_is_rejected_before_connecting():
    with pytest.raises(loader.UsedPhoneBenchmarkLoadError, match="non-benchmark"):
        loader._mysql_connection("mysql://user:pass@127.0.0.1:3306/local_life")


def test_loader_has_no_product_delete_or_update_path():
    source = Path(loader.__file__).read_text(encoding="utf-8").upper()
    assert "DELETE FROM PRODUCT" not in source
    assert "UPDATE PRODUCT" not in source
    assert "ON DUPLICATE KEY" not in source


class _FakeRedis:
    def __init__(self, keys):
        self.keys = list(keys)

    def scan_iter(self, **_kwargs):
        return iter(self.keys)


@pytest.mark.parametrize(
    ("key", "error"),
    [
        ("local-life:product:detail:v1:9999", "outside the seed"),
        ("local-life:product:detail:v1:not-an-id", "noncanonical"),
        ("local-life:product:detail:v1:01001", "noncanonical"),
        ("local-life:lock:product:detail:9999", "active product lock"),
        ("local-life:lock:product:detail:1001", "active product lock"),
        ("other:cache:key", "foreign key"),
    ],
)
def test_cache_ownership_rejects_foreign_malformed_and_all_locks(key, error):
    with pytest.raises(loader.UsedPhoneBenchmarkLoadError, match=error):
        loader._owned_detail_cache_keys(_FakeRedis([key]), {1001, 1002})


def test_cache_ownership_returns_only_existing_seed_detail_keys():
    assert loader._owned_detail_cache_keys(
        _FakeRedis([
            "local-life:product:detail:v1:1002",
            "local-life:product:detail:v1:1001",
        ]),
        {1001, 1002, 1003},
    ) == [
        "local-life:product:detail:v1:1001",
        "local-life:product:detail:v1:1002",
    ]


@pytest.mark.parametrize(
    ("url", "host", "port", "db", "identity"),
    [
        ("redis://127.0.0.2:16379/0", "127.0.0.2", 16379, 0, loader.REDIS_IDENTITY),
        ("redis://127.0.0.1:6379/0", "127.0.0.1", 6379, 0, loader.REDIS_IDENTITY),
        ("redis://127.0.0.1:16379/1", "127.0.0.1", 16379, 1, loader.REDIS_IDENTITY),
        ("redis://127.0.0.1:16379/0", "127.0.0.1", 16379, 0, "wrong-identity"),
    ],
)
def test_wrong_redis_target_is_rejected_before_client_creation(
    monkeypatch, url, host, port, db, identity
):
    import redis

    def forbidden_connection(*_args, **_kwargs):
        raise AssertionError("wrong Redis target must not be connected or scanned")

    monkeypatch.setattr(redis.Redis, "from_url", forbidden_connection)
    with pytest.raises(loader.UsedPhoneBenchmarkLoadError, match="pin mismatch"):
        loader._cache_client(
            url,
            expected_host=host,
            expected_port=port,
            expected_db=db,
            expected_identity=identity,
        )


def test_correct_endpoint_with_wrong_persistent_identity_is_rejected(monkeypatch):
    import redis

    class FakeClient:
        def ping(self):
            return True

        def get(self, _key):
            return "another-instance"

    monkeypatch.setattr(redis.Redis, "from_url", lambda *_args, **_kwargs: FakeClient())
    with pytest.raises(loader.UsedPhoneBenchmarkLoadError, match="persistent identity"):
        loader._cache_client(
            "redis://127.0.0.1:16379/0",
            expected_host=loader.REDIS_HOST,
            expected_port=loader.REDIS_PORT,
            expected_db=loader.REDIS_DB,
            expected_identity=loader.REDIS_IDENTITY,
        )


def test_db_row_comparison_normalizes_decimal_without_dropping_fields():
    from decimal import Decimal

    assert loader._rows_equal(
        [{"id": 1, "confidence": Decimal("1.0000")}],
        [{"id": 1, "confidence": 1.0}],
        ("id", "confidence"),
    )
    assert not loader._rows_equal(
        [{"id": 1, "confidence": Decimal("0.0000")}],
        [{"id": 1, "confidence": 1.0}],
        ("id", "confidence"),
    )


@pytest.mark.skipif(
    not os.getenv("USED_PHONE_BENCHMARK_MYSQL_URL"),
    reason="isolated Benchmark DB integration target not configured",
)
def test_real_isolated_database_failure_probe_rolls_back_or_exact_load_is_idempotent():
    mysql_url = os.environ["USED_PHONE_BENCHMARK_MYSQL_URL"]
    redis_url = os.environ["USED_PHONE_BENCHMARK_REDIS_URL"]
    seed_dir = Path(os.environ["USED_PHONE_BENCHMARK_SEED_DIR"])
    server_uuid = os.environ["USED_PHONE_BENCHMARK_SERVER_UUID"]
    connection = loader._mysql_connection(mysql_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS count FROM product")
            count_before = int(cursor.fetchone()["count"])
    finally:
        connection.close()
    if count_before == 0:
        with pytest.raises(loader.FailureProbeTriggered):
            loader.load_database(
                seed_dir=seed_dir,
                mysql_url=mysql_url,
                redis_url=redis_url,
                expected_server_uuid=server_uuid,
                **_redis_identity_kwargs(),
                failure_probe_after_product_inserts=True,
            )
        connection = loader._mysql_connection(mysql_url)
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT COUNT(*) AS products FROM product")
                assert int(cursor.fetchone()["products"]) == 0
                cursor.execute("SELECT COUNT(*) AS attributes FROM product_attribute")
                assert int(cursor.fetchone()["attributes"]) == 0
                cursor.execute("SELECT COUNT(*) AS versions FROM catalog_state")
                assert int(cursor.fetchone()["versions"]) == 0
        finally:
            connection.close()
    else:
        result = loader.load_database(
            seed_dir=seed_dir,
            mysql_url=mysql_url,
            redis_url=redis_url,
            expected_server_uuid=server_uuid,
            **_redis_identity_kwargs(),
        )
        assert result["databaseWritten"] is False
        assert result["productCount"] == 252
        assert result["attributeCount"] == 1547


def _real_db_snapshot(mysql_url: str) -> str:
    connection = loader._mysql_connection(mysql_url)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT " + ",".join(loader.PRODUCT_COLUMNS) + " FROM product ORDER BY id"
            )
            products = cursor.fetchall()
            cursor.execute(
                "SELECT " + ",".join(loader.ATTRIBUTE_COLUMNS)
                + " FROM product_attribute ORDER BY product_id, attribute_key"
            )
            attributes = cursor.fetchall()
            cursor.execute(
                "SELECT catalog_version,product_count,content_hash "
                "FROM catalog_state ORDER BY catalog_version"
            )
            versions = cursor.fetchall()
    finally:
        connection.close()
    payload = {
        "attributes": [
            {key: loader._canonical_db_value(value) for key, value in row.items()}
            for row in attributes
        ],
        "products": [
            {key: loader._canonical_db_value(value) for key, value in row.items()}
            for row in products
        ],
        "versions": versions,
    }
    return hashlib.sha256(_line(payload)).hexdigest()


@pytest.mark.skipif(
    not os.getenv("USED_PHONE_BENCHMARK_MYSQL_URL"),
    reason="isolated Benchmark DB integration target not configured",
)
def test_real_redis_foreign_key_matrix_preserves_db_and_every_cache_key():
    import redis

    mysql_url = os.environ["USED_PHONE_BENCHMARK_MYSQL_URL"]
    redis_url = os.environ["USED_PHONE_BENCHMARK_REDIS_URL"]
    seed_dir = Path(os.environ["USED_PHONE_BENCHMARK_SEED_DIR"])
    server_uuid = os.environ["USED_PHONE_BENCHMARK_SERVER_UUID"]
    seed = loader.load_seed_plan(seed_dir)
    seed_id = int(seed.products[0]["id"])
    foreign_id = max(int(row["id"]) for row in seed.products) + 1
    probes = [
        f"{loader.DETAIL_CACHE_PREFIX}{foreign_id}",
        f"{loader.DETAIL_CACHE_PREFIX}not-an-id",
        f"{loader.LOCK_CACHE_PREFIX}{foreign_id}",
        f"{loader.LOCK_CACHE_PREFIX}{seed_id}",
        "other:catalog:key",
    ]
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    baseline_db_sha = _real_db_snapshot(mysql_url)
    for probe_key in probes:
        client.set(probe_key, "stage3-ownership-probe")
        keys_before = sorted(str(key) for key in client.scan_iter(match="*"))
        try:
            with pytest.raises(loader.UsedPhoneBenchmarkLoadError):
                loader.load_database(
                    seed_dir=seed_dir,
                    mysql_url=mysql_url,
                    redis_url=redis_url,
                    expected_server_uuid=server_uuid,
                    **_redis_identity_kwargs(),
                )
            assert _real_db_snapshot(mysql_url) == baseline_db_sha
            assert sorted(str(key) for key in client.scan_iter(match="*")) == keys_before
            assert client.get(probe_key) == "stage3-ownership-probe"
        finally:
            client.delete(probe_key)


@pytest.mark.skipif(
    not os.getenv("USED_PHONE_BENCHMARK_MYSQL_URL"),
    reason="isolated Benchmark DB integration target not configured",
)
def test_real_wrong_persistent_redis_identity_preserves_db_and_redis():
    import redis

    mysql_url = os.environ["USED_PHONE_BENCHMARK_MYSQL_URL"]
    redis_url = os.environ["USED_PHONE_BENCHMARK_REDIS_URL"]
    seed_dir = Path(os.environ["USED_PHONE_BENCHMARK_SEED_DIR"])
    server_uuid = os.environ["USED_PHONE_BENCHMARK_SERVER_UUID"]
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    baseline_db_sha = _real_db_snapshot(mysql_url)
    correct_identity = client.get(loader.REDIS_IDENTITY_KEY)
    assert correct_identity == loader.REDIS_IDENTITY
    client.set(loader.REDIS_IDENTITY_KEY, "wrong-instance-identity")
    keys_before = {
        str(key): client.get(str(key)) for key in client.scan_iter(match="*")
    }
    try:
        with pytest.raises(
            loader.UsedPhoneBenchmarkLoadError, match="persistent identity"
        ):
            loader.load_database(
                seed_dir=seed_dir,
                mysql_url=mysql_url,
                redis_url=redis_url,
                expected_server_uuid=server_uuid,
                **_redis_identity_kwargs(),
            )
        assert _real_db_snapshot(mysql_url) == baseline_db_sha
        assert {
            str(key): client.get(str(key)) for key in client.scan_iter(match="*")
        } == keys_before
    finally:
        client.set(loader.REDIS_IDENTITY_KEY, correct_identity)
