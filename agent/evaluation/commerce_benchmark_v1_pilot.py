"""Oracle-first 96-record Commerce Benchmark V1 pilot design generator."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence, TypedDict

from .commerce_world_v1 import CATEGORIES, ENVIRONMENTS, TARGET_COUNTS, CommerceWorldError, load_world_manifest, load_world_products, sha256_file
from .scenario_lab_v1 import SCHEMA_VERSIONS, audit_public_input_leaks, audit_public_private_value_leaks, canonical_bytes, evaluate_constraint_ast, make_runner_receipt, validate_record, write_jsonl

INTENT_FAMILIES = ("product_finder", "knowledge_to_product", "multi_product_merchant", "coupon_budget", "dynamic_replanning")
STRATA = ("S0_SIMPLE", "S1_FIXED_MULTISTEP", "S2_OBSERVATION_DEPENDENT")


def _ast_for(category: str) -> Mapping[str, Any]:
    return {"kind": "atom", "atom": {"field": "category", "operator": "EQ", "value": category, "unknownPolicy": "fail"}}


def _public_text(intent: str, stratum: str, category: str, index: int) -> str:
    if intent == "product_finder":
        return f"我想找适合日常使用的{category}，请推荐合适的选择。"
    if intent == "knowledge_to_product":
        return f"先告诉我选{category}时应该关注什么，再按这些条件帮我找商品。"
    if intent == "multi_product_merchant":
        return f"我想从同一家店购买两件{category}相关商品，帮我一起筛选。"
    if intent == "coupon_budget":
        return f"预算有限，帮我在{category}中组合购买，并说明优惠后的价格。"
    return f"帮我找{category}；如果库存、价格或证据发生变化，请重新核对后再回答。"


def _injections(intent: str, stratum: str) -> list[Mapping[str, Any]]:
    if stratum != "S2_OBSERVATION_DEPENDENT":
        return []
    point = {"coupon_budget": "stale_price", "dynamic_replanning": "stock_change", "knowledge_to_product": "evidence_conflict"}.get(intent, "empty_result")
    invariant = {"stale_price": "recalculate_or_abstain", "stock_change": "do_not_sell_out", "evidence_conflict": "cite_one_revision", "empty_result": "requery_or_abstain"}[point]
    return [{"injectionId": "inject-01", "point": point, "trigger": "after_first_observation", "mutation": {"kind": point, "value": "synthetic_pending"}, "expectedInvariant": invariant, "repeatCount": 1}]


def enumerate_acceptable_products(products: Sequence[Mapping[str, Any]], category: str) -> tuple[str, ...]:
    """Enumerate the full acceptable universe; never pick a target item first."""
    return tuple(sorted(str(product["productId"]) for product in products if str(product.get("category")) == category))


def enumerate_acceptable_bundles(products: Sequence[Mapping[str, Any]], category: str, *, bundle_size: int = 2, max_results: int = 256) -> tuple[tuple[str, ...], ...]:
    """Enumerate a bounded prefix of bundles, never a quadratic oracle list.

    Phase 1 only needs a deterministic design-time witness.  A formal world
    may contain thousands of products per category, so materializing every
    same-merchant combination is both unnecessary and an accidental denial of
    service.  The full acceptable universe remains a lazy constraint for the
    Phase 2 runner.
    """
    if bundle_size < 2 or max_results < 1:
        return ()
    by_merchant: dict[str, list[str]] = {}
    for product in products:
        if str(product.get("category")) == category:
            by_merchant.setdefault(str(product.get("merchantId")), []).append(str(product["productId"]))
    bundles: list[tuple[str, ...]] = []
    for merchant in sorted(by_merchant):
        for bundle in combinations(sorted(by_merchant[merchant]), bundle_size):
            bundles.append(tuple(bundle))
            if len(bundles) >= max_results:
                return tuple(sorted(bundles))
    return tuple(sorted(bundles))


def enumerate_acceptable_carts(products: Sequence[Mapping[str, Any]], category: str, *, max_total: float, max_results: int = 256) -> tuple[tuple[str, ...], ...]:
    """Return a bounded deterministic prefix of budget-valid carts."""
    if max_results < 1:
        return ()
    candidates = [product for product in products if str(product.get("category")) == category]
    carts: list[tuple[str, ...]] = []
    for size in (1, 2):
        for items in combinations(sorted(candidates, key=lambda row: str(row["productId"])), size):
            if sum(float(item.get("price", 0)) for item in items) <= max_total:
                carts.append(tuple(str(item["productId"]) for item in items))
                if len(carts) >= max_results:
                    return tuple(sorted(carts))
    return tuple(sorted(carts))


def generate_pilot_designs(manifest: Mapping[str, Any], products: Sequence[Mapping[str, Any]] = (), coupons: Sequence[Mapping[str, Any]] = ()) -> Mapping[str, list[Mapping[str, Any]]]:
    """Generate 60 dev + 30 validation + 6 red-team records deterministically.

    The private oracle is constructed before the public text.  Phase 1 is only
    a contract/design skeleton: even a synthetic world with the target counts
    remains ``PENDING_SCENARIO_AUTHORING`` until Phase 2 derives constraints
    from real facts, policy documents, offers and fault observations.
    """
    actuals = manifest.get("actuals", {})
    # ``ready`` is intentionally not used to promote a record in Phase 1.
    # Keep the shape checks as diagnostic information only; world counts do
    # not prove that a scenario has a non-cosmetic oracle.
    world_shape_seen = (
        manifest.get("status") == "READY"
        and manifest.get("manifestAudit") == "verified"
        and manifest.get("licenseGate") in {"verified", "synthetic_fixture"}
        and dict(actuals) == TARGET_COUNTS
        and len(products) == TARGET_COUNTS["products"]
        and (not coupons or len(coupons) == TARGET_COUNTS["coupons"])
    )
    product_by_category: dict[str, list[Mapping[str, Any]]] = {category: [] for category in CATEGORIES}
    for product in products:
        if str(product.get("category")) in product_by_category:
            product_by_category[str(product["category"])].append(product)
    inputs: list[Mapping[str, Any]] = []
    oracles: list[Mapping[str, Any]] = []
    faults: list[Mapping[str, Any]] = []
    predictions: list[Mapping[str, Any]] = []

    def add_record(scenario_id: str, split: str, intent: str, stratum: str, index: int, red: bool = False) -> None:
        category = CATEGORIES[index % len(CATEGORIES)]
        acceptable = []
        solution_type = "cart" if intent == "coupon_budget" else ("bundle" if intent == "multi_product_merchant" else "single_product")
        bundles = ()
        max_total = 1000.0
        carts = ()
        ready_for_record = False
        point = _injections(intent, stratum)[0]["point"] if _injections(intent, stratum) else None
        success_conditions = {"terminalClasses": ["SUCCESS"], "solutionType": solution_type, "constraintAst": _ast_for(category), "evidence": {"minCitations": 1, "forbidUnsupportedClaims": True}, "requiredObservationPoints": [point] if point else []}
        if ready_for_record and solution_type == "single_product":
            if not acceptable:
                ready_for_record = False
            else:
                success_conditions["acceptableProductIds"] = acceptable
        if ready_for_record and solution_type == "bundle":
            if not bundles:
                ready_for_record = False
            else:
                success_conditions["acceptableBundles"] = [list(bundle) for bundle in bundles]
                success_conditions["acceptableProductIds"] = sorted({product_id for bundle in bundles for product_id in bundle})
        if solution_type == "cart":
            if ready_for_record and not carts:
                ready_for_record = False
            success_conditions["cartRules"] = {"minItems": 1, "maxItems": 2, "currency": "CNY", "maxTotal": max_total}
        if intent == "knowledge_to_product":
            success_conditions["knowledgeDependency"] = {"knowledgeId": f"knowledge-{category}-01", "field": "category"}
        if intent == "dynamic_replanning":
            success_conditions["environmentSequence"] = ["E0", "E2"]
        record_status = "PENDING_SCENARIO_AUTHORING"
        reason_prefix = "contract red-team; " if red else ""
        oracle = {"scenarioId": scenario_id, "schemaVersion": SCHEMA_VERSIONS["oracle"], "worldId": manifest["worldId"], "catalogRevision": manifest["catalogRevision"], "environmentRevision": "E0", "split": split, "intentFamily": intent, "complexityStratum": stratum, "oracleStatus": record_status, "designReason": reason_prefix + "PENDING_SCENARIO_AUTHORING: Phase 1 skeleton; derive facts/docs/offers/fault oracle in Phase 2" + ("; target world shape observed" if world_shape_seen else "; world shape not materialized"), "successConditions": success_conditions}
        # The oracle and private fault are frozen before this public text is materialized.
        public = {"scenarioId": scenario_id, "schemaVersion": SCHEMA_VERSIONS["input"], "worldId": manifest["worldId"], "catalogRevision": manifest["catalogRevision"], "environmentRevision": "E0", "language": "zh-CN", "turns": [{"turnId": "turn-01", "role": "user", "text": _public_text(intent, stratum, category, index)}], "publicEnvironmentRefs": ["catalog", "offers"]}
        fault = {"scenarioId": scenario_id, "schemaVersion": SCHEMA_VERSIONS["fault"], "worldId": manifest["worldId"], "catalogRevision": manifest["catalogRevision"], "environmentRevision": "E0", "faultStatus": "PENDING_SCENARIO_AUTHORING", "injections": _injections(intent, stratum)}
        run_id = f"not-run-{scenario_id}"
        prediction = {"predictionId": f"not-run-{scenario_id}", "runId": run_id, "scenarioId": scenario_id, "schemaVersion": SCHEMA_VERSIONS["prediction"], "worldId": manifest["worldId"], "catalogRevision": manifest["catalogRevision"], "environmentRevision": "E0", "runStatus": "NOT_RUN", "terminalClass": "ABSTAIN_OR_EXPLAIN", "selectedProductIds": [], "answerText": "未运行：仅设计占位，不进入评分。", "claims": [], "claimExtraction": {"provenance": "not-run", "version": "v1", "claimsComplete": True}, "evidenceCitations": [], "toolTrace": [], "resourceUsage": {"modelCalls": 0, "toolCalls": 0, "latencyMs": 0, "inputTokens": 0, "outputTokens": 0}}
        validate_record(oracle, "oracle"); validate_record(public, "input"); validate_record(fault, "fault"); validate_record(prediction, "prediction")
        oracles.append(oracle); inputs.append(public); faults.append(fault); predictions.append(prediction)

    for intent in INTENT_FAMILIES:
        for stratum in STRATA:
            for index in range(4):
                add_record(f"ACB-V1-DEV-{intent}-{stratum}-{index + 1:02d}", "development", intent, stratum, index)
    for intent in INTENT_FAMILIES:
        for stratum in STRATA:
            for index in range(2):
                add_record(f"ACB-V1-VAL-{intent}-{stratum}-{index + 1:02d}", "validation", intent, stratum, index + 4)
    for index in range(6):
        intent = INTENT_FAMILIES[index % len(INTENT_FAMILIES)]
        stratum = STRATA[index % len(STRATA)]
        add_record(f"ACB-V1-RED-contract-{index + 1:02d}", "contract_red_team", intent, stratum, index, red=True)
    return {"input": inputs, "oracle": oracles, "fault": faults, "prediction": predictions}


def write_pilot_designs(manifest_path: Path | str, output_dir: Path | str, products_path: Path | str | None = None, coupons_path: Path | str | None = None) -> Mapping[str, Path]:
    manifest_path = Path(manifest_path)
    manifest = load_world_manifest(manifest_path)
    products = load_world_products(products_path) if products_path else []
    coupons = load_world_products(coupons_path) if coupons_path else []
    records = dict(generate_pilot_designs(manifest, products, coupons))
    world_sha = sha256_file(manifest_path)
    environment_path = manifest_path.parent / "environments" / "E0.jsonl"
    environment_sha = sha256_file(environment_path)
    records["receipt"] = [
        make_runner_receipt(prediction, oracle, fault, world_manifest_sha256=world_sha, environment_artifact_sha256=environment_sha, status="NOT_COLLECTED")
        for prediction, oracle, fault in zip(records["prediction"], records["oracle"], records["fault"])
    ]
    output_dir = Path(output_dir)
    paths = {kind: output_dir / f"{kind}.jsonl" for kind in records}
    for kind, path in paths.items():
        write_jsonl(path, records[kind], kind)
    return paths


# ---------------------------------------------------------------------------
# Phase 2B — executable 96-scenario oracle-first authoring
# ---------------------------------------------------------------------------

PILOT_96_VERSION = "commerce-pilot-96-oracle-first-v1"
PILOT_96_DIRNAME = "agentic_commerce_benchmark_v1_pilot_96_20260821"
_DYNAMIC_FIELDS = frozenset(("price", "stock", "promotionEligible", "environmentRevision"))
_SKIP_PRODUCT_FIELDS = frozenset(("category", "title", "price", "optionalSellerNote"))
_POLICY_TOPICS = ("returns", "shipping", "warranty", "service")
_POLICY_TOPIC_LABELS = {"returns": "退换货", "shipping": "配送", "warranty": "保修", "service": "售后服务"}
_POLICY_PUBLIC_CONCERNS = {
    "returns": "退换条件",
    "shipping": "配送安排",
    "warranty": "质量问题的售后保障",
    "service": "售后处理方式",
}
_CATEGORY_POLICY_CONCERNS = {
    "books": {
        "returns": "收到后发现缺页或破损时怎么处理",
        "warranty": "出现缺页、破损等情况时怎么处理",
        "shipping": "配送时怎样保护书籍",
        "service": "收到书后有问题时怎么联系处理",
    },
    "sneakers": {"warranty": "开胶、开线等质量问题怎么处理"},
    "phone_accessory": {"warranty": "接口或连接出现问题时怎么处理"},
    "drinkware": {"warranty": "漏水或保温异常时怎么处理"},
}
_CATEGORY_KNOWLEDGE_FOCUS = {
    "drinkware": {"版本": "杯型和款式"},
    "cleaning": {"型号": "适用场景和使用方式"},
    "stationery": {"包装数量": "套装内容和数量"},
    "books": {"版本": "版本和装帧"},
}
_CATEGORY_SELLER_FACT_REQUESTS = {
    "used_phone": "电池状态和维修情况",
    "phone_accessory": "接口状态和兼容情况",
    "earbuds": "电池状态和连接情况",
    "smartwatch": "屏幕和表壳的实际状况",
    "tshirt": "衣物是否有瑕疵或起球",
    "jeans": "裤子是否有磨损或褪色",
    "sneakers": "鞋底磨损和鞋面状况",
    "handbag": "五金和包身的实际状况",
    "drinkware": "杯盖和密封圈的实际状况",
    "cleaning": "包装是否完整、是否临近保质期",
    "stationery": "包装是否完整、配件是否齐全",
    "books": "是否缺页、是否有影响阅读的破损",
}
_CATEGORY_MULTI_MODIFIERS = {
    "used_phone": "两部手机都希望成色稳妥、续航可靠",
    "phone_accessory": "两件配件都希望耐用、兼容",
    "earbuds": "两副耳机都希望佩戴舒适、连接稳定",
    "smartwatch": "两块手表都希望适合日常使用",
    "tshirt": "两件T恤都希望穿着舒适、容易搭配",
    "jeans": "两条牛仔裤都希望合身、耐穿",
    "sneakers": "两双鞋都希望脚感舒适、耐磨",
    "handbag": "两个包都希望收纳方便、耐用",
    "drinkware": "两个水杯都希望好清洗、不易漏水",
    "cleaning": "两套清洁用品都希望使用简单",
    "stationery": "两套文具都希望便于日常学习和收纳",
    "books": "两本书都希望适合当前的阅读计划",
}
_S2_POINTS = {
    "product_finder": ("empty_result", "requery_or_abstain"),
    "knowledge_to_product": ("evidence_conflict", "cite_one_revision"),
    "multi_product_merchant": ("stock_change", "do_not_sell_out"),
    "coupon_budget": ("stale_price", "recalculate_or_abstain"),
    "dynamic_replanning": ("stock_change", "do_not_sell_out"),
}
_CATEGORY_LABELS = {
    "used_phone": "二手手机", "phone_accessory": "手机配件", "earbuds": "无线耳机",
    "smartwatch": "智能手表", "tshirt": "T恤", "jeans": "牛仔裤", "sneakers": "运动鞋",
    "handbag": "手提包", "drinkware": "水杯", "cleaning": "清洁用品", "stationery": "文具", "books": "图书",
}
_FIELD_LABELS = {
    "brand": "品牌", "model": "型号", "series": "系列", "variant": "版本", "condition": "成色",
    "platform": "平台", "storageGb": "存储容量", "batteryHealthPct": "电池健康度", "cameraGrade": "相机等级",
    "accessoryType": "配件类型", "compatibleWith": "兼容平台", "connector": "接口", "material": "材质",
    "color": "颜色", "batteryHours": "续航时间", "connection": "连接方式", "fit": "佩戴方式",
    "noiseControl": "降噪类型", "waterproofRating": "防水等级", "batteryDays": "续航时间", "caseMaterial": "表壳材质",
    "screenType": "屏幕类型", "waterResistance": "防水等级", "fabric": "面料", "stretch": "弹力",
    "waistSize": "腰围", "wash": "水洗", "upperMaterial": "鞋面材质", "soleMaterial": "鞋底材质", "sport": "运动用途",
    "size": "尺码", "cushioning": "缓震", "closure": "闭合方式", "capacity": "容量", "strapType": "肩带类型",
    "leakproof": "防漏", "capacityMl": "容量", "insulation": "保温结构", "lidType": "杯盖类型",
    "useSurface": "适用表面", "form": "剂型", "ingredient": "成分", "scent": "气味", "volumeMl": "容量",
    "itemType": "文具类型", "packCount": "包装数量", "refillable": "可替换", "season": "季节", "language": "语言",
    "genre": "类型", "audience": "读者层级", "binding": "装帧", "edition": "版本年份", "pageCount": "页数",
    "publisher": "出版社", "merchantId": "商家", "price": "价格", "stock": "库存", "promotionEligible": "促销资格",
}

# Public language uses a category-appropriate measure word.  Keeping this
# mapping beside the renderer prevents a generic “一件” from silently
# becoming the visible grammar for every product family.
_CATEGORY_QUANTIFIERS = {
    "used_phone": ("一部二手手机", "两部二手手机", "几部二手手机"),
    "phone_accessory": ("一件手机配件", "两件手机配件", "几件手机配件"),
    "earbuds": ("一副无线耳机", "两副无线耳机", "几副无线耳机"),
    "smartwatch": ("一块智能手表", "两块智能手表", "几块智能手表"),
    "tshirt": ("一件T恤", "两件T恤", "几件T恤"),
    "jeans": ("一条牛仔裤", "两条牛仔裤", "几条牛仔裤"),
    "sneakers": ("一双运动鞋", "两双运动鞋", "几双运动鞋"),
    "handbag": ("一个手提包", "两个手提包", "几个手提包"),
    "drinkware": ("一个水杯", "两个水杯", "几个水杯"),
    "cleaning": ("一套清洁用品", "两套清洁用品", "几套清洁用品"),
    "stationery": ("一套文具", "两套文具", "几套文具"),
    "books": ("一本图书", "两本图书", "几本图书"),
}

_CATEGORY_MODIFIERS = {
    "used_phone": ("我更看重成色、续航和长期稳定性", "希望关键配置和售后情况说清楚", "我想重点核对电池健康与价格", "最好耐用、省心，后续维护也方便", "我会把电池状态和成色放在前面比较", "希望系统、存储和价格都交代清楚", "如果条件相近，优先考虑更稳妥的成色", "我想买一部日常使用起来不折腾的手机"),
    "phone_accessory": ("我比较在意接口兼容和做工", "希望连接稳定、携带方便", "我想把兼容平台和材质核对清楚", "最好耐用，不容易接触不良", "我会先确认接口是否和手头设备匹配", "希望线材或配件的使用限制说清楚", "如果有多个合适选项，优先考虑更结实的", "我想选一个日常携带不占地方的配件"),
    "earbuds": ("我比较在意佩戴舒适和降噪效果", "希望续航与连接稳定性说清楚", "我想重点比较佩戴方式和通话体验", "最好轻便，长时间使用也不累", "我会先看佩戴是否稳固，再比较功能", "希望耳机的连接协议和续航说得明白", "如果条件相近，优先考虑更轻便的款式", "我想选一副通勤时戴着舒服的耳机"),
    "smartwatch": ("我比较在意佩戴舒适和续航", "希望屏幕与防水能力说明白", "我想重点核对表壳材质和功能取向", "最好适合日常运动，操作别太复杂", "我会把续航和佩戴感受放在前面比较", "希望屏幕类型与防护能力说清楚", "如果条件相近，优先选择更适合运动的一款", "我想选一块日常查看信息方便的手表"),
    "tshirt": ("我更在意面料亲肤和穿着舒适", "希望版型合身，也要方便打理", "我想重点比较颜色、弹力和水洗方式", "最好耐穿，日常搭配不费心", "我会先看面料和版型，再比较颜色", "希望清洗方式与弹力情况说清楚", "如果条件相近，优先考虑更容易搭配的颜色", "我想选一件穿着轻松、打理简单的T恤"),
    "jeans": ("我比较在意版型合身和面料耐穿", "希望腰围、弹力与水洗方式说清楚", "我想重点核对面料和尺码建议", "最好日常好搭配，穿着活动方便", "我会把腰围和版型放在前面比较", "希望面料厚薄与水洗方式说得明白", "如果条件相近，优先考虑活动更方便的版型", "我想选一条日常穿着不拘束的牛仔裤"),
    "sneakers": ("我比较在意脚感、缓震和耐磨性", "希望鞋面材质与适用运动说清楚", "我想重点比较尺码、鞋底和缓震", "最好长时间走路也舒适", "我会先看缓震和鞋底，再比较外观", "希望适用运动与鞋面材料说明白", "如果条件相近，优先考虑更耐磨的鞋底", "我想选一双通勤和运动都能穿的鞋"),
    "handbag": ("我比较在意容量、材质和日常搭配", "希望闭合方式和肩带使用感说清楚", "我想重点核对颜色、材质与收纳空间", "最好耐用，通勤携带方便", "我会先看容量和收纳，再比较材质", "希望肩带长度与闭合方式说清楚", "如果条件相近，优先考虑更好搭配的颜色", "我想选一个通勤时拿取方便的包"),
    "drinkware": ("我比较在意保温、防漏和清洗方便", "希望杯盖、容量与保温结构说清楚", "我想重点核对容量和适用场景", "最好携带方便，不容易漏水", "我会先看防漏和清洗，再比较容量", "希望杯盖类型与保温结构说明白", "如果条件相近，优先考虑更适合随身携带的", "我想选一个办公室和外出都方便用的水杯"),
    "cleaning": ("我比较在意适用表面和清洁成分", "希望剂型、气味与使用范围说清楚", "我想重点核对适用表面和成分", "最好用起来简单，清洁后不留明显气味", "我会先确认适用表面，再比较成分", "希望剂型和气味信息交代清楚", "如果条件相近，优先考虑使用步骤更简单的", "我想选一套家里常用、容易操作的清洁用品"),
    "stationery": ("我比较在意书写体验和补充方便", "希望文具类型、包装数量与可替换性说清楚", "我想重点核对规格和使用场景", "最好适合日常学习，收纳也方便", "我会先看规格和书写用途，再比较包装", "希望可替换性与包装数量说明白", "如果条件相近，优先考虑更便于收纳的", "我想选一套学习时拿取方便的文具"),
    "books": ("我比较在意内容难度、装帧和阅读体验", "希望读者层级、版本和页数说清楚", "我想重点核对适读人群与版本信息", "最好方便长期阅读和保存", "我会先看适读人群，再比较版本与装帧", "希望页数、出版社和装帧信息交代清楚", "如果条件相近，优先考虑更适合长期阅读的版本", "我想选一本适合当前阅读计划的书"),
}
_PUBLIC_MODIFIERS = (
    "我更看重长期使用的稳定性",
    "希望日常操作省心一些",
    "我比较在意耐用度和后续维护",
    "最好能和我现有的使用习惯匹配",
    "在满足条件的前提下尽量省预算",
    "我希望推荐理由清楚、方便比较",
    "优先考虑售后沟通比较顺畅的选择",
    "我想把关键事实和价格都核对清楚",
    "平时使用频率较高，希望不容易出问题",
    "如果有几种都合适，优先推荐更划算的",
    "我更喜欢简单可靠、少折腾的方案",
    "希望最后留下的选择比较容易做决定",
)

# The world intentionally keeps synthetic merchant identifiers.  Public
# language must use stable, human-like aliases instead of index-shaped labels.
_MERCHANT_ALIAS_PREFIXES = (
    "青禾数码馆", "松风生活馆", "云杉优选", "白鹭百货", "栖木精选", "远山好物",
    "星河小铺", "晴川商行", "沐光集市", "拾野杂货铺", "秋实优品", "知物仓",
    "森屿商店", "禾木生活馆", "简集好物", "暖橙小店", "予你优选", "望舒百货",
    "漫野商行", "山茶铺子", "清和好物", "微澜精选", "朝露小铺", "月白商店",
    "浮光集", "见山优选", "初见生活馆", "长街好物", "原野商行", "澄品小店",
    "归一百货", "风物集市", "花间杂货铺", "砚台精选", "听雨商行", "鹿鸣小铺",
    "云上生活馆", "南枝优品", "拾光商店", "野渡好物", "知行百货", "木棉小店",
    "春山精选", "望海商行", "栖迟生活馆", "青石优品", "有间好物", "慢物集",
)

_ENUM_VALUE_LABELS: Mapping[str, Mapping[str, str]] = {
    "accessoryType": {"cable": "连接线", "case": "保护壳", "charger": "充电器", "stand": "支架"},
    "audience": {"advanced": "适合进阶读者", "beginner": "适合入门读者", "intermediate": "适合有一定基础的读者"},
    "binding": {"ebook": "电子版", "hardcover": "精装版", "paperback": "平装版"},
    "capacity": {"large": "大容量", "medium": "中等容量", "small": "小容量"},
    "caseMaterial": {"aluminum": "铝合金", "ceramic": "陶瓷", "steel": "钢制"},
    "color": {"black": "黑色", "blue": "蓝色", "brown": "棕色", "cream": "米白色", "gray": "灰色", "green": "绿色", "navy": "藏蓝色", "red": "红色", "white": "白色"},
    "condition": {"excellent": "成色优秀", "good": "成色良好", "fair": "有明显使用痕迹"},
    "connection": {"Bluetooth-5.2": "蓝牙 5.2", "Bluetooth-5.3": "蓝牙 5.3"},
    "cushioning": {"balanced": "均衡缓震", "firm": "偏硬缓震", "soft": "柔和缓震"},
    "closure": {"drawstring": "抽绳开合", "magnetic": "磁吸开合", "zipper": "拉链开合"},
    "fabric": {"cotton": "棉质", "denim": "牛仔布", "linen": "亚麻", "modal": "莫代尔", "organic-denim": "有机牛仔布", "recycled": "再生材质", "stretch-denim": "弹力牛仔布"},
    "fit": {"ear-hook": "耳挂式", "in-ear": "入耳式", "oversized": "宽松版型", "regular": "常规版型", "relaxed": "舒适宽松版型", "semi-in-ear": "半入耳式", "slim": "修身版型", "straight": "直筒版型"},
    "form": {"liquid": "液体", "spray": "喷雾", "wipe": "湿巾"},
    "genre": {"business": "商业管理", "fiction": "文学小说", "history": "历史读物", "technology": "科技读物"},
    "ingredient": {"enzyme": "酵素配方", "neutral": "温和配方", "plant-based": "植物来源配方"},
    "insulation": {"double-wall": "双层隔热", "single-wall": "单层结构", "vacuum": "真空保温"},
    "itemType": {"folder": "文件夹", "marker": "马克笔", "notebook": "笔记本", "pen": "签字笔"},
    "language": {"bilingual": "双语", "en-US": "英文", "zh-CN": "中文"},
    "leakproof": {"True": "防漏", "False": "不特别保证防漏"},
    "lidType": {"flip": "翻盖", "screw": "旋盖", "straw": "吸管盖"},
    "material": {"aluminum": "铝合金", "canvas": "帆布", "glass": "玻璃", "leather": "皮革", "metal": "金属", "nylon": "尼龙", "paper": "纸质", "plastic": "塑料", "silicone": "硅胶", "stainless-steel": "不锈钢", "tritan": "Tritan 材质"},
    "noiseControl": {"ANC": "主动降噪", "adaptive": "自适应降噪", "passive": "被动隔音"},
    "platform": {"AndroidWear": "安卓手表系统", "HarmonyOS": "鸿蒙系统", "watchOS": "苹果手表系统"},
    "scent": {"citrus": "柑橘香", "floral": "花香", "unscented": "无香型"},
    "screenType": {"AMOLED": "AMOLED 屏", "LCD": "LCD 屏", "OLED": "OLED 屏"},
    "season": {"autumn": "秋季", "spring": "春季", "summer": "夏季"},
    "soleMaterial": {"carbon": "碳纤维", "foam": "泡棉", "rubber": "橡胶"},
    "sport": {"running": "跑步", "training": "综合训练", "walking": "日常步行"},
    "strapType": {"crossbody": "斜挎带", "shoulder": "肩带", "top-handle": "手提式"},
    "stretch": {"True": "有弹力", "False": "无明显弹力"},
    "useSurface": {"bathroom": "浴室表面", "fabric": "织物表面", "glass": "玻璃表面", "kitchen": "厨房表面"},
    "upperMaterial": {"knit": "针织鞋面", "leather": "皮革鞋面", "mesh": "网面鞋面"},
    "wash": {"dark": "深色水洗", "light": "浅色水洗", "medium": "中度水洗"},
}


def _stable_digest(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _write_private_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for record in records:
            fh.write(canonical_bytes(record).decode("utf-8"))


def _field_info(product: Mapping[str, Any], field: str) -> tuple[bool, Any, str, str]:
    if field == "merchantId":
        return True, product.get("merchantId"), f"{product.get('sourceRef', '')}#product={product.get('productId')}&field=merchantId", str(product.get("factTier", "synthetic_fixture"))
    fact = (product.get("facts") or {}).get(field)
    if isinstance(fact, dict):
        return bool(fact.get("known", False)), fact.get("value"), str(fact.get("sourceRef", "")), str(fact.get("factTier", product.get("factTier", "synthetic_fixture")))
    if field in product:
        return True, product.get(field), f"{product.get('sourceRef', '')}#product={product.get('productId')}&field={field}", str(product.get("factTier", "synthetic_fixture"))
    return False, None, "", ""


def _known_static_fields(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    fields: set[str] = set()
    for product in rows:
        for field, fact in (product.get("facts") or {}).items():
            if field in _SKIP_PRODUCT_FIELDS:
                continue
            if isinstance(fact, dict) and fact.get("known") is True:
                fields.add(str(field))
    return tuple(sorted(fields))


def _atom(field: str, operator: str, value: Any) -> Mapping[str, Any]:
    return {"kind": "atom", "atom": {"field": field, "operator": operator, "value": value, "unknownPolicy": "fail"}}


def _all_atoms(atoms: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    clean = [dict(atom) for atom in atoms]
    return clean[0] if len(clean) == 1 else {"kind": "all", "children": clean}


def _flatten_atoms(node: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    if node.get("kind") == "atom":
        return (node,)
    if node.get("kind") in {"all", "any"}:
        return tuple(atom for child in node.get("children", []) for atom in _flatten_atoms(child))
    if node.get("kind") == "not":
        return _flatten_atoms(node.get("child", {}))
    return ()


def _facts_for(product: Mapping[str, Any], environment_row: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    facts = dict(product.get("facts") or {})
    for field in ("merchantId", "category", "title"):
        known, value, source_ref, fact_tier = _field_info(product, field)
        facts.setdefault(field, {"known": known, "value": value, "sourceRef": source_ref, "factTier": fact_tier})
    if environment_row is None:
        known, value, source_ref, fact_tier = _field_info(product, "price")
        facts.setdefault("price", {"known": known, "value": value, "sourceRef": source_ref, "factTier": fact_tier})
    else:
        for field in _DYNAMIC_FIELDS:
            facts[field] = {"known": True, "value": environment_row.get(field), "sourceRef": environment_row.get("sourceRef", ""), "factTier": environment_row.get("factTier", "synthetic_fixture")}
    return facts


def _matches(product: Mapping[str, Any], ast: Mapping[str, Any], environment_row: Mapping[str, Any] | None = None) -> bool:
    """Fast deterministic AST evaluator for authoring's simple positive ASTs.

    The production evaluator remains the contract authority.  Authoring runs
    over 12k rows repeatedly, so avoiding ``typing.Mapping`` runtime checks in
    that hot loop keeps the full-universe proof bounded without changing AST
    semantics.
    """
    def fact(field: str) -> tuple[bool, Any]:
        if environment_row is not None and field in _DYNAMIC_FIELDS:
            return field in environment_row, environment_row.get(field)
        if field == "merchantId":
            return True, product.get("merchantId")
        if field in {"category", "title"}:
            return field in product, product.get(field)
        raw = (product.get("facts") or {}).get(field)
        if isinstance(raw, dict):
            return bool(raw.get("known", False)), raw.get("value")
        if field in product:
            return True, product.get(field)
        return False, None

    def atom_matches(atom_node: Mapping[str, Any]) -> bool:
        atom = atom_node.get("atom") or {}
        known, actual = fact(str(atom.get("field", "")))
        if not known:
            return False
        expected = atom.get("value")
        operator = str(atom.get("operator", ""))
        try:
            if operator == "EQ": return actual == expected
            if operator == "NEQ": return actual != expected
            if operator == "IN": return isinstance(expected, (list, tuple, set)) and actual in expected
            if operator == "NOT_IN": return isinstance(expected, (list, tuple, set)) and actual not in expected
            if operator == "CONTAINS": return isinstance(actual, str) and str(expected) in actual
            if operator == "LTE": return float(actual) <= float(expected)
            if operator == "GTE": return float(actual) >= float(expected)
        except (TypeError, ValueError):
            return False
        return False
    kind = ast.get("kind")
    if kind == "atom": return atom_matches(ast)
    if kind == "all": return bool(ast.get("children")) and all(_matches(product, child, environment_row) for child in ast.get("children", []))
    if kind == "any": return bool(ast.get("children")) and any(_matches(product, child, environment_row) for child in ast.get("children", []))
    if kind == "not": return False if not _matches(product, ast.get("child", {}), environment_row) else False
    return False


def _universe(products: Sequence[Mapping[str, Any]], ast: Mapping[str, Any], environment_rows: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[str, ...]:
    return tuple(sorted(str(product["productId"]) for product in products if _matches(product, ast, environment_rows.get(str(product["productId"])) if environment_rows is not None else None)))


def _token(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _group_candidates(rows: Sequence[Mapping[str, Any]], fields: Sequence[str], *, min_count: int, max_count: int) -> list[tuple[tuple[Any, ...], list[Mapping[str, Any]]]]:
    groups: dict[tuple[str, ...], list[Mapping[str, Any]]] = {}
    for product in rows:
        values: list[Any] = []
        valid = True
        for field in fields:
            known, value, _, _ = _field_info(product, field)
            if not known:
                valid = False
                break
            values.append(value)
        if valid:
            groups.setdefault(tuple(_token(value) for value in values), []).append(product)
    result: list[tuple[tuple[Any, ...], list[Mapping[str, Any]]]] = []
    for key, group in sorted(groups.items()):
        if min_count <= len(group) <= max_count:
            result.append((tuple(json.loads(value) for value in key), sorted(group, key=lambda row: str(row["productId"]))))
    return result


def _choose_static_group(category_rows: Sequence[Mapping[str, Any]], category: str, slot: int, *, min_count: int = 1, max_count: int = 256, field_count: int = 2, extra_atoms: Sequence[Mapping[str, Any]] = (), start_rows: Mapping[str, Mapping[str, Any]] | None = None, terminal_rows: Mapping[str, Mapping[str, Any]] | None = None, require_delta: bool = False, require_terminal: int = 1) -> tuple[Mapping[str, Any], list[Mapping[str, Any]], tuple[str, ...], tuple[str, ...]]:
    fields = _known_static_fields(category_rows)
    candidates: list[tuple[tuple[str, ...], tuple[Any, ...], list[Mapping[str, Any]], tuple[str, ...], tuple[str, ...], Mapping[str, Any]]] = []
    for field_tuple in combinations(fields, field_count):
        for values, group in _group_candidates(category_rows, field_tuple, min_count=min_count, max_count=max_count):
            atoms = [_atom("category", "EQ", category)] + [_atom(field, "EQ", value) for field, value in zip(field_tuple, values)] + list(extra_atoms)
            ast = _all_atoms(atoms)
            start = _universe(category_rows, ast, start_rows) if start_rows is not None else tuple(sorted(str(row["productId"]) for row in group))
            terminal = _universe(category_rows, ast, terminal_rows) if terminal_rows is not None else start
            if len(terminal) < require_terminal or (require_delta and set(start) == set(terminal)):
                continue
            # Non-dynamic authoring can stop at the first canonical group;
            # computing every other group made a 12k world unnecessarily
            # quadratic.  Dynamic cases continue only until a real delta is
            # witnessed, then remain deterministic by sorted iteration order.
            if not require_delta:
                return ast, group, start, terminal
            candidates.append((field_tuple, values, group, start, terminal, ast))
            if len(candidates) >= 1:
                return ast, group, start, terminal
    if not candidates:
        raise CommerceWorldError(f"cannot derive a complete static oracle group for {category}")
    candidates.sort(key=lambda item: (len(item[2]), item[0], tuple(_token(value) for value in item[1]), item[3], item[4]))
    chosen = candidates[slot % len(candidates)]
    return chosen[5], chosen[2], chosen[3], chosen[4]


def _source_binding(product: Mapping[str, Any], field: str, *, environment_row: Mapping[str, Any] | None = None, source_type: str | None = None, role: str = "constraint") -> Mapping[str, Any]:
    if environment_row is not None and field in _DYNAMIC_FIELDS:
        known, value, source_ref, fact_tier = True, environment_row.get(field), str(environment_row.get("sourceRef", "")), str(environment_row.get("factTier", "synthetic_fixture"))
    else:
        known, value, source_ref, fact_tier = _field_info(product, field)
    if not known or not source_ref:
        raise CommerceWorldError(f"missing source provenance for {field}")
    return {"sourceType": source_type or ("dynamic_offer" if field in _DYNAMIC_FIELDS else "product_fact"), "role": role, "field": field, "value": value, "sourceRef": source_ref, "factTier": fact_tier}


def _ast_source_bindings(ast: Mapping[str, Any], representative: Mapping[str, Any], environment_row: Mapping[str, Any] | None = None) -> list[Mapping[str, Any]]:
    return [_source_binding(representative, str(node["atom"]["field"]), environment_row=environment_row) for node in _flatten_atoms(ast)]


def _knowledge_candidates(category_rows: Sequence[Mapping[str, Any]], category: str, knowledge_rows: Sequence[Mapping[str, Any]], slot: int, *, start_rows: Mapping[str, Mapping[str, Any]] | None = None, terminal_rows: Mapping[str, Mapping[str, Any]] | None = None, require_delta: bool = False) -> tuple[Mapping[str, Any], Mapping[str, Any], tuple[str, ...], tuple[str, ...]]:
    candidates: list[tuple[Mapping[str, Any], Mapping[str, Any], tuple[str, ...], tuple[str, ...]]] = []
    category_docs = [row for row in knowledge_rows if str(row.get("category")) == category]
    field_counts = Counter(str(row.get("attributeField", "")) for row in category_docs)
    for doc in sorted(category_docs, key=lambda row: str(row.get("knowledgeId"))):
        rule = doc.get("selectionRule")
        if not isinstance(rule, Mapping) or str(rule.get("unknownPolicy")) != "fail":
            continue
        # The public request names the semantic field, never the private
        # value.  Choose a field with one frozen knowledge rule in this
        # category so the request has a unique document interpretation.
        if field_counts.get(str(doc.get("attributeField", "")), 0) != 1:
            continue
        atom = _atom(str(rule.get("field")), str(rule.get("operator")), rule.get("value"))
        ast = _all_atoms([_atom("category", "EQ", category), atom])
        start = _universe(category_rows, ast, start_rows) if start_rows is not None else _universe(category_rows, ast)
        terminal = _universe(category_rows, ast, terminal_rows) if terminal_rows is not None else start
        if not start or len(start) >= len(category_rows) or not terminal or (require_delta and set(start) == set(terminal)):
            continue
        candidates.append((doc, ast, start, terminal))
    if not candidates:
        raise CommerceWorldError(f"no materially narrowing knowledge rule for {category}")
    candidates.sort(key=lambda item: (len(item[2]), str(item[0]["knowledgeId"]), item[2], item[3]))
    return candidates[slot % len(candidates)]


def _policy_for_merchant(policies: Sequence[Mapping[str, Any]], merchant_id: str, slot: int) -> Mapping[str, Any]:
    rows = sorted((row for row in policies if str(row.get("merchantId")) == merchant_id), key=lambda row: str(row.get("topic")))
    if len(rows) != 4 or {str(row.get("topic")) for row in rows} != set(_POLICY_TOPICS):
        raise CommerceWorldError(f"merchant {merchant_id} does not have four policy topics")
    return next(row for row in rows if str(row.get("topic")) == _POLICY_TOPICS[slot % len(_POLICY_TOPICS)])


def _policy_dependency(policy: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = sorted(str(field) for field in (policy.get("keyFields") or []) if field in (policy.get("terms") or {}))
    if not fields:
        raise CommerceWorldError(f"policy {policy.get('policyId')} has no usable term")
    field = fields[0]
    return {"documentType": "policy", "documentId": str(policy["policyId"]), "field": field, "operator": "EQ", "value": policy["terms"][field], "binding": "selected_product_merchant"}


def _knowledge_dependency(doc: Mapping[str, Any]) -> Mapping[str, Any]:
    rule = doc["selectionRule"]
    return {"documentType": "knowledge", "documentId": str(doc["knowledgeId"]), "field": str(rule["field"]), "operator": str(rule["operator"]), "value": rule["value"], "binding": "product_constraint"}


def _load_formal_world(world_dir: Path | str) -> Mapping[str, Any]:
    root = Path(world_dir).resolve()
    if root.name != "commerce_world_cn_v1_controlled_20260821_r2":
        raise CommerceWorldError("Phase 2B requires the accepted controlled-world r2 directory")
    manifest_path = root / "manifest.json"
    manifest = load_world_manifest(manifest_path)
    if manifest.get("status") != "READY" or manifest.get("manifestAudit") != "verified":
        raise CommerceWorldError("accepted controlled world is not READY/audited")
    if dict(manifest.get("actuals", {})) != TARGET_COUNTS or set(manifest.get("environmentRevisions", [])) != set(ENVIRONMENTS):
        raise CommerceWorldError("controlled world counts or revisions are incomplete")
    artifact_paths = {str(row["path"]): root / str(row["path"]) for row in manifest["artifacts"]}
    required = {"products.jsonl", "merchants.jsonl", "policies.jsonl", "coupons.jsonl", "knowledge.jsonl", *(f"environments/{revision}.jsonl" for revision in ENVIRONMENTS)}
    if set(artifact_paths) != required or any(not path.is_file() for path in artifact_paths.values()):
        raise CommerceWorldError("controlled world artifact set is incomplete")
    products = tuple(load_world_products(artifact_paths["products.jsonl"]))
    merchants = tuple(load_world_products(artifact_paths["merchants.jsonl"]))
    policies = tuple(load_world_products(artifact_paths["policies.jsonl"]))
    coupons = tuple(load_world_products(artifact_paths["coupons.jsonl"]))
    knowledge = tuple(load_world_products(artifact_paths["knowledge.jsonl"]))
    environments = {revision: {str(row["productId"]): row for row in load_world_products(artifact_paths[f"environments/{revision}.jsonl"])} for revision in ENVIRONMENTS}
    if len(products) != TARGET_COUNTS["products"] or len(merchants) != TARGET_COUNTS["merchants"] or len(policies) != TARGET_COUNTS["policies"] or len(coupons) != TARGET_COUNTS["coupons"] or len(knowledge) != TARGET_COUNTS["knowledge"]:
        raise CommerceWorldError("controlled world artifact counts changed after manifest audit")
    return {"root": root, "manifest": manifest, "manifestPath": manifest_path, "worldSha256": sha256_file(manifest_path), "artifactPaths": artifact_paths, "artifactShas": {path: str(row["sha256"]) for path, row in ((str(row["path"]), row) for row in manifest["artifacts"])}, "products": products, "merchants": merchants, "policies": policies, "coupons": coupons, "knowledge": knowledge, "environments": environments}


def _choose_bundle_filter(category_rows: Sequence[Mapping[str, Any]], category: str, slot: int, *, dynamic: bool, start_rows: Mapping[str, Mapping[str, Any]], terminal_rows: Mapping[str, Mapping[str, Any]]) -> tuple[Mapping[str, Any], list[Mapping[str, Any]], tuple[str, ...], tuple[str, ...]]:
    fields = _known_static_fields(category_rows)
    # Search inside a merchant partition first.  The old category-wide group
    # enumeration repeatedly rescanned 1,000 rows and can become quadratic on
    # the controlled world even though a merchant has only a few dozen rows in
    # one category.
    by_merchant_all: dict[str, list[Mapping[str, Any]]] = {}
    for row in category_rows:
        by_merchant_all.setdefault(str(row.get("merchantId")), []).append(row)
    for merchant_id, merchant_all in sorted(by_merchant_all.items()):
        for field_count in (1, 2):
            for field_tuple in combinations(fields, field_count):
                for values, grouped in _group_candidates(merchant_all, field_tuple, min_count=2, max_count=64):
                    atoms = [_atom("category", "EQ", category), _atom("merchantId", "EQ", merchant_id)] + [_atom(field, "EQ", value) for field, value in zip(field_tuple, values)]
                    if dynamic:
                        atoms.append(_atom("stock", "GTE", 1))
                    ast = _all_atoms(atoms)
                    start = _universe(grouped, ast, start_rows)
                    terminal = _universe(grouped, ast, terminal_rows)
                    if len(terminal) >= 2 and (not dynamic or set(start) != set(terminal)):
                        return ast, grouped, start, terminal
        # A same-merchant bundle remains meaningful even when no typed field
        # repeats; this fallback is still bounded to one merchant and keeps
        # the merchant constraint explicit.
        ast = _all_atoms([_atom("category", "EQ", category), _atom("merchantId", "EQ", merchant_id)] + ([_atom("stock", "GTE", 1)] if dynamic else []))
        start = _universe(merchant_all, ast, start_rows)
        terminal = _universe(merchant_all, ast, terminal_rows)
        if len(terminal) >= 2 and (not dynamic or set(start) != set(terminal)):
            return ast, merchant_all, start, terminal
    candidates: list[tuple[int, str, tuple[str, ...], tuple[Any, ...], Mapping[str, Any], list[Mapping[str, Any]], tuple[str, ...], tuple[str, ...]]] = []
    for field_count in (1, 2):
        for field_tuple in combinations(fields, field_count):
            for values, grouped in _group_candidates(category_rows, field_tuple, min_count=2, max_count=64):
                by_merchant: dict[str, list[Mapping[str, Any]]] = {}
                for product in grouped:
                    by_merchant.setdefault(str(product["merchantId"]), []).append(product)
                for merchant_id, merchant_rows in sorted(by_merchant.items()):
                    atoms = [_atom("category", "EQ", category), _atom("merchantId", "EQ", merchant_id)] + [_atom(field, "EQ", value) for field, value in zip(field_tuple, values)]
                    if dynamic:
                        atoms.append(_atom("stock", "GTE", 1))
                    ast = _all_atoms(atoms)
                    # ``merchant_rows`` is already the exact static group;
                    # overlaying offers on that bounded group avoids a full
                    # 1,000-row category scan for every field combination.
                    start = _universe(merchant_rows, ast, start_rows)
                    terminal = _universe(merchant_rows, ast, terminal_rows)
                    bundles = list(combinations(terminal, 2))
                    if len(bundles) < 1 or (dynamic and set(start) == set(terminal)):
                        continue
                    if not dynamic:
                        return ast, merchant_rows, start, terminal
                    candidates.append((len(bundles), merchant_id, field_tuple, values, ast, merchant_rows, start, terminal))
                    if dynamic:
                        return ast, merchant_rows, start, terminal
    if not candidates:
        raise CommerceWorldError(f"cannot derive complete same-merchant bundles for {category}")
    candidates.sort(key=lambda item: (item[0], item[1], item[2], tuple(_token(value) for value in item[3]), item[7]))
    chosen = candidates[slot % len(candidates)]
    return chosen[4], chosen[5], chosen[6], chosen[7]


def _choose_coupon_filter(category_rows: Sequence[Mapping[str, Any]], category: str, slot: int, coupons: Sequence[Mapping[str, Any]], e0_rows: Mapping[str, Mapping[str, Any]], *, dynamic_fault: bool) -> tuple[Mapping[str, Any], Mapping[str, Any], tuple[str, ...], tuple[str, ...], Mapping[str, Any]]:
    choices: list[tuple[int, str, Mapping[str, Any], Mapping[str, Any], tuple[str, ...]]] = []
    for coupon in sorted((row for row in coupons if row.get("kind") == "fixed" and row.get("scope") == "merchant" and row.get("environmentRevision") == "E0"), key=lambda row: str(row["couponId"])):
        merchant_id = str(coupon["merchantId"])
        eligible = [row for row in category_rows if str(row.get("merchantId")) == merchant_id and e0_rows[str(row["productId"])].get("stock", 0) >= 1 and float(e0_rows[str(row["productId"])].get("price", 0)) >= float(coupon["threshold"])]
        if not eligible:
            continue
        fields = _known_static_fields(eligible)
        field_options: list[tuple[tuple[str, ...], tuple[Any, ...], list[Mapping[str, Any]]]] = [((), (), eligible)]
        for n in (1,):
            field_options.extend((field_tuple, values, grouped) for field_tuple in combinations(fields, n) for values, grouped in _group_candidates(eligible, field_tuple, min_count=1, max_count=64))
        for field_tuple, values, grouped in field_options:
            atoms = [_atom("category", "EQ", category), _atom("merchantId", "EQ", merchant_id), _atom("price", "GTE", float(coupon["threshold"]))]
            atoms.extend(_atom(field, "EQ", value) for field, value in zip(field_tuple, values))
            if dynamic_fault:
                atoms.append(_atom("stock", "GTE", 1))
            ast = _all_atoms(atoms)
            accepted = _universe(category_rows, ast, e0_rows)
            if not accepted:
                continue
            # The first sorted witness is already a complete universe.  Do
            # not enumerate every other field combination on a 12k world.
            if not choices:
                return ast, coupon, accepted, accepted, {"faultDerived": bool(dynamic_fault)}
            choices.append((len(accepted), str(coupon["couponId"]), coupon, ast, accepted))
    if not choices:
        raise CommerceWorldError(f"cannot derive coupon-necessary cart for {category}")
    choices.sort(key=lambda item: (item[0], item[1], item[4]))
    _, _, coupon, ast, start = choices[slot % len(choices)]
    terminal = start
    return ast, coupon, start, terminal, {"faultDerived": bool(dynamic_fault)}


def _merchant_alias(merchant_id: str) -> str:
    """Map a synthetic merchant key to a stable natural public alias."""
    try:
        ordinal = int(str(merchant_id).rsplit("-", 1)[-1]) - 1
    except (TypeError, ValueError):
        ordinal = int(hashlib.sha256(str(merchant_id).encode("utf-8")).hexdigest()[:8], 16)
    return _MERCHANT_ALIAS_PREFIXES[ordinal % len(_MERCHANT_ALIAS_PREFIXES)]


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _format_money(value: Any) -> str:
    """Speak an exact RMB amount in ordinary shopping language.

    A decimal amount is less literary than ``x 元 y 角`` / ``x 元 yy 分``,
    but it is how shoppers ordinarily state an exact non-integer budget.  It
    also keeps the numerical boundary machine-readable without turning the
    request into a ledger entry.
    """
    try:
        cents = round(float(value) * 100)
    except (TypeError, ValueError):
        return f"{value}元"
    yuan, remainder = divmod(cents, 100)
    if remainder == 0:
        return f"{yuan}元"
    return f"{yuan}.{remainder:02d}".rstrip("0") + "元"


def _render_value(field: str, value: Any) -> str:
    """Render controlled-world values as user language, never source tokens."""
    if field in _ENUM_VALUE_LABELS and str(value) in _ENUM_VALUE_LABELS[field]:
        return _ENUM_VALUE_LABELS[field][str(value)]
    if field == "compatibleWith" and str(value) == "Universal":
        return "通用平台"
    if field == "promotionEligible":
        return "可参加当前促销" if bool(value) else "暂不参加当前促销"
    if field == "cameraGrade":
        return f"{value}级相机表现"
    if field == "waterproofRating":
        return f"{value}防水等级"
    if field == "waterResistance":
        return f"{value}防水"
    if field == "batteryHealthPct":
        return f"{_format_number(value)}%"
    if field in {"batteryHours"}:
        return f"{_format_number(value)}小时"
    if field in {"batteryDays"}:
        return f"{_format_number(value)}天"
    if field in {"capacityMl", "volumeMl"}:
        return f"{_format_number(value)}毫升"
    if field == "storageGb":
        return f"{_format_number(value)}GB"
    if field == "pageCount":
        return f"{_format_number(value)}页"
    if field == "packCount":
        return f"{_format_number(value)}件装"
    if field in {"waistSize", "size"} and str(value).isdigit():
        return f"{_format_number(value)}码"
    if field == "edition" and str(value).isdigit():
        return f"{_format_number(value)}年版"
    if field == "price":
        return _format_money(value)
    return str(value)


class _PublicClause(TypedDict):
    """A typed public-language requirement before sentence composition.

    The authoring oracle remains an AST.  This deliberately separate type
    prevents the public materializer from treating a field label plus value
    as a ready-to-join string: each clause retains its grammatical role and
    semantic key until the final request is composed.
    """

    role: str
    semantic_key: str
    field: str
    value: Any
    text: str


def _public_clause(atom: Mapping[str, Any], *, category: str | None = None) -> _PublicClause:
    """Render one AST atom as a typed, conversational requirement clause."""
    field = str(atom.get("field", ""))
    operator = str(atom.get("operator", "EQ"))
    value = atom.get("value")
    label = _FIELD_LABELS.get(field, field)
    role = "attribute"
    semantic_key = field
    text: str
    if field == "optionalSellerNote":
        requested_fact = _CATEGORY_SELLER_FACT_REQUESTS.get(str(category), "商品实际状况")
        role, text = "seller_fact", f"卖家能确认{requested_fact}，不确定的话也请说明"
    elif field == "stock" and operator == "GTE" and isinstance(value, (int, float)) and float(value) >= 1:
        role, text = "availability", "现在有货"
    elif field == "promotionEligible" and operator == "EQ":
        role, text = "promotion", ("可以参加店内优惠活动" if bool(value) else "暂不参加店内优惠活动")
    else:
        rendered = _render_value(field, value)
        if field == "accessoryType" and operator == "EQ":
            role, text = "product_type", f"选{rendered}"
        elif field in {"leakproof", "refillable", "stretch"} and operator == "EQ":
            role, text = "feature", (f"要{rendered}" if bool(value) else f"不要{rendered}")
        elif field in {"batteryHours", "batteryDays"} and operator == "EQ":
            role, text = "numeric", f"续航大约{rendered}"
        elif field == "audience" and operator == "EQ":
            role, text = "audience", rendered
        elif field == "capacity" and operator == "EQ":
            role, text = "capacity", f"容量要选{rendered}"
        elif field == "price" and operator == "GTE":
            role, text = "price", f"价格至少{_format_money(value)}"
        elif field == "price" and operator == "LTE":
            role, text = "price", f"价格不超过{_format_money(value)}"
        elif field == "batteryHealthPct" and operator == "GTE":
            role, text = "numeric", f"电池健康度至少{rendered}"
        elif field in {"batteryHours", "batteryDays", "storageGb", "capacityMl", "volumeMl", "pageCount", "packCount"} and operator == "GTE":
            role, text = "numeric", f"{label}至少{rendered}"
        elif operator == "EQ" and field == "brand":
            role, text = "brand", f"品牌要选{rendered}"
        elif operator == "EQ" and field in {"fabric", "material", "color", "condition", "fit", "binding", "edition", "itemType"}:
            role, text = "attribute", f"{label}要选{rendered}"
        elif operator == "EQ" and field == "variant":
            variant_label = {"drinkware": "杯型", "handbag": "款式", "books": "版本"}.get(str(category), "款式")
            role, text = "attribute", f"{variant_label}想要{rendered}"
        elif operator == "EQ":
            role, text = "attribute", f"{label}是{rendered}"
        elif operator == "GTE":
            role, text = "numeric", f"{label}至少{rendered}"
        elif operator == "LTE":
            role, text = "numeric", f"{label}不超过{rendered}"
        elif operator == "IN":
            values = value if isinstance(value, (list, tuple)) else (value,)
            role, text = "choice", f"{label}可选{'或'.join(_render_value(field, item) for item in values)}"
        elif operator == "NOT_IN":
            values = value if isinstance(value, (list, tuple)) else (value,)
            role, text = "exclusion", f"{label}不要选{'或'.join(_render_value(field, item) for item in values)}"
        elif operator == "CONTAINS":
            role, text = "content", f"{label}里要有{rendered}"
        else:
            role, text = "attribute", f"{label}符合要求"
    return {"role": role, "semantic_key": semantic_key, "field": field, "value": value, "text": text}


def _atom_text(atom: Mapping[str, Any], *, category: str | None = None) -> str:
    """Compatibility view of one typed public-language clause."""
    return _public_clause(atom, category=category)["text"]


def _compose_public_clauses(clauses: Sequence[_PublicClause]) -> str:
    """Compose heterogeneous requirements by role instead of delimiter joining."""
    distinct: list[_PublicClause] = []
    seen: set[str] = set()
    for clause in clauses:
        key = str(clause["semantic_key"])
        if key not in seen:
            seen.add(key)
            distinct.append(clause)
    if not distinct:
        return "适合日常使用"
    texts = [str(clause["text"]) for clause in distinct]
    if len(texts) == 1:
        return texts[0]
    if len(texts) == 2:
        return f"{texts[0]}，{texts[1]}"
    return f"{texts[0]}，{texts[1]}，并且{texts[2]}"


def _split_merchant_hint(public_hint: str | None) -> tuple[str, str]:
    if not public_hint:
        return "这家店", "店铺规则"
    if "的" in public_hint:
        alias, topic = public_hint.split("的", 1)
        return alias, topic or "店铺规则"
    return public_hint, "店铺规则"


def _knowledge_focus(category: str, public_hint: str | None) -> str:
    """Turn a catalog field label into an ordinary category-specific concern."""
    hint = public_hint or "关键使用体验"
    return _CATEGORY_KNOWLEDGE_FOCUS.get(category, {}).get(hint, hint)


def _merchant_policy_concern(category: str, topic: str) -> str:
    topic_key = next((key for key, label in _POLICY_TOPIC_LABELS.items() if label == topic), topic)
    return _CATEGORY_POLICY_CONCERNS.get(category, {}).get(topic_key, _POLICY_PUBLIC_CONCERNS.get(topic_key, topic))


def _multi_modifier(category: str, modifier: str) -> str:
    """Keep a two-candidate request grammatically aligned with its follow-on."""
    adjusted = modifier.rstrip("。；")
    for singular, plural in (
        ("一部", "两部"), ("一件", "两件"), ("一副", "两副"),
        ("一块", "两块"), ("一条", "两条"), ("一双", "两双"),
        ("一个", "两个"), ("一套", "两套"), ("一本", "两本"),
        ("一款", "两款"),
    ):
        adjusted = adjusted.replace(singular, plural)
    shared = _CATEGORY_MULTI_MODIFIERS[category]
    # A category's fixed two-item concern and a template modifier can express
    # the same user need in different words.  Preserve one utterance rather
    # than producing a longer but semantically duplicate request.
    semantic_terms = ("阅读计划", "日常使用", "耐用", "收纳", "舒适", "方便")
    if any(term in shared and term in adjusted for term in semantic_terms):
        return shared
    return f"{shared}。{adjusted}"


def _followup_text(atom: Mapping[str, Any] | None, *, category: str | None, variant_index: int) -> str:
    if atom is None:
        return (
            "另外我只考虑现在仍有货的商品。",
            "还有一点，麻烦确认当前库存，不要推荐已经卖完的选项。",
            "如果库存已经变化，我希望按现在能买到的情况来选。",
            "我只想看目前可以买到的商品。",
        )[variant_index % 4]
    field = str(atom.get("field", ""))
    if field == "stock":
        return (
            "另外我只考虑现在仍有货的商品。",
            "还有一点，麻烦确认当前库存，不要推荐已经卖完的选项。",
            "我只想看目前可以买到的商品。",
            "库存也请按现在的情况确认。",
        )[variant_index % 4]
    if field == "promotionEligible":
        return (
            "如果有正在进行的优惠活动，我更愿意选可以参加的商品。",
            "另外请优先考虑当前能参加促销的选项。",
            "能参加店里优惠活动的商品对我更合适。",
            "若有可用活动，优先留下能参加活动的商品。",
        )[variant_index % 4]
    return f"另外，{_atom_text(atom, category=category)}这一点也请注意。"


def _legacy_public_request(
    intent: str,
    category: str,
    ast: Mapping[str, Any],
    modifier: str,
    *,
    budget: float | None = None,
    followup: bool = False,
    dynamic: bool = False,
    public_hint: str | None = None,
    followup_field: str | None = None,
    stratum: str = "S0_SIMPLE",
    variant_index: int = 0,
) -> tuple[str, ...]:
    label = _CATEGORY_LABELS[category]
    item_label, multi_item_label, cart_label = _CATEGORY_QUANTIFIERS[category]
    atom_nodes = [
        node["atom"]
        for node in _flatten_atoms(ast)
        if node["atom"].get("field") not in {"category", "merchantId"}
        and not (intent == "product_finder" and budget is not None and node["atom"].get("field") == "price")
        and (not followup or node["atom"].get("field") != followup_field)
    ]
    clauses = [_public_clause(atom, category=category) for atom in atom_nodes[:3]]
    condition_text = _compose_public_clauses(clauses)
    modifier = modifier.rstrip("。；")
    budget_text = f"预算最多{_format_money(budget)}" if budget is not None else ""
    budget_joiner = "，" if budget_text else ""
    budget_sentence = f"{budget_text}；" if budget_text else ""
    budget_amount = _format_money(budget) if budget is not None else "合适范围"
    budget_fallback = budget_text or "可以适当灵活"
    # The price-oriented template already supplies the word “价格”; do not
    # splice the full “预算控制在…” phrase after it (which reads as a
    # duplicated/unnatural requirement such as “价格上预算控制在…”).
    budget_limit = f"不要超过{_format_money(budget)}" if budget is not None else "不要太高"
    alias, topic = _split_merchant_hint(public_hint)
    template_index = (variant_index + {"S0_SIMPLE": 0, "S1_FIXED_MULTISTEP": 2, "S2_OBSERVATION_DEPENDENT": 4}.get(stratum, 0))
    if intent == "product_finder":
        templates = (
            "我想挑{item_label}，{budget_text}{budget_joiner}{condition_text}，另外{modifier}。",
            "麻烦帮我看看{item_label}：{budget_text}{budget_joiner}{condition_text}；{modifier}。",
            "我最近准备买{item_label}，重点是{condition_text}。{budget_sentence}另外{modifier}。",
            "想在{label}里找合适的选择，要求{condition_text}，同时{modifier}。",
            "帮我从{label}中筛一下，{budget_text}{budget_joiner}{condition_text}，另外{modifier}。",
            "我在比较{label}，希望{condition_text}；预算方面{budget_fallback}，并且{modifier}。",
            "如果要买{item_label}，我会优先考虑{condition_text}的商品，另外{modifier}。",
            "请帮我选{item_label}，先看{condition_text}，价格上{budget_limit}，另外{modifier}。",
        )
        first = templates[template_index % len(templates)].format(**locals())
    elif intent == "knowledge_to_product":
        focus = _knowledge_focus(category, public_hint)
        focus_question = f"关于{focus}该怎么判断"
        templates = (
            "我准备买{item_label}，比较关注{focus}。能先讲讲应该看哪些标准，再帮我找合适的商品吗？{modifier}。",
            "挑{item_label}时我最在意{focus}，想先了解判断方法，再按这个思路筛选；{modifier}。",
            "我在选{item_label}，希望先弄清{focus_question}，再看看有哪些合适的商品。{modifier}。",
            "请帮我买{item_label}，但先别急着推荐，先说说{focus}通常要看什么；{modifier}。",
            "关于{item_label}的选择，我想重点了解{focus}，再根据可靠依据做决定。{modifier}。",
            "我对{item_label}的{focus}不太熟，想先听听选购建议，再筛出符合要求的商品；{modifier}。",
            "帮我看看{item_label}，先围绕{focus}说明选择思路，之后再比较商品。{modifier}。",
            "我想买{item_label}，请把{focus}相关的判断依据讲清楚，方便我挑选；{modifier}。",
        )
        first = templates[template_index % len(templates)].format(**locals())
    elif intent == "multi_product_merchant":
        policy_concern = _merchant_policy_concern(category, topic)
        multi_modifier = _multi_modifier(category, modifier)
        templates = (
            "我想在{alias}买{multi_item_label}，希望{condition_text}，也想了解{policy_concern}；{multi_modifier}。",
            "准备在{alias}一起买{multi_item_label}，希望{condition_text}，请顺便确认{policy_concern}。{multi_modifier}。",
            "能帮我在{alias}挑{multi_item_label}吗？我关注{condition_text}，另外想问问{policy_concern}；{multi_modifier}。",
            "我想在{alias}选{multi_item_label}，先按{condition_text}筛选，再了解{policy_concern}。{multi_modifier}。",
            "请从{alias}里找合适的{multi_item_label}，重点看{condition_text}和{policy_concern}，{multi_modifier}。",
            "这次想在{alias}选{multi_item_label}，希望{condition_text}，店铺方面主要关心{policy_concern}。{multi_modifier}。",
            "帮我比较{alias}的{multi_item_label}，商品要{condition_text}，同时也想了解{policy_concern}；{multi_modifier}。",
            "我打算在{alias}选{multi_item_label}，先确认{condition_text}，再问问{policy_concern}。{multi_modifier}。",
        )
        first = templates[template_index % len(templates)].format(**locals())
    elif intent == "coupon_budget":
        templates = (
            "我想在{alias}买{cart_label}，预算最多{budget_amount}，希望找得到合适的优惠组合；{modifier}。",
            "准备买{cart_label}，总价最好不超过{budget_amount}，优先看看{alias}能用的优惠。{modifier}。",
            "能帮我在{alias}挑{cart_label}吗？我把预算定在{budget_amount}，也想知道优惠后是否划算；{modifier}。",
            "我想买{cart_label}并尽量用上优惠，预算大约{budget_amount}，店铺先看{alias}。{modifier}。",
            "请帮我在{alias}组合购买{cart_label}，实付金额希望压在{budget_amount}以内；{modifier}。",
            "我在考虑{cart_label}，如果在{alias}使用优惠后能控制在{budget_amount}，会比较合适。{modifier}。",
            "帮我算算{alias}的{cart_label}怎么搭配更省，预算上限是{budget_amount}；{modifier}。",
            "我想在{alias}买{cart_label}，先按不超过{budget_amount}来考虑，再看哪张优惠真正适用。{modifier}。",
        )
        first = templates[template_index % len(templates)].format(**locals())
    else:
        templates = (
            "我想找{item_label}，要求{condition_text}，请按最新情况确认；{modifier}。",
            "帮我看看{item_label}，重点是{condition_text}。{modifier}。",
            "我准备买{item_label}，希望你按最新情况筛选，重点是{condition_text}；{modifier}。",
            "想选{item_label}，先看{condition_text}；{modifier}。",
            "请帮我找{item_label}，我比较在意{condition_text}；{modifier}。",
            "我在挑{item_label}，希望它符合这些要求：{condition_text}；{modifier}。",
            "能帮我确认{item_label}是否仍然合适吗？我希望{condition_text}，{modifier}。",
            "我想买{item_label}，请按最新情况看看{condition_text}的选择；{modifier}。",
        )
        first = templates[template_index % len(templates)].format(**locals())
    turns = [first]
    if followup:
        selected_followup = next((node["atom"] for node in _flatten_atoms(ast) if node["atom"].get("field") == followup_field), None) if followup_field else None
        hidden_atoms = [node["atom"] for node in _flatten_atoms(ast) if node["atom"].get("field") in {"price", "stock", "promotionEligible"}]
        turns.append(_followup_text(selected_followup or (hidden_atoms[-1] if hidden_atoms else None), category=category, variant_index=variant_index + 1))
    return tuple(turns)


class _DiscoursePlan(TypedDict):
    """Intent-level meaning plan kept until consumer wording is realized."""

    intent: str
    category: str
    item_label: str
    multi_item_label: str
    cart_label: str
    hard: list[_PublicClause]
    availability: list[_PublicClause]
    promotion: list[_PublicClause]
    soft_preference: str
    budget: float | None
    merchant_alias: str
    policy_concern: str
    knowledge_focus: str
    followup: bool
    followup_field: str | None
    variant_index: int


def _plan_consumer_utterance(
    intent: str,
    category: str,
    ast: Mapping[str, Any],
    modifier: str,
    *,
    budget: float | None,
    followup: bool,
    public_hint: str | None,
    followup_field: str | None,
    variant_index: int,
) -> _DiscoursePlan:
    """Group AST meaning by discourse purpose rather than source field order."""
    clauses = [
        _public_clause(node["atom"], category=category)
        for node in _flatten_atoms(ast)
        if str(node["atom"].get("field")) not in {"category", "merchantId"}
        and not (intent == "product_finder" and budget is not None and str(node["atom"].get("field")) == "price")
        and not (followup and str(node["atom"].get("field")) == followup_field)
    ]
    hard: list[_PublicClause] = []
    availability: list[_PublicClause] = []
    promotion: list[_PublicClause] = []
    for clause in clauses:
        if clause["role"] == "availability":
            availability.append(clause)
        elif clause["role"] == "promotion":
            promotion.append(clause)
        else:
            hard.append(clause)
    alias, topic = _split_merchant_hint(public_hint)
    item_label, multi_item_label, cart_label = _CATEGORY_QUANTIFIERS[category]
    return {
        "intent": intent,
        "category": category,
        "item_label": item_label,
        "multi_item_label": multi_item_label,
        "cart_label": cart_label,
        "hard": hard,
        "availability": availability,
        "promotion": promotion,
        "soft_preference": modifier.rstrip("。；"),
        "budget": budget,
        "merchant_alias": alias,
        "policy_concern": _merchant_policy_concern(category, topic),
        "knowledge_focus": _knowledge_focus(category, public_hint),
        "followup": followup,
        "followup_field": followup_field,
        "variant_index": variant_index,
    }


def _consumer_noun_phrase(plan: _DiscoursePlan) -> str:
    """Put category, product type and brand under one consumer-facing head."""
    category = plan["category"]
    brand = next((str(clause["value"]) for clause in plan["hard"] if clause["field"] == "brand"), "")
    item_type = next((str(_render_value("accessoryType", clause["value"])) for clause in plan["hard"] if clause["field"] == "accessoryType"), "")
    head = item_type or plan["item_label"]
    if item_type and category == "phone_accessory":
        head = item_type
    if brand:
        return f"{brand} 的{head}"
    return head


def _constraint_proposition(clause: _PublicClause, *, category: str) -> str:
    """Realize a remaining predicate as a sentence constituent, not a label."""
    field = clause["field"]
    if field in {"brand", "accessoryType"}:
        return ""
    if field == "fabric" and category == "jeans":
        return "偏好牛仔布面料"
    if field == "audience":
        return str(_render_value(field, clause["value"]))
    if field == "optionalSellerNote":
        return str(clause["text"])
    if field == "condition":
        return f"成色为{_render_value(field, clause['value'])}"
    if field in {"material", "color", "fit", "binding", "edition", "itemType", "variant"}:
        label = _FIELD_LABELS.get(field, field)
        return f"{label}为{_render_value(field, clause['value'])}"
    return str(clause["text"])


def _join_propositions(parts: Sequence[str]) -> str:
    values = [part for part in parts if part]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]}，同时{values[1]}"
    return f"{values[0]}，同时{values[1]}，并且{values[2]}"


def _realize_consumer_utterance(plan: _DiscoursePlan) -> tuple[str, ...]:
    """Surface an intent plan as ordinary shopping speech with grouped roles."""
    noun = _consumer_noun_phrase(plan)
    constraints = _join_propositions([_constraint_proposition(clause, category=plan["category"]) for clause in plan["hard"]])
    availability = "现在有货" if plan["availability"] else ""
    promotion = "最好能参加店内优惠活动" if plan["promotion"] else ""
    budget = _format_money(plan["budget"]) if plan["budget"] is not None else ""
    soft = plan["soft_preference"]
    intent = plan["intent"]
    if intent == "knowledge_to_product":
        question = f"买{plan['item_label']}时，{plan['knowledge_focus']}通常该怎么看"
        first = f"{question}？请先讲讲判断方法，再帮我找合适的商品。"
        if soft:
            first += soft + "。"
    elif intent == "multi_product_merchant":
        detail = _join_propositions([constraints, availability])
        first = f"我想在{plan['merchant_alias']}一次买{plan['multi_item_label']}"
        if detail:
            first += f"，想找{detail}的商品"
        first += f"。如果收到后有问题，{plan['policy_concern']}？"
        multi_soft = _multi_modifier(plan["category"], soft) if soft else ""
        if multi_soft:
            first += multi_soft + "。"
    elif intent == "coupon_budget":
        first = f"我想在{plan['merchant_alias']}买{plan['cart_label']}"
        if budget:
            first += f"，实付最好控制在{budget}以内"
        first += "。店里现在有哪些优惠可以用？"
        if soft:
            first += soft + "。"
    elif intent == "dynamic_replanning":
        detail = _join_propositions([constraints, availability])
        first = f"我想找{noun}"
        if detail:
            first += f"，希望{detail}"
        first += "。请按当前价格和库存再确认一次。"
        if soft:
            first += soft + "。"
    else:
        detail = _join_propositions([constraints, availability, promotion])
        first = f"我想买{noun}"
        if detail:
            first += f"，希望{detail}"
        if budget:
            first += f"，预算不超过{budget}"
        first += "。"
        if soft:
            first += soft + "。"
    turns = [first]
    if plan["followup"]:
        selected = next((clause for clause in plan["availability"] + plan["promotion"] + plan["hard"] if clause["field"] == plan["followup_field"]), None)
        turns.append(_followup_text({"field": selected["field"], "operator": "GTE", "value": selected["value"]} if selected else None, category=plan["category"], variant_index=plan["variant_index"] + 1))
    return tuple(turns)


def _public_request(
    intent: str,
    category: str,
    ast: Mapping[str, Any],
    modifier: str,
    *,
    budget: float | None = None,
    followup: bool = False,
    dynamic: bool = False,
    public_hint: str | None = None,
    followup_field: str | None = None,
    stratum: str = "S0_SIMPLE",
    variant_index: int = 0,
) -> tuple[str, ...]:
    """Plan consumer intent first; realize surface language only afterwards."""
    del dynamic, stratum
    plan = _plan_consumer_utterance(
        intent, category, ast, modifier, budget=budget, followup=followup,
        public_hint=public_hint, followup_field=followup_field,
        variant_index=variant_index,
    )
    return _realize_consumer_utterance(plan)


def _normalize_public_text(text: str) -> str:
    """Normalize variable entities for a global template-cluster audit."""
    normalized = str(text)
    variable_terms = list(_CATEGORY_LABELS.values()) + list(_MERCHANT_ALIAS_PREFIXES) + list(_FIELD_LABELS.values())
    for values in _ENUM_VALUE_LABELS.values():
        variable_terms.extend(values.values())
    for term in sorted({term for term in variable_terms if term}, key=len, reverse=True):
        normalized = normalized.replace(term, "<value>")
    normalized = re.sub(r"\d+(?:\.\d+)?", "<number>", normalized)
    normalized = re.sub(r"[A-Za-z]+(?:-[A-Za-z0-9]+)*", "<token>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def public_language_lint(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, int]:
    """Return corpus-level authoring smells, keyed by language class.

    This is deliberately a conservative regression lint, not a claim that
    automated checks replace a native-speaker review.  It makes the known
    register failures reproducible across the whole corpus and across
    categories, while the independent blinded review remains the release
    gate.
    """
    errors: Counter[str] = Counter()
    for row in rows:
        text = " ".join(str(turn.get("text", "")) for turn in row.get("turns", ()))
        if re.search(r"(?:品牌|为|约)[^。；，]*、", text):
            errors["field_string_join"] += 1
        if re.search(r"[A-Za-z]+品牌", text) or "条件是连接线" in text or "商品连接线" in text:
            errors["missing_head_or_copula"] += 1
        if re.search(r"(?:品牌|价格|现货)[^。；，]*、(?:能参加|目前有货|现在有货)", text):
            errors["mixed_role_join"] += 1
        if re.search(r"\d+元\d+(?:角|分)", text):
            errors["ledger_money"] += 1
        if "最好使用简单" in text or "商家能用的优惠" in text or "希望商品" in text:
            errors["missing_collocation"] += 1
        if "一套里有多少件该怎么比较" in text or "更适合入门读者" in text:
            errors["knowledge_fragment"] += 1
        if "牛仔裤，面料为牛仔布" in text:
            errors["category_redundancy"] += 1
        if "两本书都希望适合当前的阅读计划" in text and "两本适合当前阅读计划" in text:
            errors["semantic_duplicate"] += 1
    return dict(sorted(errors.items()))


def public_template_register_audit(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, int]:
    """Named full-corpus discourse failures; r9 is the frozen red probe."""
    errors: Counter[str] = Counter()
    for row in rows:
        text = " ".join(str(turn.get("text", "")) for turn in row.get("turns", ()))
        if text.count("要选") >= 2 or "品牌要选" in text:
            errors["typed_discourse_composition"] += 1
        if re.search(r"(?:品牌要选|商品要品牌|我比较在意品牌)", text):
            errors["brand_head_copula"] += 1
        if re.search(r"(?:品牌要选|价格[^。]*|现在有货)[，、].*(?:促销|有货|价格)", text):
            errors["mixed_condition_grammar"] += 1
        if re.search(r"\d+元\d+(?:角|分)", text):
            errors["conversational_money"] += 1
        if "最好使用简单" in text or "条件是连接线" in text or "商品连接线" in text:
            errors["collocation_complement"] += 1
        if "一套里有多少件该怎么比较" in text or "更适合入门读者，现在有货" in text:
            errors["knowledge_focus_grammar"] += 1
        if re.search(r"牛仔裤[^。]*面料要选牛仔布", text):
            errors["category_inherent_redundancy"] += 1
        if "两本书都希望适合当前的阅读计划" in text and "两本适合当前阅读计划" in text:
            errors["semantic_deduplication"] += 1
        if text.count("要选") >= 2 or "商品要品牌要选" in text or "希望商品" in text:
            errors["template_register"] += 1
    return dict(sorted(errors.items()))


def _choose_soldout_filter(
    category_rows: Sequence[Mapping[str, Any]],
    category: str,
    e0_rows: Mapping[str, Mapping[str, Any]],
    e2_rows: Mapping[str, Mapping[str, Any]],
    slot: int,
) -> tuple[Mapping[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Find a complete predicate whose E0 universe is sold out at E2.

    This is used only for the red-team no-answer case.  It deliberately
    enumerates predicate groups, rather than choosing a product and writing a
    query around it.
    """
    fields = _known_static_fields(category_rows)
    candidates: list[tuple[int, tuple[str, ...], tuple[Any, ...], Mapping[str, Any], tuple[str, ...], tuple[str, ...]]] = []
    for field_count in (1, 2, 3):
        for field_tuple in combinations(fields, field_count):
            for values, _group in _group_candidates(category_rows, field_tuple, min_count=1, max_count=256):
                ast = _all_atoms([_atom("category", "EQ", category)] + [_atom(field, "EQ", value) for field, value in zip(field_tuple, values)] + [_atom("stock", "GTE", 1)])
                start = _universe(_group, ast, e0_rows)
                terminal = _universe(_group, ast, e2_rows)
                if start and not terminal:
                    candidates.append((len(start), field_tuple, values, ast, start, terminal))
    if not candidates:
        raise CommerceWorldError(f"cannot derive sold-out predicate for {category}")
    candidates.sort(key=lambda item: (item[0], item[1], tuple(_token(value) for value in item[2]), item[4]))
    chosen = candidates[slot % len(candidates)]
    return chosen[3], chosen[4], chosen[5]


def _source_provenance(bindings: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Project authoring bindings into the strict private-oracle schema."""
    result: list[Mapping[str, Any]] = []
    seen: set[tuple[str, str, Any]] = set()
    for binding in bindings:
        source_ref = str(binding.get("sourceRef", ""))
        fact_tier = str(binding.get("factTier", ""))
        field = str(binding.get("field", ""))
        if not source_ref or fact_tier not in {"synthetic_fixture", "source_claim"} or not field:
            continue
        key = (source_ref, field, binding.get("value"))
        if key in seen:
            continue
        seen.add(key)
        result.append({"sourceRef": source_ref, "factTier": fact_tier, "field": field})
    return result


def _same_value(left: Any, right: Any) -> bool:
    return _token(left) == _token(right)


def _public_value_tokens(field: str, value: Any, *, category: str | None, public_hint: str | None) -> tuple[str, ...]:
    """Return values that are actually exposed by the authored public text.

    Internal identifiers and document-derived predicates are deliberately not
    treated as public merely because the query mentions the corresponding
    *field*.  For example, ``电池健康度`` exposes a topic, not the knowledge
    rule's hidden value ``84``; ``受控商家 001`` exposes a display label, not
    ``merchant-001``.
    """
    tokens: list[str] = []
    if field == "category" and category:
        tokens.append(str(_CATEGORY_LABELS.get(category, category)))
    if field == "merchantId" and public_hint:
        tokens.append(str(public_hint))
    if field not in {"category", "merchantId"}:
        if value is not None:
            tokens.append(str(value))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    tokens.append(f"{float(value):.2f}")
                except (TypeError, ValueError):
                    pass
    return tuple(token for token in tokens if token)


def _is_public_exact_value(field: str, value: Any, query_text: str, *, category: str | None, public_hint: str | None) -> bool:
    """Check exact-value visibility without mistaking a topic for a value."""
    return any(token in query_text for token in _public_value_tokens(field, value, category=category, public_hint=public_hint))


def _public_label(field: str, *, category: str | None, public_hint: str | None) -> str:
    if field == "category" and category:
        return str(_CATEGORY_LABELS.get(category, category))
    if field == "merchantId" and public_hint:
        return str(public_hint)
    return str(public_hint or _FIELD_LABELS.get(field, field))


def _build_requirement_bindings(
    scenario_id: str,
    ast: Mapping[str, Any],
    public_turns: Sequence[Mapping[str, Any]],
    bindings: Sequence[Mapping[str, Any]],
    dependencies: Sequence[Mapping[str, Any]],
    *,
    followup_field: str | None = None,
    public_hint: str | None = None,
    category: str | None = None,
) -> list[Mapping[str, Any]]:
    """Build an auditable requirement-to-source graph for one authored case.

    ``sourceTypes`` is intentionally not accepted as an input.  Every type in
    the returned graph is backed by an exact source binding, including the
    public query turn and any second-turn AST atom.
    """
    flattened = tuple(node["atom"] for node in _flatten_atoms(ast))
    result: list[Mapping[str, Any]] = []
    query_text = " ".join(str(turn.get("text", "")) for turn in public_turns)

    def exact_candidates(field: str, value: Any, operator: str, preferred: Sequence[str] = ()) -> list[Mapping[str, Any]]:
        def satisfies(actual: Any) -> bool:
            try:
                if operator == "EQ":
                    return _same_value(actual, value)
                if operator == "NEQ":
                    return not _same_value(actual, value)
                if operator == "IN":
                    return isinstance(value, (list, tuple, set)) and actual in value
                if operator == "NOT_IN":
                    return isinstance(value, (list, tuple, set)) and actual not in value
                if operator == "CONTAINS":
                    return isinstance(actual, str) and str(value) in actual
                if operator == "LTE":
                    return float(actual) <= float(value)
                if operator == "GTE":
                    return float(actual) >= float(value)
            except (TypeError, ValueError):
                return False
            return False

        rows = [row for row in bindings if str(row.get("field", "")) == field and satisfies(row.get("value")) and str(row.get("sourceRef", ""))]
        rank = {source_type: index for index, source_type in enumerate(preferred)}
        return sorted(rows, key=lambda row: (rank.get(str(row.get("sourceType")), len(rank) + 1), str(row.get("sourceType", "")), str(row.get("sourceRef", ""))))

    for index, atom in enumerate(flattened, 1):
        field = str(atom.get("field", ""))
        value = atom.get("value")
        operator = str(atom.get("operator", ""))
        requirement_id = f"req-{index:02d}"
        evidence = exact_candidates(field, value, operator, ("knowledge", "merchant_policy", "coupon", "dynamic_offer", "product_fact"))
        evidence_status = "EXACT_SATISFIES"
        if not evidence and field == "price":
            # A coupon threshold is the authorized source for the price gate;
            # retain its exact field/sourceRef rather than inventing a product
            # citation with the same numeric value.
            evidence = [row for row in bindings if str(row.get("sourceType")) == "coupon" and str(row.get("field")) == "threshold" and _same_value(row.get("value"), value) and str(row.get("sourceRef", ""))]
        if not evidence:
            # A red-team unknown atom is evidenced by the frozen fact's
            # explicit known=false/value=null record.  It must remain
            # fail-closed; absence of a satisfying value is the point of the
            # contract, not permission to invent one.
            evidence = [row for row in bindings if str(row.get("field", "")) == field and row.get("value") is None and str(row.get("sourceRef", ""))]
            if evidence:
                evidence_status = "UNKNOWN_FAIL_CLOSED"
        if not evidence:
            raise CommerceWorldError(f"{scenario_id}: no exact evidence binding for AST atom {field}={value!r}")
        dependency_match = next(
            (
                dep
                for dep in dependencies
                if str(dep.get("field")) == field and _same_value(dep.get("value"), value)
            ),
            None,
        )
        coupon_rule = next(
            (
                row
                for row in bindings
                if field == "price"
                and operator in {"GTE", "LTE", "EQ"}
                and str(row.get("sourceType")) == "coupon"
                and str(row.get("field")) == "threshold"
                and _same_value(row.get("value"), value)
                and str(row.get("sourceRef", ""))
            ),
            None,
        )
        # The query binding records only what the user actually exposed.  A
        # public topic/label is not permission to write the private predicate
        # value into a query-origin binding.
        public_value_visible = _is_public_exact_value(field, value, query_text, category=category, public_hint=public_hint)
        # Dependency values and coupon thresholds are private rules even if a
        # coincidental number happens to occur elsewhere in the query (e.g.
        # the budget cap equals a coupon threshold).
        if dependency_match is not None or coupon_rule is not None:
            public_value_visible = False
        if field in {"category", "merchantId"}:
            # These use a display label in Chinese; the internal category or
            # merchant key must remain a derived/private value.
            public_value_visible = bool(_public_value_tokens(field, value, category=category, public_hint=public_hint))
        public_origin_value = value if public_value_visible and field not in {"category", "merchantId"} else None
        public_origin = {
            "sourceType": "query",
            "role": "public_origin",
            "turnId": str(public_turns[0].get("turnId", "turn-01")),
            "field": field,
            "value": public_origin_value,
            "publicValue": (
                _public_value_tokens(field, value, category=category, public_hint=public_hint)[0]
                if public_value_visible and _public_value_tokens(field, value, category=category, public_hint=public_hint)
                else _public_label(field, category=category, public_hint=public_hint)
            ),
            "publicValueVisible": bool(public_value_visible),
            "sourceRef": f"synthetic://commerce-pilot/{scenario_id}/query/turn-01/{field}",
            "factTier": "synthetic_fixture",
        }
        origins: list[Mapping[str, Any]] = [public_origin]
        if followup_field == field and len(public_turns) > 1:
            origins.append({
                "sourceType": "user_followup",
                "role": "clarification_origin",
                "turnId": str(public_turns[-1].get("turnId", "turn-02")),
                "field": field,
                "value": value,
                "sourceRef": f"synthetic://commerce-pilot/{scenario_id}/followup/{field}",
                "factTier": "synthetic_fixture",
            })
        elif dependency_match is not None:
            document = dependency_match
            document_binding = next((row for row in evidence if str(row.get("sourceType")) == "knowledge"), evidence[0])
            origins.append({**dict(document_binding), "role": "document_rule", "documentId": document.get("documentId")})
        elif not public_value_visible:
            # Hidden exact values must be derived from the frozen fact that
            # supplies the predicate, never attributed to the natural-language
            # query.  Coupon thresholds are especially important here: the
            # public budget is not the coupon's private threshold.
            derivation = dict(coupon_rule or evidence[0])
            source_type = str(derivation.get("sourceType", ""))
            derivation["role"] = {
                "knowledge": "document_rule",
                "merchant_policy": "document_rule",
                "coupon": "coupon_rule",
            }.get(source_type, "predicate_derived")
            origins.append(derivation)
        # When an exact predicate is public, the query binding itself is the
        # origin; evidenceBindings still retain the independent frozen fact.
        public_observed = query_text if public_origin else None
        result.append({
            "requirementId": requirement_id,
            "kind": "ast_atom",
            "field": field,
            "operator": operator,
            "value": value,
            "originBindings": origins,
            "evidenceBindings": [dict(evidence[0])],
            "evidenceStatus": evidence_status,
            "publicTextObserved": public_observed,
        })

    for dependency_index, dependency in enumerate(dependencies, 1):
        document_type = str(dependency.get("documentType"))
        field = str(dependency.get("field"))
        value = dependency.get("value")
        evidence = exact_candidates(field, value, str(dependency.get("operator", "")), (document_type,))
        if not evidence:
            raise CommerceWorldError(f"{scenario_id}: no exact evidence binding for {document_type} dependency {field}")
        # A document dependency is requested by topic/intent in public text;
        # its exact value is a rule derived from the audited document.
        origin = {
            "sourceType": "query",
            "role": "public_origin",
            "turnId": str(public_turns[0].get("turnId", "turn-01")),
            "field": field,
            "value": None,
            "publicValue": _public_label(field, category=category, public_hint=public_hint),
            "publicValueVisible": True,
            "sourceRef": f"synthetic://commerce-pilot/{scenario_id}/query/turn-01/{document_type}",
            "factTier": "synthetic_fixture",
        }
        document_binding = next((row for row in evidence if str(row.get("sourceType")) == document_type), evidence[0])
        exact_origin = {**dict(document_binding), "role": "document_rule", "documentId": dependency.get("documentId")}
        result.append({
            "requirementId": f"doc-{dependency_index:02d}",
            "kind": "document_dependency",
            "documentType": document_type,
            "documentId": str(dependency.get("documentId", "")),
            "field": field,
            "operator": str(dependency.get("operator", "")),
            "value": value,
            "originBindings": [origin, exact_origin],
            "evidenceBindings": [dict(evidence[0])],
            "publicTextObserved": query_text,
        })
    return result


def _fault_record(
    scenario_id: str,
    world: Mapping[str, Any],
    point: str | None,
    *,
    mutation: Mapping[str, Any] | None = None,
    invariant: str | None = None,
) -> Mapping[str, Any]:
    injections: list[Mapping[str, Any]] = []
    if point:
        injections.append({
            "injectionId": f"inject-{scenario_id.lower().replace('_', '-').replace(' ', '-')}",
            "point": point,
            "trigger": "after_first_observation",
            "mutation": dict(mutation or {"kind": point}),
            "expectedInvariant": invariant or {
                "empty_result": "requery_or_abstain",
                "stale_price": "recalculate_or_abstain",
                "stock_change": "do_not_sell_out",
                "evidence_conflict": "cite_one_revision",
                "tool_timeout": "bounded_retry_or_abstain",
                "checkpoint_restart": "recover_or_fail_closed",
            }[point],
            "repeatCount": 5,
        })
    return {
        "scenarioId": scenario_id,
        "schemaVersion": SCHEMA_VERSIONS["fault"],
        "worldId": world["manifest"]["worldId"],
        "catalogRevision": world["manifest"]["catalogRevision"],
        "environmentRevision": "E0",
        "faultStatus": "READY" if injections else "NONE",
        "injections": injections,
    }


def _dependency_citation(doc: Mapping[str, Any], dependency: Mapping[str, Any]) -> Mapping[str, Any]:
    document_type = str(dependency["documentType"])
    field = str(dependency["field"])
    if document_type == "knowledge":
        value = doc.get("attributeValue")
    else:
        value = (doc.get("terms") or {}).get(field)
    citation: dict[str, Any] = {
        "evidenceId": f"doc-{doc.get('knowledgeId', doc.get('policyId'))}",
        "documentType": document_type,
        "documentId": str(doc.get("knowledgeId", doc.get("policyId"))),
        "field": field,
        "value": value,
        "sourceRef": str(doc.get("sourceRef")),
        "revision": "catalog-controlled-7bfa1ae237d235a2",
        "factTier": str(doc.get("factTier", "synthetic_fixture")),
    }
    if document_type == "policy":
        citation["documentVersion"] = str(doc.get("version"))
    return citation


def _build_product_evidence(product: Mapping[str, Any], ast: Mapping[str, Any], *, e0_row: Mapping[str, Any], terminal_row: Mapping[str, Any] | None = None, include_dynamic: bool = False) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Create private authoring evidence bindings for one representative item."""
    product_bindings: list[Mapping[str, Any]] = []
    for node in _flatten_atoms(ast):
        atom = node["atom"]
        field = str(atom["field"])
        if field in _DYNAMIC_FIELDS:
            row = terminal_row or e0_row
            product_bindings.append(_source_binding(product, field, environment_row=row, source_type="dynamic_offer"))
        else:
            product_bindings.append(_source_binding(product, field, source_type="product_fact"))
    if include_dynamic and not any(binding.get("sourceType") == "dynamic_offer" for binding in product_bindings):
        product_bindings.append(_source_binding(product, "price", environment_row=e0_row, source_type="dynamic_offer", role="independent_observation"))
    return product_bindings, _source_provenance(product_bindings)


def _cart_necessity(
    product_ids: Sequence[str],
    products: Mapping[str, Mapping[str, Any]],
    e0_rows: Mapping[str, Mapping[str, Any]],
    coupon: Mapping[str, Any],
) -> tuple[float, Mapping[str, Any]]:
    prices = [float(e0_rows[product_id]["price"]) for product_id in product_ids]
    amount = float(coupon.get("amount", 0.0))
    if not prices or amount <= 0:
        raise CommerceWorldError("coupon does not create a non-trivial budget witness")
    max_total = round(max(0.0, min(price - amount for price in prices)), 2)
    no_coupon_valid = sum(1 for price in prices if price <= max_total + 1e-9)
    coupon_valid = sum(1 for price in prices if max(0.0, price - min(amount, float(coupon.get("cap", amount)))) <= max_total + 1e-9)
    if no_coupon_valid != 0 or coupon_valid <= 0:
        raise CommerceWorldError("coupon necessity proof could not be established")
    return max_total, {"noCouponValidCount": no_coupon_valid, "couponValidCount": coupon_valid}


def _coupon_discount(coupon: Mapping[str, Any], subtotal: float) -> float:
    """Recompute one coupon's discount for the canonical one-item cart."""
    threshold = float(coupon.get("threshold", math.inf))
    if not math.isfinite(threshold) or subtotal < threshold:
        return 0.0
    if str(coupon.get("kind")) == "fixed":
        raw = float(coupon.get("amount", 0.0))
    elif str(coupon.get("kind")) == "percent":
        raw = subtotal * float(coupon.get("amount", 0.0))
    else:
        raise CommerceWorldError(f"unsupported coupon kind {coupon.get('kind')!r}")
    cap = float(coupon.get("cap", raw))
    if not math.isfinite(raw) or not math.isfinite(cap) or raw < 0 or cap < 0:
        raise CommerceWorldError(f"invalid coupon arithmetic for {coupon.get('couponId')}")
    return round(min(subtotal, raw, cap), 2)


def _canonical_cart_solution(
    product_id: str,
    coupon: Mapping[str, Any] | None,
    products: Mapping[str, Mapping[str, Any]],
    environment_rows: Mapping[str, Mapping[str, Any]],
    *,
    max_total: float | None = None,
    price_delta: float = 0.0,
    environment_revision: str = "E0",
) -> Mapping[str, Any] | None:
    product = products.get(str(product_id))
    offer = environment_rows.get(str(product_id))
    if product is None or offer is None or str(offer.get("environmentRevision")) != environment_revision or int(offer.get("stock", 0)) < 1:
        return None
    subtotal = round(float(offer.get("price", 0.0)) + price_delta, 2)
    if coupon is None:
        coupon_ids: list[str] = []
        discount = 0.0
    else:
        if str(coupon.get("environmentRevision")) != environment_revision or str(product.get("merchantId")) != str(coupon.get("merchantId")):
            return None
        coupon_ids = [str(coupon["couponId"])]
        discount = _coupon_discount(coupon, subtotal)
    total = round(max(0.0, subtotal - discount), 2)
    if max_total is not None and total > float(max_total) + 1e-9:
        return None
    return {
        "selectedProductIds": [str(product_id)],
        "appliedCouponIds": coupon_ids,
        "environmentRevision": environment_revision,
        "subtotal": subtotal,
        "discount": discount,
        "total": total,
    }


def _coupon_solution_universe(
    product_ids: Sequence[str],
    coupon: Mapping[str, Any] | None,
    products: Mapping[str, Mapping[str, Any]],
    environment_rows: Mapping[str, Mapping[str, Any]],
    *,
    max_total: float | None = None,
    price_delta: float = 0.0,
    environment_revision: str = "E0",
) -> list[Mapping[str, Any]]:
    rows = [
        solution
        for product_id in sorted({str(item) for item in product_ids})
        if (solution := _canonical_cart_solution(product_id, coupon, products, environment_rows, max_total=max_total, price_delta=price_delta, environment_revision=environment_revision)) is not None
    ]
    return sorted(rows, key=lambda row: canonical_bytes(row))


def _coupon_solution_audit(
    product_ids: Sequence[str],
    coupon: Mapping[str, Any],
    products: Mapping[str, Mapping[str, Any]],
    environment_rows: Mapping[str, Mapping[str, Any]],
) -> tuple[float, list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Independently derive maxTotal, no-coupon and allowed-coupon rows."""
    all_coupon_rows = _coupon_solution_universe(product_ids, coupon, products, environment_rows)
    if not all_coupon_rows:
        raise CommerceWorldError(f"coupon {coupon.get('couponId')} has no valid cart solution")
    max_total = round(min(float(row["total"]) for row in all_coupon_rows), 2)
    no_coupon_rows = _coupon_solution_universe(product_ids, None, products, environment_rows, max_total=max_total)
    coupon_rows = _coupon_solution_universe(product_ids, coupon, products, environment_rows, max_total=max_total)
    if no_coupon_rows or not coupon_rows:
        raise CommerceWorldError(f"coupon necessity proof failed for {coupon.get('couponId')}")
    return max_total, no_coupon_rows, coupon_rows


def _author_pilot_96(world: Mapping[str, Any]) -> Mapping[str, Any]:
    """Author the executable 96-cell pilot from an audited world.

    The function keeps the private construction path separate from public text
    materialization.  It returns records only; ``write_pilot_96`` is the
    filesystem boundary.
    """
    manifest = world["manifest"]
    products = tuple(world["products"])
    product_by_id = {str(row["productId"]): row for row in products}
    by_category = {category: tuple(sorted((row for row in products if str(row.get("category")) == category), key=lambda row: str(row["productId"]))) for category in CATEGORIES}
    env = world["environments"]
    e0 = env["E0"]
    e2 = env["E2"]
    categories_seen: dict[str, int] = {category: 0 for category in CATEGORIES}
    inputs: list[Mapping[str, Any]] = []
    oracles: list[Mapping[str, Any]] = []
    faults: list[Mapping[str, Any]] = []
    predictions: list[Mapping[str, Any]] = []
    audits: list[Mapping[str, Any]] = []

    def append_case(
        *,
        scenario_id: str,
        split: str,
        intent: str,
        stratum: str,
        category: str,
        ast: Mapping[str, Any],
        solution_type: str,
        acceptable: Sequence[str] = (),
        bundles: Sequence[Sequence[str]] = (),
        cart_rules: Mapping[str, Any] | None = None,
        dependencies: Sequence[Mapping[str, Any]] = (),
        terminal_classes: Sequence[str] = ("SUCCESS",),
        allow_no_answer: bool = False,
        environment_sequence: Sequence[str] = (),
        required_points: Sequence[str] = (),
        fault: Mapping[str, Any] | None = None,
        bindings: Sequence[Mapping[str, Any]] = (),
        audit_extra: Mapping[str, Any] | None = None,
        start_universe: Sequence[Any] | None = None,
        terminal_universe: Sequence[Any] | None = None,
        public_budget: float | None = None,
        public_hint: str | None = None,
        force_followup: bool = False,
        followup_field: str | None = None,
        dynamic_public: bool = False,
        public_entity_bindings: Sequence[Mapping[str, Any]] = (),
        red: bool = False,
    ) -> None:
        terminal_revision = str(environment_sequence[-1]) if environment_sequence else "E0"
        all_bindings = list(bindings)
        representative_ids = list(acceptable)
        if not representative_ids and bundles:
            representative_ids = list(bundles[0])
        if representative_ids:
            representative = product_by_id[sorted(representative_ids)[0]]
            all_bindings.extend(_build_product_evidence(representative, ast, e0_row=e0[str(representative["productId"])], terminal_row=env[terminal_revision].get(str(representative["productId"])), include_dynamic=(stratum != "S0_SIMPLE" or intent == "dynamic_replanning"))[0])
        conditions: dict[str, Any] = {
            "terminalClasses": list(terminal_classes),
            "solutionType": solution_type,
            "constraintAst": ast,
            "evidence": {"minCitations": 1 if acceptable or bundles else 0, "forbidUnsupportedClaims": True},
        }
        if acceptable:
            conditions["acceptableProductIds"] = list(sorted(set(str(item) for item in acceptable)))
        if bundles:
            conditions["acceptableBundles"] = [list(bundle) for bundle in bundles]
            conditions["acceptableProductIds"] = list(sorted({str(item) for bundle in bundles for item in bundle}))
        if cart_rules is not None:
            conditions["cartRules"] = dict(cart_rules)
        if dependencies:
            conditions["documentDependencies"] = [dict(item) for item in dependencies]
        if allow_no_answer:
            conditions["allowNoAnswer"] = True
        if required_points:
            conditions["requiredObservationPoints"] = list(required_points)
        if environment_sequence:
            conditions["environmentSequence"] = list(environment_sequence)
        if conditions["evidence"]["minCitations"] and stratum != "S0_SIMPLE":
            conditions["evidence"]["requiredFields"] = sorted({str(node["atom"]["field"]) for node in _flatten_atoms(ast) if str(node["atom"]["field"]) not in {"category", "merchantId"}})
        oracle = {
            "scenarioId": scenario_id,
            "schemaVersion": SCHEMA_VERSIONS["oracle"],
            "worldId": manifest["worldId"],
            "catalogRevision": manifest["catalogRevision"],
            "environmentRevision": "E0",
            "split": split,
            "intentFamily": intent,
            "complexityStratum": stratum,
            "oracleStatus": "READY",
            "designReason": "oracle-first: derive constraints, dependencies, complete frozen universe, then materialize Chinese public text; all facts are controlled synthetic_fixture.",
            "successConditions": conditions,
            "sourceProvenance": _source_provenance(all_bindings),
        }
        if fault is None:
            fault = _fault_record(scenario_id, world, None)
        public_turns = _public_request(
            intent,
            category,
            ast,
            _CATEGORY_MODIFIERS[category][categories_seen[category] % len(_CATEGORY_MODIFIERS[category])],
            budget=public_budget,
            followup=force_followup and bool(followup_field),
            dynamic=dynamic_public,
            public_hint=public_hint,
            followup_field=followup_field,
            stratum=stratum,
            variant_index=len(inputs),
        )
        meaningful_followup = bool(
            len(public_turns) > 1
            and followup_field
            and any(str(node["atom"].get("field")) == followup_field for node in _flatten_atoms(ast))
            and intent in {"product_finder", "knowledge_to_product"}
            and stratum != "S0_SIMPLE"
        )
        if meaningful_followup:
            all_bindings.append({"sourceType": "user_followup", "role": "clarification_constraint", "field": followup_field, "value": next(node["atom"].get("value") for node in _flatten_atoms(ast) if str(node["atom"].get("field")) == followup_field), "sourceRef": f"synthetic://commerce-pilot/{scenario_id}/followup", "factTier": "synthetic_fixture"})
        public_turn_rows = [{"turnId": f"turn-{index + 1:02d}", "role": "user", "text": text} for index, text in enumerate(public_turns)]
        requirement_bindings = _build_requirement_bindings(
            scenario_id,
            ast,
            public_turn_rows,
            all_bindings,
            dependencies,
            followup_field=followup_field if meaningful_followup else None,
            public_hint=public_hint,
            category=category,
        )
        for requirement in requirement_bindings:
            all_bindings.extend(requirement.get("originBindings", ()))
            all_bindings.extend(requirement.get("evidenceBindings", ()))
        oracle["sourceProvenance"] = _source_provenance(all_bindings)
        public = {
            "scenarioId": scenario_id,
            "schemaVersion": SCHEMA_VERSIONS["input"],
            "worldId": manifest["worldId"],
            "catalogRevision": manifest["catalogRevision"],
            "environmentRevision": "E0",
            "language": "zh-CN",
            "turns": public_turn_rows,
            "publicEnvironmentRefs": ["catalog", "offers"],
            "sessionSeed": f"pilot-{split[:3]}-{len(inputs) + 1:03d}".lower(),
        }
        prediction = {
            "predictionId": f"not-run-{scenario_id}",
            "runId": f"not-run-{scenario_id}",
            "scenarioId": scenario_id,
            "schemaVersion": SCHEMA_VERSIONS["prediction"],
            "worldId": manifest["worldId"],
            "catalogRevision": manifest["catalogRevision"],
            "environmentRevision": "E0",
            "runStatus": "NOT_RUN",
            "terminalClass": "ABSTAIN_OR_EXPLAIN",
            "selectedProductIds": [],
            "answerText": "未运行：仅冻结设计，不进入评分。",
            "claims": [],
            "claimExtraction": {"provenance": "not-run", "version": "v1", "claimsComplete": True},
            "evidenceCitations": [],
            "toolTrace": [],
            "resourceUsage": {"modelCalls": 0, "toolCalls": 0, "latencyMs": 0, "inputTokens": 0, "outputTokens": 0},
        }
        validate_record(oracle, "oracle")
        validate_record(public, "input")
        validate_record(fault, "fault")
        validate_record(prediction, "prediction")
        start_value = list(start_universe) if start_universe is not None else (list(acceptable) if acceptable else [list(bundle) for bundle in bundles])
        terminal_value = list(terminal_universe) if terminal_universe is not None else (list(acceptable) if acceptable else [list(bundle) for bundle in bundles])
        start_value = sorted(start_value, key=canonical_bytes)
        terminal_value = sorted(terminal_value, key=canonical_bytes)
        audit = {
            "scenarioId": scenario_id,
            "oracleFirst": True,
            "split": split,
            "intentFamily": intent,
            "complexityStratum": stratum,
            "category": category,
            # This is deliberately public-only input to R11's authored-text
            # registry.  It captures consumer meaning, never the scenario
            # identity, split, private oracle value or generation position.
            "publicSemanticContext": {
                "category": category,
                "consumerGoal": intent,
                "softPreference": _CATEGORY_MODIFIERS[category][categories_seen[category] % len(_CATEGORY_MODIFIERS[category])],
                "budget": public_budget,
                "merchantHint": public_hint,
                "followup": meaningful_followup,
                "followupField": followup_field if meaningful_followup else None,
            },
            "sourceBindings": [dict(item) for item in all_bindings],
            "sourceTypes": sorted({str(item.get("sourceType")) for item in all_bindings if str(item.get("sourceType", ""))}),
            "requirementBindings": requirement_bindings,
            "publicEntityBindings": [dict(item) for item in public_entity_bindings],
            "languageMaterialization": {
                "templateVariant": len(inputs),
                "normalizedTemplate": _normalize_public_text(" ".join(row["text"] for row in public_turn_rows)),
                "turnCount": len(public_turn_rows),
            },
            "startUniverse": {"count": len(start_value), "sha256": _stable_digest(start_value)},
            "terminalUniverse": {"count": len(terminal_value), "sha256": _stable_digest(terminal_value)},
            "completeUniverse": True,
            "faultDerivedTerminal": False,
            "followupChangesConstraint": meaningful_followup,
            "runnerContract": {
                "requiredPostInjectionAuthoritativeRequery": bool(required_points),
                "requiresAfterObservationDigest": bool(required_points),
                "bareInjectionTraceAccepted": False,
                "verificationStatus": "AUTHORING_CONTRACT_ONLY",
            },
        }
        if audit_extra:
            audit.update(dict(audit_extra))
        inputs.append(public); oracles.append(oracle); faults.append(fault); predictions.append(prediction); audits.append(audit)
        categories_seen[category] += 1

    def author_regular(global_index: int, split: str, intent: str, stratum: str, ordinal: int) -> None:
        category = CATEGORIES[global_index % len(CATEGORIES)]
        category_rows = by_category[category]
        case_id = f"ACB-V1-{('DEV' if split == 'development' else 'VAL')}-case-{global_index + 1:03d}"
        s0, s2 = e0, e2
        if intent == "product_finder":
            extra: list[Mapping[str, Any]] = []
            threshold: float | None = None
            if stratum == "S1_FIXED_MULTISTEP":
                category_ids = {str(item["productId"]) for item in category_rows}
                threshold = round(sorted(float(s0[product_id]["price"]) for product_id in category_ids)[(len(category_rows) * 3) // 4], 2)
                extra.append(_atom("price", "LTE", threshold))
            if stratum == "S2_OBSERVATION_DEPENDENT":
                extra.append(_atom("stock", "GTE", 1))
            if stratum != "S0_SIMPLE":
                extra.append(_atom("promotionEligible", "EQ", True))
            ast, _group, start, terminal = _choose_static_group(category_rows, category, ordinal, min_count=1, max_count=256, field_count=2, extra_atoms=extra, start_rows=s0, terminal_rows=s2 if stratum == "S2_OBSERVATION_DEPENDENT" else s0, require_delta=stratum == "S2_OBSERVATION_DEPENDENT")
            if not terminal:
                raise CommerceWorldError(f"product finder {category} has no complete terminal universe")
            point = _S2_POINTS[intent][0] if stratum == "S2_OBSERVATION_DEPENDENT" else None
            fault = _fault_record(case_id, world, point, mutation={"kind": point, "terminalAction": "requery_or_abstain"} if point else None)
            append_case(scenario_id=case_id, split=split, intent=intent, stratum=stratum, category=category, ast=ast, solution_type="single_product", acceptable=terminal, terminal_classes=("SUCCESS",), environment_sequence=("E0", "E2") if stratum == "S2_OBSERVATION_DEPENDENT" else (), required_points=(point,) if point else (), fault=fault, public_budget=threshold if stratum == "S1_FIXED_MULTISTEP" else None, force_followup=stratum != "S0_SIMPLE", followup_field="promotionEligible" if stratum == "S1_FIXED_MULTISTEP" else ("stock" if stratum == "S2_OBSERVATION_DEPENDENT" else None), dynamic_public=stratum != "S0_SIMPLE")
            return
        if intent == "knowledge_to_product":
            doc, ast, start, terminal = _knowledge_candidates(category_rows, category, world["knowledge"], ordinal, start_rows=s0, terminal_rows=s0, require_delta=False)
            if stratum != "S0_SIMPLE":
                followup_atoms = [_atom("stock", "GTE", 1)]
                ast = _all_atoms(list(_flatten_atoms(ast)) + followup_atoms)
                start = _universe(category_rows, ast, s0)
                terminal = _universe(category_rows, ast, s2 if stratum == "S2_OBSERVATION_DEPENDENT" else s0)
            dependency = _knowledge_dependency(doc)
            bindings = [{"sourceType": "knowledge", "role": "dependency", "field": dependency["field"], "value": dependency["value"], "sourceRef": doc["sourceRef"], "factTier": doc["factTier"]}]
            point = _S2_POINTS[intent][0] if stratum == "S2_OBSERVATION_DEPENDENT" else None
            fault = _fault_record(case_id, world, point, mutation={"kind": point, "documentRevision": "conflicted"} if point else None)
            append_case(scenario_id=case_id, split=split, intent=intent, stratum=stratum, category=category, ast=ast, solution_type="single_product", acceptable=terminal, dependencies=(dependency,), terminal_classes=("SUCCESS",), environment_sequence=("E0", "E2") if stratum == "S2_OBSERVATION_DEPENDENT" else (), required_points=(point,) if point else (), fault=fault, bindings=bindings, public_hint=_FIELD_LABELS.get(str(dependency["field"]), str(dependency["field"])), force_followup=stratum != "S0_SIMPLE", followup_field="stock" if stratum != "S0_SIMPLE" else None, dynamic_public=stratum == "S2_OBSERVATION_DEPENDENT")
            return
        if intent == "multi_product_merchant":
            dynamic = stratum == "S2_OBSERVATION_DEPENDENT"
            ast, grouped, start, terminal = _choose_bundle_filter(category_rows, category, ordinal, dynamic=dynamic, start_rows=s0, terminal_rows=s2 if dynamic else s0)
            bundles = [tuple(bundle) for bundle in combinations(sorted(terminal), 2)]
            if not bundles:
                raise CommerceWorldError(f"multi-product {category} has no complete bundles")
            merchant_id = str(product_by_id[bundles[0][0]]["merchantId"])
            dependencies: list[Mapping[str, Any]] = []
            bindings: list[Mapping[str, Any]] = []
            # Every multi-product case binds a concrete merchant policy.  It
            # keeps the source coverage floor honest and prevents same-store
            # selection from being a purely product-only shortcut.
            if True:
                policy = _policy_for_merchant(world["policies"], merchant_id, ordinal)
                dependency = _policy_dependency(policy); dependencies.append(dependency)
                bindings.append({"sourceType": "merchant_policy", "role": "dependency", "field": dependency["field"], "value": dependency["value"], "sourceRef": policy["sourceRef"], "factTier": policy["factTier"]})
            point = _S2_POINTS[intent][0] if dynamic else None
            fault = _fault_record(case_id, world, point, mutation={"kind": point, "terminalAction": "do_not_sell_out"} if point else None)
            merchant_alias = _merchant_alias(merchant_id)
            topic_label = _POLICY_TOPIC_LABELS[str(policy["topic"])]
            # The selected merchant is a public entity in the controlled world;
            # exposing its human label prevents the private merchantId from
            # becoming a hidden single-target answer.  The policy value and
            # document identity remain private and require evidence.
            append_case(scenario_id=case_id, split=split, intent=intent, stratum=stratum, category=category, ast=ast, solution_type="bundle", bundles=bundles, dependencies=dependencies, environment_sequence=("E0", "E2") if dynamic else (), required_points=(point,) if point else (), fault=fault, bindings=bindings, public_hint=f"{merchant_alias}的{topic_label}", public_entity_bindings=({"entityType": "merchant", "alias": merchant_alias, "merchantId": merchant_id},), force_followup=False, dynamic_public=dynamic)
            return
        if intent == "coupon_budget":
            dynamic_fault = stratum == "S2_OBSERVATION_DEPENDENT"
            ast, coupon, start, terminal, coupon_meta = _choose_coupon_filter(category_rows, category, ordinal, world["coupons"], e0, dynamic_fault=dynamic_fault)
            ids = tuple(sorted(start))
            max_total, no_coupon_solutions, terminal_solutions = _coupon_solution_audit(ids, coupon, product_by_id, e0)
            start_solutions = _coupon_solution_universe(ids, coupon, product_by_id, e0, max_total=max_total, price_delta=1.0 if dynamic_fault else 0.0)
            if not terminal_solutions or (dynamic_fault and start_solutions == terminal_solutions):
                raise CommerceWorldError(f"coupon {coupon.get('couponId')} has no authoritative cart-universe delta")
            terminal_ids = tuple(sorted({str(item) for row in terminal_solutions for item in row["selectedProductIds"]}))
            merchant_id = str(coupon["merchantId"])
            merchant_alias = _merchant_alias(merchant_id)
            rules = {"minItems": 1, "maxItems": 1, "currency": "CNY", "maxTotal": max_total, "requiredMerchantId": merchant_id, "allowedCouponIds": [str(coupon["couponId"])]}
            bindings = [{"sourceType": "coupon", "role": "budget", "field": "threshold", "value": coupon["threshold"], "sourceRef": coupon["sourceRef"], "factTier": coupon["factTier"]}]
            point = _S2_POINTS[intent][0] if dynamic_fault else None
            fault = _fault_record(case_id, world, point, mutation={"kind": point, "priceDelta": 1.0, "terminalAction": "recalculate_or_abstain"} if point else None)
            append_case(scenario_id=case_id, split=split, intent=intent, stratum=stratum, category=category, ast=ast, solution_type="cart", acceptable=terminal_ids, cart_rules=rules, terminal_classes=("SUCCESS",), allow_no_answer=False, required_points=(point,) if point else (), fault=fault, bindings=bindings, start_universe=start_solutions, terminal_universe=terminal_solutions, public_hint=merchant_alias, public_entity_bindings=({"entityType": "merchant", "alias": merchant_alias, "merchantId": merchant_id},), audit_extra={"couponNecessity": {"noCouponValidCount": len(no_coupon_solutions), "couponValidCount": len(terminal_solutions), "recomputed": True, "noCouponTotalsDigest": _stable_digest(no_coupon_solutions), "couponTotalsDigest": _stable_digest(terminal_solutions)}, "couponId": coupon["couponId"], "solutionUniverseType": "commerce-cart-solution-v1", "startSolutions": start_solutions, "terminalSolutions": terminal_solutions, "faultDerivedTerminal": dynamic_fault, "terminalContract": "success_after_e0_requery" if dynamic_fault else "success"}, public_budget=max_total, force_followup=False, dynamic_public=dynamic_fault)
            return
        if intent == "dynamic_replanning":
            extra = [_atom("stock", "GTE", 1)]
            ast, _group, start, terminal = _choose_static_group(category_rows, category, ordinal, min_count=1, max_count=256, field_count=2, extra_atoms=extra, start_rows=s0, terminal_rows=s2 if stratum == "S2_OBSERVATION_DEPENDENT" else s0, require_delta=stratum == "S2_OBSERVATION_DEPENDENT")
            point = _S2_POINTS[intent][0] if stratum == "S2_OBSERVATION_DEPENDENT" else None
            fault = _fault_record(case_id, world, point, mutation={"kind": point, "terminalAction": "do_not_sell_out"} if point else None)
            append_case(scenario_id=case_id, split=split, intent=intent, stratum=stratum, category=category, ast=ast, solution_type="single_product", acceptable=terminal, terminal_classes=("SUCCESS",), environment_sequence=("E0", "E2") if stratum == "S2_OBSERVATION_DEPENDENT" else (), required_points=(point,) if point else (), fault=fault, force_followup=False, dynamic_public=True)
            return
        raise CommerceWorldError(f"unsupported pilot intent {intent}")

    global_index = 0
    for split, repeats in (("development", 4), ("validation", 2)):
        for intent in INTENT_FAMILIES:
            for stratum in STRATA:
                for ordinal in range(1, repeats + 1):
                    author_regular(global_index, split, intent, stratum, ordinal)
                    global_index += 1

    # Six schema-valid, executable fail-closed red-team contracts.  They are
    # intentionally mixed across families so red-team behavior is not a second
    # cosmetic matrix.
    red_specs = (
        ("product_finder", "S0_SIMPLE", "unknown"),
        ("dynamic_replanning", "S2_OBSERVATION_DEPENDENT", "soldout"),
        ("coupon_budget", "S2_OBSERVATION_DEPENDENT", "stale_price"),
        ("knowledge_to_product", "S2_OBSERVATION_DEPENDENT", "evidence_conflict"),
        ("product_finder", "S2_OBSERVATION_DEPENDENT", "tool_timeout"),
        ("multi_product_merchant", "S2_OBSERVATION_DEPENDENT", "checkpoint_restart"),
    )
    for red_index, (intent, stratum, red_kind) in enumerate(red_specs, 1):
        category = CATEGORIES[global_index % len(CATEGORIES)]
        category_rows = by_category[category]
        # The public scenario identity must not encode the private fault
        # point; use an opaque contract ordinal instead.
        sid = f"ACB-V1-RED-contract-{red_index:02d}"
        if red_kind == "unknown":
            ast = _all_atoms([_atom("category", "EQ", category), _atom("optionalSellerNote", "EQ", "必须由卖家补充")])
            unknown_product = category_rows[0]
            bindings = [{"sourceType": "product_fact", "role": "unknown_required_fact", "field": "optionalSellerNote", "value": None, "sourceRef": (unknown_product.get("facts") or {}).get("optionalSellerNote", {}).get("sourceRef", ""), "factTier": str(unknown_product.get("factTier", "synthetic_fixture"))}, _source_binding(unknown_product, "category")]
            append_case(scenario_id=sid, split="contract_red_team", intent=intent, stratum=stratum, category=category, ast=ast, solution_type="single_product", terminal_classes=("ABSTAIN_OR_EXPLAIN",), allow_no_answer=True, bindings=bindings, audit_extra={"redTeam": red_kind, "unknownRequiredFact": True}, force_followup=False)
        elif red_kind == "soldout":
            ast, start, terminal = _choose_soldout_filter(category_rows, category, e0, e2, red_index)
            fault = _fault_record(sid, world, "stock_change", mutation={"kind": "stock_change", "terminalAction": "do_not_sell_out"})
            representative = product_by_id[start[0]]
            evidence, _ = _build_product_evidence(representative, ast, e0_row=e0[start[0]], terminal_row=e0[start[0]], include_dynamic=True)
            append_case(scenario_id=sid, split="contract_red_team", intent=intent, stratum=stratum, category=category, ast=ast, solution_type="single_product", terminal_classes=("ABSTAIN_OR_EXPLAIN",), allow_no_answer=True, environment_sequence=("E0", "E2"), required_points=("stock_change",), fault=fault, bindings=evidence, audit_extra={"redTeam": red_kind, "startUniverse": {"count": len(start), "sha256": _stable_digest(list(start))}, "terminalUniverse": {"count": len(terminal), "sha256": _stable_digest(list(terminal))}, "faultDerivedTerminal": False}, force_followup=True, dynamic_public=True)
        elif red_kind == "stale_price":
            ast, coupon, start, _terminal, _meta = _choose_coupon_filter(category_rows, category, red_index, world["coupons"], e0, dynamic_fault=True)
            max_total, no_coupon_solutions, terminal_solutions = _coupon_solution_audit(start, coupon, product_by_id, e0)
            start_solutions = _coupon_solution_universe(start, coupon, product_by_id, e0, max_total=max_total, price_delta=1.0)
            rules = {"minItems": 1, "maxItems": 1, "currency": "CNY", "maxTotal": max_total, "requiredMerchantId": str(coupon["merchantId"]), "allowedCouponIds": [str(coupon["couponId"])]}
            merchant_id = str(coupon["merchantId"])
            merchant_alias = _merchant_alias(merchant_id)
            fault = _fault_record(sid, world, "stale_price", mutation={"kind": "stale_price", "priceDelta": 1.0, "terminalAction": "recalculate_or_abstain"})
            representative = product_by_id[start[0]]
            evidence, _ = _build_product_evidence(representative, ast, e0_row=e0[start[0]], terminal_row=e0[start[0]], include_dynamic=True)
            append_case(scenario_id=sid, split="contract_red_team", intent=intent, stratum=stratum, category=category, ast=ast, solution_type="cart", acceptable=(), cart_rules=rules, terminal_classes=("ABSTAIN_OR_EXPLAIN",), allow_no_answer=True, required_points=("stale_price",), fault=fault, bindings=tuple(evidence) + ({"sourceType": "coupon", "role": "budget", "field": "threshold", "value": coupon["threshold"], "sourceRef": coupon["sourceRef"], "factTier": coupon["factTier"]},), start_universe=start_solutions, terminal_universe=terminal_solutions, public_hint=merchant_alias, public_entity_bindings=({"entityType": "merchant", "alias": merchant_alias, "merchantId": merchant_id},), audit_extra={"redTeam": red_kind, "couponNecessity": {"noCouponValidCount": len(no_coupon_solutions), "couponValidCount": len(terminal_solutions), "recomputed": True, "noCouponTotalsDigest": _stable_digest(no_coupon_solutions), "couponTotalsDigest": _stable_digest(terminal_solutions)}, "couponId": coupon["couponId"], "solutionUniverseType": "commerce-cart-solution-v1", "startSolutions": start_solutions, "terminalSolutions": terminal_solutions, "faultDerivedTerminal": True, "terminalContract": "explicit_abstain_after_stale_price"}, public_budget=max_total, force_followup=False, dynamic_public=True)
        elif red_kind == "evidence_conflict":
            doc, ast, start, terminal = _knowledge_candidates(category_rows, category, world["knowledge"], red_index, start_rows=e0, terminal_rows=e0, require_delta=False)
            ast = _all_atoms(list(_flatten_atoms(ast)) + [_atom("stock", "GTE", 1)])
            start = _universe(category_rows, ast, e0); terminal = _universe(category_rows, ast, e2)
            dependency = _knowledge_dependency(doc)
            fault = _fault_record(sid, world, "evidence_conflict", mutation={"kind": "evidence_conflict", "terminalAction": "cite_one_revision"})
            append_case(scenario_id=sid, split="contract_red_team", intent=intent, stratum=stratum, category=category, ast=ast, solution_type="single_product", acceptable=terminal, dependencies=(dependency,), terminal_classes=("SUCCESS", "ABSTAIN_OR_EXPLAIN"), allow_no_answer=True, environment_sequence=("E0", "E2"), required_points=("evidence_conflict",), fault=fault, bindings=({"sourceType": "knowledge", "role": "dependency", "field": dependency["field"], "value": dependency["value"], "sourceRef": doc["sourceRef"], "factTier": doc["factTier"]},), public_hint=_FIELD_LABELS.get(str(dependency["field"]), str(dependency["field"])), audit_extra={"redTeam": red_kind}, force_followup=True, dynamic_public=True)
        elif red_kind == "tool_timeout":
            ast, _group, start, terminal = _choose_static_group(category_rows, category, red_index, min_count=1, max_count=256, field_count=2, extra_atoms=(_atom("stock", "GTE", 1),), start_rows=e0, terminal_rows=e2, require_delta=True)
            fault = _fault_record(sid, world, "tool_timeout", mutation={"kind": "tool_timeout", "maxRetries": 2, "terminalAction": "bounded_retry_or_abstain"})
            append_case(scenario_id=sid, split="contract_red_team", intent=intent, stratum=stratum, category=category, ast=ast, solution_type="single_product", acceptable=terminal, terminal_classes=("SUCCESS", "ABSTAIN_OR_EXPLAIN"), allow_no_answer=True, environment_sequence=("E0", "E2"), required_points=("tool_timeout",), fault=fault, audit_extra={"redTeam": red_kind}, force_followup=True, dynamic_public=True)
        else:
            ast, _group, start, terminal = _choose_bundle_filter(category_rows, category, red_index, dynamic=True, start_rows=e0, terminal_rows=e2)
            bundles = list(combinations(sorted(terminal), 2))
            merchant_id = str(product_by_id[bundles[0][0]]["merchantId"]) if bundles else ""
            policy = _policy_for_merchant(world["policies"], merchant_id, red_index)
            dependency = _policy_dependency(policy)
            merchant_alias = _merchant_alias(merchant_id)
            fault = _fault_record(sid, world, "checkpoint_restart", mutation={"kind": "checkpoint_restart", "terminalAction": "recover_or_fail_closed"})
            append_case(scenario_id=sid, split="contract_red_team", intent=intent, stratum=stratum, category=category, ast=ast, solution_type="bundle", bundles=bundles, dependencies=(dependency,), terminal_classes=("SUCCESS", "ABSTAIN_OR_EXPLAIN"), allow_no_answer=True, environment_sequence=("E0", "E2"), required_points=("checkpoint_restart",), fault=fault, bindings=({"sourceType": "merchant_policy", "role": "dependency", "field": dependency["field"], "value": dependency["value"], "sourceRef": policy["sourceRef"], "factTier": policy["factTier"]},), public_hint=f"{merchant_alias}的{_POLICY_TOPIC_LABELS[str(policy['topic'])]}", public_entity_bindings=({"entityType": "merchant", "alias": merchant_alias, "merchantId": merchant_id},), audit_extra={"redTeam": red_kind}, force_followup=True, dynamic_public=True)
        global_index += 1

    if len(inputs) != 96 or len(oracles) != 96 or len(faults) != 96 or len(predictions) != 96:
        raise CommerceWorldError("pilot authoring did not produce exactly 96 aligned records")
    if any(count != 8 for count in categories_seen.values()):
        raise CommerceWorldError(f"pilot category coverage is not exactly 8 each: {categories_seen}")
    if len({row["scenarioId"] for row in inputs}) != 96 or len({row["turns"][0]["text"] for row in inputs}) != 96:
        text_cases: dict[str, list[str]] = {}
        for row in inputs:
            text_cases.setdefault(str(row["turns"][0]["text"]), []).append(str(row["scenarioId"]))
        duplicates = {text: ids for text, ids in text_cases.items() if len(ids) > 1}
        raise CommerceWorldError(f"pilot public requests are not unique: {duplicates}")
    for public, oracle, fault in zip(inputs, oracles, faults):
        audit_public_input_leaks(public)
        audit_public_private_value_leaks(public, oracle, fault)
    # Recompute start/terminal witnesses from the audited world after all
    # oracle rows are frozen.  This keeps the S2 delta audit authoritative and
    # prevents a caller from accidentally reporting the terminal gold as both
    # sides of the transition.
    for audit, oracle in zip(audits, oracles):
        if str(oracle.get("complexityStratum")) != "S2_OBSERVATION_DEPENDENT" or audit.get("faultDerivedTerminal"):
            continue
        category_rows = by_category[str(audit["category"])]
        sequence = (oracle.get("successConditions") or {}).get("environmentSequence") or ["E0"]
        start_products = _universe(category_rows, oracle["successConditions"]["constraintAst"], e0)
        terminal_products = _universe(category_rows, oracle["successConditions"]["constraintAst"], env[str(sequence[-1])])
        if oracle["successConditions"].get("solutionType") == "bundle":
            def bundle_universe(ids: Sequence[str]) -> list[list[str]]:
                merchant_groups: dict[str, list[str]] = {}
                for product_id in ids:
                    merchant_groups.setdefault(str(product_by_id[product_id].get("merchantId")), []).append(product_id)
                return [list(bundle) for merchant_id in sorted(merchant_groups) for bundle in combinations(sorted(merchant_groups[merchant_id]), 2)]
            start_value = bundle_universe(start_products); terminal_value = bundle_universe(terminal_products)
        else:
            start_value = list(start_products); terminal_value = list(terminal_products)
        audit["startUniverse"] = {"count": len(start_value), "sha256": _stable_digest(start_value)}
        audit["terminalUniverse"] = {"count": len(terminal_value), "sha256": _stable_digest(terminal_value)}
        if audit["startUniverse"]["sha256"] == audit["terminalUniverse"]["sha256"]:
            raise CommerceWorldError(f"S2 scenario {audit['scenarioId']} has no authoritative start/terminal delta")
    source_counts: dict[str, int] = {}
    for audit in audits:
        actual_types = {str(binding.get("sourceType")) for binding in audit["sourceBindings"] if str(binding.get("sourceType", ""))}
        if actual_types != set(audit["sourceTypes"]):
            raise CommerceWorldError(f"pilot sourceTypes echo mismatch for {audit['scenarioId']}")
        for source_type in actual_types:
            source_counts[source_type] = source_counts.get(source_type, 0) + 1
    if source_counts.get("query", 0) != 96 or source_counts.get("user_followup", 0) < 12 or source_counts.get("product_fact", 0) < 60 or source_counts.get("knowledge", 0) < 18 or source_counts.get("merchant_policy", 0) < 18 or source_counts.get("coupon", 0) < 18 or source_counts.get("dynamic_offer", 0) < 30:
        raise CommerceWorldError(f"pilot source coverage floors are unmet: {source_counts}")
    template_counts = Counter(str(audit["languageMaterialization"]["normalizedTemplate"]) for audit in audits)
    max_template_cluster = max(template_counts.values()) if template_counts else 0
    if max_template_cluster > 4:
        raise CommerceWorldError(f"public language template cluster exceeds four: {max_template_cluster}")
    language_stats = {
        "normalizedTemplateCount": len(template_counts),
        "largestNormalizedTemplateCluster": max_template_cluster,
        "normalizedTemplateCounts": dict(sorted(template_counts.items())),
        "publicTurnSequenceCount": len({tuple(turn["text"] for turn in row["turns"]) for row in inputs}),
    }
    if language_stats["publicTurnSequenceCount"] != 96:
        raise CommerceWorldError("public turn sequences are not globally unique")
    return {"input": inputs, "oracle": oracles, "fault": faults, "prediction": predictions, "audit": audits, "sourceCounts": source_counts, "categories": categories_seen, "languageStats": language_stats}


def generate_pilot_96(world_dir: Path | str) -> Mapping[str, Any]:
    """Load the accepted controlled world and author the full Pilot in memory."""
    generated = _author_pilot_96(_load_formal_world(world_dir))
    # R11 intentionally stops here: final Chinese comes from a reviewed,
    # signature-bound source asset.  The old renderer only remains upstream
    # to derive the already-approved structural oracle/audit shape.
    from .commerce_benchmark_v1_public_text_authoring import materialize_authored_public_text

    return materialize_authored_public_text(generated)


def load_public_pilot_inputs(path: Path | str) -> tuple[Mapping[str, Any], ...]:
    """Public-only loader used by runner-facing tests and integrations."""
    from .commerce_benchmark_v1_public import load_public_inputs

    rows = load_public_inputs(path)
    for row in rows:
        audit_public_input_leaks(row)
    return rows


def _artifact_row(path: Path, root: Path, *, record_count: int | None = None) -> Mapping[str, Any]:
    row: dict[str, Any] = {"path": str(path.relative_to(root)).replace("\\", "/"), "sha256": sha256_file(path)}
    if record_count is not None:
        row["recordCount"] = int(record_count)
    return row


def _blind_language_checklist(public_rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Freeze a private, unsigned 20-row review checklist before any run."""
    selected = [row for index, row in enumerate(public_rows) if index % 5 == 0]
    return [
        {
            "scenarioId": str(row["scenarioId"]),
            "publicText": " ".join(str(turn.get("text", "")) for turn in row.get("turns", [])),
            "reviewStatus": "PENDING_INDEPENDENT_REVIEW",
            "naturalness": {"status": "PENDING_INDEPENDENT_REVIEW", "note": ""},
            "comprehension": {"status": "PENDING_INDEPENDENT_REVIEW", "note": ""},
            "answerLeakage": {"status": "PENDING_INDEPENDENT_REVIEW", "note": ""},
        }
        for row in selected
    ]


def write_pilot_96(world_dir: Path | str, output_dir: Path | str) -> Mapping[str, Path]:
    """Materialize the sealed public/private/run Pilot directory once.

    Existing files are never overwritten.  Rebuilds must use another
    versioned directory, which makes accidental replacement of a frozen Pilot
    impossible.
    """
    world = _load_formal_world(world_dir)
    output = Path(output_dir)
    if output.exists() and any(output.rglob("*")):
        # A freeze is immutable even when the caller presents the same
        # generator/version marker.  Repairs must use a new versioned root so
        # the original evidence remains byte-for-byte auditable.
        raise CommerceWorldError(f"refusing to overwrite non-empty pilot directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    # Keep this call through the validation-only authoring materializer; it
    # never uses a scenario ID, list position, split or renderer template to
    # select words.
    generated = generate_pilot_96(world["root"])
    public_dir = output / "public"
    private_dir = output / "private"
    run_dir = output / "run"
    for directory in (public_dir, private_dir, run_dir):
        directory.mkdir(parents=True, exist_ok=True)
    records_by_path: list[tuple[Path, Sequence[Mapping[str, Any]], str]] = [
        (public_dir / "scenario.input.jsonl", generated["input"], "input"),
        (private_dir / "oracle.private.jsonl", generated["oracle"], "oracle"),
        (private_dir / "fault.private.jsonl", generated["fault"], "fault"),
        (run_dir / "prediction.not_run.jsonl", generated["prediction"], "prediction"),
    ]
    receipt_rows = [
        make_runner_receipt(prediction, oracle, fault, world_manifest_sha256=world["worldSha256"], environment_artifact_sha256=sha256_file(world["artifactPaths"][f"environments/{_resolve_terminal_revision(oracle)}.jsonl"]), status="NOT_COLLECTED")
        for prediction, oracle, fault in zip(generated["prediction"], generated["oracle"], generated["fault"])
    ]
    records_by_path.append((run_dir / "receipt.not_collected.jsonl", receipt_rows, "receipt"))
    artifacts: list[Mapping[str, Any]] = []
    path_map: dict[str, Path] = {}
    for path, records, kind in records_by_path:
        write_jsonl(path, records, kind)
        path_map[kind] = path
        artifacts.append(_artifact_row(path, output, record_count=len(records)))
    audit_path = private_dir / "authoring_audit.private.jsonl"
    _write_private_jsonl(audit_path, generated["audit"])
    artifacts.append(_artifact_row(audit_path, output, record_count=len(generated["audit"])))
    from .commerce_benchmark_v1_public_text_authoring import (
        authoring_asset_sha256,
        build_text_only_blind_rows,
    )
    checklist_path = private_dir / "blind_language_texts.private.jsonl"
    checklist_rows = build_text_only_blind_rows(generated)
    _write_private_jsonl(checklist_path, checklist_rows)
    artifacts.append(_artifact_row(checklist_path, output, record_count=len(checklist_rows)))
    freeze = {
        "schemaVersion": "commerce-pilot-freeze-v1",
        "pilotVersion": PILOT_96_VERSION,
        "worldId": world["manifest"]["worldId"],
        "worldManifestSha256": world["worldSha256"],
        "catalogRevision": world["manifest"]["catalogRevision"],
        "generatorSha256": sha256_file(Path(__file__)),
        "counts": {"total": 96, "development": 60, "validation": 30, "contract_red_team": 6},
        "categoryCounts": dict(generated["categories"]),
        "sourceCoverage": dict(generated["sourceCounts"]),
        "languageAudit": dict(generated["languageStats"]),
        "faultStatuses": {status: sum(1 for row in generated["fault"] if row["faultStatus"] == status) for status in ("READY", "NONE")},
        "oracleStatuses": {status: sum(1 for row in generated["oracle"] if row["oracleStatus"] == status) for status in ("READY", "PENDING_DATA_SCALE", "PENDING_SCENARIO_AUTHORING")},
        "runStatuses": {"NOT_RUN": len(generated["prediction"])},
        "receiptStatuses": {"NOT_COLLECTED": len(receipt_rows)},
        "publicTextAuthoringSource": {
            "path": "agent/evaluation/assets/commerce_pilot_96_public_text_authoring_r11.jsonl",
            "recordCount": 96,
            "sha256": authoring_asset_sha256(),
            "binding": "canonical_public_semantic_payload_sha256",
        },
        "blindLanguageTexts": {"path": str(checklist_path.relative_to(output)).replace("\\", "/"), "recordCount": len(checklist_rows), "sha256": sha256_file(checklist_path), "reviewStatus": "PENDING_INDEPENDENT_REVIEW"},
        "artifacts": artifacts,
        "unsupportedClaims": [
            "The 96 records are deterministic synthetic-world authoring artifacts, not real-market evidence.",
            "No model, Docker, production Agent, FAST, PAE, or BOUNDED_REACT run was performed.",
            "The unsigned blind-language checklist is an audit plan, not an independent human result.",
            "The 20-row blind-language checklist is private and unsigned; it contains no completed naturalness, comprehension, or answer-leakage judgement.",
            "Coupon S2 is a deterministic stale-price requery contract over the authoritative E0 offer and E0 coupon; no live run or architecture score is claimed.",
        ],
    }
    freeze_path = output / "freeze_manifest.json"
    freeze_path.write_bytes(canonical_bytes(freeze))
    readme = (
        "# Commerce Pilot 96 — Oracle-first freeze\n\n"
        "This directory contains a deterministic, Chinese, synthetic-world Pilot.\n"
        "Public input is physically separated from oracle/fault/authoring audit.\n"
        "All predictions are NOT_RUN and all receipts are NOT_COLLECTED; no score is implied.\n\n"
        "The accepted controlled world manifest is bound by SHA-256 in freeze_manifest.json.\n"
        "The next authorized step is Runner contract preparation, followed by a sealed execution;\n"
        "this freeze does not claim architecture superiority or statistical significance.\n"
        "private/blind_language_checklist.private.jsonl is an unsigned 20-row review plan; it is not a human evaluation result.\n"
    )
    readme_path = output / "README.md"
    readme_path.write_text(readme, encoding="utf-8", newline="\n")
    path_map.update({"audit": audit_path, "manifest": freeze_path, "readme": readme_path})
    return path_map


def _resolve_terminal_revision(oracle: Mapping[str, Any]) -> str:
    sequence = (oracle.get("successConditions") or {}).get("environmentSequence")
    if isinstance(sequence, list) and sequence:
        return str(sequence[-1])
    return str(oracle.get("environmentRevision", "E0"))
