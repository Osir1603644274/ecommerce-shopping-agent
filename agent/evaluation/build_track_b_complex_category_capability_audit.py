"""Audit category capability for a future complex Track B Agent pilot.

This stage is read-only with respect to product evidence and emits no query,
qrel, judgment, or metric.  Attribute groups are explicit, versioned rules;
unkeyed booleans/numbers and title-only marketing claims never become facts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from evaluation.build_track_b_complex_intent_design import (
    canonical_json,
    sha256_file,
    stable_repository_relative_path,
    write_json,
    write_utf8_lf,
)


SCHEMA_VERSION = "track-b-complex-category-capability-audit-v1"
RULE_VERSION = "complex-category-capability-rules-v1"
AUDIT_ID = "kuaisearch-track-b-complex-category-capability-audit-v01"
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
MANDATORY_CATEGORY_KEYS = (
    "1/39/0", "1/13/103", "23/40/217", "46/133/185", "30/59/168", "30/59/57",
)
AUTO_BACKUP_COUNT = 3
GATE = {
    "minimumEvidenceProducts": 120,
    "minimumUsableGroups": 4,
    "minimumSafeGroups": 3,
    "minimumFullySatisfied": 8,
    "minimumSoftMissingOrPartial": 4,
    "minimumHardFail": 8,
    "minimumUnknownOrConflict": 4,
    "minimumSupportedCapabilities": 5,
    "maximumSelectedGoCategories": 3,
    "groupMinimumCoverage": 0.10,
    "groupMinimumValueSupport": 4,
    "shortcutDominanceThreshold": 0.80,
}


@dataclass(frozen=True)
class GroupRule:
    group_id: str
    aliases: dict[str, tuple[str, ...]]
    exclusive: bool = True
    ordered_values: tuple[str, ...] = ()
    note: str = ""


def group(group_id: str, aliases: dict[str, Iterable[str]], *, exclusive: bool = True,
          ordered: Iterable[str] = (), note: str = "") -> GroupRule:
    return GroupRule(group_id, {key: tuple(values) for key, values in aliases.items()}, exclusive, tuple(ordered), note)


CATALOG: dict[str, list[GroupRule]] = {
    "1/39/0": [
        group("sleeve_length", {"short": ["短袖"], "long": ["长袖"], "sleeveless": ["无袖"], "half": ["五分袖"], "three_quarter": ["七分袖"], "nine_tenths": ["九分袖"]}, ordered=["sleeveless", "short", "half", "three_quarter", "nine_tenths", "long"]),
        group("fit", {"loose": ["宽松型", "宽松"], "standard": ["标准型", "标准"], "slim": ["修身型", "修身"]}),
        group("collar", {"round": ["圆领"], "v_neck": ["v领"], "polo": ["polo领"], "square": ["方领"]}),
        group("material", {"cotton": ["棉", "纯棉"], "polyester": ["涤纶(聚酯纤维)", "聚酯纤维", "涤纶"], "viscose": ["粘胶纤维"], "modal": ["莫代尔"]}, exclusive=False, note="棉包含明确含棉混纺；纯棉仍需更强证据"),
        group("pattern", {"solid": ["纯色"], "print": ["印花"], "stripe": ["条纹"], "letter": ["字母"]}),
    ],
    "1/13/103": [
        group("waist_height", {"low": ["低腰"], "mid_low": ["中低腰"], "mid": ["中腰"], "high": ["高腰"]}, ordered=["low", "mid_low", "mid", "high"], note="中低腰面对中腰硬约束仍为 unknown"),
        group("leg_shape", {"straight": ["直筒裤", "直筒"], "wide": ["阔脚裤", "阔腿裤", "阔脚", "阔腿"], "flare": ["微喇裤", "微喇"], "skinny": ["小脚裤", "小脚"], "harem": ["哈伦裤", "哈伦"]}),
        group("fit", {"loose": ["宽松"], "slim": ["修身"]}),
        group("material", {"denim": ["牛仔布"], "cotton": ["棉"], "polyester": ["涤纶(聚酯纤维)", "聚酯纤维", "涤纶"]}, exclusive=False),
        group("stretch", {"none": ["无弹"], "micro": ["微弹"], "medium": ["适中"], "high": ["高弹"]}, ordered=["none", "micro", "medium", "high"]),
        group("length", {"short": ["短裤", "超短裤"], "cropped": ["七分裤"], "ankle": ["九分裤"], "long": ["长裤"]}, ordered=["short", "cropped", "ankle", "long"]),
    ],
    "23/40/217": [
        group("closure", {"lace_up": ["系带"], "pull_on": ["套脚", "一脚蹬"], "hook_loop": ["魔术贴"], "zip": ["拉链"]}, note="一脚蹬归入套脚包含关系"),
        group("heel_height", {"flat": ["平底"], "low": ["低跟(1-3cm)", "低跟"], "mid": ["中跟(3-5cm)", "中跟"], "high": ["高跟(5-8cm)", "高跟"]}, ordered=["flat", "low", "mid", "high"], note="平底与低跟共同出现按冲突处理"),
        group("upper_material", {"synthetic_leather": ["合成革"], "textile": ["织物/纺织品", "织物", "纺织品"], "synthetic_fiber": ["合成纤维"], "leather": ["真皮"]}, exclusive=False),
        group("toe_shape", {"round": ["圆头"], "pointed": ["尖头"], "square": ["方头"]}),
        group("opening_depth", {"shallow": ["浅口"], "deep": ["深口"]}),
        group("sole_material", {"rubber": ["橡胶", "橡胶底"], "eva": ["eva"], "pu": ["pu"]}, exclusive=False),
    ],
    "46/133/185": [
        group("battery_health", {"lt70": ["70%以下"], "70_80": ["70%-80%"], "80_90": ["80%-90%"], "90_plus": ["90%+"]}, ordered=["lt70", "70_80", "80_90", "90_plus"], note="仅为来源声明的健康区间，不推断续航时长"),
        group("motherboard_repair", {"not_repaired": ["主板未维修"], "repaired": ["主板有过维修"]}),
        group("screen_originality", {"original": ["原装屏"], "non_original": ["非原装内屏/外屏"]}),
        group("battery_originality", {"original": ["原装电池"], "non_original": ["非原装电池"]}),
        group("shell_condition", {"normal": ["外壳正常"], "damaged": ["外壳有磕碰", "外壳有缺失"]}),
        group("scratch_level", {"none": ["无划痕"], "light": ["轻微划痕"], "obvious": ["明显划痕"]}, ordered=["none", "light", "obvious"]),
        group("region", {"mainland": ["国行"], "us": ["美版"], "hk": ["港版"]}),
        group("condition_grade", {"eight": ["8新"], "nine": ["9新"], "ninety_five": ["95新"], "almost_new": ["99新"]}, ordered=["eight", "nine", "ninety_five", "almost_new"]),
        group("os", {"android": ["android/安卓", "安卓"], "ios": ["ios"]}),
    ],
    "30/59/168": [
        group("brand", {"apple": ["苹果/apple", "apple/苹果"], "huawei": ["华为/huawei"], "vivo": ["vivo"], "iqoo": ["iqoo"], "oppo": ["oppo"], "honor": ["荣耀/honor"], "xiaomi": ["小米/mi", "小米"], "redmi": ["红米", "redmi"]}),
        group("chipset_vendor", {"qualcomm": ["高通", "高通骁龙"], "mediatek": ["联发科", "天玑"], "apple": ["苹果a系列"]}),
        group("network_mode", {"five_g": ["5g全网通", "双卡5g", "5g"], "four_g": ["4g全网通", "4g"]}),
        group("sim_mode", {"dual": ["双卡双待", "双卡双待全网通", "双卡5g"], "single": ["单卡"]}),
        group("screen_form", {"full": ["全面屏"], "punch": ["挖孔屏"], "fold": ["折叠屏"]}),
        group("port", {"type_c": ["type-c"], "lightning": ["lightning"]}),
    ],
    "30/59/57": [
        group("material", {"silicone": ["硅胶", "液态硅胶"], "tpu": ["tpu"], "acrylic": ["亚克力"], "pc": ["pc"], "leather": ["皮套", "真皮"]}, exclusive=False),
        group("shell_rigidity", {"soft": ["软壳", "软胶"], "hard": ["硬壳"]}),
        group("compatible_brand", {"apple": ["苹果(apple)", "苹果", "iphone"], "huawei": ["华为"], "vivo": ["vivo"], "oppo": ["oppo"], "xiaomi": ["小米"], "honor": ["荣耀(honor)", "荣耀"], "nubia": ["努比亚(nubia)", "努比亚"]}, exclusive=False, note="多品牌值不能证明具体机型兼容"),
        group("visual_style", {"simple": ["简约"], "cartoon": ["卡通动漫"], "illustrated": ["彩绘"], "trend": ["ins风", "网红潮流"]}),
        group("surface_finish", {"matte": ["磨砂", "磨砂壳"], "transparent": ["透明"], "embossed": ["浮雕"]}, exclusive=False),
        group("construction_feature", {"magnetic": ["磁吸"], "stand": ["支架"], "custom": ["定制", "个性定制"]}, exclusive=False, note="不含防摔效果等性能宣称"),
    ],
    "1/13/10": [
        group("waist_height", {"low": ["低腰"], "mid": ["中腰"], "high": ["高腰"]}, ordered=["low", "mid", "high"]),
        group("fit", {"loose": ["宽松", "宽松型"], "slim": ["修身", "修身型"], "standard": ["标准型"]}),
        group("stretch", {"none": ["无弹"], "micro": ["微弹"], "medium": ["适中"], "high": ["高弹"]}, ordered=["none", "micro", "medium", "high"]),
        group("length", {"short": ["短裤"], "ankle": ["九分裤"], "long": ["长裤"]}, ordered=["short", "ankle", "long"]),
        group("material", {"cotton": ["棉"], "polyester": ["涤纶(聚酯纤维)", "聚酯纤维", "涤纶"], "nylon": ["锦纶"]}, exclusive=False),
        group("thickness", {"thin": ["薄款"], "regular": ["常规"], "thick": ["加厚"]}, ordered=["thin", "regular", "thick"]),
    ],
    "1/130/0": [
        group("sleeve_length", {"sleeveless": ["无袖"], "short": ["短袖"], "long": ["长袖"]}, ordered=["sleeveless", "short", "long"]),
        group("skirt_length", {"short": ["短裙"], "mid": ["中长裙"], "long": ["长裙"]}, ordered=["short", "mid", "long"]),
        group("silhouette", {"a_line": ["a型", "a字裙"], "straight": ["直筒裙"], "slim": ["修身型", "修身"]}),
        group("collar", {"round": ["圆领"], "v_neck": ["v领"], "square": ["方领"], "polo": ["polo领"]}),
        group("material", {"cotton": ["棉"], "polyester": ["涤纶(聚酯纤维)", "聚酯纤维", "涤纶"], "chiffon": ["雪纺"]}, exclusive=False),
        group("pattern", {"solid": ["纯色"], "print": ["印花"], "floral": ["碎花"]}),
    ],
    "1/137/193": [
        group("set_piece_count", {"two": ["两件套"], "three": ["三件套"], "single": ["单件"]}, ordered=["single", "two", "three"]),
        group("bottom_type", {"pants": ["裤套装"], "skirt": ["裙套装"]}),
        group("sleeve_length", {"short": ["短袖"], "long": ["长袖"], "sleeveless": ["无袖"]}, ordered=["sleeveless", "short", "long"]),
        group("fit", {"loose": ["宽松"], "slim": ["修身"], "standard": ["常规款"]}),
        group("material", {"cotton": ["棉"], "polyester": ["涤纶(聚酯纤维)", "聚酯纤维", "涤纶"]}, exclusive=False),
        group("collar", {"round": ["圆领"], "v_neck": ["v领"], "polo": ["polo领"]}),
    ],
}


PROHIBITED_NEEDS = {
    "1/39/0": ["凉爽/不闷/舒适度", "显瘦效果", "耐用性、质量、性价比"],
    "1/13/103": ["显瘦显高效果", "久穿不变形", "耐磨、舒适度、质量、性价比"],
    "23/40/217": ["走一天不累脚", "防滑/耐磨实测", "增高后的实际身高效果", "质量、耐用性、性价比"],
    "46/133/185": ["真实续航时长", "游戏FPS/流畅度", "暗病或未来可靠性", "来源声明之外的真实维修历史"],
    "30/59/168": ["游戏FPS与散热", "真实续航", "拍照质量", "流畅度、质量、性价比", "无键名的支持/不支持/是/否对应能力"],
    "30/59/57": ["真实防摔效果", "散热、手感、耐黄、耐用性", "仅靠标题推断具体机型兼容"],
    "1/13/10": ["显瘦效果", "凉感/透气体验", "耐磨、质量、性价比"],
    "1/130/0": ["显瘦/气质效果", "舒适度", "质量、耐用性、性价比"],
    "1/137/193": ["搭配效果", "舒适度", "质量、耐用性、性价比"],
}


def normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def token_matches(token: str, alias: str) -> bool:
    token_n, alias_n = normalize(token), normalize(alias)
    return token_n == alias_n or alias_n in token_n.split(" ") or (
        len(alias_n) >= 2 and alias_n in token_n and any(mark in token_n for mark in ("/", " "))
    )


def values_from_texts(texts: Iterable[str], rule: GroupRule, *, title: bool) -> set[str]:
    found: set[str] = set()
    for text in texts:
        text_n = normalize(text)
        for canonical, aliases in rule.aliases.items():
            if any((normalize(alias) in text_n if title else token_matches(text_n, alias)) for alias in aliases):
                found.add(canonical)
    return found


def observe(attrs: tuple[str, ...], title: str, rule: GroupRule) -> tuple[set[str], bool, list[str]]:
    attr_values = values_from_texts(attrs, rule, title=False)
    title_values = values_from_texts((title,), rule, title=True)
    reasons: list[str] = []
    if rule.exclusive and len(attr_values) > 1:
        reasons.append("multiple_incompatible_attr_values")
    if rule.exclusive and attr_values and title_values and not title_values.issubset(attr_values):
        reasons.append("title_attr_disagreement")
    if rule.group_id == "heel_height" and {"flat", "low"}.issubset(attr_values | title_values):
        reasons.append("flat_low_joint_evidence")
    return attr_values, bool(reasons), sorted(set(reasons))


def auto_rank(feasibility_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for row in feasibility_rows:
        count = int(row.get("evidenceProductCount") or 0)
        top = row.get("topAttrValues") or []
        usable = [value for value in top if value.get("classification") == "lexically_self_describing_candidate" and 0.03 <= float(value.get("coverageAmongEvidenceProducts") or 0) <= 0.85]
        if count < 80 or len(usable) < 5:
            continue
        signature_ratio = min(1.0, float(row.get("distinctAttrSignatures") or 0) / count)
        dominant = max((float(value.get("coverageAmongEvidenceProducts") or 0) for value in top if value.get("classification") == "lexically_self_describing_candidate"), default=1.0)
        score = math.log1p(count) * min(len(usable), 20) / 20 * signature_ratio * (1 - max(0.0, dominant - 0.85))
        ranked.append({"categoryKey": row["categoryKey"], "score": round(score, 6), "evidenceProductCount": count, "usableTopValueCount": len(usable)})
    return sorted(ranked, key=lambda item: (-item["score"], item["categoryKey"]))


def analyze_group(products: list[dict[str, Any]], rule: GroupRule) -> dict[str, Any]:
    value_counts = {value: 0 for value in rule.aliases}
    unknown = conflict = 0
    conflict_reasons: dict[str, int] = {}
    observations = []
    for product in products:
        values, is_conflict, reasons = observe(product["attrs"], product["title"], rule)
        observations.append((values, is_conflict))
        if is_conflict:
            conflict += 1
            for reason in reasons:
                conflict_reasons[reason] = conflict_reasons.get(reason, 0) + 1
        elif not values:
            unknown += 1
        else:
            for value in values:
                value_counts[value] += 1
    decidable = len(products) - unknown - conflict
    supported_values = {key: count for key, count in value_counts.items() if count >= GATE["groupMinimumValueSupport"]}
    dominant_share = max(supported_values.values(), default=0) / decidable if decidable else 1.0
    coverage = decidable / len(products) if products else 0.0
    usable = coverage >= GATE["groupMinimumCoverage"] and len(supported_values) >= 2
    shortcut = usable and dominant_share >= GATE["shortcutDominanceThreshold"]
    return {
        "groupId": rule.group_id,
        "configuredValueCount": len(rule.aliases),
        "supportedValueCounts": dict(sorted(supported_values.items())),
        "decidableCount": decidable,
        "decidableCoverage": round(coverage, 6),
        "unknownCount": unknown,
        "unknownRate": round(unknown / len(products), 6) if products else 1.0,
        "conflictCount": conflict,
        "conflictRate": round(conflict / len(products), 6) if products else 0.0,
        "conflictReasons": dict(sorted(conflict_reasons.items())),
        "dominantValueShareAmongDecidable": round(dominant_share, 6),
        "shortcutRisk": shortcut,
        "ordered": bool(rule.ordered_values),
        "usable": usable,
        "note": rule.note,
        "_observations": observations,
    }


def status(observation: tuple[set[str], bool], target: str) -> str:
    values, conflict = observation
    if conflict:
        return "conflict"
    if not values:
        return "unknown"
    return "pass" if target in values else "fail"


def best_four_layer(groups: list[dict[str, Any]], product_count: int) -> dict[str, Any]:
    usable = [item for item in groups if item["usable"]]
    best: dict[str, Any] | None = None
    for hard in usable:
        for soft in usable:
            if hard["groupId"] == soft["groupId"]:
                continue
            for hard_value in hard["supportedValueCounts"]:
                for soft_value in soft["supportedValueCounts"]:
                    counts = {"fullySatisfied": 0, "softMissingOrPartial": 0, "hardFail": 0, "unknownOrConflict": 0}
                    for hard_obs, soft_obs in zip(hard["_observations"], soft["_observations"]):
                        hard_status, soft_status = status(hard_obs, hard_value), status(soft_obs, soft_value)
                        if hard_status == "pass" and soft_status == "pass":
                            counts["fullySatisfied"] += 1
                        if hard_status == "pass" and soft_status in {"fail", "unknown"}:
                            counts["softMissingOrPartial"] += 1
                        if hard_status == "fail" and soft_status == "pass":
                            counts["hardFail"] += 1
                        if hard_status in {"unknown", "conflict"}:
                            counts["unknownOrConflict"] += 1
                    checks = {
                        "fullySatisfiedAtLeast8": counts["fullySatisfied"] >= GATE["minimumFullySatisfied"],
                        "softMissingOrPartialAtLeast4": counts["softMissingOrPartial"] >= GATE["minimumSoftMissingOrPartial"],
                        "hardFailAtLeast8": counts["hardFail"] >= GATE["minimumHardFail"],
                        "unknownOrConflictAtLeast4": counts["unknownOrConflict"] >= GATE["minimumUnknownOrConflict"],
                    }
                    margin = min(
                        counts["fullySatisfied"] / GATE["minimumFullySatisfied"],
                        counts["softMissingOrPartial"] / GATE["minimumSoftMissingOrPartial"],
                        counts["hardFail"] / GATE["minimumHardFail"],
                        counts["unknownOrConflict"] / GATE["minimumUnknownOrConflict"],
                    )
                    candidate = {
                        "constructable": all(checks.values()),
                        "hardGroup": hard["groupId"], "hardValue": hard_value,
                        "softGroup": soft["groupId"], "softValue": soft_value,
                        "counts": counts, "gateChecks": checks, "balanceMargin": round(margin, 6),
                    }
                    if best is None or (candidate["constructable"], margin, sum(counts.values())) > (best["constructable"], best["balanceMargin"], sum(best["counts"].values())):
                        best = candidate
    return best or {
        "constructable": False, "hardGroup": None, "hardValue": None, "softGroup": None, "softValue": None,
        "counts": {"fullySatisfied": 0, "softMissingOrPartial": 0, "hardFail": 0, "unknownOrConflict": product_count},
        "gateChecks": {"fullySatisfiedAtLeast8": False, "softMissingOrPartialAtLeast4": False, "hardFailAtLeast8": False, "unknownOrConflictAtLeast4": product_count >= 4},
        "balanceMargin": 0.0,
    }


def capabilities(groups: list[dict[str, Any]], layers: dict[str, Any]) -> dict[str, Any]:
    usable = [item for item in groups if item["usable"]]
    ordered = [item["groupId"] for item in usable if item["ordered"]]
    safe = [item["groupId"] for item in usable if not item["shortcutRisk"]]
    unknown_groups = [item["groupId"] for item in usable if item["unknownCount"] + item["conflictCount"] >= 4]
    supported = {
        "tradeOff": layers["constructable"] and len(safe) >= 2,
        "negation": layers["counts"]["hardFail"] >= 8,
        "comparison": bool(ordered),
        "substitute": layers["counts"]["hardFail"] >= 8 and layers["counts"]["fullySatisfied"] >= 8,
        "noSolutionOrEvidenceInsufficient": bool(unknown_groups),
        "sameSessionConstraintUpdate": layers["constructable"] and len(usable) >= 2,
    }
    return {
        key: {
            "supported": value,
            "evidence": (
                ordered if key == "comparison" else unknown_groups if key == "noSolutionOrEvidenceInsufficient" else safe[:4]
            ),
        }
        for key, value in supported.items()
    }


def clean_group(group_audit: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in group_audit.items() if not key.startswith("_")}


def audit_category(category_key: str, selection_source: str, feasibility: dict[str, Any] | None,
                   products: list[dict[str, Any]]) -> dict[str, Any]:
    rules = CATALOG.get(category_key, [])
    group_audits = [analyze_group(products, rule) for rule in rules]
    usable_count = sum(item["usable"] for item in group_audits)
    safe_count = sum(item["usable"] and not item["shortcutRisk"] for item in group_audits)
    layers = best_four_layer(group_audits, len(products))
    caps = capabilities(group_audits, layers)
    capability_count = sum(value["supported"] for value in caps.values())
    checks = {
        "evidenceProductsAtLeast120": len(products) >= GATE["minimumEvidenceProducts"],
        "usableGroupsAtLeast4": usable_count >= GATE["minimumUsableGroups"],
        "safeGroupsAtLeast3": safe_count >= GATE["minimumSafeGroups"],
        "fourLayersConstructable": layers["constructable"],
        "capabilitiesAtLeast5": capability_count >= GATE["minimumSupportedCapabilities"],
    }
    readiness = (
        capability_count * 10 + usable_count * 3 + safe_count * 2 + min(20.0, layers["balanceMargin"])
        + math.log1p(len(products))
    )
    reasons = [key for key, passed in checks.items() if not passed]
    shortcut_groups = [item["groupId"] for item in group_audits if item["shortcutRisk"]]
    path = "/".join(str(feasibility.get(key, "UNKNOWN")) for key in ("categoryLevel1Name", "categoryLevel2Name", "categoryLevel3Name")) if feasibility else "UNKNOWN"
    return {
        "categoryKey": category_key,
        "categoryPath": path,
        "selectionSource": selection_source,
        "sourceCounts": {
            "evidenceProductCount": len(products),
            "itemCount": int(feasibility.get("itemCount") or 0) if feasibility else 0,
            "productsWithNonemptyAttr": int(feasibility.get("productsWithNonemptyAttr") or 0) if feasibility else 0,
            "distinctAttrSignatures": int(feasibility.get("distinctAttrSignatures") or 0) if feasibility else 0,
        },
        "configuredIndependentGroupCount": len(rules),
        "usableIndependentGroupCount": usable_count,
        "safeIndependentGroupCount": safe_count,
        "groups": [clean_group(item) for item in group_audits],
        "fourLayerSupport": layers,
        "capabilities": caps,
        "supportedCapabilityCount": capability_count,
        "shortcutRisk": {"present": bool(shortcut_groups), "groups": shortcut_groups, "mitigation": "不得仅用高频值出题；组合必须跨至少两个安全属性组并保留困难层"},
        "prohibitedOrUnverifiableNeeds": PROHIBITED_NEEDS.get(category_key, ["未建立受控字段映射的需求"]),
        "gateChecks": checks,
        "rawGatePassed": all(checks.values()),
        "readinessScore": round(readiness, 6),
        "verdict": "PENDING_SELECTION",
        "reasons": reasons,
        "limitations": [
            "attr_value 是无键名扁平值；仅受控自描述值可用于判定",
            "标题仅用于冲突检测，不能补足缺失属性",
            "本审计计数是自动规则诊断，不是人工金标或 qrel",
        ],
    }


def build(evidence_path: Path, feasibility_path: Path, output_dir: Path, schema_path: Path) -> dict[str, Any]:
    feasibility_rows = json.loads(feasibility_path.read_text(encoding="utf-8"))
    if not isinstance(feasibility_rows, list):
        raise ValueError("exact category feasibility input must be a JSON array")
    feasibility_by_key = {row["categoryKey"]: row for row in feasibility_rows}
    ranking = auto_rank(feasibility_rows)
    backups = [row["categoryKey"] for row in ranking if row["categoryKey"] not in MANDATORY_CATEGORY_KEYS and row["categoryKey"] in CATALOG][:AUTO_BACKUP_COUNT]
    candidate_keys = list(MANDATORY_CATEGORY_KEYS) + backups
    products: dict[str, list[dict[str, Any]]] = {key: [] for key in candidate_keys}
    with evidence_path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            category_key = row.get("categoryKey")
            if category_key in products:
                products[category_key].append({
                    "title": str(row.get("title", "")),
                    "attrs": tuple(str(value) for value in row.get("normalizedAttrValues", [])),
                })
    categories = [
        audit_category(key, "mandatory" if key in MANDATORY_CATEGORY_KEYS else "automatic_ranked_backup", feasibility_by_key.get(key), products[key])
        for key in candidate_keys
    ]
    eligible = sorted((row for row in categories if row["rawGatePassed"]), key=lambda row: (-row["readinessScore"], row["categoryKey"]))
    selected_keys = {row["categoryKey"] for row in eligible[:GATE["maximumSelectedGoCategories"]]}
    for row in categories:
        if row["categoryKey"] in selected_keys:
            row["verdict"] = "GO"
            row["reasons"] = ["all_data_gates_passed", "selected_within_maximum_three_go_categories"]
        elif row["rawGatePassed"]:
            row["verdict"] = "HOLD_CAPABLE_NOT_SELECTED"
            row["reasons"] = ["all_data_gates_passed", "not_selected_due_to_three_category_cap"]
        else:
            row["verdict"] = "NO_GO"
            row["reasons"] = [key for key, passed in row["gateChecks"].items() if not passed]

    if not selected_keys:
        overall = "NO_GO"
        first_scope = None
    else:
        overall = "GO"
        first = next(row for row in eligible if row["categoryKey"] in selected_keys)
        first_scope = {
            "categoryKey": first["categoryKey"], "categoryPath": first["categoryPath"],
            "caseCount": 8, "scope": "one_category_eight_cases_first; expand only after independent AI blind audit/adjudication",
            "supportedCapabilityTypes": [key for key, value in first["capabilities"].items() if value["supported"]],
        }

    audit = {
        "schemaVersion": SCHEMA_VERSION,
        "auditId": AUDIT_ID,
        "datasetRevision": DATASET_REVISION,
        "ruleVersion": RULE_VERSION,
        "status": "audit_complete_no_queries_no_qrels",
        "networkUsed": False,
        "usesLlmOrApi": False,
        "inputs": {
            "evidenceProductsAudit": {"path": stable_repository_relative_path(evidence_path), "bytes": evidence_path.stat().st_size, "sha256": sha256_file(evidence_path)},
            "exactCategoryAttributeFeasibilityAudit": {"path": stable_repository_relative_path(feasibility_path), "bytes": feasibility_path.stat().st_size, "sha256": sha256_file(feasibility_path)},
        },
        "gateRules": GATE,
        "automaticRanking": {"method": "evidence_count_x_usable_top_values_x_signature_diversity_minus_dominance", "top20": ranking[:20], "selectedBackupCategoryKeys": backups},
        "candidateCategoryCount": len(categories),
        "categories": categories,
        "selection": {
            "overallVerdict": overall,
            "goCategoryKeys": sorted(selected_keys),
            "maximumGoCategories": GATE["maximumSelectedGoCategories"],
            "recommendedFirstEightCaseScope": first_scope,
        },
        "provenance": {
            "categoryLabels": "automatic_rule_based_capability_audit_not_gold",
            "humanApproved": False,
            "adjudicationMode": "independent_ai_blind_audit",
            "queriesGenerated": False,
            "qrelsGenerated": False,
            "formalMetricsRun": False,
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "complex_category_capability_audit.json", audit)
    report_lines = [
        "# Track B complex category capability audit v1", "",
        "> 只读、规则化、无网络审计；没有生成 Query、qrel、商品判断或正式指标。", "",
        "| Verdict | 类目 | Evidence | 可用组/安全组 | 四层 | 能力数 | 高频捷径组 |",
        "|---|---|---:|---:|---|---:|---|",
    ]
    for row in categories:
        report_lines.append(
            f"| {row['verdict']} | {row['categoryKey']} {row['categoryPath']} | {row['sourceCounts']['evidenceProductCount']} | "
            f"{row['usableIndependentGroupCount']}/{row['safeIndependentGroupCount']} | {'是' if row['fourLayerSupport']['constructable'] else '否'} | "
            f"{row['supportedCapabilityCount']} | {', '.join(row['shortcutRisk']['groups']) or '无'} |"
        )
    report_lines += ["", "## GO 结论", "", f"整体：**{overall}**。", ""]
    if first_scope:
        report_lines.append(f"首批建议：仅 `{first_scope['categoryKey']} {first_scope['categoryPath']}` 做 8 case；独立 AI 盲审/裁决后再考虑其余 GO 类目。")
    else:
        report_lines.append("没有类目达到 Gate；停止扩张。")
    report_lines += [
        "", "## 输入与自动备选", "",
        f"- evidence products：`{audit['inputs']['evidenceProductsAudit']['path']}`，bytes={audit['inputs']['evidenceProductsAudit']['bytes']}，SHA-256=`{audit['inputs']['evidenceProductsAudit']['sha256']}`。",
        f"- exact-category feasibility：`{audit['inputs']['exactCategoryAttributeFeasibilityAudit']['path']}`，bytes={audit['inputs']['exactCategoryAttributeFeasibilityAudit']['bytes']}，SHA-256=`{audit['inputs']['exactCategoryAttributeFeasibilityAudit']['sha256']}`。",
        f"- 自动深审备选：{', '.join(backups) if backups else '无'}。排名只用于发现候选，不直接产生 GO。",
        "", "## 类目详情", "",
    ]
    for row in categories:
        supported = [key for key, value in row["capabilities"].items() if value["supported"]]
        unsupported = [key for key, value in row["capabilities"].items() if not value["supported"]]
        layer_counts = row["fourLayerSupport"]["counts"]
        report_lines += [
            f"### {row['categoryKey']} {row['categoryPath']} — {row['verdict']}", "",
            f"- 证据商品 {row['sourceCounts']['evidenceProductCount']}；配置组 {row['configuredIndependentGroupCount']}；可用组 {row['usableIndependentGroupCount']}；非捷径安全组 {row['safeIndependentGroupCount']}。",
            f"- 四层：full={layer_counts['fullySatisfied']}，soft-missing/partial={layer_counts['softMissingOrPartial']}，hard-fail={layer_counts['hardFail']}，unknown/conflict={layer_counts['unknownOrConflict']}。",
            f"- 支持能力：{', '.join(supported) if supported else '无'}；不支持：{', '.join(unsupported) if unsupported else '无'}。",
            f"- 高频捷径组：{', '.join(row['shortcutRisk']['groups']) or '无'}；这些组不得单独构题。",
            f"- 判定理由：{', '.join(row['reasons'])}。",
            f"- 禁用需求：{'；'.join(row['prohibitedOrUnverifiableNeeds'])}。",
            "",
        ]
    report_lines += ["## 共同禁区", "", "- 无键名的支持/不支持/是/否/数字不能映射到具体字段。", "- 标题营销词不能替代 attr_value。", "- 不推断 FPS、真实续航、舒适、耐用、质量、性价比或真实防摔效果。", ""]
    write_utf8_lf(output_dir / "complex_category_capability_report.md", "\n".join(report_lines))
    write_utf8_lf(output_dir / schema_path.name, schema_path.read_text(encoding="utf-8"))
    artifact_names = sorted(path.name for path in output_dir.iterdir() if path.name != "manifest.json")
    manifest = {
        "auditId": AUDIT_ID, "schemaVersion": SCHEMA_VERSION, "ruleVersion": RULE_VERSION,
        "status": "audit_complete_no_queries_no_qrels", "overallVerdict": overall,
        "counts": {"candidates": len(categories), "go": sum(row["verdict"] == "GO" for row in categories), "hold": sum(row["verdict"].startswith("HOLD") for row in categories), "noGo": sum(row["verdict"] == "NO_GO" for row in categories)},
        "inputs": audit["inputs"],
        "artifacts": [{"path": name, "bytes": (output_dir / name).stat().st_size, "sha256": sha256_file(output_dir / name)} for name in artifact_names],
        "networkUsed": False, "queriesGenerated": False, "qrelsGenerated": False, "formalMetricsRun": False,
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--feasibility", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema", type=Path, default=Path(__file__).resolve().parent / "schemas" / "track_b_complex_category_capability_audit_v1.schema.json")
    args = parser.parse_args()
    manifest = build(args.evidence.resolve(), args.feasibility.resolve(), args.output.resolve(), args.schema.resolve())
    print(canonical_json({"counts": manifest["counts"], "overallVerdict": manifest["overallVerdict"]}))


if __name__ == "__main__":
    main()
