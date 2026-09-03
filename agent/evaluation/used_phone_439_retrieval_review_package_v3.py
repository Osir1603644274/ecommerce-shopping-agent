"""Build the isolated attempt005 review package for the 439-phone retrieval study.

This module reuses the immutable attempt004 retrieval observations.  It does
not call Elasticsearch, embeddings, an LLM, or the web runtime.  Its job is to
freeze the corrected intent contract, apply deterministic production-aligned
hard filtering/final reranking, and distribute two mapping-free review packs.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import shutil
from pathlib import Path
from typing import Any, Iterable, Mapping

from agent.evaluation import used_phone_439_retrieval_grid_v1 as grid
from agent.evaluation import used_phone_439_retrieval_review_package_v2 as v2


ROOT = grid.ROOT
SOURCE_RUN = (
    ROOT
    / "agent/evaluation/runs/used_phone_439_retrieval_grid_v1_20260828_attempt004"
)
DEFAULT_RUN_DIR = (
    ROOT
    / "agent/evaluation/runs/used_phone_439_retrieval_grid_v1_20260829_attempt012"
)
SCORER_PATH = ROOT / "agent/evaluation/used_phone_439_retrieval_scorer_v1.py"
REVIEWERS = ("reviewer01", "reviewer02")
RAW_FILES = ("trace.jsonl", "report.json", "receipt.json", "human_review_pool.jsonl")
REUSED_OBSERVATIONS = ("dense_latency_trace_v2.jsonl", "web_runtime_snapshot_v2.json")


def _write_json(path: Path, value: object) -> None:
    path.write_text(grid.canonical(value) + "\n", encoding="utf-8")


def _pin(path: Path, *, rows: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"bytes": path.stat().st_size, "sha256": grid.sha256(path)}
    if rows is not None:
        value["rows"] = rows
    return value


def _copy_inputs(private_dir: Path) -> None:
    private_dir.mkdir(parents=True, exist_ok=False)
    for name in (*RAW_FILES, *REUSED_OBSERVATIONS):
        shutil.copy2(SOURCE_RUN / name, private_dir / name)


def corrected_hard_requirements() -> dict[str, list[dict[str, Any]]]:
    requirements = copy.deepcopy(v2.HARD_REQUIREMENTS)
    requirements["uphqv2-019"] = []
    return requirements


def corrected_soft_preferences() -> dict[str, list[dict[str, Any]]]:
    preferences = copy.deepcopy(v2.SOFT_PREFERENCES)
    preferences["uphqv2-019"] = [
        {
            "key": "price_target_minor",
            "value": 30000,
            "sourceText": "推荐300",
            "matchPolicy": {
                "fullRelativeDeviationLte": 0.20,
                "linearDecayToZeroRelativeDeviation": 0.30,
            },
        },
        {"key": "battery", "value": "long_life", "sourceText": "耗电慢"},
    ]
    return preferences


def taskstate_rows(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hard = corrected_hard_requirements()
    soft = corrected_soft_preferences()
    ids = {str(row["scenarioId"]) for row in queries}
    if ids != set(hard) or ids != set(soft) or len(ids) != 24:
        raise ValueError("corrected intent map must cover exactly 24 queries")
    rows = []
    for query in queries:
        query_id = str(query["scenarioId"])
        rows.append({
            "schemaVersion": "used-phone-439-frozen-taskstate-intent-v3",
            "queryId": query_id,
            "query": query["rawQuery"],
            "split": query["split"],
            "category": "phone",
            "supportedHardRequirements": hard[query_id],
            "explicitSoftPreferences": soft[query_id],
            "unsupportedStructuredEvidence": v2.UNSUPPORTED_STRUCTURED_EVIDENCE.get(query_id, []),
            "policies": {
                "within": "hard_lte",
                "numericRange": "hard_gte_and_lte",
                "aroundOrPlus": "soft_only_no_silent_hard_ceiling",
                "recommendedPrice": "soft_target_full_within_20pct_linear_to_zero_at_30pct",
                "hardUnknown": "not_eligible",
                "titleClaims": "retrieval_relevance_only_not_answer_fact",
                "price": "frozen_synthetic_reference_price_disclosed_not_market_price",
            },
            "status": "FROZEN_EXPERIMENT_INPUT",
        })
    return rows


def review_contract() -> dict[str, Any]:
    return {
        "schemaVersion": "used-phone-439-retrieval-review-contract-v3",
        "status": "FROZEN_BEFORE_CODEX_ASSISTED_REVIEW",
        "authority": "CODEX_ASSISTED_QREL_NOT_FORMAL_HUMAN_GOLD",
        "review": {
            "reviewerCount": 2,
            "independent": True,
            "allowedInputs": ["contract.json", "blind_pack.jsonl", "receipt.json"],
            "mappingForbidden": True,
            "otherReviewerForbidden": True,
            "repositoryContextForbidden": True,
            "outputSchema": {
                "oneRowPerBlindQuery": True,
                "oneReviewPerCandidateToken": True,
                "reviewFields": ["candidateToken", "relevance", "reason", "evidenceBasis"],
                "relevance": [0, 1, 2, 3, "unknown"],
                "reason": "non_empty",
                "evidenceBasis": "non_empty_subset_of_evidencePolicy.allowed",
                "unknown": "not_converted_to_zero_and_excluded_from_formal_qrel",
            },
            "accessAttestationSchema": {
                "exactFields": [
                    "schemaVersion", "reviewer", "role", "authority", "selfAttested",
                    "taskIdentity", "inputsRead", "blindPackSha256", "contractSha256",
                    "distributionReceiptSha256", "mappingSeen", "otherReviewerSeen",
                    "repositoryContextSeen"
                ],
                "taskIdentity": "attempt012-{reviewer}",
                "artifactHashes": "must exactly bind the distributed blind pack, contract, and receipt",
                "mappingSeen": False,
                "otherReviewerSeen": False,
                "repositoryContextSeen": False,
            },
        },
        "labels": {
            "3": "明确满足主题和全部显式硬约束；用途型查询可由标题或属性中的直接强证据支持。",
            "2": "明显有帮助，但用途证据较弱、型号泛化，或只覆盖主要软偏好。",
            "1": "同类商品但与核心意图仅有弱联系，不应排在强相关商品之前。",
            "0": "品类错误、核心意图无关，或 TaskState 判定任一显式硬约束失败/unknown-not-eligible。",
        },
        "evidencePolicy": {
            "allowed": ["title_claim", "attribute_fact", "brand_fact", "synthetic_price", "constraint_conflict"],
            "titleUseCaseClaim": "may_support_grade_2_or_3_retrieval_relevance",
            "measuredPerformanceClaim": "forbidden_without_verified_measurement",
            "syntheticPrice": "may_support_frozen_demo relevance only and must be named simulated reference price",
            "outsidePool": "UNJUDGED_NOT_NEGATIVE",
            "unknown": "ordinary relevance uncertainty is not a qrel; hard-field unknown is separately a TaskState non-eligibility outcome",
        },
        "relevanceMetrics": {
            "unit": "macro_mean_over_eligible_queries",
            "relevantCutoff": "grade>=2",
            "names": ["pooledRecallAt50", "pooledNdcgAt10", "pooledHitAt3"],
            "ndcgGain": "2^grade-1",
            "ndcgDiscount": "1/log2(rank+1)",
            "noRelevantInPool": "NO_RELEVANT_IN_POOL_excluded_from_relevance_macro_and_reported",
            "weakMetrics": "not_computed",
        },
        "minimumEligibleQueries": {"development": 9, "validation": 6, "sealed_test": 3},
        "adjudication": {
            "agreement": "same numeric grade retained automatically; reasons and evidence remain attributable to both reviewers",
            "disagreementOrUnknown": "requires a third mapping-free blind adjudication before scoring",
            "adjudicatorAllowedInputs": ["query", "intent", "candidate evidence", "reviewer labels/reasons/evidenceBasis"],
            "adjudicatorForbiddenInputs": ["queryId", "split", "productId", "mapping", "analyzer", "arm", "rank", "metrics"],
            "evidencePriority": ["constraint_conflict", "attribute_fact", "brand_fact", "synthetic_price", "title_claim"],
            "sealedIsolation": "all reviews and adjudications hash-frozen before any mapping or metric access",
            "hashChain": ["reviewer01 output", "reviewer02 output", "adjudication output", "final qrel", "score report"],
        },
        "selection": {
            "development": "max macro nDCG@10 subject to zero hard violations; ties: Recall@50, Hit@3, lower p95, fixed arm name",
            "validation": {
                "macroNdcgAt10DeltaVsStandardEs": ">=0.02",
                "macroRecallAt50DeltaVsStandardEs": ">=0",
                "macroHitAt3DeltaVsStandardEs": ">=0",
                "hardViolationRate": 0,
                "p95Ms": "<=100",
                "pairedBootstrap": {"samples": 10000, "seed": 20260828, "percentileCi": 0.95, "ndcgDeltaLower": ">0"},
            },
            "sealed": {
                "mode": "one_shot_descriptive_no_retuning",
                "rejectIf": [
                    "any_hard_violation",
                    "macro_ndcg_or_recall_delta_vs_standard_es_below_-0.05",
                    "hit_at_3_hit_query_count_loss_at_least_2",
                    "p95_ms_above_100",
                ],
            },
            "productionDefault": "switch_only_if_all_gates_accept; otherwise_hold",
        },
    }


def _normal(value: object) -> str:
    return re.sub(r"[\s+_\-/]+", "", str(value or "").casefold())


BRAND_ALIASES = {
    "apple": ("apple", "苹果"),
    "huawei": ("huawei", "华为"),
    "xiaomi": ("xiaomi", "小米", "mi"),
    "redmi": ("redmi", "红米", "hongmi"),
    "vivo": ("vivo",),
    "iqoo": ("iqoo", "i酷"),
}


SOFT_TERMS = {
    ("form_factor", "small_screen"): ("小屏", "迷你", "mini"),
    ("value", "high"): ("性价比", "低价", "便宜", "特价"),
    ("use_case", "cashier"): ("收银", "商用"),
    ("use_case", "camera"): ("拍照", "相机", "像素"),
    ("use_case", "gaming"): ("游戏", "电竞", "竞技", "和平精英"),
    ("audience", "college_student"): ("大学生", "学生"),
    ("audience", "student"): ("学生", "学生党"),
    ("display", "high_refresh"): ("高刷", "高帧", "120hz", "144hz"),
    ("use_case", "backup"): ("备用", "备用机"),
    ("battery", "large_capacity"): ("大电池", "大容量电池", "5000mah", "6000mah"),
    ("battery", "long_life"): ("续航", "长待机", "耗电慢", "大电池", "5000mah", "6000mah"),
    ("audio", "loud"): ("声音大", "大音量", "扬声器"),
    ("audio", "good_quality"): ("音质", "音乐"),
    ("camera", "good_pixels"): ("拍照", "像素", "相机"),
    ("price", "cheap"): ("便宜", "低价", "特价", "清仓"),
    ("memory", "large"): ("大内存", "12g", "16g", "512g"),
    ("reviews", "many_positive"): ("好评",),
    ("chipset_text", "snapdragon_8_gen_1"): ("骁龙8gen1", "骁龙8 gen1"),
}


def _known_attribute(product: Mapping[str, Any], key: str) -> object | None:
    attributes = product.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    value = attributes.get(key)
    if not isinstance(value, Mapping) or value.get("status") != "known":
        return None
    return value.get("value")


def _brand_matches(actual: object, expected: str) -> bool:
    tokens = {
        _normal(token)
        for token in re.split(r"[/|,，\s]+", str(actual or "").casefold())
        if _normal(token)
    }
    aliases = {_normal(alias) for alias in BRAND_ALIASES.get(expected, (expected,))}
    return bool(tokens & aliases)


def hard_eligible(product: Mapping[str, Any], price_minor: int, requirements: Iterable[Mapping[str, Any]]) -> bool:
    for req in requirements:
        key, operator, expected = str(req["key"]), str(req["operator"]), req["value"]
        if key == "price_minor":
            actual: object | None = price_minor
        elif key == "brand":
            actual = product.get("brand")
        else:
            actual = _known_attribute(product, key)
        if actual is None:
            return False
        if key == "brand":
            passed = operator == "eq" and _brand_matches(actual, str(expected))
        elif operator == "eq":
            passed = _normal(actual) == _normal(expected)
        elif operator == "lte":
            passed = float(actual) <= float(expected)
        elif operator == "gte":
            passed = float(actual) >= float(expected)
        else:
            raise ValueError(f"unsupported hard operator: {operator}")
        if not passed:
            return False
    return True


def _price_target_score(actual: int, target: int, policy: Mapping[str, Any] | None) -> float:
    relative = abs(actual - target) / target
    full = float((policy or {}).get("fullRelativeDeviationLte", 0.20))
    zero = float((policy or {}).get("linearDecayToZeroRelativeDeviation", 0.30))
    if relative <= full:
        return 1.0
    if relative >= zero:
        return 0.0
    return (zero - relative) / (zero - full)


def soft_match(product: Mapping[str, Any], price_minor: int, preferences: list[Mapping[str, Any]]) -> float:
    if not preferences:
        return 0.0
    text = _normal(f"{product.get('title', '')} {product.get('brand', '')} {product.get('attributeText', '')}")
    scores = []
    for pref in preferences:
        key, value = str(pref["key"]), pref["value"]
        if key == "price_target_minor":
            scores.append(_price_target_score(price_minor, int(value), pref.get("matchPolicy")))
            continue
        if key in {"model_text", "memory_text", "storage_text", "price_phrase"}:
            terms = (str(value), str(pref.get("sourceText", "")))
        elif key == "negative_use_case":
            terms = SOFT_TERMS.get(("use_case", str(value)), (str(value),))
            scores.append(0.0 if any(_normal(term) in text for term in terms) else 1.0)
            continue
        else:
            terms = SOFT_TERMS.get((key, str(value)), (str(pref.get("sourceText", "")), str(value)))
        scores.append(1.0 if any(_normal(term) and _normal(term) in text for term in terms) else 0.0)
    return sum(scores) / len(scores)


def derived_rankings(
    traces: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    catalog: Mapping[int, Mapping[str, Any]],
    prices: Mapping[int, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    intent_by_id = {row["queryId"]: row for row in intents}
    rows = []
    for trace in traces:
        if int(trace["repeat"]) != 1:
            continue
        intent = intent_by_id[trace["queryId"]]
        hard = intent["supportedHardRequirements"]
        soft = intent["explicitSoftPreferences"]
        raw = list(zip(trace["rankedIds"], trace["scores"], strict=True))
        maximum = max(float(score) for _product_id, score in raw)
        scored = []
        for product_id, rrf_score in raw:
            product_id = int(product_id)
            product = catalog[product_id]
            price = int(prices[product_id]["referencePriceMinor"])
            if not hard_eligible(product, price, hard):
                continue
            normalized_rrf = float(rrf_score) / maximum
            soft_value = soft_match(product, price, soft)
            completeness = grid.non_hard_structured_completeness(
                product, hard_keys=(req["key"] for req in hard)
            )
            final, active = grid.final_rerank_score(
                normalized_rrf=normalized_rrf,
                explicit_soft_match=soft_value,
                completeness=completeness,
                soft_preference_count=len(soft),
            )
            scored.append({
                "productId": product_id,
                "finalScore": round(final, 12),
                "normalizedRrf": round(normalized_rrf, 12),
                "explicitSoftMatch": round(soft_value, 12),
                "nonHardStructuredCompleteness": round(completeness, 12),
                "activeWeights": active,
            })
        scored.sort(key=lambda item: (-item["finalScore"], item["productId"]))
        rows.append({
            "schemaVersion": "used-phone-439-production-aligned-ranking-v3",
            "queryId": trace["queryId"],
            "split": trace["split"],
            "analyzer": trace["analyzer"],
            "arm": trace["arm"],
            "hardInputCount": 50,
            "eligibleCount": len(scored),
            "ranked": scored,
        })
    return rows


def _all_keys(value: object) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


def validate_mapping(
    mapping: Mapping[str, Any],
    packs: Mapping[str, list[dict[str, Any]]],
    evaluator: list[dict[str, Any]],
    intents: list[dict[str, Any]],
    authoritative_splits: Mapping[str, str],
) -> None:
    evaluator_by_query = {row["queryId"]: row for row in evaluator}
    intent_by_query = {row["queryId"]: row for row in intents}
    if len(evaluator) != 24 or len(evaluator_by_query) != 24 or len(intents) != 24 or len(intent_by_query) != 24:
        raise ValueError("evaluator query identity mismatch")
    if set(authoritative_splits) != set(evaluator_by_query):
        raise ValueError("authoritative split coverage mismatch")
    if set(mapping) != {"schemaVersion", "visibility", "reviewers", "packHashes"}:
        raise ValueError("mapping top-level schema mismatch")
    if mapping["schemaVersion"] != "used-phone-439-retrieval-blind-mapping-v3" or mapping["visibility"] != "PRIVATE_EVALUATOR_ONLY":
        raise ValueError("mapping identity mismatch")
    if set(mapping["reviewers"]) != set(REVIEWERS) or set(mapping["packHashes"]) != set(REVIEWERS):
        raise ValueError("mapping reviewer set mismatch")
    for row in evaluator:
        candidates = row.get("candidates") or []
        product_ids = [int(candidate["productId"]) for candidate in candidates]
        if len(product_ids) != len(set(product_ids)):
            raise ValueError("evaluator contains duplicate candidate")
    global_tokens: set[str] = set()
    for reviewer in REVIEWERS:
        reviewer_map = mapping.get("reviewers", {}).get(reviewer)
        if not isinstance(reviewer_map, Mapping):
            raise ValueError("reviewer mapping missing")
        pack_rows = packs[reviewer]
        pack_by_blind = {row["blindQueryId"]: row for row in pack_rows}
        if len(pack_rows) != 24 or len(pack_by_blind) != 24 or set(pack_by_blind) != set(reviewer_map):
            raise ValueError("blind query mapping is not an exact bijection")
        mapped_queries = [str(item["queryId"]) for item in reviewer_map.values()]
        if len(set(mapped_queries)) != 24 or set(mapped_queries) != set(evaluator_by_query):
            raise ValueError("real query mapping is not an exact bijection")
        reviewer_tokens: set[str] = set()
        for blind_id, item in reviewer_map.items():
            if set(item) != {"queryId", "split", "candidates"}:
                raise ValueError("mapping query item schema mismatch")
            query_id = str(item["queryId"])
            source_intent = intent_by_query[query_id]
            expected_intent = {
                "supportedHardRequirements": source_intent["supportedHardRequirements"],
                "explicitSoftPreferences": source_intent["explicitSoftPreferences"],
                "unsupportedStructuredEvidence": source_intent["unsupportedStructuredEvidence"],
                "policies": source_intent["policies"],
            }
            public_query = pack_by_blind[blind_id]
            if (
                set(public_query) != {"schemaVersion", "blindQueryId", "query", "intent", "candidates"}
                or
                public_query.get("schemaVersion") != "used-phone-439-retrieval-blind-item-v3"
                or item["split"] != authoritative_splits[query_id]
                or evaluator_by_query[query_id]["split"] != authoritative_splits[query_id]
                or source_intent["split"] != authoritative_splits[query_id]
                or public_query["query"] != evaluator_by_query[query_id]["query"]
                or public_query["intent"] != expected_intent
            ):
                raise ValueError("blind query or intent differs from frozen source")
            candidate_map = item.get("candidates")
            if not isinstance(candidate_map, Mapping):
                raise ValueError("candidate mapping missing")
            public_tokens = [row["candidateToken"] for row in pack_by_blind[blind_id]["candidates"]]
            if len(public_tokens) != len(set(public_tokens)) or set(public_tokens) != set(candidate_map):
                raise ValueError("candidate token mapping is not an exact bijection")
            product_ids = [int(value) for value in candidate_map.values()]
            expected_candidates = {
                int(row["productId"]): row for row in evaluator_by_query[query_id]["candidates"]
            }
            expected_ids = set(expected_candidates)
            if len(product_ids) != len(set(product_ids)) or set(product_ids) != expected_ids:
                raise ValueError("candidate product mapping is not an exact bijection")
            public_by_token = {
                row["candidateToken"]: row for row in pack_by_blind[blind_id]["candidates"]
            }
            for token, product_id in candidate_map.items():
                source = expected_candidates[int(product_id)]
                public = public_by_token[token]
                expected_public = {
                    "candidateToken": token,
                    "title": source["title"],
                    "brand": source["brand"],
                    "syntheticReferencePriceMinor": source["syntheticReferencePriceMinor"],
                    "priceStatus": source["priceStatus"],
                    "priceDisclosureZh": source["priceDisclosureZh"],
                    "controlledAttributes": source["controlledAttributes"],
                    "rawAttributeText": source["attributeText"],
                    "review": {"relevance": None, "reason": None, "evidenceBasis": None},
                }
                if public != expected_public:
                    raise ValueError("blind candidate fields differ from evaluator source")
            reviewer_tokens.update(public_tokens)
        if reviewer_tokens & global_tokens:
            raise ValueError("candidate token reused across reviewers")
        global_tokens.update(reviewer_tokens)


def validate(run_dir: Path = DEFAULT_RUN_DIR) -> None:
    private = run_dir / "private_evaluator"
    traces, raw = v2._strict_raw_validation(private)
    receipt = json.loads((run_dir / "receipt_v3.json").read_text(encoding="utf-8"))
    if set(receipt) != {"schemaVersion", "status", "sourceRun", "artifacts", "sources", "dataPins"}:
        raise ValueError("total receipt schema mismatch")
    if receipt["schemaVersion"] != "used-phone-439-attempt012-total-receipt-v3":
        raise ValueError("total receipt identity mismatch")
    if receipt["status"] != "HOLD_PENDING_TWO_INDEPENDENT_CODEX_ASSISTED_REVIEWS" or receipt["sourceRun"] != SOURCE_RUN.relative_to(ROOT).as_posix():
        raise ValueError("total receipt status or source run mismatch")
    expected_data_paths = (grid.CATALOG_PATH, grid.DOCUMENTS_PATH, grid.PRICE_PATH, grid.QUERY_PATH)
    expected_data_pins = {
        path.relative_to(ROOT).as_posix(): {"bytes": path.stat().st_size, "sha256": grid.sha256(path)}
        for path in expected_data_paths
    }
    if receipt["dataPins"] != expected_data_pins:
        raise ValueError("data pin mismatch")
    actual_files = {
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file() and path.name != "receipt_v3.json"
    }
    if set(receipt["artifacts"]) != actual_files:
        raise ValueError("artifact manifest is not an exact disk-file set")
    expected_sources = {
        Path(grid.__file__).resolve().relative_to(ROOT).as_posix(),
        Path(v2.__file__).resolve().relative_to(ROOT).as_posix(),
        Path(__file__).resolve().relative_to(ROOT).as_posix(),
        SCORER_PATH.relative_to(ROOT).as_posix(),
    }
    if set(receipt["sources"]) != expected_sources:
        raise ValueError("source manifest mismatch")
    for relative, pin in receipt["artifacts"].items():
        path = run_dir / relative
        if not path.is_file() or _pin(path, rows=pin.get("rows")) != pin:
            raise ValueError(f"v3 artifact pin mismatch: {relative}")
    for relative, expected in receipt["sources"].items():
        if grid.sha256(ROOT / relative) != expected:
            raise ValueError(f"v3 source pin mismatch: {relative}")
    intents = grid.read_jsonl(private / "frozen_taskstate_intents_v3.jsonl")
    query_ids = {str(row["scenarioId"]) for row in raw["queries"]}
    if len(intents) != 24 or {row["queryId"] for row in intents} != query_ids:
        raise ValueError("intent coverage mismatch")
    intent019 = next(row for row in intents if row["queryId"] == "uphqv2-019")
    price_preferences = [
        pref for pref in intent019["explicitSoftPreferences"]
        if pref.get("key") == "price_target_minor"
    ]
    if intent019["supportedHardRequirements"] or len(price_preferences) != 1 or not (
        price_preferences[0].get("value") == 30000
        and price_preferences[0].get("sourceText") == "推荐300"
    ) or not any(
        pref.get("key") == "battery" and pref.get("value") == "long_life"
        for pref in intent019["explicitSoftPreferences"]
    ):
        raise ValueError("recommended 300 intent was not corrected to soft target")
    evaluator = grid.read_jsonl(private / "evaluator_pool_v3.jsonl")
    packs = {
        reviewer: grid.read_jsonl(run_dir / f"distribution/{reviewer}/blind_pack.jsonl")
        for reviewer in REVIEWERS
    }
    forbidden = {"queryId", "split", "productId", "analyzer", "arm", "rank", "weights", "poolWitnesses"}
    for reviewer, rows in packs.items():
        reviewer_dir = run_dir / "distribution" / reviewer
        if {path.name for path in reviewer_dir.iterdir()} != {"contract.json", "blind_pack.jsonl", "receipt.json"}:
            raise ValueError(f"unexpected initial distribution file: {reviewer}")
        if forbidden & set(_all_keys(rows)):
            raise ValueError(f"blind leakage: {reviewer}")
        public_contract = json.loads((reviewer_dir / "contract.json").read_text(encoding="utf-8"))
        if public_contract != review_contract():
            raise ValueError("distributed review contract drift")
        pack_receipt = json.loads((reviewer_dir / "receipt.json").read_text(encoding="utf-8"))
        if pack_receipt != {
            "schemaVersion": "used-phone-439-reviewer-distribution-receipt-v3",
            "reviewer": reviewer,
            "visibility": "REVIEWER_ONLY_MAPPING_FREE",
            "artifacts": {
            "blind_pack.jsonl": _pin(reviewer_dir / "blind_pack.jsonl", rows=24),
            "contract.json": _pin(reviewer_dir / "contract.json"),
            },
        }:
            raise ValueError("distribution receipt mismatch")
    mapping = json.loads((private / "sealed_mapping_v3.json").read_text(encoding="utf-8"))
    authoritative_splits = {
        str(row["scenarioId"]): str(row["split"]) for row in raw["queries"]
    }
    validate_mapping(mapping, packs, evaluator, intents, authoritative_splits)
    for reviewer in REVIEWERS:
        if mapping["packHashes"][reviewer] != grid.sha256(run_dir / f"distribution/{reviewer}/blind_pack.jsonl"):
            raise ValueError("mapping pack hash mismatch")
    rankings = grid.read_jsonl(private / "production_aligned_rankings_v3.jsonl")
    expected_cells = {
        (str(row["scenarioId"]), analyzer, arm)
        for row in raw["queries"] for analyzer in grid.INDEX_NAMES for arm in grid.WEIGHT_ARMS
    }
    actual_cells = [(row["queryId"], row["analyzer"], row["arm"]) for row in rankings]
    if len(actual_cells) != len(set(actual_cells)) or set(actual_cells) != expected_cells:
        raise ValueError("production-aligned ranking grid mismatch")
    if any(row["eligibleCount"] != len(row["ranked"]) or row["eligibleCount"] > 50 for row in rankings):
        raise ValueError("production-aligned ranking shape mismatch")
    prices, catalog = v2._price_and_catalog()
    intent_by_query = {row["queryId"]: row for row in intents}
    for row in rankings:
        requirements = intent_by_query[row["queryId"]]["supportedHardRequirements"]
        for item in row["ranked"]:
            product_id = int(item["productId"])
            if not hard_eligible(
                catalog[product_id], int(prices[product_id]["referencePriceMinor"]), requirements
            ):
                raise ValueError("production-aligned ranking contains hard violation")
    searchable_catalog = copy.deepcopy(catalog)
    for document in raw["documents"]:
        product = searchable_catalog[int(document["id"])]
        product["title"] = document["title"]
        product["attributeText"] = document["attributeText"]
        product["brand"] = document["brand"]
    expected_rankings = derived_rankings(traces, intents, searchable_catalog, prices)
    if rankings != expected_rankings:
        raise ValueError("production-aligned rankings do not exactly replay")
    report = json.loads((private / "report_v3.json").read_text(encoding="utf-8"))
    if report["decision"]["productionDefaultSwitch"] != "HOLD":
        raise ValueError("package builder changed default decision")
    if report["callLayering"] != {
        "rawGridObserved": {"embeddingCatalogBuilds": 1, "embeddingQueryCalls": 24},
        "latencyReplayObserved": {"embeddingCatalogBuilds": 3, "embeddingQueryCalls": 72},
        "packageBuildNewCalls": {"embeddingCatalogBuilds": 0, "embeddingQueryCalls": 0, "llmDecisionCalls": 0, "taskManagerCalls": 0, "finalAnswerCalls": 0},
        "tokens": {"prompt": 0, "completion": 0},
    }:
        raise ValueError("call layering mismatch")
    if len(traces) != 1008:
        raise ValueError("raw trace count drift")


def build(run_dir: Path = DEFAULT_RUN_DIR) -> Path:
    if run_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing run: {run_dir}")
    private = run_dir / "private_evaluator"
    distribution = run_dir / "distribution"
    run_dir.mkdir(parents=True)
    _copy_inputs(private)
    distribution.mkdir()
    traces, raw = v2._strict_raw_validation(private)
    prices, catalog = v2._price_and_catalog()
    raw_pool = grid.read_jsonl(private / "human_review_pool.jsonl")
    evaluator = v2._enrich_pool(raw_pool, prices, catalog)
    intents = taskstate_rows(raw["queries"])
    packs, mapping = v2._blind_packs(evaluator, intents)
    mapping["schemaVersion"] = "used-phone-439-retrieval-blind-mapping-v3"
    for rows in packs.values():
        for row in rows:
            row["schemaVersion"] = "used-phone-439-retrieval-blind-item-v3"
            for candidate in row["candidates"]:
                candidate["review"]["evidenceBasis"] = None
    searchable_catalog = copy.deepcopy(catalog)
    for document in raw["documents"]:
        product = searchable_catalog[int(document["id"])]
        product["title"] = document["title"]
        product["attributeText"] = document["attributeText"]
        product["brand"] = document["brand"]
    rankings = derived_rankings(traces, intents, searchable_catalog, prices)
    grid.write_jsonl(private / "frozen_taskstate_intents_v3.jsonl", intents)
    grid.write_jsonl(private / "evaluator_pool_v3.jsonl", evaluator)
    grid.write_jsonl(private / "production_aligned_rankings_v3.jsonl", rankings)
    _write_json(private / "review_contract_v3.json", review_contract())
    for reviewer in REVIEWERS:
        reviewer_dir = distribution / reviewer
        reviewer_dir.mkdir()
        _write_json(reviewer_dir / "contract.json", review_contract())
        grid.write_jsonl(reviewer_dir / "blind_pack.jsonl", packs[reviewer])
        pack_receipt = {
            "schemaVersion": "used-phone-439-reviewer-distribution-receipt-v3",
            "reviewer": reviewer,
            "visibility": "REVIEWER_ONLY_MAPPING_FREE",
            "artifacts": {
                "blind_pack.jsonl": _pin(reviewer_dir / "blind_pack.jsonl", rows=24),
                "contract.json": _pin(reviewer_dir / "contract.json"),
            },
        }
        _write_json(reviewer_dir / "receipt.json", pack_receipt)
    mapping["packHashes"] = {
        reviewer: grid.sha256(distribution / reviewer / "blind_pack.jsonl")
        for reviewer in REVIEWERS
    }
    _write_json(private / "sealed_mapping_v3.json", mapping)
    report = {
        "schemaVersion": "used-phone-439-retrieval-review-package-report-v3",
        "status": "HOLD_PENDING_TWO_INDEPENDENT_CODEX_ASSISTED_REVIEWS",
        "decision": {
            "determinismGate": "ACCEPT",
            "packageStructureGate": "ACCEPT",
            "reviewerAccessIsolationGate": "PENDING_SIGNED_ATTESTATIONS",
            "mappingBijectionGate": "ACCEPT",
            "relevanceWinner": "NOT_EVALUATED",
            "productionDefaultSwitch": "HOLD",
        },
        "queryCount": 24,
        "candidatePairsPerReviewer": sum(row["candidateCount"] for row in evaluator),
        "productionAlignedRankingCells": len(rankings),
        "reviewAuthority": "CODEX_ASSISTED_QREL_NOT_FORMAL_HUMAN_GOLD",
        "callLayering": {
            "rawGridObserved": {"embeddingCatalogBuilds": 1, "embeddingQueryCalls": 24},
            "latencyReplayObserved": {"embeddingCatalogBuilds": 3, "embeddingQueryCalls": 72},
            "packageBuildNewCalls": {"embeddingCatalogBuilds": 0, "embeddingQueryCalls": 0, "llmDecisionCalls": 0, "taskManagerCalls": 0, "finalAnswerCalls": 0},
            "tokens": {"prompt": 0, "completion": 0},
        },
        "latencyAuthority": "reused_attempt004_observations_no_new_runtime_probe",
        "limitations": [
            "Codex-assisted qrels are experiment labels, not formal human gold.",
            "Pooled Recall@50 is not whole-directory recall.",
            "Twenty-four queries cannot establish general retrieval superiority.",
        ],
    }
    _write_json(private / "report_v3.json", report)
    artifacts: dict[str, Any] = {}
    row_counts = {
        "private_evaluator/trace.jsonl": len(traces),
        "private_evaluator/human_review_pool.jsonl": len(raw_pool),
        "private_evaluator/dense_latency_trace_v2.jsonl": 72,
        "private_evaluator/frozen_taskstate_intents_v3.jsonl": 24,
        "private_evaluator/evaluator_pool_v3.jsonl": 24,
        "private_evaluator/production_aligned_rankings_v3.jsonl": 336,
        "distribution/reviewer01/blind_pack.jsonl": 24,
        "distribution/reviewer02/blind_pack.jsonl": 24,
    }
    for path in sorted(run_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(run_dir).as_posix()
            artifacts[relative] = _pin(path, rows=row_counts.get(relative))
    sources = {
        Path(grid.__file__).resolve().relative_to(ROOT).as_posix(): grid.sha256(Path(grid.__file__).resolve()),
        Path(v2.__file__).resolve().relative_to(ROOT).as_posix(): grid.sha256(Path(v2.__file__).resolve()),
        Path(__file__).resolve().relative_to(ROOT).as_posix(): grid.sha256(Path(__file__).resolve()),
        SCORER_PATH.relative_to(ROOT).as_posix(): grid.sha256(SCORER_PATH),
    }
    _write_json(run_dir / "receipt_v3.json", {
        "schemaVersion": "used-phone-439-attempt012-total-receipt-v3",
        "status": report["status"],
        "sourceRun": SOURCE_RUN.relative_to(ROOT).as_posix(),
        "artifacts": artifacts,
        "sources": sources,
        "dataPins": {
            path.relative_to(ROOT).as_posix(): {"bytes": path.stat().st_size, "sha256": grid.sha256(path)}
            for path in (grid.CATALOG_PATH, grid.DOCUMENTS_PATH, grid.PRICE_PATH, grid.QUERY_PATH)
        },
    })
    validate(run_dir)
    return run_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    validate(args.run_dir) if args.validate_only else build(args.run_dir)
    print(grid.canonical({"status": "ACCEPT", "runDir": str(args.run_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
