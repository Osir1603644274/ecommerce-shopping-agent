"""Real-query seeded synthetic USER scripts; never prewritten Agent replies."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema

from .artifacts import ROOT, canonical, file_sha, now, sha, write_new
from .subscription import SubscriptionClient

SEEDS = ROOT / "data/derived/ecommerce/used_phone_real_query_qrel_v1/queries.jsonl"
SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"seedQuery": {"type": "string"}, "turns": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"turn": {"type": "integer"}, "userText": {"type": "string", "minLength": 2},
            "intent": {"type": "string", "enum": ["initial", "clarify", "modify", "withdraw", "compare", "recall", "discussion", "note"]}},
        "required": ["turn", "userText", "intent"]}}}, "required": ["seedQuery", "turns"]}


def validate(value, *, seed, turns):
    jsonschema.validate(value, SCHEMA)
    if value["seedQuery"] != seed or len(value["turns"]) != turns:
        raise ValueError("seed_or_turn_count_mismatch")
    if value["turns"][0]["userText"] != seed:
        raise ValueError("initial_query_not_original")
    if [row["turn"] for row in value["turns"]] != list(range(1, turns + 1)):
        raise ValueError("noncontiguous_script")
    texts = [row["userText"] for row in value["turns"]]
    if len(set(texts)) != turns:
        raise ValueError("repeated_user_turn")
    intents = {row["intent"] for row in value["turns"]}
    if not {"modify", "withdraw", "compare", "recall", "note"}.issubset(intents):
        raise ValueError("missing_context_scenario_coverage")
    if any("assistant" in row or "expectedAnswer" in row for row in value["turns"]):
        raise ValueError("fabricated_agent_answer")


async def generate(output: Path, *, count=2, turns=24):
    output.mkdir(parents=True, exist_ok=False)
    # Read queries only. Relevance judgments, sealed labels and catalog qrels
    # are never read, sent to the author model, or used as an answer oracle.
    rows = [json.loads(line) for line in SEEDS.read_text(encoding="utf-8").splitlines() if line.strip()]
    selected = sorted(rows, key=lambda row: sha(["history-v1-development", row["query_id"]]))[:count]
    client = SubscriptionClient(output / "author_calls", timeout_seconds=300, max_calls=count)
    write_new(output / "started.json", {"at": now(), "split": "DEVELOPMENT_ONLY", "seedFile": str(SEEDS),
        "seedFileSha256": file_sha(SEEDS), "qrelsRead": False, "syntheticUserTurns": True,
        "agentReplies": "GENERATED_BY_ACTUAL_SUT_DURING_EXECUTION", "authorModel": "gpt-5.6-sol",
        "count": count, "turnsPerConversation": turns, "generatorSha256": file_sha(__file__)})
    records = []
    for ordinal, seed in enumerate(selected, 1):
        prompt = ("根据真实的二手手机搜索 query，生成自然但可执行的单次购物任务多轮用户脚本。"
            "只生成用户会说的话，不生成或预设助手回复、工具结果、商品ID、金标准答案。"
            "所有轮次仍是同一件购买任务，不要创建跨会话永久画像。"
            "逐步澄清购买对象、用途、硬预算、软品牌偏好；至少三次真实条件变化、两次撤销；"
            "有不玩游戏等否定用途，区别倾向品牌和只要品牌。至少一轮用中文数词预算或简单预算算术，"
            "让自然语言状态抽取也接受检验。加入两段各150至250汉字的有用背景说明和任务备注，"
            "内容必须含真实任务取舍和随后需要回查的细节，不能重复废话拉长。"
            "后续修改一条旧备注并明确撤销旧值；较晚轮次分别询问当前值和以前值。"
            "商品比较用最近实际展示的第一款第二款；历史批次明确说之前某轮，不假定任意商品ID。"
            "未展示足够候选时，用户要求说明原因而不是编造候选。注意普通电商Agent只有冻结机况字段，"
            "可执行硬条件只用：品牌、人民币预算、Android/iOS、电池健康分档、屏幕原装与否、"
            "主板是否维修、电池是否原装、划痕等级、外壳状态。容量、成色几成新、充电循环、"
            "进水、Face ID、镜头、续航时长等不在此任务工具的结构化过滤契约中，只能当作待核实问题，"
            "不能假定可精确筛选。第一轮保留真实 query；后续若扩大型号范围，要由用户明确撤销或放宽，"
            "不得暗中放宽。不要连续堆满无法执行的硬要求；这批先测试受控购物流程内的上下文记忆。"
            "不要要求下单、支付或真实库存。不要把安全要求写成莫名其妙的品牌排除。"
            f"恰好 {turns} 轮，第一轮 userText 必须逐字等于下面 query。后续对话自然连贯。"
            "只返回符合 schema 的 JSON，intent 表示这句用户话的类型，不是对 Agent 的答案指导。\n"
            + canonical({"query": seed["query"], "schema": SCHEMA}))
        response = await client.chat.completions.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": prompt}])
        value = json.loads(response.choices[0].message.content)
        validate(value, seed=seed["query"], turns=turns)
        scenario = {"scenarioId": f"dev-{ordinal:03d}", "familyId": seed["query_id"],
            "seedProvenance": {"source": seed["source"], "originalSourceSplit": seed["split"],
                               "originalQueryId": seed["query_id"], "querySha256": sha(seed["query"])},
            "provenance": "REAL_QUERY_SEEDED_AI_SYNTHETIC_USER_SCRIPT", "split": "development", **value}
        destination = output / f"dev-{ordinal:03d}.json"
        write_new(destination, scenario)
        records.append({"id": scenario["scenarioId"], "familyId": scenario["familyId"],
            "file": destination.name, "sha256": file_sha(destination), "turns": turns,
            "userCharacters": sum(len(row["userText"]) for row in value["turns"])})
        print(canonical({"status": "DEV_SCRIPT_GENERATED", **records[-1]}), flush=True)
    write_new(output / "manifest.json", {"status": "DEVELOPMENT_SCRIPTS_GENERATED_NOT_REVIEWED", "scenarios": records,
        "formalHoldout": "NOT_GENERATED_OR_OPENED", "sourceFamilySplit": "RESERVE_OTHER_FAMILIES_BEFORE_FORMAL_GENERATION",
        "syntheticNotRealConversation": True, "authorUsageSeparateFromSut": True})


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--turns", type=int, default=24)
    args = parser.parse_args()
    if not 1 <= args.count <= 8 or not 12 <= args.turns <= 72:
        raise ValueError("development_generation_ceiling")
    asyncio.run(generate(args.output, count=args.count, turns=args.turns))
