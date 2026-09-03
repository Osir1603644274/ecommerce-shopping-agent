"""Build the bounded V13 memory benchmark from pinned Shopping Companion Parquet.

No model call is made. Test query-to-target labels remain in a sealed file and
are never loaded by dev/validation selection code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd

REVISION = "9a8a2a1c13f0d88de070238352bcf71f98ca851f"
TRAIN_HASH = "bc87aad3685da0176df4777d05942c8aa05d4dd4ac3230d6dc96a78f33214377"
TEST_HASH = "65cb4e36a182b52fc7e671770d8601e4c6cca6ecb0bccd2efb62cdea8f600a8d"


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


def ground_truth(row: pd.Series) -> dict[str, Any]:
    reward = row["reward_model"]
    if type(reward) is not dict or type(reward.get("ground_truth")) is not str:
        raise ValueError("invalid reward_model")
    value = json.loads(reward["ground_truth"])
    single = {
        "product_id", "product_name", "price", "category", "aspects", "prompt",
        "wanted_features", "does_not_matter_features", "dialogue",
    }
    bundle = {"n", "voucher_type", "preferences", "voucher"}
    if type(value) is not dict or frozenset(value) not in {frozenset(single), frozenset(bundle)}:
        raise ValueError("invalid ground truth")
    return value


def feature_set(
    values: list[str],
    kind: str,
    category_id: str,
    aspects: list[list[str]],
) -> list[dict[str, str]]:
    result = []
    for raw in values:
        if type(raw) is not str:
            raise ValueError("invalid preference feature")
        if ":" in raw:
            key, value = raw.split(":", 1)
        else:
            matches = [key for key, value in aspects if str(value) == raw]
            if not matches or len(set(matches)) != 1:
                raise ValueError("unresolvable preference feature")
            key, value = matches[0], raw
        result.append({
            "categoryId": category_id,
            "preferenceKind": kind,
            "attributeKey": attribute_key(key),
            "normalizedValue": slug(value.strip(), limit=128),
            "catalogRevision": f"shopping-companion-{REVISION[:12]}",
            "recipientScope": "self",
            "source": "user_confirmed",
            "displayValue": value.strip(),
        })
    return result


def memory_episode(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    category_id = slug(value["category"], limit=64)
    episode = {
        "categoryId": category_id,
        "dialogue": value["dialogue"],
        "preferences": (
            feature_set(value["wanted_features"], "prefer", category_id, value["aspects"])
            + feature_set(value["does_not_matter_features"], "indifferent", category_id, value["aspects"])
        ),
    }
    product = {
        "productId": str(value["product_id"]),
        "productName": str(value["product_name"]),
        "price": value["price"],
        "categoryId": category_id,
        "aspects": [
            {
                "attributeKey": attribute_key(key),
                "normalizedValue": slug(item, limit=128),
                "displayValue": str(item),
            }
            for key, item in value["aspects"]
        ],
    }
    return episode, product


def scenario(row: pd.Series) -> dict[str, Any]:
    info = row["extra_info"]
    if type(info) is not dict:
        raise ValueError("invalid extra_info")
    truth = ground_truth(row)
    raw_preferences = (
        [truth] if "product_id" in truth else truth["preferences"]
    )
    pairs = [memory_episode(value) for value in raw_preferences]
    return {
        "conversationId": str(info["conversation_id"]),
        "questionId": str(info["question_id"]),
        "questionType": str(info["question_type"]),
        "query": str(info["question"]),
        "memoryEpisodes": [pair[0] for pair in pairs],
        "targetProducts": [pair[1] for pair in pairs],
        "voucher": truth.get("voucher"),
        "voucherType": truth.get("voucher_type"),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    train_path = args.raw_dir / "train.parquet"
    test_path = args.raw_dir / "test.parquet"
    if sha256(train_path) != TRAIN_HASH or sha256(test_path) != TEST_HASH:
        raise SystemExit("raw hash mismatch")
    train = pd.read_parquet(train_path)
    test = pd.read_parquet(test_path)
    if train.shape != (1600, 6) or test.shape != (400, 6):
        raise SystemExit("raw shape mismatch")
    train_stage1 = train[train["ability"] == "stage_1"]
    test_stage1 = test[test["ability"] == "stage_1"]
    train_rows = [scenario(row) for _, row in train_stage1.iterrows()]
    test_rows = [scenario(row) for _, row in test_stage1.iterrows()]
    conversation_ids = sorted(
        {row["conversationId"] for row in train_rows},
        key=lambda item: (hashlib.sha256(item.encode()).hexdigest(), item),
    )
    if len(conversation_ids) != 400:
        raise SystemExit("conversation cardinality mismatch")
    dev_ids = set(conversation_ids[:300])
    dev = [row for row in train_rows if row["conversationId"] in dev_ids]
    validation = [row for row in train_rows if row["conversationId"] not in dev_ids]
    if len(dev) != 600 or len(validation) != 200 or len(test_rows) != 200:
        raise SystemExit("split cardinality mismatch")
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "dev.jsonl", dev)
    write_jsonl(output / "validation.jsonl", validation)
    write_jsonl(output / "sealed-test.jsonl", test_rows)

    products: dict[str, dict[str, Any]] = {}
    catalog: dict[tuple[str, str, str, str], dict[str, str]] = {}
    for row in dev + validation:
        for product in row["targetProducts"]:
            products[product["productId"]] = product
        for episode in row["memoryEpisodes"]:
            for preference in episode["preferences"]:
                key = (
                    preference["catalogRevision"], preference["categoryId"],
                    preference["attributeKey"], preference["normalizedValue"],
                )
                catalog[key] = {
                    "catalogRevision": key[0], "categoryId": key[1],
                    "attributeKey": key[2], "normalizedValue": key[3],
                    "displayLabel": preference["displayValue"],
                }
    write_jsonl(output / "train-target-catalog.jsonl", sorted(products.values(), key=lambda x: x["productId"]))
    write_jsonl(output / "catalog-values.jsonl", [catalog[key] for key in sorted(catalog)])
    manifest = {
        "schemaVersion": "shopping-memory-v13-dataset-v1",
        "sourceRevision": REVISION,
        "rawHashes": {"train.parquet": TRAIN_HASH, "test.parquet": TEST_HASH},
        "counts": {
            "devQuestions": len(dev), "validationQuestions": len(validation),
            "sealedTestQuestions": len(test_rows), "trainCatalogProducts": len(products),
            "catalogValues": len(catalog),
        },
        "files": {},
    }
    for name in ("dev.jsonl", "validation.jsonl", "sealed-test.jsonl", "train-target-catalog.jsonl", "catalog-values.jsonl"):
        manifest["files"][name] = sha256(output / name)
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
