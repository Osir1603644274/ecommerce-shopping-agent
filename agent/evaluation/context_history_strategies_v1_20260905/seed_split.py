"""Pre-outcome conservative source-family split; no qrels or formal scripts."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifacts import HERE, file_sha, sha, write_new

# Merge, never split, the earlier AI families. These decisions inspect public
# query meaning only, not Agent outcomes or relevance labels.
MERGES = {
    "local_phone_search": ("海城二手手机", "绥中二手手机", "鹤壁二手苹果", "爱酷z10turbpro+西宁店"),
    "iphone_pro_max_used_price": ("苹果16pm现在多少钱二手", "苹果promax2手国行双卡双待多少钱"),
    "iqoo_budget_recommendation": ("二手iqoo手机3000左右", "爱酷1000块左右的手机"),
    "very_low_budget_phone": ("手机200元数据多一点", "vivo两百", "200元骁龙855", "三星100元折叠手机"),
}
STRESS_ONLY = {
    "内核手机小米14", "苹果0", "苹果手机专卖店官方旗舰店16", "荣耀300全新多少钱",
    "十大杂牌手机排行榜", "安卓12版本手机", "苹果13一手", "适合12的手机",
    "手机政府补贴华为", "答题赢iphone", "3张备用机推荐",
    "手机200元数据多一点", "vivo两百", "200元骁龙855", "三星100元折叠手机",
}


def run(output: Path):
    source = HERE / "seed_family_pool001/grouping/families.json"
    pool = HERE / "seed_family_pool001/queries.jsonl"
    document = json.loads(source.read_text(encoding="utf-8"))
    if file_sha(pool) != document["sourceSha256"] or document["qrelsRead"]:
        raise ValueError("family_source_mismatch")
    rows = [json.loads(line) for line in pool.read_text(encoding="utf-8").splitlines()]
    by_query = {row["query"]: row for row in rows}
    if len(by_query) != len(rows):
        raise ValueError("duplicate_query_requires_additional_audit")
    membership = {row["query"]: group["familyId"] for group in document["families"] for row in group["members"]}
    if set(membership) != set(by_query):
        raise ValueError("family_coverage_mismatch")
    grouped = {group["familyId"]: set(row["query"] for row in group["members"]) for group in document["families"]}
    for label, queries in MERGES.items():
        if not set(queries) <= set(by_query):
            raise ValueError("merge_query_not_in_pool")
        selected = [key for key, values in grouped.items() if values.intersection(queries)]
        merged = set().union(*(grouped.pop(key) for key in selected))
        grouped[label] = merged
    dev_queries = {row["query"] for group in document["families"] if group["containsKnownDevelopmentSeed"] for row in group["members"]}
    result = []
    eligible = sorted((key for key, queries in grouped.items() if not queries.intersection(dev_queries | STRESS_ONLY)),
        key=lambda key: sha(["context-history-development-addition-v1", sorted(grouped[key])]))
    # Add two seed families for development diversity BEFORE new outcome access.
    additional_dev = set(eligible[:2])
    for key, queries in sorted(grouped.items()):
        split = "development" if queries.intersection(dev_queries) or key in additional_dev else (
            "capability_stress_reserve" if queries.intersection(STRESS_ONLY) else "formal_candidate_reserve")
        result.append({"familyId": "source-family-" + sha(sorted(queries))[:16], "auditGroup": key,
            "split": split, "members": [by_query[query] for query in sorted(queries)]})
    assignments = [row["query_id"] for group in result for row in group["members"]]
    if len(assignments) != len(set(assignments)) or set(assignments) != {row["query_id"] for row in rows}:
        raise ValueError("split_assignment_not_bijective")
    old_to_new = {row["query"]: group["familyId"] for group in result for row in group["members"]}
    if any(len({old_to_new[row["query"]] for row in group["members"]}) != 1 for group in document["families"]):
        raise ValueError("old_family_split")
    write_new(output, {"status": "DEVELOPMENT_AND_RESERVED_SOURCE_FAMILIES_AUDITED", "source": str(source),
        "sourceSha256": file_sha(source), "poolSha256": file_sha(pool), "scriptSha256": file_sha(__file__),
        "families": result, "qrelsRead": False, "formalScriptsGenerated": False,
        "eligibilityRule": "Known development families and overlaps remain development; two further non-stress families selected by fixed hash order. Non-shopping, unsupported commerce modes, ambiguous hardware intent and extreme-budget seeds reserved as capability stress, not silently discarded after outcomes.",
        "limitation": "Conservative semantic heuristic, not a proof of distribution independence. No formal user scripts or outcomes have been accessed; a future formal freeze must bind this manifest."})
    print(json.dumps({"families": len(result), "queries": len(assignments),
        "bySplit": {split: sum(group["split"] == split for group in result) for split in sorted({group["split"] for group in result})}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
