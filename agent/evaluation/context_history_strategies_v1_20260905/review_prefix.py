"""Anonymous, fresh-session development-prefix review with disagreement handling."""
import argparse
import asyncio
import json
from pathlib import Path
import random

import jsonschema

from .artifacts import HERE, canonical, file_sha, sha, write_new
from .blind_smoke_review import RUBRIC
from .subscription import SubscriptionClient
from .review_evidence import validated_turn_evidence

DIMENSIONS = ("correctness", "constraints", "relevance", "usefulness")


async def run(output, turns=24, attempts=None):
    output.mkdir(parents=True, exist_ok=False)
    sources = [HERE / name for name in (attempts or ("long_dev2_A001", "long_dev2_B001", "long_dev2_C001"))]
    values, mapping, query_lists = [], [], []
    for index, directory in enumerate(sources):
        files = [directory / f"turn-{turn:02d}.json" for turn in range(1, turns + 1)]
        rows = [json.loads(path.read_text(encoding="utf-8")) for path in files]
        query_lists.append([row["query"] for row in rows])
        identifier = "sample-" + sha(["prefix-review", index])[:12]
        dialogue = []
        for row in rows:
            dialogue.extend([{"role": "user", "content": row["query"]}, {"role": "assistant", "content": row["answer"]}])
        final = rows[-1]
        evidence = []
        if any(phase.get("phase") == "validator" and phase.get("outcome") == "passed"
               for phase in (final.get("traceSummary") or {}).get("phases", [])):
            for trace in final["toolTraces"]:
                if trace["ok"] and trace["tool"] in {"search_products", "compare_products"}:
                    detail = trace["detail"]
                    products = detail.get("products", detail.get("candidates", []))
                    evidence.append({"tool": trace["tool"], "products": [
                        {key: product.get(key) for key in ("id", "product", "name", "title", "facts", "checks") if key in product}
                        for product in products]})
        values.append({"sampleId": identifier, "dialogue": dialogue, "verifiedEvidenceForFinalTurn": evidence,
            "verifiedEvidenceByTurn": [validated_turn_evidence(row) for row in rows]})
        mapping.append({"sampleId": identifier, "directory": str(directory),
            "turnHashes": {path.name: file_sha(path) for path in files}, "dialogueHash": sha(dialogue)})
    if any(queries != query_lists[0] for queries in query_lists):
        raise ValueError("unpaired_user_script")
    write_new(output / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json", mapping)
    async def judge(number):
        client = SubscriptionClient(output / f"judge-{number}", max_calls=1)
        ordered = list(values)
        random.Random(20260905 + number).shuffle(ordered)
        prompt = ("你是无其他上下文的独立质量评审。阅读每份完整对话，评价最后一条助手答复。"
            "只根据原始用户话语及提供的已验证商品证据；助手旧话不是需求真值，不能当作正确答案。"
            "不知道样本实现与成本，不要猜测。每项0至4整数：correctness事实正确性、constraints现行条件遵守、"
            "relevance相关性、usefulness实际帮助；4充分满足，3小遗漏，2明显不足，1严重问题，0完全失败。"
            "最后答复的评分与整段对话的严重错误分别判断。seriousErrors列出任何轮次的明确硬约束违反、"
            "逐轮工具证据可能补充了旧聊天摘要未提及的字段，不得因此指控虚构。缺少某轮证据只能说无法核实，不能据此认定捏造。"
            "错误商品指代、虚构证据，必须注明轮号与用户/助手原话依据；缺少商品时合理解释未知不算严重错误。"
            "不要把预算/品牌软偏好误认成硬条件；已撤销条件不能恢复。安全停止也不是优质回答，按用户实际得到的帮助打分。"
            "基础工具只能按品牌、预算、Android/iOS、电池健康分档、屏幕原装、主板维修、电池原装、划痕、外壳状态筛选；"
            "容量、充电循环、进水等可以明确未知，不能捏造核验。价格为已标明合成参考价，不是实时报价。"
            "rationale用简短中文指出最后一轮为何得分。只返回符合schema的JSON，每个sampleId恰好一次。\n"
            + canonical({"schema": RUBRIC, "samples": ordered}))
        response = await client.chat.completions.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": prompt}])
        value = json.loads(response.choices[0].message.content)
        jsonschema.validate(value, RUBRIC)
        if sorted(row["sampleId"] for row in value["scores"]) != sorted(row["sampleId"] for row in mapping):
            raise ValueError("judge_sample_identity_mismatch")
        write_new(output / f"judgment-{number}.json", value)
        print(canonical({"judge": number, "status": "INDEPENDENT_REVIEW_RETURNED"}), flush=True)
        return {row["sampleId"]: row for row in value["scores"]}
    judges = [await judge(1), await judge(2)]
    disagreements = [key for key in judges[0] if any(abs(judges[0][key][dim] - judges[1][key][dim]) >= 2 for dim in DIMENSIONS)
        or bool(judges[0][key]["seriousErrors"]) != bool(judges[1][key]["seriousErrors"])]
    if disagreements:
        judges.append(await judge(3))
    write_new(output / "result.json", {"status": "DEVELOPMENT_PREFIX_REVIEW_COMPLETE", "turnsPerSample": turns,
        "judges": len(judges), "disagreements": disagreements, "allIndependentAndAnonymous": True,
        "humanGold": False, "formalAcceptance": False,
        "note": "Common real-user-script prefix only. No claim about compression benefit, full-conversation quality or holdout noninferiority."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--turns", type=int, default=24)
    parser.add_argument("--attempts", nargs=3)
    args = parser.parse_args()
    asyncio.run(run(args.output, args.turns, args.attempts))
