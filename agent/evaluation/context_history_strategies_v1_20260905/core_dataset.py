"""A longer, supported-capability development script from an audited dev seed."""
import argparse
import asyncio
import json
from pathlib import Path

from .artifacts import HERE, canonical, file_sha, sha, write_new
from .dataset import SCHEMA, validate
from .subscription import SubscriptionClient


async def run(output, turns):
    split_path = HERE / "seed_split001.json"
    split = json.loads(split_path.read_text(encoding="utf-8"))
    query_id = "ksq-0ba6f2fe5ba8b50425893a20"
    family, seed = next((family, member) for family in split["families"] for member in family["members"] if member["query_id"] == query_id)
    if family["split"] != "development":
        raise ValueError("seed_not_development")
    output.mkdir(parents=True, exist_ok=False)
    write_new(output / "started.json", {"sourceSplitSha256": file_sha(split_path), "queryId": query_id,
        "familyId": family["familyId"], "split": "development", "turns": turns, "qrelsRead": False,
        "generatorSha256": file_sha(__file__), "selectionReason": "New hash-selected development family with explicit budget shopping intent; no Agent outcomes used for selection."})
    client = SubscriptionClient(output / "author_calls", max_calls=1)
    prompt = ("生成单个二手手机购物任务的真实Query派生多轮用户脚本，只写用户话，不编助手答复、商品ID或工具结果。"
        "这是上下文管理开发集，不是实际用户对话。第一轮必须逐字保留真实Query。第二轮一次补齐购买对象、用途、"
        "具体预算、系统和品牌是否为硬条件；不要把一次澄清拆成许多不回答助手问题的碎片。"
        "所有轮次同一购物任务；不涉及下单、支付、跨会话画像。全量历史组也必须能完成，不能拿工具不支持的能力当压缩问题。"
        "可筛选字段：人民币预算；品牌；Android/iOS；电池健康70-80%、80-90%或90%以上档；屏幕原装与否；"
        "主板维修与否；电池原装与否；无/轻微/明显划痕；外壳正常/损坏。不要用模糊良好档、85%精确阈值、"
        "几成新、指定型号、存储容量、性能、循环次数、进水、Face ID作系统硬筛选。后者只能作为现场核实问题。"
        "同时最多3项机况硬筛选，预算从2000元开始，有用户明确授权的修改和撤销；至少一次中文预算算术，"
        "至少四次撤销/替换，严格区别软偏好与硬条件，不得暗中放宽。不必每个修改都要求马上重搜。"
        "自然交替：采购背景取舍、检索、比较、备注、当前状态问答、旧值回忆、待核实清单。"
        "展示或比较必须引用最近实际展示候选，不足两款就要求如实说明，不能假定一定有结果。"
        "历史批次只用于回查过去实际展示内容或条件变化，不要求把旧批次冒充当前可执行范围。"
        "至少8段各250至400汉字的实质任务备注，分散在前四分之三对话。每段涉及不同的采购取舍/验机安排/"
        "信息缺口/运输配件约定/应用迁移/卖家问题/使用日程/现场检查次序；不要重复填充。"
        "这些备注中至少6个具体细节要在更晚轮次被修改、对比或回问，让长历史真的有用。"
        "后半段分别询问现在值和已经撤销的以前值，明确轮号，不能把旧值恢复成现行要求。"
        "轮次相互引用必须和实际脚本一致。最后一轮要综合当前条件、软偏好、用途、有效备注与旧值区分。"
        f"恰好{turns}轮，第一轮userText为原query，只返回schema JSON。\n" + canonical({"query": seed["query"], "schema": SCHEMA}))
    response = await client.chat.completions.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": prompt}])
    value = json.loads(response.choices[0].message.content)
    validate(value, seed=seed["query"], turns=turns)
    notes = [row for row in value["turns"] if row["intent"] == "note" and 250 <= len(row["userText"]) <= 450]
    if len(notes) < 8:
        raise ValueError("meaningful_long_note_coverage_missing")
    scenario = {"scenarioId": f"core-development-{turns}", "split": "development", "familyId": family["familyId"],
        "seedProvenance": {"source": seed["source"], "originalQueryId": query_id,
            "originalSourceSplit": seed["split"], "querySha256": sha(seed["query"])},
        "provenance": "REAL_QUERY_SEEDED_AI_SYNTHETIC_USER_SCRIPT", **value}
    write_new(output / "script.json", scenario)
    write_new(output / "result.json", {"status": "DEVELOPMENT_CORE_SCRIPT_AWAITING_SEMANTIC_AUDIT",
        "turns": turns, "longNotes": len(notes), "userCharacters": sum(len(row["userText"]) for row in value["turns"]),
        "scriptSha256": file_sha(output / "script.json"), "formalAcceptance": False,
        "actualHistoryTokens": "UNKNOWN_UNTIL_REAL_AGENT_REPLAY", "realAgentRepliesRequired": True})
    print(canonical({"status": "SCRIPT_GENERATED_REQUIRES_AUDIT", "turns": turns, "longNotes": len(notes)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--turns", type=int, choices=[24, 48, 72], default=48)
    args = parser.parse_args()
    asyncio.run(run(args.output, args.turns))
