"""Label-free query-family grouping before generation of any formal scripts."""
import argparse
import asyncio
import json
from pathlib import Path

import jsonschema

from .artifacts import HERE, canonical, file_sha, sha, write_new
from .dataset import SEEDS
from .subscription import SubscriptionClient

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["families"],
    "properties": {"families": {"type": "array", "items": {"type": "object", "additionalProperties": False,
        "required": ["name", "indices"], "properties": {"name": {"type": "string"},
            "indices": {"type": "array", "minItems": 1, "items": {"type": "integer", "minimum": 0}}}}}}}


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(line) for line in SEEDS.read_text(encoding="utf-8").splitlines() if line.strip()]
    known_dev = {json.loads(path.read_text(encoding="utf-8"))["seedProvenance"]["originalQueryId"]
                 for path in (HERE / "dev_dataset002").glob("dev-*.json")}
    client = SubscriptionClient(output / "grouping_calls", max_calls=1)
    prompt = ("将这批公开手机搜索Query按近义查询家族分组，以防同源近义改写同时进入开发集与留出集。"
        "只判断文本语义，不回答Query，不生成购物需求、商品结果或相关性标签。"
        "同一具体机型的别名、缩写、问价/二手价格近义表达必须同组，例如16PM与iPhone16ProMax。"
        "地名不同但都是当地二手机交易搜索，可保守合为同类。预算档、品牌偏好、功能诉求明显不同的可分开；"
        "不要把所有手机搜索都合为一组。每个数字index必须且只能出现一次。只输出符合schema的JSON。\n"
        + canonical({"schema": SCHEMA, "queries": [{"index": index, "query": row["query"]} for index, row in enumerate(rows)]}))
    response = await client.chat.completions.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": prompt}])
    value = json.loads(response.choices[0].message.content)
    jsonschema.validate(value, SCHEMA)
    assigned = [index for family in value["families"] for index in family["indices"]]
    if sorted(assigned) != list(range(len(rows))):
        raise ValueError("family_assignment_missing_duplicate_or_unknown_query")
    families = []
    for family in value["families"]:
        members = [rows[index] for index in sorted(family["indices"])]
        ids = sorted(row["query_id"] for row in members)
        families.append({"familyId": "family-" + sha(ids)[:16], "name": family["name"],
            "members": members, "containsKnownDevelopmentSeed": bool(known_dev.intersection(ids))})
    write_new(output / "families.json", {"status": "LABEL_FREE_AI_FAMILY_CANDIDATE_REQUIRES_SPLIT_AUDIT",
        "source": str(SEEDS), "sourceSha256": file_sha(SEEDS), "qrelsRead": False,
        "families": families, "formalScriptsGenerated": False,
        "note": "AI grouping is a leakage-prevention heuristic, not human semantic gold. Entire known-development families remain development."})
    print(canonical({"families": len(families), "queries": len(rows),
        "knownDevelopmentFamilies": sum(family["containsKnownDevelopmentSeed"] for family in families)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    asyncio.run(run(parser.parse_args().output))
