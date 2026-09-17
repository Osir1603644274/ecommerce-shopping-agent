from __future__ import annotations

import argparse
import random
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import (
    AGENT_DIR,
    DATASET_ID,
    PACKAGE_DIR,
    SCHEMA_VERSION,
    SYSTEM_PROMPT,
    canonical_json,
    render_messages,
    sha256_file,
    sha256_text,
    validate_arguments,
    write_json,
    write_jsonl,
)


SPLIT_COUNTS = {"train": 360, "dev": 90, "test": 120}
SPLIT_SEEDS = {"train": 90411, "dev": 90422, "test": 90433}
FIXED_TIME = datetime(2026, 9, 4, tzinfo=timezone.utc).isoformat()

CATEGORY_CN = {"phone": "手机", "laptop": "笔记本", "headphones": "耳机"}
BRANDS = {
    "phone": ["华为", "小米", "荣耀", "vivo", "OPPO", "三星"],
    "laptop": ["联想", "华硕", "惠普", "戴尔", "宏碁"],
    "headphones": ["索尼", "漫步者", "华为", "小米", "森海塞尔"],
}
BUDGETS = {
    "phone": [1600, 1800, 2000, 2200, 2500, 2800, 3200, 3500, 4000],
    "laptop": [3500, 4000, 4500, 5000, 5500, 6000, 7000, 8000],
    "headphones": [300, 500, 700, 900, 1200, 1500, 2000, 2500],
}

INITIAL_BUDGET_TEMPLATES = {
    "train": [
        "想买一台{category_cn}，预算不超过{budget}元",
        "帮我选个{category_cn}，最多花{budget}块",
        "给我推荐{budget}元以内的{category_cn}",
    ],
    "dev": [
        "预算封顶{budget}元，想挑一台{category_cn}",
        "我想看看价格不高于{budget}元的{category_cn}",
    ],
    "test": [
        "我准备了{budget}元，想选一台{category_cn}",
        "买{category_cn}的话控制在{budget}元以内",
        "请按最高{budget}元帮我找{category_cn}",
    ],
}
INITIAL_BRAND_TEMPLATES = {
    "train": [
        "想买{brand}的{category_cn}，预算{budget}元以内",
        "{budget}块以下，品牌选{brand}的{category_cn}",
    ],
    "dev": [
        "给我找{brand}{category_cn}，价格别超过{budget}元",
        "预算上限{budget}元，只考虑{brand}{category_cn}",
    ],
    "test": [
        "我只看{brand}的{category_cn}，最多出{budget}元",
        "{brand}{category_cn}有没有{budget}元以内合适的",
    ],
}
EXISTING_OVERRIDE_TEMPLATES = {
    "train": ["预算改成{budget}元以内", "把最高预算调整到{budget}块"],
    "dev": ["现在最多只能花{budget}元", "预算上限改为{budget}元"],
    "test": ["刚才的预算收紧到{budget}元", "改一下，价格不能高于{budget}块"],
}
EXISTING_REMOVE_TEMPLATES = {
    "train": {
        "price_minor": ["预算先不限制了", "把价格上限取消"],
        "brand": ["品牌随意，不限定了", "取消之前的品牌要求"],
    },
    "dev": {
        "price_minor": ["不设最高预算了", "价格条件去掉"],
        "brand": ["哪个牌子都可以", "品牌约束不用保留"],
    },
    "test": {
        "price_minor": ["预算方面放开，不要上限", "删掉先前的价格限制"],
        "brand": ["我不再指定品牌", "之前限定的牌子作废"],
    },
}
UNCHANGED_TEMPLATES = {
    "train": ["条件就按之前的，继续", "不用改要求，接着找"],
    "dev": ["维持刚才那些条件", "沿用原来的筛选要求"],
    "test": ["之前的要求全部保留", "条件不变，继续往下做"],
}
BLOCKING_TEMPLATES = {
    "train": [
        "帮我挑一个合适的", "我想买个东西，给点建议", "给我推荐个商品",
        "想买东西但还没决定品类", "先帮我选购", "帮忙看看买什么好",
    ],
    "dev": [
        "推荐一个给我吧", "帮我选选，但我还没说买什么", "想做一次购买决策",
        "我需要导购建议，品类还没定", "先问我必要条件吧", "帮我挑件合适的商品",
    ],
    "test": [
        "想让你帮忙挑一下", "给我做个购买推荐，具体买什么还没定", "我要买东西，请协助筛选",
        "品类未定，先帮我梳理", "我想开始选购", "请提供购买方向建议",
    ],
}
RESOLVE_TEMPLATES = {
    "train": ["我要手机，预算{budget}元以内", "品类是手机，最多{budget}块"],
    "dev": ["买的是手机，价格不超过{budget}元", "明确一下：手机，预算{budget}元"],
    "test": ["刚才没说清，是手机，最高{budget}块", "目标品类选手机，预算上限{budget}元"],
}

PHRASE_WRAPPERS = {
    "train": ["{text}", "我的要求是{text}", "筛选时{text}"],
    "dev": ["这次选购希望{text}", "需求补充：{text}", "请记住{text}"],
    "test": ["请严格按这点筛选：{text}", "我明确要求{text}", "选购条件是{text}"],
}

CONTROLLED_PHONE = [
    ("os", "ios", "只要iOS"),
    ("os", "android", "只要安卓"),
    ("battery_health", "90_plus", "电池健康度要90%以上"),
    ("battery_health", "80_90", "电池健康度80%到90%即可"),
    ("screen_originality", "original", "必须原装屏"),
    ("motherboard_repair", "not_repaired", "主板不能维修过"),
    ("battery_originality", "original", "要原装电池"),
    ("scratch_level", "light", "轻微划痕可以接受"),
    ("shell_condition", "normal", "外壳要正常"),
]

NEGATIVE_PHONE = [
    ("brand", ["apple"], "text", "不要苹果品牌"),
    ("screen_originality", ["non_original"], "enum", "不要非原装屏"),
    ("motherboard_repair", ["repaired"], "enum", "不能要修过主板的"),
    ("scratch_level", ["obvious"], "enum", "不能有明显划痕"),
    ("battery_originality", ["non_original"], "enum", "排除非原装电池"),
]

SPECS = {
    "phone": [
        ("memory_gb", "gte", 8, "GB", "内存至少8GB"),
        ("memory_gb", "gte", 12, "GB", "内存至少12GB"),
        ("storage_gb", "gte", 256, "GB", "存储至少256GB"),
        ("storage_gb", "gte", 512, "GB", "存储要512GB起"),
        ("battery_mah", "gte", 4500, "mAh", "电池容量至少4500mAh"),
        ("supports_5g", "eq", True, "bool", "必须支持5G"),
    ],
    "laptop": [
        ("memory_gb", "gte", 16, "GB", "内存至少16GB"),
        ("memory_gb", "gte", 32, "GB", "内存要32GB起"),
        ("storage_gb", "gte", 512, "GB", "硬盘至少512GB"),
        ("storage_gb", "gte", 1024, "GB", "存储要1TB以上"),
        ("weight_kg", "lte", 1.5, "kg", "重量不超过1.5公斤"),
        ("screen_inch", "gte", 14, "inch", "屏幕至少14英寸"),
    ],
    "headphones": [
        ("battery_hours", "gte", 30, "hour", "续航至少30小时"),
        ("battery_hours", "gte", 40, "hour", "续航要40小时起"),
        ("noise_cancelling", "eq", True, "bool", "必须支持主动降噪"),
        ("wireless", "eq", True, "bool", "只要无线耳机"),
        ("weight_g", "lte", 250, "g", "重量不要超过250克"),
    ],
}


def requirement(
    key: str,
    operator: str,
    value: Any,
    unit: str,
    *,
    priority: str = "hard",
    source: str = "user",
) -> dict[str, Any]:
    return {
        "key": key,
        "operator": operator,
        "value": value,
        "unit": unit,
        "priority": priority,
        "source": source,
    }


def guide(category: str, requirements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "mode": "recommend",
        "category": category,
        "useCases": [],
        "requirements": requirements,
        "brandAvoidances": [],
        "candidateIds": [],
        "comparedIds": [],
        "evidenceStatus": "missing",
    }


def state(
    example_id: str,
    message: str,
    *,
    current_guide: dict[str, Any] | None = None,
    status: str | None = None,
    unknowns: list[str] | None = None,
    pending: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "taskId": f"pt-{example_id}",
        "taskType": "ecommerce_guide",
        "sessionId": f"session-{example_id}",
        "status": status or ("ready" if current_guide else "collecting_information"),
        "revision": 2 if current_guide else 1,
        "goal": "继续当前导购任务" if current_guide else message,
        "facts": [],
        "constraints": [],
        "unknowns": unknowns or [],
        "pendingQuestions": pending or [],
        "domainState": ({"shoppingGuide": current_guide} if current_guide else {}),
        "activePlan": None,
        "planningFailure": None,
        "createdAt": FIXED_TIME,
        "updatedAt": FIXED_TIME,
    }


def ready_initial(category: str, requirements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "status": "ready",
        "addUnknowns": [],
        "pendingQuestions": [],
        "domainStatePatch": {
            "shoppingGuide": {
                "mode": "recommend",
                "category": category,
                "requirements": requirements,
            }
        },
    }


def ready_incremental(
    *,
    upserts: list[dict[str, Any]] | None = None,
    removals: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": "ready",
        "addUnknowns": [],
        "pendingQuestions": [],
        "domainStatePatch": {
            "shoppingGuide": {
                "upsertRequirements": upserts or [],
                "removeRequirementKeys": removals or [],
            }
        },
    }


def _pick(rng: random.Random, values: list[Any]) -> Any:
    return values[rng.randrange(len(values))]


def _spec_requirement(spec: tuple[str, str, Any, str, str]) -> dict[str, Any]:
    key, operator, value, unit, _ = spec
    return requirement(key, operator, value, unit)


def _wrap_phrase(split: str, rng: random.Random, text: str) -> tuple[str, int]:
    wrappers = PHRASE_WRAPPERS[split]
    wrapper_index = rng.randrange(len(wrappers))
    return wrappers[wrapper_index].format(text=text), wrapper_index


def build_case(
    split: str,
    index: int,
    rng: random.Random,
    case_type: str,
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    example_id = f"{split}-{index:04d}"
    category = _pick(rng, list(CATEGORY_CN))
    category_cn = CATEGORY_CN[category]
    budget = _pick(rng, BUDGETS[category])

    if case_type == "initial_budget":
        templates = INITIAL_BUDGET_TEMPLATES[split]
        template_index = rng.randrange(len(templates))
        message = templates[template_index].format(category_cn=category_cn, budget=budget)
        reqs = [requirement("price_minor", "lte", budget * 100, "CNY_MINOR")]
        return f"{split}-initial-budget-{template_index}", message, state(example_id, message), ready_initial(category, reqs)

    if case_type == "initial_brand_budget":
        templates = INITIAL_BRAND_TEMPLATES[split]
        template_index = rng.randrange(len(templates))
        brand = _pick(rng, BRANDS[category])
        message = templates[template_index].format(
            brand=brand, category_cn=category_cn, budget=budget
        )
        reqs = [
            requirement("brand", "eq", brand, "text"),
            requirement("price_minor", "lte", budget * 100, "CNY_MINOR"),
        ]
        return f"{split}-initial-brand-budget-{template_index}", message, state(example_id, message), ready_initial(category, reqs)

    if case_type == "initial_spec":
        spec = _pick(rng, SPECS[category])
        detail, wrapper_index = _wrap_phrase(split, rng, spec[4])
        message = f"想买{category_cn}，{detail}"
        return f"{split}-initial-spec-{category}-{spec[0]}-{wrapper_index}", message, state(example_id, message), ready_initial(category, [_spec_requirement(spec)])

    if case_type == "initial_controlled_phone":
        key, value, phrase = _pick(rng, CONTROLLED_PHONE)
        detail, wrapper_index = _wrap_phrase(split, rng, phrase)
        message = f"帮我找二手手机，{detail}"
        req = requirement(key, "eq", value, "enum")
        return f"{split}-initial-controlled-{key}-{wrapper_index}", message, state(example_id, message), ready_initial("phone", [req])

    if case_type == "initial_negation":
        key, values, unit, phrase = _pick(rng, NEGATIVE_PHONE)
        detail, wrapper_index = _wrap_phrase(split, rng, phrase)
        message = f"想买二手手机，{detail}"
        req = requirement(key, "not_in", values, unit)
        return f"{split}-initial-negation-{key}-{wrapper_index}", message, state(example_id, message), ready_initial("phone", [req])

    if case_type == "existing_override":
        templates = EXISTING_OVERRIDE_TEMPLATES[split]
        template_index = rng.randrange(len(templates))
        old_budget = _pick(rng, [value for value in BUDGETS[category] if value != budget])
        current = guide(category, [
            requirement("price_minor", "lte", old_budget * 100, "CNY_MINOR")
        ])
        message = templates[template_index].format(budget=budget)
        target = ready_incremental(upserts=[
            requirement("price_minor", "lte", budget * 100, "CNY_MINOR")
        ])
        return f"{split}-existing-override-{template_index}", message, state(example_id, message, current_guide=current), target

    if case_type == "existing_add":
        spec = _pick(rng, SPECS[category])
        current = guide(category, [
            requirement("price_minor", "lte", budget * 100, "CNY_MINOR")
        ])
        detail, wrapper_index = _wrap_phrase(split, rng, spec[4])
        message = f"再加一个条件，{detail}"
        target = ready_incremental(upserts=[_spec_requirement(spec)])
        return f"{split}-existing-add-{category}-{spec[0]}-{wrapper_index}", message, state(example_id, message, current_guide=current), target

    if case_type == "existing_remove":
        key = _pick(rng, ["price_minor", "brand"])
        brand = _pick(rng, BRANDS[category])
        current = guide(category, [
            requirement("price_minor", "lte", budget * 100, "CNY_MINOR"),
            requirement("brand", "eq", brand, "text"),
        ])
        templates = EXISTING_REMOVE_TEMPLATES[split][key]
        template_index = rng.randrange(len(templates))
        message = templates[template_index]
        target = ready_incremental(removals=[key])
        return f"{split}-existing-remove-{key}-{template_index}", message, state(example_id, message, current_guide=current), target

    if case_type == "unchanged":
        template_index = rng.randrange(len(UNCHANGED_TEMPLATES[split]))
        message = UNCHANGED_TEMPLATES[split][template_index]
        current = guide(category, [
            requirement("price_minor", "lte", budget * 100, "CNY_MINOR")
        ])
        return f"{split}-unchanged-{template_index}", message, state(example_id, message, current_guide=current), {"status": "ready", "addUnknowns": [], "pendingQuestions": []}

    if case_type == "blocking":
        template_index = rng.randrange(len(BLOCKING_TEMPLATES[split]))
        message = BLOCKING_TEMPLATES[split][template_index]
        target = {
            "status": "collecting_information",
            "addUnknowns": ["目标商品品类未明确"],
            "pendingQuestions": ["你想买手机、笔记本还是耳机？"],
        }
        return f"{split}-blocking-{template_index}", message, state(example_id, message), target

    if case_type == "resolve_blocker":
        template_index = rng.randrange(len(RESOLVE_TEMPLATES[split]))
        budget = _pick(rng, BUDGETS["phone"])
        message = RESOLVE_TEMPLATES[split][template_index].format(budget=budget)
        current_state = state(
            example_id,
            message,
            status="collecting_information",
            unknowns=["目标商品品类未明确"],
            pending=["你想买手机、笔记本还是耳机？"],
        )
        target = ready_initial("phone", [
            requirement("price_minor", "lte", budget * 100, "CNY_MINOR")
        ])
        target["resolveUnknowns"] = ["目标商品品类未明确"]
        return f"{split}-resolve-blocker-{template_index}", message, current_state, target

    raise ValueError(f"unknown case type: {case_type}")


CASE_TYPES = [
    "initial_budget",
    "initial_brand_budget",
    "initial_spec",
    "initial_controlled_phone",
    "initial_negation",
    "existing_override",
    "existing_add",
    "existing_remove",
    "unchanged",
    "blocking",
    "resolve_blocker",
]


def build_split(split: str, count: int) -> list[dict[str, Any]]:
    rng = random.Random(SPLIT_SEEDS[split])
    rows: list[dict[str, Any]] = []
    signatures: set[str] = set()
    attempts = 0
    while len(rows) < count:
        attempts += 1
        if attempts > count * 200:
            raise RuntimeError(f"unable to generate {count} unique {split} rows")
        case_type = CASE_TYPES[(attempts - 1) % len(CASE_TYPES)]
        template_family, message, raw_state, target = build_case(
            split, len(rows) + 1, rng, case_type
        )
        record = {
            "schemaVersion": SCHEMA_VERSION,
            "datasetId": DATASET_ID,
            "exampleId": f"{split}-{len(rows) + 1:04d}",
            "split": split,
            "family": case_type,
            "templateFamily": template_family,
            "sourceClass": "programmatic_template_only",
            "state": raw_state,
            "userMessage": message,
            "targetTool": "update_task_state",
            "targetArguments": target,
        }
        prompt = canonical_json(render_messages(record))
        signature = sha256_text(prompt)
        if signature in signatures:
            continue
        validate_arguments(record, target)
        record["fingerprints"] = {
            "promptSha256": signature,
            "targetSha256": sha256_text(canonical_json(target)),
        }
        signatures.add(signature)
        rows.append(record)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=PACKAGE_DIR / "datasets")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    all_rows: dict[str, list[dict[str, Any]]] = {}
    all_ids: set[str] = set()
    all_prompts: set[str] = set()
    templates_by_split: dict[str, set[str]] = {}
    for split, count in SPLIT_COUNTS.items():
        rows = build_split(split, count)
        ids = {row["exampleId"] for row in rows}
        prompts = {row["fingerprints"]["promptSha256"] for row in rows}
        templates = {row["templateFamily"] for row in rows}
        if len(ids) != count or len(prompts) != count:
            raise RuntimeError(f"{split} contains duplicate ids or prompts")
        if all_ids & ids or all_prompts & prompts:
            raise RuntimeError(f"{split} overlaps an earlier split")
        for earlier, earlier_templates in templates_by_split.items():
            if templates & earlier_templates:
                raise RuntimeError(f"template families overlap: {earlier}/{split}")
        all_ids.update(ids)
        all_prompts.update(prompts)
        templates_by_split[split] = templates
        all_rows[split] = rows
        write_jsonl(args.output_dir / f"{split}.jsonl", rows)

    source_files = {
        "generator": Path(__file__).resolve(),
        "common": PACKAGE_DIR / "common.py",
        "llmContract": AGENT_DIR / "app" / "llm.py",
        "shoppingModels": AGENT_DIR / "app" / "domains" / "ecommerce" / "models.py",
        "shoppingStateUpdate": AGENT_DIR / "app" / "domains" / "ecommerce" / "shopping_state_update.py",
    }
    manifest = {
        "schemaVersion": "shopping-taskstate-posttraining-dataset-manifest-v1",
        "datasetId": DATASET_ID,
        "createdAt": "2026-09-04T02:00:00+08:00",
        "sourcePolicy": {
            "allowed": ["programmatic templates", "production code contracts"],
            "forbidden": [
                "sealed data",
                "old validation data",
                "old formal attempts",
                "real user messages",
                "chain-of-thought",
            ],
            "externalDataRead": False,
        },
        "prompt": {
            "version": "shopping-taskstate-interpreter-prompt-v1",
            "systemPromptSha256": sha256_text(SYSTEM_PROMPT),
        },
        "splits": {
            split: {
                "path": f"datasets/{split}.jsonl",
                "rowCount": len(rows),
                "sha256": sha256_file(args.output_dir / f"{split}.jsonl"),
                "familyCounts": dict(sorted(Counter(row["family"] for row in rows).items())),
                "templateFamilies": sorted(templates_by_split[split]),
            }
            for split, rows in all_rows.items()
        },
        "isolation": {
            "exampleIdOverlap": 0,
            "promptSha256Overlap": 0,
            "templateFamilyOverlap": 0,
        },
        "goldProductionValidation": {
            "validatedRows": sum(map(len, all_rows.values())),
            "failedRows": 0,
            "validator": "app.llm._build_validated_task_state_payload",
        },
        "sourceHashes": {
            name: {
                "path": path.relative_to(AGENT_DIR.parent).as_posix(),
                "sha256": sha256_file(path),
            }
            for name, path in source_files.items()
        },
    }
    write_json(args.output_dir / "manifest.json", manifest)
    print(canonical_json({
        "status": "PASS",
        "datasetId": DATASET_ID,
        "counts": {split: len(rows) for split, rows in all_rows.items()},
        "manifest": str(args.output_dir / "manifest.json"),
    }))


if __name__ == "__main__":
    main()
