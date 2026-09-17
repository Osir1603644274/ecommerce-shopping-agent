"""Frozen fixed-stage fixtures from public user messages, not old model answers."""
import asyncio
from datetime import datetime, timedelta, timezone
import random
import re
import subprocess
from collections import Counter
from .common import *

PUBLIC = ROOT / "agent/evaluation/assets/used_phone_harness_behavior_v1_20260825/public/scenarios.jsonl"
BREADTH = ROOT / "agent/evaluation/context_program_v3_20260904/p1/breadth001"
FINAL_INSTRUCTION = """你处于购物最终回答阶段，直接回答本轮用户问题，不调用工具。validatedResults按数组顺序对应本次受控展示的第一款、第二款等；这是重建展示，不代表历史线上真实推荐。根据已有事实比较取舍，严格遵守仍有效的硬条件；已撤销的条件不再限制。没有合格商品、证据不足或指代无法确定时明确说明。商品为历史目录快照，价格如有是synthetic/budget_and_ranking模拟值，不是实时价；库存、售后及实测游戏性能没有证据不能断言。最多重点讨论三款。"""

def derive_hard(messages):
    """Narrow, auditable reference-state builder from public text only.

    Unparsed preferences/brand nuances stay in history; never filled from gold.
    """
    values = {}
    for text in messages:
        lower = text.lower()
        if "取消预算" in text: values.pop("price_minor", None)
        else:
            match = re.search(r"预算(?:临时)?(?:改为|改成|是|先按)?\s*(\d+)", text)
            if match and "左右" not in text: values["price_minor"] = int(match[1]) * 100
            elif "八百" in text and ("以内" in text or "以下" in text): values["price_minor"] = 80000
            elif "三千以内" in text: values["price_minor"] = 300000
        if "取消存储" in text: values.pop("storage_gb", None)
        match = re.search(r"存储至少\s*(\d+)\s*GB", text, re.I)
        if match: values["storage_gb"] = int(match[1])
        if ("ios" in lower and "android" in lower and "都可以" in lower) or "苹果或者安卓都可以" in text:
            values.pop("os", None)
        elif "只看ios" in lower or "只看 ios" in lower: values["os"] = "ios"
        elif "只看安卓" in text or "系统改成安卓" in text or "系统改为只看安卓" in text: values["os"] = "android"
    return values

def requirements(hard):
    spec = {"price_minor": ("lte", "CNY_MINOR"), "storage_gb": ("gte", "GB"), "os": ("eq", "enum")}
    return [{"key": k, "operator": spec[k][0], "unit": spec[k][1], "value": v,
        "priority": "hard", "source": "controlled_snapshot:public_user_text"} for k,v in hard.items()]

def select_specs():
    specs = []
    public = jsonl(PUBLIC)
    for r in public[:16]:
        texts = [t["text"] for t in r["turns"]]
        specs.append({"sourceId": r["scenarioId"], "family": "harness:" + "+".join(r["tags"]),
            "sourceKind": r["provenanceKind"], "sourcePath": str(PUBLIC.relative_to(ROOT)),
            "texts": texts, "stage": "final", "sourceRowHash": sha(r)})
    dev, confirm = jsonl(BREADTH / "dev.jsonl"), jsonl(BREADTH / "confirm.jsonl")
    for r in dev + confirm[:4]:
        specs.append({"sourceId": r["conversationId"], "family": "composition:" + r["primitiveFamily"],
            "sourceKind": "SYNTHETIC_COMPOSITION_PREVIOUSLY_USED", "sourcePath": str((BREADTH / ("dev.jsonl" if r in dev else "confirm.jsonl")).relative_to(ROOT)),
            "texts": [t["rawUserText"] for t in r["turns"]], "stage": "final", "sourceRowHash": sha(r)})
    operations = {"budget", "storage", "withdraw_budget", "withdraw_storage", "system"}
    for i, r in enumerate(confirm[4:16]):
        possible = [j for j,t in enumerate(r["turns"]) if j > 0 and (t.get("operation") in operations or "取消" in t["rawUserText"] or "系统改" in t["rawUserText"])]
        if not possible: raise RuntimeError("missing_state_cut:" + r["conversationId"])
        j = possible[i % len(possible)]
        texts = [t["rawUserText"] for t in r["turns"][:j+1]]
        # Source annotations verify the independent parser; never enter prompts.
        expected = r["turns"][j]["expectedHard"]
        parsed = derive_hard(texts)
        if parsed != expected: raise RuntimeError("reference_state_parser_mismatch:" + r["conversationId"] + ":" + canonical([parsed, expected]))
        specs.append({"sourceId": r["conversationId"] + f":t{j+1}", "family": "composition:" + r["primitiveFamily"],
            "sourceKind": "SYNTHETIC_COMPOSITION_PREVIOUSLY_USED", "sourcePath": str((BREADTH / "confirm.jsonl").relative_to(ROOT)),
            "texts": texts, "stage": "state", "sourceRowHash": sha(r), "expectedHard": expected,
            "absentKeys": r["turns"][j].get("absentKeys", [])})
    assert len(specs) == 48
    return specs

async def make_state(spec, index):
    llm, _, _, _, _, State, _, _, _ = p.load_app()
    instant = datetime(2026, 9, 4, tzinfo=timezone.utc)
    state = State(taskId=f"abc-study-task-{index}", taskType="ecommerce_guide", sessionId=f"abc-study-session-{index}",
        status="ready", revision=1, goal="为用户选择二手手机，遵守当前仍有效的需求", createdAt=instant, updatedAt=instant)
    # Final stage follows controlled reference-state extraction; state stage precedes the current edit.
    texts = spec["texts"] if spec["stage"] == "final" else spec["texts"][:-1]
    hard = derive_hard(texts)
    args = {"status": "ready", "goal": state.goal, "domainStatePatch": {"shoppingGuide": {
        "category": "phone", "mode": "recommend", "requirements": requirements(hard)}}}
    payload, _ = llm._build_validated_task_state_payload(state, args, message=state.goal, require_status=True)
    from agent.app.domains.ecommerce.shopping_state_authority import bind_authoritative_write
    domain = bind_authoritative_write(payload["domainStatePatch"], task_id=state.task_id, task_revision=state.revision,
        goal=state.goal, unknowns=[], pending_questions=[], compatibility_projection_changed=True)
    return state.model_copy(update={"domain_state": domain}), hard

async def build_context(fixture, arm):
    llm, builder, Pack, Projector, compiler, State, _, _, _ = p.load_app()
    state = State.model_validate(fixture["state"])
    if arm == "A_RAW":
        context = fixture["raw"]
        receipt = None
    else:
        pack = await builder(state, allowed_tools=fixture["allowedTools"], history=fixture["history"], run_id=fixture["runId"])
        before = pack.model_dump(by_alias=True, mode="json")
        receipt = None
        if arm == "C_PACK_COMPILER":
            compiled = await compiler(pack, tenant_id="codex-abc-study", owner_id=state.session_id,
                session_id=state.session_id, task_id=state.task_id, task_revision=state.revision,
                phase="SHOPPING_PLANNER" if fixture["stage"] == "state" else "SHOPPING_FINAL_ANSWER", model_call_ordinal=0,
                tool_schemas=[llm.TASK_STATE_TOOL_SCHEMA] if fixture["stage"] == "state" else [],
                model_config={"provider": "codex_subscription", "model": p.MODEL},
                deadline_at=datetime.now(timezone.utc)+timedelta(hours=12), budget_tokens=20000,
                history_policy="query_focused", query=fixture["query"], persist=False, evaluation_mode=True)
            assert before == pack.model_dump(by_alias=True, mode="json")
            pack = Pack.model_validate({**compiled.model_view, "runId": pack.run_id})
            receipt = compiled.receipt.model_dump(by_alias=True, mode="json")
        context = (pack.model_dump(by_alias=True, mode="json") if fixture["stage"] == "state" else
            Projector(pack).final_answer_view(validated_results=fixture["products"], evidence_refs=fixture["evidenceRefs"],
                phase_task_revision=state.revision).model_dump(by_alias=True, mode="json"))
    prompt = "\n\n".join([llm.AGENT_SYSTEM_PROMPT,
        llm.TASK_STATE_PLANNING_PROMPT if fixture["stage"] == "state" else FINAL_INSTRUCTION,
        "本阶段返回 update_task_state 参数 JSON；null 表示不更新。" if fixture["stage"] == "state" else "只输出面向用户的最终回答正文。",
        "<context>" + canonical(context) + "</context>", "当前用户请求：" + fixture["query"]])
    return prompt, context, receipt

async def prepare():
    if (HERE / "inputs").exists(): raise RuntimeError("inputs_already_exist_use_new_build")
    llm, _, _, _, _, _, strict, _, old = p.load_app()
    assert file_sha(p.CATALOG) == p.CATALOG_SHA
    products = tuple(old._product_from_catalog(x) for x in jsonl(p.CATALOG))
    transport = old.FrozenCatalogTransport(products)
    rng = random.Random(SEED)
    built = []
    for index, spec in enumerate(select_specs(), 1):
        state, hard = await make_state(spec, index)
        # Fixed candidate counts create genuine evidence-size strata, not repeated-text padding.
        candidate_count = (3, 5, 8)[(index-1) % 3] if spec["stage"] == "final" else 0
        chosen = rng.sample(list(products), candidate_count)
        tool = await transport("get_product_details", {"productIds": [x["id"] for x in chosen]}) if chosen else None
        if tool and not tool.ok: raise RuntimeError("frozen_details_failed")
        evidence = tool.detail["products"] if tool else []
        history = [{"role": "user", "content": s} for s in spec["texts"][:-1]]
        allowed = ["update_task_state"] if spec["stage"] == "state" else []
        fixture = {"id": f"case-{index:03d}", "stage": spec["stage"], "source": spec,
            "provenance": "PUBLIC_USER_WORDING_WITH_CONTROLLED_RECONSTRUCTED_STATE_AND_CANDIDATES",
            "state": state.model_dump(by_alias=True, mode="json"), "query": spec["texts"][-1], "history": history,
            "allowedTools": allowed, "products": evidence, "evidenceRefs": [f"frozen-catalog:{x['id']}" for x in evidence],
            "runId": f"abc-study-source-{index}", "candidateCount": candidate_count,
            "schema": strict(llm.TASK_STATE_TOOL_SCHEMA)["function"]["parameters"] if spec["stage"] == "state" else None,
            "referenceHardBefore": hard, "oracle": {"expectedHard": spec.get("expectedHard"), "absentKeys": spec.get("absentKeys", [])}}
        fixture["raw"] = {"taskState": fixture["state"], "fullHistory": history,
            "validatedResults": evidence, "allowedTools": allowed}
        fixture["prompts"], fixture["contexts"], fixture["appInputTokens"] = {}, {}, {}
        for arm in ARMS:
            prompt, context, receipt = await build_context(fixture, arm)
            fixture["prompts"][arm], fixture["contexts"][arm] = prompt, context
            fixture["appInputTokens"][arm] = tokens(prompt)
            if receipt: fixture["compilerReceipt"] = receipt
        assert all(h["content"] in fixture["prompts"]["A_RAW"] for h in history)
        assert all('"oracle"' not in t and '"expectedHard"' not in t and '"sourceRowHash"' not in t for t in fixture["prompts"].values())
        length = fixture["appInputTokens"]["A_RAW"]
        fixture["lengthBin"] = "short_0_4k" if length <= 4000 else "medium_4_8k" if length <= 8000 else "long_8_16k" if length <= 16000 else "over_16k"
        fixture["sameApplicationInputBC"] = fixture["prompts"][ARMS[1]] == fixture["prompts"][ARMS[2]]
        built.append(fixture)
    inputs = HERE / "inputs"
    inputs.mkdir()
    for f in built:
        write_new(inputs / (f["id"] + ".json"), f)
        if f["schema"]: write_new(inputs / (f["id"] + ".schema.json"), f["schema"])
    (inputs / "base_instructions.txt").write_text(p.BASE_INSTRUCTIONS, encoding="utf-8")
    schedule, serial = [], 0
    orderings = [(0,1,2),(1,2,0),(2,0,1),(0,2,1),(2,1,0),(1,0,2)]
    # All arms adjacent; repeat blocks interleave the same frozen sample pool.
    for repeat in range(1, 4):
        indices = list(range(48)); rng.shuffle(indices)
        for position, i in enumerate(indices):
            for arm_index in orderings[(i + repeat - 1) % 6]:
                serial += 1
                schedule.append({"ordinal": serial, "caseId": built[i]["id"], "arm": ARMS[arm_index], "repeat": repeat, "kind": "study"})
            if (position + 1) % 12 == 0:
                serial += 1
                schedule.append({"ordinal": serial, "caseId": built[0]["id"], "arm": "A_RAW", "repeat": repeat, "kind": "identical_input_control"})
    assert len(schedule) == 444
    write_new(inputs / "schedule.json", schedule)
    sources = list((ROOT / "agent/app").rglob("*.py")) + [PUBLIC, BREADTH / "dev.jsonl", BREADTH / "confirm.jsonl", p.CATALOG,
        Path(p.__file__), ROOT / "agent/evaluation/real_user_multiturn_ab_executor_20260902_v2/lane_runtime.py"]
    manifest = {"at": now(), "seed": SEED, "model": p.MODEL, "effort": p.EFFORT, "tokenizer": tokenizer_receipt(),
        "sources": {str(f.relative_to(ROOT)): file_sha(f) for f in sources},
        "inputFiles": {f.name: file_sha(f) for f in inputs.iterdir() if f.is_file()},
        "sampleCount": 48, "scheduledExecutions": 444, "stages": dict(Counter(f["stage"] for f in built)),
        "lengthBins": dict(Counter(f["lengthBin"] for f in built)), "sourcesByKind": dict(Counter(f["source"]["sourceKind"] for f in built)),
        "sameInputBC": sum(f["sameApplicationInputBC"] for f in built),
        "compilerBudgetEstimatedTokens": 20000, "compilerBudgetChoice": "unchanged_from_pilot_nonbinding; no budget sweep in this study",
        "durationLimitSeconds": 21600, "perExecutionTimeoutSeconds": 300,
        "rawLengthDefinition": "fixed o200k_base application prompt only, excluding CLI injected context and external schema framing",
        "notes": ["existing sources are not a new independent holdout", "no fabricated assistant history", "candidate-count strata are not history-length strata",
                  "reference TaskState is reconstructed by narrow public-text rules; not a claim of production extraction correctness",
                  "private expectations used only to check reference parser; excluded from all SUT prompts", "no model calls in preparation"]}
    write_new(inputs / "manifest.json", manifest)
    print(canonical({k: manifest[k] for k in ("sampleCount", "scheduledExecutions", "stages", "lengthBins", "sourcesByKind", "sameInputBC")}), flush=True)

if __name__ == "__main__": asyncio.run(prepare())
