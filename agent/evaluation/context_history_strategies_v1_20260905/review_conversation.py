"""Fresh anonymous judges score EVERY answer, with per-turn verified evidence."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import random

import jsonschema

from .artifacts import HERE, canonical, file_sha, sha, write_new
from .review_evidence import validated_turn_evidence
from .subscription import SubscriptionClient

DIMENSIONS = ("correctness", "constraints", "relevance", "usefulness")
ERROR_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "category": {"enum": ["hard_constraint", "wrong_reference", "fabricated_evidence"]},
    "assistantQuote": {"type": "string", "minLength": 1},
    "userTurn": {"type": "integer", "minimum": 1}, "userQuote": {"type": "string", "minLength": 1},
    "reason": {"type": "string", "minLength": 1}},
    "required": ["category", "assistantQuote", "userTurn", "userQuote", "reason"]}
SCORE_SCHEMA = {"type": "object", "additionalProperties": False, "properties": {
    "sampleId": {"type": "string"}, "turn": {"type": "integer", "minimum": 1},
    **{key: {"type": "integer", "minimum": 0, "maximum": 4} for key in DIMENSIONS},
    "rationale": {"type": "string", "minLength": 1},
    "seriousErrors": {"type": "array", "items": ERROR_SCHEMA}},
    "required": ["sampleId", "turn", *DIMENSIONS, "rationale", "seriousErrors"]}
SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"scores": {"type": "array", "items": SCORE_SCHEMA}}, "required": ["scores"]}


def validate_judgment(value, samples, targets):
    jsonschema.validate(value, SCHEMA)
    by_id = {sample["sampleId"]: sample for sample in samples}
    keys = [(score["sampleId"], score["turn"]) for score in value["scores"]]
    expected = {(identifier, turn) for identifier in by_id for turn in targets}
    if len(keys) != len(set(keys)) or set(keys) != expected:
        raise ValueError("judge_target_coverage_mismatch")
    for score in value["scores"]:
        turns = by_id[score["sampleId"]]["turns"]
        answer = turns[score["turn"] - 1]["assistant"]
        for error in score["seriousErrors"]:
            if error["userTurn"] > score["turn"]:
                raise ValueError("judge_claim_uses_future_instruction")
            if error["assistantQuote"] not in answer or error["userQuote"] not in turns[error["userTurn"] - 1]["user"]:
                raise ValueError("judge_quote_not_in_original_message")
    return {(score["sampleId"], score["turn"]): score for score in value["scores"]}


def disagreements(first, second):
    return [key for key in first if any(abs(first[key][dim] - second[key][dim]) >= 2 for dim in DIMENSIONS)
        or {item["category"] for item in first[key]["seriousErrors"]} != {item["category"] for item in second[key]["seriousErrors"]}]


async def run(output, attempts, turns=48, chunk_size=8):
    output.mkdir(parents=True, exist_ok=False)
    samples, mapping, scripts = [], [], []
    for number, name in enumerate(attempts):
        directory = HERE / name
        paths = [directory / f"turn-{turn:02d}.json" for turn in range(1, turns + 1)]
        records = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
        scripts.append([row["query"] for row in records])
        identifier = "sample-" + sha(["complete-trajectory-v1", number, name])[:12]
        dialogue = [{"turn": row["turn"], "user": row["query"], "assistant": row["answer"]} for row in records]
        samples.append({"sampleId": identifier, "turns": dialogue,
            "verifiedEvidenceByTurn": [validated_turn_evidence(row) for row in records]})
        mapping.append({"sampleId": identifier, "directory": str(directory), "turnHashes": {
            path.name: file_sha(path) for path in paths}, "dialogueHash": sha(dialogue)})
    if any(script != scripts[0] for script in scripts):
        raise ValueError("unpaired_user_script")
    write_new(output / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json", mapping)
    all_scores = {1: {}, 2: {}, 3: {}}
    decisions = []
    for start in range(1, turns + 1, chunk_size):
        targets = list(range(start, min(start + chunk_size, turns + 1)))
        # Only preceding/current turns. Future changes cannot define past truth.
        visible = [{**sample, "turns": sample["turns"][:targets[-1]],
            "verifiedEvidenceByTurn": sample["verifiedEvidenceByTurn"][:targets[-1]]} for sample in samples]

        async def judge(number):
            client = SubscriptionClient(output / f"chunk-{start:02d}-judge-{number}", max_calls=1)
            ordered = list(visible)
            random.Random(20260905 + start * 31 + number).shuffle(ordered)
            prompt = ("你是独立匿名评审，没有任何实现、实验组或成本信息。对 targetTurns 中每一条助手答复分别评分，"
                "不能只评最后一轮，也不能用未来用户要求评价过去答复。每个sampleId每个目标轮次恰好一次。"
                "四项0至4整数：correctness事实正确性；constraints当时有效条件遵守；relevance相关性；"
                "usefulness实际帮助。4充分满足，3小遗漏，2明显不足，1严重问题，0完全失败。"
                "需求真值来自原始用户话语，不来自助手以前的说法。软偏好不是硬条件；明确撤销的旧值不再有效。"
                "只复述当前预算却收到无关商品列表，应按相关性和帮助扣分；安全停止不是优质回答。"
                "seriousErrors仅列可确认的硬约束违反、错商品指代、虚构证据，逐字引用原助手话和有关用户原话，注明用户轮号。"
                "不能因旧聊天没提某属性就认定虚构：先检查每轮工具证据。某轮无证据只代表无法核实，不自动构成捏造。"
                "区分实际展示商品、后台保留候选与明确标注未知的closest_alternative；保留在候选池不等于声称满足所有硬条件。"
                "若只有候选数量表述不精确，按事实/相关性评分，不能直接升级为向用户推荐了违规商品。"
                "answerFormatContracts记录真正发给模型的运行时输出限制；有maxProducts上限时不能声称该上限是模型虚构。"
                "展示数量或格式不满足用户愿望可在相应维度扣分，但hard_constraint严重类别专指商品硬筛选条件被违反或擅自放宽，"
                "不能把仅有展示数量短缺归入该严重类别。persistedRequirements是被测Agent实际状态，不是用户需求真值；"
                "若将明确软偏好存成硬条件，即使答复文本正确也要在rationale指出该状态不一致，不伪造原文引句。"
                "最近第一/第二款以实际展示顺序为准；明确历史轮号才能回指旧批次。"
                "工具只支持品牌、预算、Android/iOS、电池健康分档、屏幕/电池原装、主板维修、划痕、外壳状态；"
                "容量、循环次数、进水等可保留待核实，不能编造核验。标为合成参考价的价格不是实时报价。"
                "rationale保持简短中文。只返回符合schema的JSON。\n" + canonical({"schema": SCHEMA,
                    "targetTurns": targets, "samples": ordered}))
            response = await client.chat.completions.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": prompt}])
            value = json.loads(response.choices[0].message.content)
            scores = validate_judgment(value, visible, targets)
            write_new(output / f"chunk-{start:02d}-judgment-{number}.json", value)
            all_scores[number].update(scores)
            print(canonical({"chunk": targets, "judge": number, "scores": len(scores)}), flush=True)
            return scores

        first, second = await asyncio.gather(judge(1), judge(2))
        disputed = disagreements(first, second)
        decisions.append({"turns": targets, "disputed": [list(key) for key in disputed]})
        if disputed:
            await judge(3)
    aggregates = []
    for sample in samples:
        keys = [(sample["sampleId"], turn) for turn in range(1, turns + 1)]
        aggregates.append({"sampleId": sample["sampleId"], "perJudge": [{"judge": number,
            "meanAllTurns": {dim: sum(all_scores[number][key][dim] for key in keys) / turns for dim in DIMENSIONS},
            "seriousClaimTurns": [key[1] for key in keys if all_scores[number][key]["seriousErrors"]]} for number in (1, 2)]})
    write_new(output / "result.json", {"status": "DEVELOPMENT_EVERY_TURN_REVIEW_REQUIRES_CLAIM_AUDIT",
        "turnsPerSample": turns, "decisions": decisions, "aggregates": aggregates,
        "allIndependentAndAnonymous": True, "humanGold": False, "formalAcceptance": False,
        "claimQuoteChecksPassed": True, "semanticClaimAuditRequired": True,
        "note": "Raw double scores and third-judge disagreements remain separate; do not silently remove contested errors or declare noninferiority from this development review."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--attempts", nargs=3, required=True)
    parser.add_argument("--turns", type=int, default=48)
    parser.add_argument("--chunk-size", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.turns <= 72 or not 1 <= args.chunk_size <= 12:
        raise ValueError("review_size_ceiling")
    asyncio.run(run(args.output, args.attempts, args.turns, args.chunk_size))
