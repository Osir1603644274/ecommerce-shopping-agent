"""Expand Track B to 18 evidence-grounded single-attribute short queries.

This extension consumes only the hash-pinned Track B evidence-product audit.
It preserves the original Pilot and writes a separate manifest/layer. New query
and candidate judgments are user-delegated assistant work, never human gold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from evaluation.kuaisearch_synthetic_evidence_benchmark import (
    BENCHMARK_ID,
    BUILDER_VERSION,
    CONTROLLED_GROUPS,
    JoinedProduct,
    JsonlWriter,
    RelevanceEvidence,
    artifact_fingerprint,
    bm25_scores,
    leak_check,
    normalize_text,
    product_to_audit_row,
    split_attr_values,
    stable_hash,
    write_json,
    write_text,
)


EXTENSION_VERSION = "track-b-short-query-extension-v1"
DELEGATION_EVIDENCE_SHA256 = "6135ebf957a02b79d1e13e8e4f3110c226e0915a4fbcb028787ffaaccfbe0719"


SHORT_QUERY_DEFINITIONS: list[dict[str, Any]] = [
    # Original three Pilot single-attribute queries.
    {"slotId": "tb-tshirt-01-single", "categoryKey": "1/39/0", "group": "sleeve_length", "value": "短袖", "queryText": "想找短袖的女款T恤，其他款式都可以。", "existingQueryId": "synq-c47854c5df3032ba"},
    {"slotId": "tb-jeans-01-single", "categoryKey": "1/13/103", "group": "waist_height", "value": "中腰", "queryText": "想找中腰的女款牛仔裤。", "existingQueryId": "synq-610da833ad40c6eb"},
    {"slotId": "tb-shoes-01-single", "categoryKey": "23/40/217", "group": "heel_height", "value": "平底", "queryText": "想找平底的女款休闲板鞋。", "existingQueryId": "synq-99f722adf4c6b82e"},
    # T-shirt extension.
    {"slotId": "tb-tshirt-04-long-sleeve", "categoryKey": "1/39/0", "group": "sleeve_length", "value": "长袖", "queryText": "想找长袖的女款T恤。"},
    {"slotId": "tb-tshirt-05-loose", "categoryKey": "1/39/0", "group": "fit", "value": "宽松型", "queryText": "想找版型宽松的女款T恤。"},
    {"slotId": "tb-tshirt-06-slim", "categoryKey": "1/39/0", "group": "fit", "value": "修身型", "queryText": "想找修身版型的女款T恤。"},
    {"slotId": "tb-tshirt-07-cotton", "categoryKey": "1/39/0", "group": "material", "value": "棉", "queryText": "想找棉质的女款T恤。"},
    {"slotId": "tb-tshirt-08-v-neck", "categoryKey": "1/39/0", "group": "collar", "value": "v领", "queryText": "想找V领的女款T恤。"},
    # Jeans extension.
    {"slotId": "tb-jeans-04-high-waist", "categoryKey": "1/13/103", "group": "waist_height", "value": "高腰", "queryText": "想找高腰的女款牛仔裤。"},
    {"slotId": "tb-jeans-05-low-waist", "categoryKey": "1/13/103", "group": "waist_height", "value": "低腰", "queryText": "想找低腰的女款牛仔裤。"},
    {"slotId": "tb-jeans-06-straight", "categoryKey": "1/13/103", "group": "leg_shape", "value": "直筒裤", "queryText": "想找直筒版型的女款牛仔裤。"},
    {"slotId": "tb-jeans-07-wide-leg", "categoryKey": "1/13/103", "group": "leg_shape", "value": "阔脚裤", "queryText": "想找阔腿版型的女款牛仔裤。"},
    {"slotId": "tb-jeans-08-flare", "categoryKey": "1/13/103", "group": "leg_shape", "value": "微喇裤", "queryText": "想找微喇版型的女款牛仔裤。"},
    # Casual board-shoe extension.
    {"slotId": "tb-shoes-04-low-heel", "categoryKey": "23/40/217", "group": "heel_height", "value": "低跟(1-3cm)", "queryText": "想找低跟的女款休闲板鞋。"},
    {"slotId": "tb-shoes-05-mid-heel", "categoryKey": "23/40/217", "group": "heel_height", "value": "中跟(3-5cm)", "queryText": "想找中跟的女款休闲板鞋。"},
    {"slotId": "tb-shoes-06-lace-up", "categoryKey": "23/40/217", "group": "closure", "value": "系带", "queryText": "想找系带的女款休闲板鞋。"},
    {"slotId": "tb-shoes-07-synthetic-leather", "categoryKey": "23/40/217", "group": "upper_material", "value": "合成革", "queryText": "想找合成革鞋面的女款休闲板鞋。"},
    {"slotId": "tb-shoes-08-textile", "categoryKey": "23/40/217", "group": "upper_material", "value": "织物/纺织品", "queryText": "想找织物鞋面的女款休闲板鞋。"},
]


TITLE_ALIASES: dict[str, list[str]] = {
    "短袖": ["短袖", "半袖"],
    "长袖": ["长袖"],
    "无袖": ["无袖"],
    "五分袖": ["五分袖"],
    "七分袖": ["七分袖"],
    "九分袖": ["九分袖"],
    "宽松型": ["宽松"],
    "修身型": ["修身"],
    "标准型": ["标准型"],
    "棉": ["纯棉", "棉质"],
    "涤纶(聚酯纤维)": ["涤纶", "聚酯纤维"],
    "聚酯纤维": ["聚酯纤维"],
    "圆领": ["圆领"],
    "v领": ["v领"],
    "polo领": ["polo领"],
    "高腰": ["高腰"],
    "中腰": ["中腰"],
    "低腰": ["低腰"],
    "直筒裤": ["直筒"],
    "阔脚裤": ["阔腿", "阔脚"],
    "微喇裤": ["微喇"],
    "小脚裤": ["小脚"],
    "喇叭裤": ["喇叭裤"],
    "平底": ["平底"],
    "低跟(1-3cm)": ["低跟"],
    "中跟(3-5cm)": ["中跟"],
    "高跟(5-8cm)": ["高跟"],
    "系带": ["系带", "绑带"],
    "套脚": ["套脚", "一脚蹬"],
    "魔术贴": ["魔术贴"],
    "一脚蹬": ["一脚蹬"],
    "合成革": ["合成革"],
    "织物/纺织品": ["织物", "纺织品", "帆布"],
    "合成纤维": ["合成纤维"],
    "真皮": ["真皮"],
}


CATEGORY_CONFLICT_TERMS = {
    "1/39/0": ["半身裙", "连衣裙", "裤子", "马甲+裤子", "卫衣外套"],
    "1/13/103": ["半身裙", "连衣裙", "上衣", "t恤", "外套"],
    "23/40/217": ["拖鞋", "凉鞋", "长靴", "短靴", "袜子", "裤子", "上衣"],
}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError(f"expected object at {path}:{line_number}")
                rows.append(row)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    writer = JsonlWriter(path)
    for row in rows:
        writer.write(row)
    return writer.close()


def to_product(row: dict[str, Any]) -> JoinedProduct:
    evidence = [
        RelevanceEvidence(
            line_number=ref["lineNumber"],
            attr_raw=ref["rawValue"],
            attr_values=split_attr_values(ref["rawValue"]),
        )
        for ref in row["evidenceRefs"]
    ]
    return JoinedProduct(
        item_id=row["itemId"],
        title=row["title"],
        brand=row["brand"],
        seller=row["seller"],
        category_key=row["categoryKey"],
        category_names=tuple(row["categoryPath"]),
        evidence=evidence,
    )


def query_id(definition: dict[str, Any]) -> str:
    return definition.get("existingQueryId") or (
        "synq-" + stable_hash(BENCHMARK_ID, EXTENSION_VERSION, definition["slotId"], definition["queryText"])[:16]
    )


def query_record(definition: dict[str, Any]) -> dict[str, Any]:
    existing = "existingQueryId" in definition
    key = definition["categoryKey"]
    category_path = {
        "1/39/0": ["女装", "T恤", "UNKNOWN"],
        "1/13/103": ["女装", "裤子", "牛仔裤装"],
        "23/40/217": ["女鞋", "休闲鞋", "休闲板鞋"],
    }[key]
    qid = query_id(definition)
    return {
        "recordType": "syntheticShortQuery",
        "benchmarkId": BENCHMARK_ID,
        "extensionVersion": EXTENSION_VERSION,
        "queryId": qid,
        "slotId": definition["slotId"],
        "queryText": definition["queryText"],
        "queryOrigin": "synthetic_evidence_grounded",
        "categoryKey": key,
        "categoryPath": category_path,
        "requirementAtoms": [
            {
                "atomId": f"{definition['slotId']}-category",
                "importance": "hard",
                "attributeGroup": "exact_category",
                "expectedValue": key,
                "sourceField": "items_lite.category_level1/2/3",
            },
            {
                "atomId": f"{definition['slotId']}-attribute",
                "importance": "hard",
                "attributeGroup": definition["group"],
                "expectedValue": definition["value"],
                "sourceField": "relevance.attr_value or explicit items_lite.item_title",
            },
        ],
        "queryApprovalProvenance": (
            {
                "type": "project_owner_human_approval",
                "artifact": "query_review_decisions_v1.jsonl",
                "humanApproved": True,
            }
            if existing
            else {
                "type": "user_delegated_assistant_authored",
                "delegationEvidenceSha256": DELEGATION_EVIDENCE_SHA256,
                "humanApproved": False,
            }
        ),
        "goldStatus": "not_gold_short_query",
    }


def title_values(product: JoinedProduct, group_values: list[str]) -> set[str]:
    title = normalize_text(product.title)
    observed = set()
    for value in group_values:
        if any(normalize_text(alias) in title for alias in TITLE_ALIASES.get(value, [value])):
            observed.add(value)
    return observed


def category_conflict(product: JoinedProduct, expected_category: str) -> list[str]:
    if product.category_key != expected_category:
        return ["cross_category_candidate"]
    title = normalize_text(product.title)
    conflicts = [term for term in CATEGORY_CONFLICT_TERMS[expected_category] if normalize_text(term) in title]
    return ["catalog_category_title_conflict"] if conflicts else []


def judge(query: dict[str, Any], product: JoinedProduct) -> dict[str, Any]:
    expected_category = query["categoryKey"]
    attribute_atom = query["requirementAtoms"][1]
    group = attribute_atom["attributeGroup"]
    expected_value = attribute_atom["expectedValue"]
    group_values = CONTROLLED_GROUPS[expected_category][group]
    flags = category_conflict(product, expected_category)
    category_status = "pass" if product.category_key == expected_category and not flags else "fail"

    attr_observed = set(group_values).intersection(product.attr_values)
    title_observed = title_values(product, group_values) if product.category_key == expected_category else set()
    observed = attr_observed.union(title_observed)
    alternatives = observed - {expected_value}
    if expected_value in observed and alternatives:
        attribute_status = "unknown"
        evidence_state = "conflicting_controlled_values"
        flags.append("title_or_attribute_controlled_value_conflict")
    elif expected_value in observed:
        attribute_status = "pass"
        evidence_state = "explicit_match"
    elif alternatives:
        attribute_status = "fail"
        evidence_state = "explicit_controlled_alternative"
    else:
        attribute_status = "unknown"
        evidence_state = "missing"

    if category_status == "fail":
        grade = 0
        eligible: bool | str = False
    elif attribute_status == "pass":
        grade = 3
        eligible = True
    elif attribute_status == "fail":
        grade = 1
        eligible = False
    else:
        grade = 1
        eligible = "unknown"

    return {
        "benchmarkId": BENCHMARK_ID,
        "extensionVersion": EXTENSION_VERSION,
        "queryId": query["queryId"],
        "queryText": query["queryText"],
        "itemId": product.item_id,
        "relevanceGrade": grade,
        "eligible": eligible,
        "requirementAtoms": [
            {
                "atomId": query["requirementAtoms"][0]["atomId"],
                "status": category_status,
                "evidenceState": "structured_category_with_title_conflict_audit",
            },
            {
                "atomId": attribute_atom["atomId"],
                "status": attribute_status,
                "evidenceState": evidence_state,
                "observedControlledValues": sorted(observed),
            },
        ],
        "evidenceSnapshot": product_to_audit_row(product),
        "qualityFlags": sorted(set(flags)),
        "judgmentProvenance": {
            "type": "deterministic_evidence_rule_with_user_delegated_assistant_audit",
            "builderVersion": EXTENSION_VERSION,
            "delegationEvidenceSha256": DELEGATION_EVIDENCE_SHA256,
            "humanConfirmed": False,
            "usesSourceEditorialScore": False,
        },
        "goldStatus": "not_human_gold_assistant_judged",
    }


def select_pool(
    query: dict[str, Any], products: list[JoinedProduct]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    scores = bm25_scores(query["queryText"], products)
    all_judgments = [(product, judge(query, product)) for product in products]
    bucket_rules = [
        ("eligible_true", 8, lambda j: j["eligible"] is True),
        ("same_category_fail", 8, lambda j: j["relevanceGrade"] == 1 and j["eligible"] is False),
        ("same_category_unknown", 4, lambda j: j["eligible"] == "unknown"),
        ("category_mismatch", 4, lambda j: j["relevanceGrade"] == 0),
    ]
    selected: list[tuple[JoinedProduct, dict[str, Any], str]] = []
    selected_ids: set[str] = set()
    for bucket_name, required, predicate in bucket_rules:
        bucket = [(product, decision) for product, decision in all_judgments if predicate(decision)]
        bucket.sort(
            key=lambda pair: (
                -scores[pair[0].item_id],
                stable_hash(query["queryId"], bucket_name, pair[0].brand, pair[0].item_id),
            )
        )
        chosen = []
        seen_brands: set[str] = set()
        for product, decision in bucket:
            brand = normalize_text(product.brand) or "__missing__"
            if brand not in seen_brands:
                chosen.append((product, decision))
                seen_brands.add(brand)
            if len(chosen) == required:
                break
        if len(chosen) < required:
            for pair in bucket:
                if pair not in chosen:
                    chosen.append(pair)
                if len(chosen) == required:
                    break
        if len(chosen) < required:
            raise ValueError(
                f"short-query gate failed for {query['queryId']} bucket {bucket_name}: {len(chosen)}/{required}"
            )
        for product, decision in chosen:
            if product.item_id in selected_ids:
                raise ValueError("candidate selected into multiple mutually exclusive buckets")
            selected_ids.add(product.item_id)
            selected.append((product, decision, bucket_name))

    selected.sort(key=lambda pair: stable_hash(query["queryId"], "blind-order", pair[0].item_id))
    decisions = []
    provenance = []
    for index, (product, decision, bucket_name) in enumerate(selected, 1):
        display_id = f"{query['queryId']}-c{index:02d}"
        decision["candidateDisplayId"] = display_id
        decisions.append(decision)
        provenance.append(
            {
                "queryId": query["queryId"],
                "candidateDisplayId": display_id,
                "itemId": product.item_id,
                "poolingRoutes": [
                    {"route": "offline_bm25_diagnostic", "score": scores[product.item_id]},
                    {"route": "evidence_stratum", "stratum": bucket_name},
                    {"route": "deterministic_brand_diversity"},
                ],
                "hiddenFromBlindReview": True,
                "formalPoolStatus": "incomplete_no_dense_or_hybrid_pool",
            }
        )
    return decisions, provenance


def normalize_existing_decisions(
    benchmark_dir: Path, queries_by_id: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    rows = load_jsonl(benchmark_dir / "short_query_decisions_combined_v1.jsonl")
    normalized = []
    for row in rows:
        query = queries_by_id[row["queryId"]]
        grade = row["relevanceGrade"]
        category_status = "fail" if grade == 0 else "pass"
        old_atom = row["requirementAtoms"][0]
        attribute_status = old_atom["status"]
        if attribute_status == "missing":
            attribute_status = "unknown"
        provenance = row.get("reviewProvenance") or row.get("judgmentProvenance")
        normalized.append(
            {
                "benchmarkId": BENCHMARK_ID,
                "extensionVersion": EXTENSION_VERSION,
                "queryId": row["queryId"],
                "queryText": query["queryText"],
                "candidateDisplayId": row["candidateDisplayId"],
                "itemId": row["itemId"],
                "relevanceGrade": grade,
                "eligible": row["eligible"],
                "requirementAtoms": [
                    {"atomId": query["requirementAtoms"][0]["atomId"], "status": category_status},
                    {"atomId": query["requirementAtoms"][1]["atomId"], "status": attribute_status},
                ],
                "qualityFlags": row.get("qualityFlags", []),
                "judgmentProvenance": provenance,
                "goldStatus": (
                    "human_confirmed_short_query_judgment"
                    if row.get("humanConfirmed")
                    else "not_human_gold_assistant_judged"
                ),
            }
        )
    return normalized


def build_extension(benchmark_dir: Path, schema_dir: Path) -> dict[str, Any]:
    base_manifest = json.loads((benchmark_dir / "manifest.json").read_text(encoding="utf-8"))
    evidence_artifact = base_manifest["artifacts"]["evidenceProductsAudit"]
    evidence_path = benchmark_dir / evidence_artifact["path"]
    if artifact_fingerprint(evidence_path, rows=evidence_artifact["rows"])["sha256"] != evidence_artifact["sha256"]:
        raise ValueError("base evidence-product audit hash mismatch")
    audit_rows = load_jsonl(evidence_path)
    products = [
        to_product(row)
        for row in audit_rows
        if row["categoryKey"] in {"1/39/0", "1/13/103", "23/40/217"}
    ]
    if len(products) != 1101:
        raise ValueError(f"expected 1,101 three-category evidence products, got {len(products)}")

    queries = [query_record(definition) for definition in SHORT_QUERY_DEFINITIONS]
    if len({query["queryId"] for query in queries}) != 18:
        raise ValueError("short query IDs are not unique")
    queries_by_id = {query["queryId"]: query for query in queries}
    extension_queries = [query for query in queries if query["queryApprovalProvenance"]["humanApproved"] is False]
    extension_decisions: list[dict[str, Any]] = []
    provenance_rows: list[dict[str, Any]] = []
    blind_rows: list[dict[str, Any]] = []
    leak_rows = []
    summaries = []
    for query in extension_queries:
        decisions, provenance = select_pool(query, products)
        extension_decisions.extend(decisions)
        provenance_rows.extend(provenance)
        leak_rows.append(leak_check(query, products))
        for decision in decisions:
            product = decision["evidenceSnapshot"]
            blind_rows.append(
                {
                    "benchmarkId": BENCHMARK_ID,
                    "queryId": query["queryId"],
                    "queryText": query["queryText"],
                    "candidateDisplayId": decision["candidateDisplayId"],
                    "product": product,
                    "assistantJudgmentHidden": True,
                }
            )
        summaries.append(
            {
                "queryId": query["queryId"],
                "queryText": query["queryText"],
                "candidateCount": len(decisions),
                "gradeCounts": dict(sorted(Counter(d["relevanceGrade"] for d in decisions).items())),
                "eligibleCounts": dict(sorted(Counter(str(d["eligible"]).lower() for d in decisions).items())),
                "qualityFlagCount": sum(bool(d["qualityFlags"]) for d in decisions),
            }
        )

    existing_decisions = normalize_existing_decisions(benchmark_dir, queries_by_id)
    all_decisions = [*existing_decisions, *extension_decisions]
    if len(extension_queries) != 15 or len(extension_decisions) != 360 or len(all_decisions) != 432:
        raise ValueError("unexpected short-query extension cardinality")
    counts_by_query = Counter(decision["queryId"] for decision in all_decisions)
    if set(counts_by_query.values()) != {24} or len(counts_by_query) != 18:
        raise ValueError(f"every short query must have 24 candidates: {counts_by_query}")
    if not all(row["passesMechanicalCheck"] for row in leak_rows):
        raise ValueError("query title-leakage gate failed")

    artifacts = {
        "extensionQueries": write_jsonl(benchmark_dir / "short_query_extension_queries_v1.jsonl", extension_queries),
        "extensionDecisions": write_jsonl(benchmark_dir / "short_query_extension_decisions_v1.jsonl", extension_decisions),
        "extensionBlindAudit": write_jsonl(benchmark_dir / "short_query_extension_blind_audit_v1.jsonl", blind_rows),
        "extensionPoolProvenance": write_jsonl(benchmark_dir / "short_query_extension_pool_provenance_internal_v1.jsonl", provenance_rows),
        "allShortQueries": write_jsonl(benchmark_dir / "all_short_queries_v1.jsonl", queries),
        "allShortQueryDecisions": write_jsonl(benchmark_dir / "all_short_query_decisions_v1.jsonl", all_decisions),
        "leakageAudit": write_json(benchmark_dir / "short_query_extension_leakage_audit_v1.json", leak_rows),
    }
    schema_artifacts = {}
    for filename in ("track_b_short_query_v2.schema.json", "track_b_short_query_decision_v2.schema.json"):
        payload = (schema_dir / filename).read_bytes()
        destination = benchmark_dir / "schemas" / filename
        destination.write_bytes(payload)
        schema_artifacts[filename] = {
            "path": f"schemas/{filename}",
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    artifacts.update(schema_artifacts)

    readiness = {
        "benchmarkId": BENCHMARK_ID,
        "extensionVersion": EXTENSION_VERSION,
        "status": "short_query_construction_complete_not_gold",
        "shortQueryCount": 18,
        "categoryCount": 3,
        "queriesPerCategory": 6,
        "candidateDecisionCount": 432,
        "existingPilotShortQueries": 3,
        "newShortQueries": 15,
        "newAssistantJudgments": 360,
        "humanConfirmedCandidateJudgments": 24,
        "userDelegatedAssistantCandidateJudgments": 408,
        "querySummaries": summaries,
        "formalBenchmarkGate": "NO_GO",
        "blockers": [
            "408 of 432 candidate judgments are assistant-judged rather than human-confirmed.",
            "Candidate pooling has BM25, evidence strata, and deterministic diversity but no Dense or Hybrid route.",
            "Catalog category/title and controlled-value conflicts require human adjudication before qrel freeze.",
        ],
    }
    artifacts["readiness"] = write_json(benchmark_dir / "short_query_extension_readiness_v1.json", readiness)
    report_lines = [
        "# Track B short-query extension v1",
        "",
        "- 18 single-attribute short queries: 6 per exact category.",
        "- 24 candidates per query; 432 candidate decisions total.",
        "- New work is user-delegated assistant judgment, not human gold.",
        "- Grade 3: category plus hard attribute pass; Grade 1: same category with fail/unknown; Grade 0: category mismatch.",
        "- Eligible is evaluated separately and gates final user-visible results.",
        "",
        "Formal qrel freeze remains NO-GO until human confirmation and Dense/Hybrid pooling.",
    ]
    artifacts["report"] = write_text(benchmark_dir / "short_query_extension_report_v1.md", "\n".join(report_lines))
    manifest = {
        "benchmarkId": BENCHMARK_ID,
        "layer": "short_query_extension_v1",
        "baseManifest": artifact_fingerprint(benchmark_dir / "manifest.json"),
        "status": readiness["status"],
        "artifacts": artifacts,
    }
    write_json(benchmark_dir / "short_query_extension_manifest_v1.json", manifest)
    return readiness


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark-dir", type=Path, required=True)
    parser.add_argument("--schema-dir", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    build_extension(args.benchmark_dir, args.schema_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
