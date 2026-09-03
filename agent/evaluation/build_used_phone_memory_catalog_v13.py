"""Build the live V13 memory allowlist from the pinned 439-phone catalog."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from app.domains.ecommerce.models import canonicalize_brand


SOURCE_SHA256 = "725c5fe9209c0b278004c61d24dafab21593c128e679ea0a1ecf3ae4eb433d75"
SOURCE_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
CATALOG_REVISION = "used-phone-439-09807c773ce6"

LABELS = {
    "apple": "苹果", "samsung": "三星", "huawei": "华为",
    "honor": "荣耀", "xiaomi": "小米", "redmi": "红米",
    "oppo": "OPPO", "vivo": "vivo", "iqoo": "iQOO",
    "oneplus": "一加", "realme": "真我", "nubia": "努比亚",
    "blackshark": "黑鲨", "hi": "畅享",
    "android": "安卓", "ios": "iOS",
    "original": "原装", "non_original": "非原装",
    "repaired": "有维修", "not_repaired": "无维修",
    "70_80": "电池健康 70%-80%", "80_90": "电池健康 80%-90%",
    "90_plus": "电池健康 90%+", "light": "轻微划痕",
    "heavy": "明显划痕", "none": "无划痕", "normal": "外壳正常",
    "damaged": "外壳有损伤",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("versioned output already exists")
    if sha256(args.source) != SOURCE_SHA256:
        raise ValueError("source hash mismatch")

    values: set[tuple[str, str]] = set()
    product_count = 0
    with args.source.open("r", encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("datasetRevision") != SOURCE_REVISION:
                raise ValueError("dataset revision mismatch")
            product_count += 1
            brand = canonicalize_brand(row.get("brand", ""))
            if brand in LABELS:
                values.add(("brand", brand))
            attributes = row.get("attributes")
            if type(attributes) is not dict:
                raise ValueError("invalid product attributes")
            for key, evidence in attributes.items():
                if type(evidence) is not dict:
                    raise ValueError("invalid product attribute")
                value = evidence.get("value")
                if evidence.get("status") == "known" and type(value) is str:
                    values.add((key, value))
    if product_count != 439:
        raise ValueError("expected 439 products")

    rows = [{
        "attributeKey": key,
        "catalogRevision": CATALOG_REVISION,
        "categoryId": "phone",
        "displayLabel": LABELS.get(value, value),
        "normalizedValue": value,
    } for key, value in sorted(values)]
    args.output.mkdir(parents=True)
    catalog_path = args.output / "catalog-values.jsonl"
    with catalog_path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    manifest = {
        "schemaVersion": "used-phone-memory-catalog-v13-manifest-v1",
        "catalogRevision": CATALOG_REVISION,
        "source": str(args.source.as_posix()),
        "sourceSha256": SOURCE_SHA256,
        "sourceDatasetRevision": SOURCE_REVISION,
        "productCount": product_count,
        "catalogValueCount": len(rows),
        "catalogValuesSha256": sha256(catalog_path),
        "builderSha256": sha256(Path(__file__)),
    }
    (args.output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
