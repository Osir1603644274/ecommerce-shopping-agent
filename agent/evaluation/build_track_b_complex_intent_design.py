"""Build Track B complex-intent *design* artifacts, never formal qrels.

The builder reads only the already-audited evidence-product JSONL.  It computes
aggregate support strata in bounded memory, emits pending intent contracts and
blind approval cards, and deliberately omits per-product judgments.  All
labels produced here are automatic support diagnostics, not human gold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator


BASE_BENCHMARK_ID = "kuaisearch-synthetic-evidence-track-b-v01"
COMPLEX_BENCHMARK_ID = "kuaisearch-synthetic-evidence-track-b-complex-design-v01"
DESIGN_VERSION = "complex-intent-design-v1"
SCHEMA_VERSION = "track-b-complex-intent-contract-v1"
DATASET_REVISION = "09807c773ce67360ed8df30842e372182fcf7ad9"
SOURCE_EVIDENCE_REPO_PATH = (
    "data/processed/ecommerce/kuaisearch_synthetic_evidence_track_b_v01/"
    f"{DATASET_REVISION}/evidence_products_audit.jsonl"
)
PINNED_RAW_SOURCE_HASHES = {
    "itemsLiteTrain": "5c04e031324a37636afb2f822a4d862378d7f54eada93875d686fa8f31bd2621",
    "relevanceTrain": "b26c79a3ae6fde4aadc756c7b9eda9f6dcc6e112b725db603def868ab9f9c720",
}
MAIN_WINDOW_APPROVAL_SOURCE = {
    "sourceThreadId": "019fe29c-778b-7053-8d9b-33b2d92faeb1",
    "scope": "intent_query_naturalness_and_logic_only",
    "recordedBy": "delegated_main_window_sync",
}
APPROVED_INTENT_GROUPS = {"T1", "T2", "T3", "T4"}

CATEGORIES = {
    "1/39/0": "女装/T恤",
    "1/13/103": "女装/牛仔裤",
    "23/40/217": "女鞋/休闲板鞋",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def stable_repository_relative_path(path: Path) -> str:
    """Return a portable repository-relative path without leaking its root."""
    parts = path.resolve().parts
    marker_indexes = [index for index, part in enumerate(parts) if part in {"data", "tests"}]
    if not marker_indexes:
        raise ValueError(f"cannot derive a stable repository-relative path for {path.name!r}")
    relative = "/".join(parts[marker_indexes[-1]:])
    if not relative or relative.startswith("/") or "\\" in relative or ".." in relative.split("/"):
        raise ValueError(f"unsafe repository-relative path: {relative!r}")
    return relative


def write_utf8_lf(path: Path, text: str) -> None:
    """Write deterministic UTF-8 bytes with LF newlines on every platform."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(normalized.encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    write_utf8_lf(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    write_utf8_lf(path, "".join(canonical_json(row) + "\n" for row in rows))


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"expected object at {path}:{line_number}")
            yield row


@dataclass(frozen=True)
class Product:
    item_id: str
    category_key: str
    title: str
    brand: str
    seller: str
    attrs: tuple[str, ...]
    raw_attr: str


@dataclass(frozen=True)
class GroupSpec:
    aliases: dict[str, tuple[str, ...]]
    exclusive: bool = True
    contains: tuple[tuple[str, str], ...] = ()


GROUPS: dict[str, GroupSpec] = {
    "sleeve_length": GroupSpec(
        {"short": ("短袖",), "long": ("长袖",), "sleeveless": ("无袖",),
         "three_quarter": ("七分袖",), "half": ("五分袖",), "nine_tenths": ("九分袖",)}
    ),
    "tshirt_fit": GroupSpec(
        {"loose": ("宽松型", "宽松"), "slim": ("修身型", "修身"), "standard": ("标准型", "标准")}
    ),
    "collar": GroupSpec(
        {"round": ("圆领",), "v_neck": ("v领", "V领"), "polo": ("polo领", "POLO领")}
    ),
    # Materials are intentionally non-exclusive. Cotton blends count as cotton.
    "tshirt_material": GroupSpec(
        {"cotton": ("棉",), "pure_cotton": ("纯棉",),
         "polyester": ("涤纶(聚酯纤维)", "聚酯纤维", "涤纶")}, exclusive=False
    ),
    "waist_height": GroupSpec(
        {"high": ("高腰",), "mid": ("中腰",), "low": ("低腰",), "mid_low": ("中低腰",)}
    ),
    "leg_shape": GroupSpec(
        {"straight": ("直筒裤", "直筒"), "wide": ("阔脚裤", "阔腿裤", "阔腿", "阔脚"),
         "flare": ("微喇裤", "微喇"), "skinny": ("小脚裤", "小脚"), "harem": ("哈伦裤", "哈伦")}
    ),
    "jeans_fit": GroupSpec({"loose": ("宽松",), "slim": ("修身",)}),
    "jeans_material": GroupSpec(
        {"denim": ("牛仔布",), "cotton": ("棉",),
         "polyester": ("涤纶(聚酯纤维)", "聚酯纤维", "涤纶")}, exclusive=False
    ),
    "closure": GroupSpec(
        {"lace_up": ("系带",), "pull_on": ("套脚",), "slip_on": ("一脚蹬",),
         "hook_loop": ("魔术贴",)}, contains=(("slip_on", "pull_on"),)
    ),
    "heel_height": GroupSpec(
        {"flat": ("平底",), "low": ("低跟(1-3cm)", "低跟"),
         "mid": ("中跟(3-5cm)", "中跟"), "high": ("高跟(5-8cm)", "高跟")}
    ),
    "upper_material": GroupSpec(
        {"synthetic_leather": ("合成革",), "textile": ("织物/纺织品", "织物", "纺织品"),
         "synthetic_fiber": ("合成纤维",), "leather": ("真皮",)}, exclusive=False
    ),
    "toe_shape": GroupSpec(
        {"round": ("圆头",), "pointed": ("尖头",), "square": ("方头",)}
    ),
}


def _token_matches(token: str, alias: str, *, material: bool) -> bool:
    token_folded = token.casefold()
    alias_folded = alias.casefold()
    if token_folded == alias_folded:
        return True
    if material and alias_folded in token_folded:
        return True
    return False


def _canonical_values(texts: Iterable[str], spec: GroupSpec, *, attr: bool, group: str) -> set[str]:
    found: set[str] = set()
    material = group.endswith("material")
    for text in texts:
        for canonical, aliases in spec.aliases.items():
            for alias in aliases:
                if (attr and _token_matches(text, alias, material=material)) or (
                    not attr and alias.casefold() in text.casefold()
                ):
                    found.add(canonical)
                    break
    # Specific phrases dominate their substrings.
    if group == "waist_height" and "mid_low" in found:
        found.discard("mid")
        found.discard("low")
    if group == "tshirt_material" and "pure_cotton" in found:
        found.add("cotton")
    for child, parent in spec.contains:
        if child in found:
            found.add(parent)
    return found


def observe_group(product: Product, group: str) -> dict[str, Any]:
    spec = GROUPS[group]
    attr_values = _canonical_values(product.attrs, spec, attr=True, group=group)
    title_values = _canonical_values((product.title,), spec, attr=False, group=group)
    conflicts: list[str] = []

    if spec.exclusive:
        attr_base = set(attr_values)
        title_base = set(title_values)
        # Containment-related closure values are not competing values.
        for child, parent in spec.contains:
            if child in attr_base:
                attr_base.discard(parent)
            if child in title_base:
                title_base.discard(parent)
        if len(attr_base) > 1:
            conflicts.append("multiple_incompatible_attr_values")
        if attr_base and title_base and not title_base.issubset(attr_base):
            conflicts.append("title_attr_disagreement")

    # Frozen special rule: flat+low is unknown unless the query explicitly allows both.
    if group == "heel_height" and {"flat", "low"}.issubset(attr_values | title_values):
        conflicts.append("flat_low_joint_evidence")

    return {
        "attrValues": sorted(attr_values),
        "titleValues": sorted(title_values),
        "conflicts": sorted(set(conflicts)),
    }


def atom_status(product: Product, atom: dict[str, Any]) -> str:
    observed = observe_group(product, atom["group"])
    attr_values = set(observed["attrValues"])
    allowed = set(atom["allowedValues"])

    # Frozen boundary rule: 中低腰 cannot decide a hard 中腰 requirement.
    if atom["group"] == "waist_height" and "mid" in allowed and "mid_low" in attr_values:
        return "unknown"

    # Frozen exception: a query explicitly allowing flat OR low can accept the pair.
    flat_low_allowed = (
        atom["group"] == "heel_height" and {"flat", "low"}.issubset(allowed)
    )
    flat_low_exception = (
        flat_low_allowed
        and set(observed["conflicts"]).issubset(
            {"flat_low_joint_evidence", "multiple_incompatible_attr_values"}
        )
        and attr_values.issubset(allowed)
    )
    if observed["conflicts"] and not flat_low_exception:
        return "conflict"

    # Attribute evidence is authoritative; title-only mentions stay unknown.
    if not attr_values:
        return "unknown"

    if atom["operator"] == "IN":
        return "pass" if attr_values & allowed else "fail"
    if atom["operator"] == "NOT_IN":
        return "fail" if attr_values & allowed else "pass"
    raise ValueError(f"unsupported atom operator: {atom['operator']}")


def severe_category_title_conflict(product: Product) -> bool:
    patterns = {
        "1/39/0": ("牛仔裤", "板鞋", "连衣裙", "外套", "半身裙"),
        "1/13/103": ("T恤", "t恤", "板鞋", "连衣裙", "外套"),
        "23/40/217": ("T恤", "t恤", "牛仔裤", "连衣裙", "外套"),
    }
    return any(term in product.title for term in patterns[product.category_key])


def make_atom(
    atom_id: str,
    group: str,
    allowed: list[str],
    importance: str,
    *,
    operator: str = "IN",
) -> dict[str, Any]:
    return {
        "atomId": atom_id,
        "group": group,
        "operator": operator,
        "allowedValues": allowed,
        "polarity": "negative" if operator == "NOT_IN" else "positive",
        "importance": importance,
        "evidenceRule": "attr_primary_title_conflict_check",
        "missingTreatment": "unknown",
    }


def _definition(
    intent_id: str,
    category: str,
    atoms: list[dict[str, Any]],
    variants: tuple[str, str, str],
    risks: list[str],
) -> dict[str, Any]:
    hard_ids = [a["atomId"] for a in atoms if a["importance"] == "hard"]
    soft_ids = [a["atomId"] for a in atoms if a["importance"] == "soft"]
    return {
        "intentGroupId": intent_id,
        "categoryKey": category,
        "atoms": atoms,
        "logicAst": {
            "operator": "AND",
            "hard": [{"atomRef": atom_id} for atom_id in hard_ids],
            "softPreference": [{"atomRef": atom_id} for atom_id in soft_ids],
        },
        "variants": variants,
        "risks": risks,
    }


SOLVABLE_DEFINITIONS = [
    _definition("T1", "1/39/0", [
        make_atom("T1-sleeve", "sleeve_length", ["long"], "hard"),
        make_atom("T1-fit", "tshirt_fit", ["loose"], "hard"),
    ], ("想找长袖、宽松的女款T恤。", "想看看长袖而且版型宽松的女款T恤。", "我想买件女款T恤，袖子要长袖，穿起来别太贴身，要宽松版型的。"),
       ["标题中的版型营销词可能与属性字段冲突"]),
    _definition("T2", "1/39/0", [
        make_atom("T2-sleeve", "sleeve_length", ["short"], "hard"),
        make_atom("T2-fit", "tshirt_fit", ["slim"], "hard"),
        make_atom("T2-material", "tshirt_material", ["cotton"], "soft"),
    ], ("想找短袖修身的女款T恤，棉质优先。", "想看看短袖、修身版型的女款T恤，面料含棉的优先。", "我想买件女款T恤，短袖和修身版型是必须的，要是面料明确含棉就更好。"),
       ["棉质包含有明确含棉证据的混纺，不等同于纯棉"]),
    _definition("T3", "1/39/0", [
        make_atom("T3-sleeve", "sleeve_length", ["short", "long"], "hard"),
        make_atom("T3-collar", "collar", ["round"], "hard"),
        make_atom("T3-material", "tshirt_material", ["cotton"], "soft"),
    ], ("想找圆领女款T恤，短袖或长袖都可以，棉质优先。", "想看看圆领的女款T恤，袖长短袖、长袖均可，含棉面料优先。", "我想买件圆领女款T恤，短袖和长袖我都能接受，如果属性里明确写了含棉就优先看看。"),
       ["短袖或长袖是同一 IN 原子，不能误写为同时满足"]),
    _definition("T4", "1/39/0", [
        make_atom("T4-fit", "tshirt_fit", ["slim"], "hard", operator="NOT_IN"),
        make_atom("T4-collar", "collar", ["round"], "hard"),
        make_atom("T4-material", "tshirt_material", ["cotton"], "soft"),
    ], ("想找圆领女款T恤，不要修身版型，棉质优先。", "想看看圆领的女款T恤，版型不要修身，含棉面料优先。", "我想买件圆领女款T恤，修身版型先排除掉，其他版型可以，属性里明确含棉的优先。"),
       ["否定条件只有在属性给出明确非修身值时才能通过"]),
    _definition("J1", "1/13/103", [
        make_atom("J1-waist", "waist_height", ["high"], "hard"),
        make_atom("J1-leg", "leg_shape", ["straight"], "hard"),
    ], ("想找高腰直筒的女款牛仔裤。", "想看看腰型是高腰、裤型是直筒的女款牛仔裤。", "我想买条女款牛仔裤，高腰和直筒这两个条件都得满足。"),
       ["标题常同时堆叠直筒、阔腿等裤型词，冲突项不能进入完整匹配"]),
    _definition("J2", "1/13/103", [
        make_atom("J2-waist", "waist_height", ["high"], "hard"),
        make_atom("J2-leg", "leg_shape", ["wide"], "hard"),
        make_atom("J2-material", "jeans_material", ["denim"], "soft"),
    ], ("想找高腰阔腿的女款牛仔裤，牛仔布材质优先。", "想看看高腰、阔腿裤型的女款牛仔裤，属性明确是牛仔布的优先。", "我想买条女款牛仔裤，高腰和阔腿是必须的，如果材质字段明确写着牛仔布就优先。"),
       ["阔脚与阔腿归一；类别名牛仔裤不能替代牛仔布材质证据"]),
    _definition("J3", "1/13/103", [
        make_atom("J3-leg", "leg_shape", ["straight", "wide"], "hard"),
        make_atom("J3-waist", "waist_height", ["high"], "hard"),
        make_atom("J3-material", "jeans_material", ["denim"], "soft"),
    ], ("想找高腰女款牛仔裤，直筒或阔腿都可以，牛仔布材质优先。", "想看看高腰的女款牛仔裤，裤型直筒、阔腿均可，属性明确是牛仔布的优先。", "我想买条高腰女款牛仔裤，直筒和阔腿我都能接受，如果材质字段明确写了牛仔布就优先。"),
       ["直筒或阔腿是同一 IN 原子；标题裤型堆词可能触发冲突"]),
    _definition("J4", "1/13/103", [
        make_atom("J4-fit", "jeans_fit", ["slim"], "hard", operator="NOT_IN"),
        make_atom("J4-waist", "waist_height", ["high"], "hard"),
        make_atom("J4-material", "jeans_material", ["denim"], "soft"),
    ], ("想找高腰女款牛仔裤，不要修身版型，牛仔布材质优先。", "想看看高腰的女款牛仔裤，版型不要修身，属性明确是牛仔布的优先。", "我想买条高腰女款牛仔裤，修身版型先排除，其他版型可以，材质字段写明牛仔布的优先。"),
       ["缺少版型字段不能由‘未出现修身’推成通过"]),
    _definition("S1", "23/40/217", [
        make_atom("S1-closure", "closure", ["lace_up"], "hard"),
        make_atom("S1-heel", "heel_height", ["mid"], "hard"),
    ], ("想找系带、中跟的女款休闲板鞋。", "想看看系带款、鞋跟为中跟的女款休闲板鞋。", "我想买双女款休闲板鞋，系带和中跟这两个条件都要满足。"),
       ["鞋跟标题与属性不一致时按冲突处理"]),
    _definition("S2", "23/40/217", [
        make_atom("S2-closure", "closure", ["lace_up"], "hard"),
        make_atom("S2-upper", "upper_material", ["synthetic_leather"], "hard"),
        make_atom("S2-heel", "heel_height", ["mid"], "soft"),
    ], ("想找系带、合成革鞋面的女款休闲板鞋，中跟优先。", "想看看系带且鞋面为合成革的女款休闲板鞋，鞋跟是中跟的优先。", "我想买双女款休闲板鞋，系带和合成革鞋面是必须的，如果还是中跟就优先看看。"),
       ["复合鞋面可有多个材质值，不能机械判成冲突"]),
    _definition("S3", "23/40/217", [
        make_atom("S3-upper", "upper_material", ["textile", "synthetic_leather"], "hard"),
        make_atom("S3-closure", "closure", ["lace_up"], "hard"),
        make_atom("S3-heel", "heel_height", ["mid"], "soft"),
    ], ("想找系带女款休闲板鞋，织物或合成革鞋面都可以，中跟优先。", "想看看系带的女款休闲板鞋，鞋面是织物、合成革均可，鞋跟中跟的优先。", "我想买双系带女款休闲板鞋，织物鞋面和合成革鞋面我都接受，如果鞋跟是中跟就优先。"),
       ["织物或合成革是同一 IN 原子，复合鞋面也可满足"]),
    _definition("S4", "23/40/217", [
        make_atom("S4-toe", "toe_shape", ["pointed"], "hard", operator="NOT_IN"),
        make_atom("S4-closure", "closure", ["lace_up"], "hard"),
        make_atom("S4-upper", "upper_material", ["textile"], "soft"),
    ], ("想找系带女款休闲板鞋，不要尖头，织物鞋面优先。", "想看看系带的女款休闲板鞋，鞋头不要尖头，织物鞋面的优先。", "我想买双系带女款休闲板鞋，尖头款先排除掉，其他鞋头可以，织物鞋面就优先看看。"),
       ["否定尖头需属性给出圆头或方头；缺失不能自动通过"]),
]


def contract_logic_hash(definition: dict[str, Any]) -> str:
    logic = {
        "categoryKey": definition["categoryKey"],
        "atoms": definition["atoms"],
        "logicAst": definition["logicAst"],
    }
    return sha256_bytes(canonical_json(logic).encode("utf-8"))


def build_contract(definition: dict[str, Any]) -> dict[str, Any]:
    logic_hash = contract_logic_hash(definition)
    styles = ("standard_concise", "natural_medium", "colloquial_long")
    intent_approved = definition["intentGroupId"] in APPROVED_INTENT_GROUPS
    return {
        "schemaVersion": SCHEMA_VERSION,
        "benchmarkId": COMPLEX_BENCHMARK_ID,
        "designVersion": DESIGN_VERSION,
        "intentGroupId": definition["intentGroupId"],
        "category": {"categoryKey": definition["categoryKey"], "label": CATEGORIES[definition["categoryKey"]]},
        "logicAst": definition["logicAst"],
        "atoms": definition["atoms"],
        "expectedAction": "RETRIEVE_FILTER_AND_RANK",
        "queryVariants": [
            {"variantId": f"{definition['intentGroupId']}-{index + 1}", "style": style,
             "text": text, "semanticContractHash": logic_hash,
             "approvalStatus": "approved" if intent_approved else "pending"}
            for index, (style, text) in enumerate(zip(styles, definition["variants"]))
        ],
        "qrelContract": {"reuseKey": definition["intentGroupId"], "status": "pending_not_generated"},
        "goldStatus": "pending",
        "approval": {
            "queryNaturalness": "approved" if intent_approved else "pending",
            "semanticEquivalence": "approved" if intent_approved else "pending",
            "productAnswers": "pending",
            "source": MAIN_WINDOW_APPROVAL_SOURCE if intent_approved else None,
        },
        "provenance": {
            "queryOrigin": "synthetic_rule_and_llm_assisted_draft",
            "supportLabels": "automatic_evidence_audit_not_gold",
            "humanApproved": intent_approved,
            "humanApprovalScope": "intent_only_product_answers_pending" if intent_approved else "none",
            "usesSourceEditorialScore": False,
        },
    }


def load_products(path: Path) -> list[Product]:
    products: list[Product] = []
    for row in iter_jsonl(path):
        category = row.get("categoryKey")
        if category not in CATEGORIES:
            continue
        refs = row.get("evidenceRefs") or []
        raw_attr = " | ".join(str(ref.get("rawValue", "")) for ref in refs if ref.get("field") == "attr_value")
        products.append(Product(
            item_id=str(row["itemId"]), category_key=category, title=str(row.get("title", "")),
            brand=str(row.get("brand", "")), seller=str(row.get("seller", "")),
            attrs=tuple(str(value) for value in row.get("normalizedAttrValues", [])), raw_attr=raw_attr,
        ))
    return products


def support_counts(definition: dict[str, Any], products: Iterable[Product]) -> dict[str, Any]:
    counts = {
        "categoryProducts": 0,
        "severeCategoryTitleConflict": 0,
        "cleanFullPass": 0,
        "hardPassSoftFailOrMissing": 0,
        "singleHardFail": 0,
        "hardMissingOrConflict": 0,
        "hardMissing": 0,
        "hardConflict": 0,
        "softConflict": 0,
    }
    hard_atoms = [a for a in definition["atoms"] if a["importance"] == "hard"]
    soft_atoms = [a for a in definition["atoms"] if a["importance"] == "soft"]
    for product in products:
        if product.category_key != definition["categoryKey"]:
            continue
        counts["categoryProducts"] += 1
        if severe_category_title_conflict(product):
            counts["severeCategoryTitleConflict"] += 1
            continue
        hard = [atom_status(product, atom) for atom in hard_atoms]
        soft = [atom_status(product, atom) for atom in soft_atoms]
        if all(value == "pass" for value in hard) and all(value == "pass" for value in soft):
            counts["cleanFullPass"] += 1
        if soft and all(value == "pass" for value in hard) and all(value in {"fail", "unknown"} for value in soft):
            counts["hardPassSoftFailOrMissing"] += 1
        if hard.count("fail") == 1 and all(value in {"pass", "fail"} for value in hard):
            counts["singleHardFail"] += 1
        if any(value in {"unknown", "conflict"} for value in hard):
            counts["hardMissingOrConflict"] += 1
        if any(value == "unknown" for value in hard):
            counts["hardMissing"] += 1
        if any(value == "conflict" for value in hard):
            counts["hardConflict"] += 1
        if any(value == "conflict" for value in soft):
            counts["softConflict"] += 1
    counts["softLayerWaived"] = not soft_atoms
    checks = {
        "cleanFullPassAtLeast8": counts["cleanFullPass"] >= 8,
        "hardPassSoftFailOrMissingAtLeast4": not soft_atoms or counts["hardPassSoftFailOrMissing"] >= 4,
        "singleHardFailAtLeast8": counts["singleHardFail"] >= 8,
        "hardMissingOrConflictAtLeast4": counts["hardMissingOrConflict"] >= 4,
    }
    return {**counts, "gateChecks": checks, "gatePassed": all(checks.values())}


ACTION_INTENTS = [
    ("A-T-CLARIFY", "1/39/0", "想找不要太紧也不要太宽松的女款T恤。", "CLARIFY",
     "‘不要太紧也不要太宽松’不能唯一映射到宽松型、标准型或修身型边界。",
     "你说的版型更接近标准型，还是可以接受稍宽松一些？"),
    ("A-T-EVIDENCE", "1/39/0", "想找穿着很凉快、不会闷的女款T恤。", "ABSTAIN_OR_EXPLAIN",
     "数据库没有可靠的体感凉爽、透气或闷热证据字段。", None),
    ("A-J-CLARIFY", "1/13/103", "想找腰别太高也别太低的女款牛仔裤。", "CLARIFY",
     "表达可能指中腰或中低腰；中低腰不能自动作为中腰通过。",
     "你希望明确的中腰，还是中低腰也可以？"),
    ("A-J-EVIDENCE", "1/13/103", "想找穿久了也不变形的女款牛仔裤。", "ABSTAIN_OR_EXPLAIN",
     "数据库没有耐久、长期形变或穿着测试证据。", None),
    ("A-S-CLARIFY", "23/40/217", "想找有一点跟但不要太高的女款休闲板鞋。", "CLARIFY",
     "‘有一点跟但不要太高’无法唯一映射到低跟或中跟。",
     "低跟和中跟你更倾向哪一种，还是两种都可以？"),
    ("A-S-EVIDENCE", "23/40/217", "想找走一整天也不会累脚的女款休闲板鞋。", "ABSTAIN_OR_EXPLAIN",
     "数据库没有全天穿着舒适度、足部压力或实测证据。", None),
]


def build_action_intents() -> list[dict[str, Any]]:
    return [{
        "schemaVersion": "track-b-complex-action-intent-v1",
        "benchmarkId": COMPLEX_BENCHMARK_ID,
        "designVersion": DESIGN_VERSION,
        "intentGroupId": intent_id,
        "category": {"categoryKey": category, "label": CATEGORIES[category]},
        "queryText": query,
        "expectedAction": action,
        "decisionReason": reason,
        "clarifyingQuestionDraft": question,
        "normalNdcgQrel": False,
        "goldStatus": "pending",
        "approval": {"queryNaturalness": "pending", "actionSemantics": "pending"},
        "provenance": {"queryOrigin": "synthetic_rule_and_llm_assisted_draft", "humanApproved": False},
    } for intent_id, category, query, action, reason, question in ACTION_INTENTS]


MULTI_TURN = [
    ("M-T-REPLACE", "1/39/0", "constraint_replacement", "T1", [
        ("user", "想找长袖修身的女款T恤。"),
        ("user", "修身改成宽松吧，长袖这个条件保留。"),
    ], {"replace": {"group": "tshirt_fit", "from": "slim", "to": "loose"}, "preserve": {"sleeve_length": "long"}}),
    ("M-T-COMPLETE", "1/39/0", "clarification_completion", "T2", [
        ("user", "想找短袖修身的女款T恤。"),
        ("assistant", "面料方面有优先偏好吗？"),
        ("user", "明确含棉的优先。"),
    ], {"addSoft": {"group": "tshirt_material", "value": "cotton"}, "preserve": {"sleeve_length": "short", "tshirt_fit": "slim"}}),
    ("M-J-REPLACE", "1/13/103", "constraint_replacement", "J1", [
        ("user", "想找高腰微喇的女款牛仔裤。"),
        ("user", "微喇改成直筒，高腰继续保留。"),
    ], {"replace": {"group": "leg_shape", "from": "flare", "to": "straight"}, "preserve": {"waist_height": "high"}}),
    ("M-J-COMPLETE", "1/13/103", "clarification_completion", "J2", [
        ("user", "想找高腰阔腿的女款牛仔裤。"),
        ("assistant", "材质方面有优先偏好吗？"),
        ("user", "属性明确是牛仔布的优先。"),
    ], {"addSoft": {"group": "jeans_material", "value": "denim"}, "preserve": {"waist_height": "high", "leg_shape": "wide"}}),
    ("M-S-REPLACE", "23/40/217", "constraint_replacement", "S1", [
        ("user", "想找系带低跟的女款休闲板鞋。"),
        ("user", "低跟换成中跟，系带这个条件不变。"),
    ], {"replace": {"group": "heel_height", "from": "low", "to": "mid"}, "preserve": {"closure": "lace_up"}}),
    ("M-S-COMPLETE", "23/40/217", "clarification_completion", "S2", [
        ("user", "想找系带、合成革鞋面的女款休闲板鞋。"),
        ("assistant", "鞋跟高度有偏好吗？"),
        ("user", "中跟的优先。"),
    ], {"addSoft": {"group": "heel_height", "value": "mid"}, "preserve": {"closure": "lace_up", "upper_material": "synthetic_leather"}}),
]


def build_multi_turn(contracts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for scenario_id, category, scenario_type, target, turns, update in MULTI_TURN:
        rows.append({
            "schemaVersion": "track-b-complex-multiturn-v1",
            "benchmarkId": COMPLEX_BENCHMARK_ID,
            "designVersion": DESIGN_VERSION,
            "scenarioId": scenario_id,
            "category": {"categoryKey": category, "label": CATEGORIES[category]},
            "scenarioType": scenario_type,
            "turns": [{"turn": index + 1, "role": role, "text": text} for index, (role, text) in enumerate(turns)],
            "stateUpdateContract": update,
            "finalIntentGroupId": target,
            "finalSemanticContractHash": contracts[target]["queryVariants"][0]["semanticContractHash"],
            "qrelReuse": {"intentGroupId": target, "status": "pending_not_generated"},
            "goldStatus": "pending",
            "approval": {"dialogueNaturalness": "pending", "stateUpdateSemantics": "pending"},
            "provenance": {"dialogueOrigin": "synthetic_rule_and_llm_assisted_draft", "humanApproved": False},
        })
    return rows


FORBIDDEN_QUERY_PATTERNS = {
    "item_id": re.compile(r"\b\d{5,}\b"),
    "merchant": re.compile(r"店铺|商家|卖家"),
    "retrieval_source": re.compile(r"BM25|dense|hybrid|候选池|召回来源", re.IGNORECASE),
}


def leakage_audit(
    contracts: list[dict[str, Any]], products: list[Product],
    actions: list[dict[str, Any]], multi: list[dict[str, Any]],
) -> dict[str, Any]:
    known_brands = {p.brand.strip() for p in products if p.brand.strip() and p.brand.strip() != "无品牌"}
    known_sellers = {p.seller.strip() for p in products if p.seller.strip()}
    known_titles = {p.title.strip() for p in products if len(p.title.strip()) >= 8}
    violations: list[dict[str, str]] = []
    text_entries: list[tuple[str, str, str]] = []
    for contract in contracts:
        hashes = {variant["semanticContractHash"] for variant in contract["queryVariants"]}
        if len(hashes) != 1:
            violations.append({"intentGroupId": contract["intentGroupId"], "type": "semantic_hash_mismatch"})
        for variant in contract["queryVariants"]:
            text_entries.append((contract["intentGroupId"], variant["variantId"], variant["text"]))
    for action in actions:
        text_entries.append((action["intentGroupId"], action["intentGroupId"], action["queryText"]))
        if action.get("clarifyingQuestionDraft"):
            text_entries.append((action["intentGroupId"], f"{action['intentGroupId']}-question", action["clarifyingQuestionDraft"]))
    for scenario in multi:
        for turn in scenario["turns"]:
            text_entries.append((scenario["scenarioId"], f"{scenario['scenarioId']}-turn-{turn['turn']}", turn["text"]))

    for intent_id, text_id, query_text in text_entries:
        for name, pattern in FORBIDDEN_QUERY_PATTERNS.items():
            if pattern.search(query_text):
                violations.append({"intentGroupId": intent_id, "textId": text_id, "type": name})
        for value in known_brands | known_sellers:
            if len(value) >= 3 and value in query_text:
                violations.append({"intentGroupId": intent_id, "textId": text_id, "type": "brand_or_seller"})
                break
        if any(title in query_text for title in known_titles):
            violations.append({"intentGroupId": intent_id, "textId": text_id, "type": "copied_full_product_title"})
    return {
        "auditVersion": "complex-query-leakage-v1",
        "checks": ["no_item_id", "no_brand", "no_seller", "no_copied_full_product_title", "no_retrieval_source", "shared_semantic_contract_hash"],
        "queryVariantCount": sum(len(row["queryVariants"]) for row in contracts),
        "allNaturalLanguageTextCount": len(text_entries),
        "violationCount": len(violations),
        "violations": violations,
        "passed": not violations,
    }


def build_approval_cards(
    contracts: list[dict[str, Any]], actions: list[dict[str, Any]],
    multi: list[dict[str, Any]], support: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    definitions = {d["intentGroupId"]: d for d in SOLVABLE_DEFINITIONS}
    for contract in contracts:
        intent_id = contract["intentGroupId"]
        intent_approved = intent_id in APPROVED_INTENT_GROUPS
        atoms = contract["atoms"]
        cards.append({
            "schemaVersion": "track-b-complex-approval-card-v1",
            "cardId": f"CARD-{intent_id}", "cardType": "solvable_intent", "intentGroupId": intent_id,
            "category": contract["category"],
            "naturalLanguage": [variant["text"] for variant in contract["queryVariants"]],
            "logicSummary": {
                "hard": [{"group": a["group"], "operator": a["operator"], "allowedValues": a["allowedValues"]} for a in atoms if a["importance"] == "hard"],
                "soft": [{"group": a["group"], "operator": a["operator"], "allowedValues": a["allowedValues"]} for a in atoms if a["importance"] == "soft"],
            },
            "expectedAction": contract["expectedAction"],
            "supportCounts": {key: support[intent_id][key] for key in (
                "cleanFullPass", "hardPassSoftFailOrMissing", "singleHardFail", "hardMissingOrConflict", "gatePassed"
            )},
            "mainRisks": definitions[intent_id]["risks"],
            "approval": {
                "queryNaturalness": "approved" if intent_approved else "pending",
                "logicSemantics": "approved" if intent_approved else "pending",
                "proceedToProductReview": "pending",
                "source": MAIN_WINDOW_APPROVAL_SOURCE if intent_approved else None,
            },
            "containsProductIds": False, "containsAutomaticGrades": False, "containsRetrievalSources": False,
        })
    for action in actions:
        cards.append({
            "schemaVersion": "track-b-complex-approval-card-v1",
            "cardId": f"CARD-{action['intentGroupId']}", "cardType": "single_turn_action", "intentGroupId": action["intentGroupId"],
            "category": action["category"], "naturalLanguage": [action["queryText"]],
            "logicSummary": {"hard": [], "soft": [], "boundaryOrEvidenceGap": action["decisionReason"]},
            "expectedAction": action["expectedAction"], "supportCounts": None,
            "mainRisks": ["该题评测动作选择，不生成普通 NDCG qrel"],
            "approval": {"queryNaturalness": "pending", "logicSemantics": "pending", "proceedToProductReview": "pending"},
            "containsProductIds": False, "containsAutomaticGrades": False, "containsRetrievalSources": False,
        })
    contract_map = {row["intentGroupId"]: row for row in contracts}
    for scenario in multi:
        target = scenario["finalIntentGroupId"]
        cards.append({
            "schemaVersion": "track-b-complex-approval-card-v1",
            "cardId": f"CARD-{scenario['scenarioId']}", "cardType": "multi_turn_scenario", "intentGroupId": scenario["scenarioId"],
            "category": scenario["category"], "naturalLanguage": [turn["text"] for turn in scenario["turns"]],
            "logicSummary": {"stateUpdate": scenario["stateUpdateContract"], "finalIntentGroupId": target,
                             "finalLogicAst": contract_map[target]["logicAst"]},
            "expectedAction": "UPDATE_STATE_AND_RETRIEVE",
            "supportCounts": {key: support[target][key] for key in (
                "cleanFullPass", "hardPassSoftFailOrMissing", "singleHardFail", "hardMissingOrConflict", "gatePassed"
            )},
            "mainRisks": ["必须只替换或补齐指定约束，其余状态保持不变", f"最终状态复用 {target} 的待审批 qrel 合同"],
            "approval": {"queryNaturalness": "pending", "logicSemantics": "pending", "proceedToProductReview": "pending"},
            "containsProductIds": False, "containsAutomaticGrades": False, "containsRetrievalSources": False,
        })
    return cards


def build_report(support: dict[str, dict[str, Any]]) -> str:
    lines = [
        "# Track B complex intent design v1 — support gate audit", "",
        "> 本报告只描述自动证据支持审计，不是 qrel，也没有人工批准标签。", "",
        "| Intent | 类目 | 完整匹配 | hard通过/soft失败或缺失 | 单一hard失败 | hard缺失/冲突 | Gate |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    definition_map = {row["intentGroupId"]: row for row in SOLVABLE_DEFINITIONS}
    for intent_id, counts in support.items():
        waived = "豁免" if counts["softLayerWaived"] else str(counts["hardPassSoftFailOrMissing"])
        lines.append(
            f"| {intent_id} | {CATEGORIES[definition_map[intent_id]['categoryKey']]} | {counts['cleanFullPass']} | "
            f"{waived} | {counts['singleHardFail']} | {counts['hardMissingOrConflict']} | "
            f"{'PASS' if counts['gatePassed'] else 'NO-GO'} |"
        )
    lines += [
        "", "## 计数口径", "",
        "- 完整匹配：类目/标题无严重冲突，全部 hard 通过，且存在 soft 时全部 soft 通过。",
        "- hard通过/soft失败或缺失：全部 hard 通过，soft 明确失败或缺失；soft 冲突单独记录，不混入本层。",
        "- 单一hard失败：恰有一个 hard 明确失败，其余 hard 通过，不能含 missing/conflict。",
        "- hard缺失/冲突：至少一个 hard 为 unknown 或 conflict；这是一项诊断计数，不与其他层强制互斥。",
        "- 否定约束不能由字段未出现推成通过；标题只做冲突检查，不能覆盖或替代属性字段。",
        "", "## 固定语义", "",
        "- 棉质允许有明确含棉证据的混纺；纯棉必须有明确纯棉证据。",
        "- 平底与低跟同时出现为 conflict/unknown；查询显式允许平底或低跟时例外通过。",
        "- 中低腰面对硬约束中腰为 unknown。",
        "- 阔脚/阔腿、涤纶(聚酯纤维)/聚酯纤维做同义归一；一脚蹬包含于套脚。",
    ]
    failed = [intent_id for intent_id, counts in support.items() if not counts["gatePassed"]]
    lines += ["", "## Gate 结论", "", (
        "12 个候选全部满足 Gate，可进入主窗口意图审批；正式 qrel 仍未生成。"
        if not failed else f"以下候选未满足 Gate，必须替换后再扩张：{', '.join(failed)}。"
    ), ""]
    return "\n".join(lines)


def validate_pending(contracts: list[dict[str, Any]], actions: list[dict[str, Any]], multi: list[dict[str, Any]], cards: list[dict[str, Any]]) -> None:
    for contract in contracts:
        assert contract["goldStatus"] == "pending"
        approved = contract["intentGroupId"] in APPROVED_INTENT_GROUPS
        assert contract["provenance"]["humanApproved"] is approved
        assert contract["approval"]["productAnswers"] == "pending"
        assert contract["approval"]["queryNaturalness"] == ("approved" if approved else "pending")
        assert contract["approval"]["semanticEquivalence"] == ("approved" if approved else "pending")
        assert len({v["semanticContractHash"] for v in contract["queryVariants"]}) == 1
    for row in actions + multi:
        assert row["goldStatus"] == "pending"
        assert row["provenance"]["humanApproved"] is False
        assert all(value == "pending" for value in row["approval"].values())
    for card in cards:
        approved = card["cardType"] == "solvable_intent" and card["intentGroupId"] in APPROVED_INTENT_GROUPS
        assert card["approval"]["proceedToProductReview"] == "pending"
        assert card["approval"]["queryNaturalness"] == ("approved" if approved else "pending")
        assert card["approval"]["logicSemantics"] == ("approved" if approved else "pending")
        assert not card["containsProductIds"]
        assert not card["containsAutomaticGrades"]
        assert not card["containsRetrievalSources"]


def build(evidence_path: Path, output_dir: Path, schema_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    products = load_products(evidence_path)
    contracts = [build_contract(definition) for definition in SOLVABLE_DEFINITIONS]
    contract_map = {row["intentGroupId"]: row for row in contracts}
    support = {definition["intentGroupId"]: support_counts(definition, products) for definition in SOLVABLE_DEFINITIONS}
    actions = build_action_intents()
    multi = build_multi_turn(contract_map)
    cards = build_approval_cards(contracts, actions, multi, support)
    leakage = leakage_audit(contracts, products, actions, multi)
    validate_pending(contracts, actions, multi, cards)
    if not leakage["passed"]:
        raise ValueError(f"query leakage audit failed: {leakage['violations']}")

    outputs = {
        "solvable_intent_contracts_pending.jsonl": contracts,
        "single_turn_action_intents_pending.jsonl": actions,
        "multi_turn_scenarios_pending.jsonl": multi,
        "blind_approval_cards_pending.jsonl": cards,
    }
    for name, rows in outputs.items():
        write_jsonl(output_dir / name, rows)
    write_json(output_dir / "support_matrix_audit.json", {
        "benchmarkId": COMPLEX_BENCHMARK_ID, "designVersion": DESIGN_VERSION,
        "provenance": "automatic_evidence_support_audit_not_gold", "counts": support,
    })
    write_json(output_dir / "leakage_and_equivalence_audit.json", leakage)
    write_utf8_lf(output_dir / "support_gate_report.md", build_report(support))

    schema_names = [
        "track_b_complex_intent_contract_v1.schema.json",
        "track_b_complex_action_intent_v1.schema.json",
        "track_b_complex_multiturn_v1.schema.json",
        "track_b_complex_approval_card_v1.schema.json",
    ]
    for name in schema_names:
        write_utf8_lf(output_dir / name, (schema_dir / name).read_text(encoding="utf-8"))

    canonical_round_trip = True
    for name in outputs:
        parsed = list(iter_jsonl(output_dir / name))
        expected = "".join(canonical_json(row) + "\n" for row in parsed).encode("utf-8")
        canonical_round_trip = canonical_round_trip and (output_dir / name).read_bytes() == expected
    if not canonical_round_trip:
        raise RuntimeError("canonical JSONL round-trip gate failed")
    write_json(output_dir / "idempotence_check.json", {
        "method": "canonical_artifact_round_trip_plus_offline_double_build_test",
        "canonicalArtifactRoundTrip": canonical_round_trip,
        "offlineDoubleBuildTest": "tests/test_track_b_complex_intent_design.py",
        "passed": canonical_round_trip,
    })

    readme = f"# {COMPLEX_BENCHMARK_ID}\n\n"
    readme += "This directory is an annotation-ready design package, not gold qrel.\n\n"
    readme += "- 12 solvable intent groups × 3 equivalent synthetic query variants\n"
    readme += "- 6 single-turn CLARIFY / ABSTAIN_OR_EXPLAIN intents\n"
    readme += "- 6 multi-turn state-update scenarios\n"
    readme += "- 24 blind approval cards; T1–T4 intent semantics are approved, while every product/qrel approval remains pending\n\n"
    readme += "Reproduce offline from the repository root:\n\n"
    readme += "```powershell\ncd agent\npython -m evaluation.build_track_b_complex_intent_design --evidence ../data/processed/ecommerce/kuaisearch_synthetic_evidence_track_b_v01/09807c773ce67360ed8df30842e372182fcf7ad9/evidence_products_audit.jsonl --output ../data/processed/ecommerce/kuaisearch_synthetic_evidence_track_b_v01/09807c773ce67360ed8df30842e372182fcf7ad9/complex_intent_design_v1\npytest -q tests/test_track_b_complex_intent_design.py\n```\n"
    write_utf8_lf(output_dir / "README.md", readme)

    artifact_names = sorted(path.name for path in output_dir.iterdir() if path.name != "manifest.json")
    manifest = {
        "benchmarkId": COMPLEX_BENCHMARK_ID,
        "parentBenchmarkId": BASE_BENCHMARK_ID,
        "datasetRevision": DATASET_REVISION,
        "designVersion": DESIGN_VERSION,
        "status": "design_complete_partial_intent_approval_not_gold",
        "networkUsed": False,
        "formalQrelGenerated": False,
        "formalMetricsRun": False,
        "humanApproved": False,
        "intentApproval": {
            "approvedIntentGroups": sorted(APPROVED_INTENT_GROUPS),
            "remainingIntentGroups": sorted(set(contract_map) - APPROVED_INTENT_GROUPS),
            "source": MAIN_WINDOW_APPROVAL_SOURCE,
            "scope": "intent_only; product answers and qrel remain pending",
        },
        "sourceEvidence": {
            "path": stable_repository_relative_path(evidence_path),
            "bytes": evidence_path.stat().st_size,
            "sha256": sha256_file(evidence_path),
        },
        "pinnedRawSourceHashesInheritedFromVerifiedTrackBAudit": PINNED_RAW_SOURCE_HASHES,
        "counts": {"solvableIntentGroups": len(contracts), "queryVariants": sum(len(c["queryVariants"]) for c in contracts),
                   "singleTurnActionIntents": len(actions), "multiTurnScenarios": len(multi), "approvalCards": len(cards),
                   "evidenceProductsRead": len(products), "gatePassed": sum(v["gatePassed"] for v in support.values()),
                   "intentHumanApproved": len(APPROVED_INTENT_GROUPS)},
        "artifacts": [{"path": name, "bytes": (output_dir / name).stat().st_size, "sha256": sha256_file(output_dir / name)} for name in artifact_names],
        "limitations": [
            "Queries are synthetic and do not represent real user-log expression frequencies.",
            "Support counts are automatic evidence diagnostics, not relevance judgments.",
            "Title-only facts cannot establish pass/fail; title text is used to detect conflicts.",
            "No product answer is frozen until main-window intent approval and later blind product review.",
        ],
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parent / "schemas")
    args = parser.parse_args()
    manifest = build(args.evidence.resolve(), args.output.resolve(), args.schema_dir.resolve())
    print(json.dumps(manifest["counts"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
