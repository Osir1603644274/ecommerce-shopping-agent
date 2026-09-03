"""Seed the frozen 439-product used-phone catalog into an isolated MySQL DB.

The command is intentionally fail-closed: it inserts only into an empty
catalog, accepts an already-complete identical cardinality, and rejects any
partial or foreign catalog.  Credentials stay inside the named MySQL
container and are never emitted by this process.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
CATALOG = (
    REPOSITORY_ROOT
    / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/catalog.jsonl"
)
PRICES = (
    REPOSITORY_ROOT
    / "data/derived/ecommerce/used_phone_catalog_expansion_kuaisearch_09807c_20260823_r3/prices.jsonl"
)
EXPECTED_CATALOG_SHA256 = "725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75"
EXPECTED_PRICES_SHA256 = "0296cfa77b722ec0ddfa823b093f0751adb187332faccf50353f82676fae5608"
EXPECTED_PRODUCTS = 439
PROVENANCE_URL = "https://huggingface.co/datasets/benchen4395/KuaiSearch"
ATTRIBUTE_EVIDENCE_FIELD = "relevance.attr_value"
ATTRIBUTE_RULESET_VERSION = "used-phone-exact-token-seven-field-v2"


class SeedFailure(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            if type(value) is not dict:
                raise SeedFailure(f"invalid JSONL object in {path.name}")
            rows.append(value)
    return rows


def sql_text(value: object) -> str:
    if type(value) is not str:
        raise SeedFailure("non-string SQL text value")
    return f"CONVERT(0x{value.encode('utf-8').hex()} USING utf8mb4)"


def mysql(container: str, sql: str) -> str:
    completed = subprocess.run(
        [
            "docker", "exec", "-i", container, "sh", "-lc",
            'exec mysql -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" --batch --skip-column-names',
        ],
        input=sql,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if completed.returncode != 0:
        detail = " ".join(completed.stderr.strip().splitlines())[-800:]
        raise SeedFailure(
            f"isolated MySQL command failed with exit {completed.returncode}: {detail}"
        )
    return completed.stdout.strip()


def counts(container: str) -> tuple[int, int]:
    output = mysql(
        container,
        "SELECT CONCAT((SELECT COUNT(*) FROM product),CHAR(9),"
        "(SELECT COUNT(*) FROM product_attribute));\n",
    )
    # mysql --batch escapes control characters in result cells (TAB -> ``\\t``).
    parts = output.replace("\\t", "\t").split("\t")
    if len(parts) != 2 or any(not part.isdigit() for part in parts):
        raise SeedFailure("unexpected catalog count response")
    return int(parts[0]), int(parts[1])


def current_attribute_contract_count(container: str) -> int:
    output = mysql(
        container,
        "SELECT COUNT(*) FROM product_attribute "
        f"WHERE evidence_field={sql_text(ATTRIBUTE_EVIDENCE_FIELD)} "
        "COLLATE utf8mb4_unicode_ci "
        f"AND extraction_method={sql_text(ATTRIBUTE_RULESET_VERSION)} "
        "COLLATE utf8mb4_unicode_ci;\n",
    )
    if not output.isdigit():
        raise SeedFailure("unexpected attribute contract count response")
    return int(output)


def batched(values: list[str], size: int) -> list[list[str]]:
    return [values[index:index + size] for index in range(0, len(values), size)]


def build_rows() -> tuple[list[str], list[str], dict[str, str]]:
    observed = {
        "catalogSha256": sha256_file(CATALOG),
        "pricesSha256": sha256_file(PRICES),
    }
    if observed != {
        "catalogSha256": EXPECTED_CATALOG_SHA256,
        "pricesSha256": EXPECTED_PRICES_SHA256,
    }:
        raise SeedFailure("frozen input hash mismatch")

    catalog = read_jsonl(CATALOG)
    prices = read_jsonl(PRICES)
    if len(catalog) != EXPECTED_PRODUCTS or len(prices) != EXPECTED_PRODUCTS:
        raise SeedFailure("frozen input cardinality mismatch")
    price_by_id = {
        str(row.get("itemId")): row for row in prices
        if type(row.get("itemId")) is str
    }
    if len(price_by_id) != EXPECTED_PRODUCTS:
        raise SeedFailure("price identity mismatch")

    product_rows: list[str] = []
    attribute_rows: list[str] = []
    seen_ids: set[int] = set()
    for row in catalog:
        item_text = row.get("itemId")
        if type(item_text) is not str or not item_text.isdigit():
            raise SeedFailure("invalid product id")
        item_id = int(item_text)
        if item_id <= 0 or item_id >= 2**63 or item_id in seen_ids:
            raise SeedFailure("duplicate or out-of-range product id")
        seen_ids.add(item_id)
        category = row.get("categoryPath")
        attributes = row.get("attributes")
        if (
            type(category) is not list or len(category) != 3
            or any(type(value) is not str for value in category)
            or type(attributes) is not dict
        ):
            raise SeedFailure("invalid product structure")
        price = price_by_id.get(item_text)
        if price is None or type(price.get("referencePriceMinor")) is not int:
            raise SeedFailure("missing synthetic reference price")

        title = row.get("title")
        brand = row.get("brand")
        seller = row.get("seller")
        revision = row.get("datasetRevision")
        if any(type(value) is not str for value in (title, brand, seller, revision)):
            raise SeedFailure("invalid product text field")
        attribute_terms: list[str] = []
        for key in sorted(attributes):
            attribute = attributes[key]
            if type(attribute) is not dict or attribute.get("status") != "known":
                continue
            value = attribute.get("value")
            if type(value) is not str:
                continue
            tokens = attribute.get("matchedRawTokens")
            raw_tokens = [token for token in tokens if type(token) is str] if type(tokens) is list else []
            # The production fact extractor accepts exact comma-delimited
            # tokens. Preserve that source grammar in the Java/MySQL fixture.
            raw_value = ",".join(raw_tokens) or value
            raw_value = raw_value[:512]
            attribute_terms.extend([key, value, *raw_tokens])
            attribute_rows.append(
                "(" + ",".join([
                    str(item_id), sql_text(key), sql_text("text"), sql_text(raw_value),
                    sql_text(value), "NULL", "NULL", "NULL",
                    sql_text(ATTRIBUTE_EVIDENCE_FIELD),
                    sql_text(ATTRIBUTE_RULESET_VERSION), "1.0000",
                ]) + ")"
            )
        attribute_text = " ".join([
            title, brand, seller, *category, *attribute_terms,
        ])[:16000]
        product_rows.append(
            "(" + ",".join([
                str(item_id), sql_text("kuaisearch"), sql_text(item_text), sql_text(title),
                sql_text(brand), sql_text(seller), *(sql_text(value) for value in category),
                str(price["referencePriceMinor"]), sql_text("CNY"), sql_text("synthetic"),
                sql_text("ACTIVE"), "1", sql_text(attribute_text),
                sql_text("historical_dataset_snapshot"), sql_text(revision),
                sql_text("research_dataset"), sql_text(PROVENANCE_URL),
            ]) + ")"
        )
    return product_rows, attribute_rows, observed


def seed_commerce_fixture(container: str, size: int) -> dict[str, Any]:
    if size <= 0 or size % 2:
        raise SeedFailure("commerce fixture size must be a positive even number")
    half = size // 2
    existing_raw = mysql(
        container,
        "SELECT CONCAT("
        "(SELECT COUNT(*) FROM product WHERE source='e2e_fixture'),CHAR(9),"
        "(SELECT COUNT(*) FROM inventory_stock s JOIN product p ON p.id=s.item_id "
        " WHERE s.item_type='PRODUCT' AND p.source='e2e_fixture'));\n",
    )
    existing_parts = existing_raw.replace("\\t", "\t").split("\t")
    if len(existing_parts) != 2 or any(not part.isdigit() for part in existing_parts):
        raise SeedFailure("unexpected commerce fixture count response")
    existing = tuple(int(part) for part in existing_parts)

    def ids_for(value: str) -> list[int]:
        output = mysql(
            container,
            "SELECT product_id FROM product_attribute "
            "WHERE attribute_key='screen_originality' "
            f"AND normalized_text={sql_text(value)} COLLATE utf8mb4_unicode_ci "
            f"ORDER BY product_id LIMIT {half};\n",
        )
        result = [int(line) for line in output.splitlines() if line.isdigit()]
        if len(result) != half:
            raise SeedFailure(f"insufficient {value} screen fixture products")
        return result

    original_ids = ids_for("original")
    non_original_ids = ids_for("non_original")
    selected = original_ids + non_original_ids
    if len(set(selected)) != size:
        raise SeedFailure("commerce fixture identities overlap")
    if existing == (size, size):
        return {
            "status": "already_complete",
            "productCount": size,
            "inventoryCount": size,
            "originalScreenCount": half,
            "nonOriginalScreenCount": half,
            "productIds": selected,
        }
    if existing != (0, 0):
        raise SeedFailure(
            f"refusing partial commerce fixture: products={existing[0]} inventory={existing[1]}"
        )

    statements = ["START TRANSACTION;"]
    for rank, product_id in enumerate(selected):
        fixture_price = 80_000 + rank * 1_000
        statements.append(
            "UPDATE product SET "
            f"source='e2e_fixture',snapshot_price_minor={fixture_price},"
            "currency='CNY',price_status='verified',data_nature='test_fixture',"
            "attribute_text=CONCAT(attribute_text,' e2e-memory-fixture 二手手机'),"
            "entity_version=entity_version+1 "
            f"WHERE id={product_id};"
        )
        statements.append(
            "INSERT INTO inventory_stock("
            "item_type,item_id,total_quantity,available_quantity,reserved_quantity,sold_quantity,version"
            f") VALUES('PRODUCT',{product_id},1,1,0,0,1);"
        )
    statements.append("COMMIT;")
    mysql(container, "\n".join(statements) + "\n")
    after_raw = mysql(
        container,
        "SELECT CONCAT("
        "(SELECT COUNT(*) FROM product WHERE source='e2e_fixture'),CHAR(9),"
        "(SELECT COUNT(*) FROM inventory_stock s JOIN product p ON p.id=s.item_id "
        " WHERE s.item_type='PRODUCT' AND p.source='e2e_fixture'));\n",
    )
    after_parts = after_raw.replace("\\t", "\t").split("\t")
    if after_parts != [str(size), str(size)]:
        raise SeedFailure("post-seed commerce fixture cardinality mismatch")
    return {
        "status": "inserted",
        "productCount": size,
        "inventoryCount": size,
        "originalScreenCount": half,
        "nonOriginalScreenCount": half,
        "productIds": selected,
    }


def seed(container: str, commerce_fixture_size: int) -> dict[str, Any]:
    product_rows, attribute_rows, observed = build_rows()
    before = counts(container)
    expected_attributes = len(attribute_rows)
    if before == (EXPECTED_PRODUCTS, expected_attributes):
        if current_attribute_contract_count(container) != expected_attributes:
            raise SeedFailure("complete catalog uses a stale attribute evidence contract")
        catalog_result = {
            **observed,
            "status": "already_complete",
            "productCount": before[0],
            "attributeCount": before[1],
        }
    elif before != (0, 0):
        raise SeedFailure(
            f"refusing partial or foreign catalog: products={before[0]} attributes={before[1]}"
        )
    else:
        statements = ["SET NAMES utf8mb4;", "START TRANSACTION;"]
        product_columns = (
            "id,source,source_item_id,title,brand,seller,category_l1,category_l2,category_l3,"
            "snapshot_price_minor,currency,price_status,lifecycle_status,entity_version,attribute_text,"
            "data_nature,dataset_revision,source_license,provenance_url"
        )
        for batch in batched(product_rows, 80):
            statements.append(f"INSERT INTO product({product_columns}) VALUES" + ",".join(batch) + ";")
        attribute_columns = (
            "product_id,attribute_key,value_type,raw_value,normalized_text,normalized_number,"
            "normalized_boolean,unit,evidence_field,extraction_method,confidence"
        )
        for batch in batched(attribute_rows, 300):
            statements.append(
                f"INSERT INTO product_attribute({attribute_columns}) VALUES" + ",".join(batch) + ";"
            )
        statements.append("COMMIT;")
        mysql(container, "\n".join(statements) + "\n")
        after = counts(container)
        if after != (EXPECTED_PRODUCTS, expected_attributes):
            raise SeedFailure("post-seed catalog cardinality mismatch")
        if current_attribute_contract_count(container) != expected_attributes:
            raise SeedFailure("post-seed attribute evidence contract mismatch")
        catalog_result = {
            **observed,
            "status": "inserted",
            "productCount": after[0],
            "attributeCount": after[1],
        }
    return {
        "catalog": catalog_result,
        "commerceFixture": seed_commerce_fixture(container, commerce_fixture_size),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mysql-container",
        default="agent-memory-v18-e2e-mysql-20260830",
    )
    parser.add_argument("--commerce-fixture-size", type=int, default=20)
    args = parser.parse_args()
    try:
        result = seed(args.mysql_container, args.commerce_fixture_size)
    except Exception as exc:
        print(json.dumps({
            "status": "failed", "errorType": type(exc).__name__, "error": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
