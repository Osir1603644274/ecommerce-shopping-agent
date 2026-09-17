"""Bounded fresh-session per-trajectory review; SUT code stays untouched."""
import argparse
import asyncio
import json
from pathlib import Path
import random

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, canonical, file_sha, sha, write_new
from agent.evaluation.context_history_strategies_v1_20260905.history_strategies import tokens
from agent.evaluation.context_history_strategies_v1_20260905.review_conversation import (
    DIMENSIONS, SCHEMA, disagreements, validate_judgment)
from agent.evaluation.context_history_strategies_v1_20260905.subscription import SubscriptionClient
from .evidence import encode, validated_turn_evidence

RUBRIC = """你是独立匿名评审，不知道实验组、实现或成本。只对targetTurns中的每条助手回答逐轮评分，每个目标恰好一次。
四项0至4整数：correctness事实正确性；constraints当时有效条件遵守；relevance相关性；usefulness实际帮助。
4充分满足，3小遗漏，2明显不足，1严重问题，0完全失败。不能只评最后一轮，不能用未来要求评价过去回答。
原始用户话语是需求真值，助手旧说法和persistedRequirements不是gold；后者是被测系统真实状态，软偏好存为硬条件也应指出。
软偏好不是硬条件，撤销旧值不可恢复。备注/回忆请求却收到无关商品列表应扣相关性与帮助分，安全停止不是优质回答。
seriousErrors仅列已确认的商品硬约束违反或擅自放宽、错商品指代、虚构证据，逐字引用助手和相关用户原话，并标userTurn。
不因聊天未提某属性就认定虚构；先核对各轮工具证据。无证据表示无法核实，不自动表示捏造。
后台保留候选、明确未知的closest_alternative不等于实际展示或宣称满足硬条件。检查记录是SUT的判断，不是用户需求真值。
answerFormatContracts是实际输出限制；有maxProducts上限不能指控上限是模型虚构。展示数量短缺可以在维度扣分，
但仅数量/格式不足不能升级为严重hard_constraint；该类别专指商品硬筛选条件被违反或擅自放宽。
最近第一/第二款以实际展示顺序为准；用户明确说之前或指定历史轮次时才回指相应历史列表。
工具支持品牌、预算、Android/iOS、电池健康分档、屏幕/电池原装、主板维修、划痕、外壳状态；
容量、循环次数、进水等只能待核实，不能编造核验。模拟参考价不是实时价格，初筛也不等于已读取详情和完成比较。
evidenceDictionary是无损证据字典，不是模型摘要。每项layout指定evidenceLayouts的列名，values按这些列名一一对应，合并即可得到原始对象。
各轮productEvidenceRefs中的product引用商品事实对象；checks引用该对象当轮检查项；商品specificationsRef引用原始规格对象。
合并这些引用即可还原完整逐轮products；实际displayedProductIds、rankedProductIds与原文顺序均保留。不同事实不会因商品ID相同而合并。
所有先前对话保持逐字原文，没有未来轮次；需要参考历史证据时也请解析相应引用。rationale简短中文。只返回schema允许的JSON。
"""


def resolve_attempt(name):
    """Allow legacy flat attempts or exact children of a recorded serial cohort."""
    raw = HERE / name
    if ".." in raw.parts:
        raise ValueError("attempt_outside_study")
    directory = raw.resolve()
    root = HERE.resolve()
    if directory == root or not directory.is_relative_to(root):
        raise ValueError("attempt_outside_study")
    if any(part.is_symlink() for part in (raw, *raw.parents) if part != root and part.is_relative_to(HERE)):
        raise ValueError("linked_attempt_not_supported")
    if directory.parent == root:
        return directory
    if directory.parent.parent != root:
        raise ValueError("attempt_outside_study")
    manifest_path = directory.parent / "started.json"
    if not manifest_path.is_file():
        raise ValueError("nested_attempt_missing_cohort_binding")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL":
        raise ValueError("nested_attempt_wrong_cohort_kind")
    matches = [job for job in manifest.get("jobs", []) if Path(job.get("output", "")).resolve() == directory]
    if len(matches) != 1:
        raise ValueError("nested_attempt_not_uniquely_declared")
    return directory


def load_samples(attempts, turns):
    samples, mapping, scripts = [], [], []
    for number, name in enumerate(attempts):
        directory = resolve_attempt(name)
        paths = [directory / f"turn-{turn:02d}.json" for turn in range(1, turns + 1)]
        canonical_name = directory.relative_to(HERE.resolve()).as_posix()
        identifier = "sample-" + sha(["lossless-review-v2", number, canonical_name])[:12]
        dialogue, evidence, script, turn_hashes = [], [], [], {}
        # A full turn includes large pre/post state and traces. Extract the
        # exact review fields one row at a time instead of retaining all raw
        # snapshots alongside their independent evidence copies.
        for path in paths:
            row = json.loads(path.read_text(encoding="utf-8"))
            dialogue.append({"turn": row["turn"], "user": row["query"], "assistant": row["answer"]})
            evidence.append(validated_turn_evidence(row))
            script.append(row["query"])
            turn_hashes[path.name] = file_sha(path)
        scripts.append(script)
        samples.append({"sampleId": identifier, "turns": dialogue,
            "verifiedEvidenceByTurn": evidence})
        mapping.append({"sampleId": identifier, "directory": str(directory),
            "dialogueHash": sha(dialogue), "turnHashes": turn_hashes})
    if any(script != scripts[0] for script in scripts):
        raise ValueError("unpaired_user_scripts")
    return samples, mapping


def prepare(sample, targets):
    visible = {**sample, "turns": sample["turns"][:targets[-1]],
        "verifiedEvidenceByTurn": sample["verifiedEvidenceByTurn"][:targets[-1]]}
    packet = encode([visible])
    payload = {**packet, "targetTurns": targets, "schema": SCHEMA}
    prompt = RUBRIC + "\n" + canonical(payload)
    request = {"model": "gpt-5.6-sol", "messages": [{"role": "user", "content": prompt}]}
    return visible, request, {"sampleId": sample["sampleId"], "targetTurns": targets,
        "originalEvidenceTokens": tokens([visible]), "encodedEvidenceTokens": tokens(packet),
        "applicationRequestTokens": tokens(request), "roundTripExact": True,
        "sourceVisibleHash": sha([visible]), "encodedPacketHash": sha(packet), "requestHash": sha(request)}


async def run(output, attempts, turns, chunk_size, budget, prepare_only, judge_concurrency=2):
    output.mkdir(parents=True, exist_ok=False)
    source_paths = [*Path(__file__).parent.glob("*.py"),
        *[HERE / name for name in ("artifacts.py", "subscription.py", "review_evidence.py", "review_conversation.py", "history_strategies.py")]]
    sources = {str(path.resolve()): file_sha(path) for path in source_paths}
    samples, mapping = load_samples(attempts, turns)
    write_new(output / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json", mapping)
    write_new(output / "started.json", {"kind": "DEVELOPMENT_LOSSLESS_SINGLE_TRAJECTORY_REVIEW_V2",
        "sources": sources, "rubricHash": sha(RUBRIC), "turns": turns, "chunkSize": chunk_size,
        "packetBudget": budget, "prepareOnly": prepare_only, "humanGold": False,
        "judgeConcurrency": judge_concurrency,
        "note": "Single-trajectory sessions differ from earlier three-trajectory packets. Scores are not silently pooled across rubric/packet versions."})
    jobs, audits = [], []
    for start in range(1, turns + 1, chunk_size):
        targets = list(range(start, min(start + chunk_size, turns + 1)))
        for sample in samples:
            visible, request, audit = prepare(sample, targets)
            audits.append(audit)
            jobs.append((sample["sampleId"], targets, visible, request))
    write_new(output / "packet_audit.json", audits)
    if any(row["applicationRequestTokens"] > budget for row in audits):
        raise ValueError("review_packet_budget_exceeded_no_model_calls_started")
    if prepare_only:
        write_new(output / "result.json", {"status": "PACKET_ROUND_TRIP_PASS_NO_MODEL_CALLS",
            "packets": len(audits), "maxApplicationRequestTokens": max(row["applicationRequestTokens"] for row in audits)})
        print(canonical({"packets": len(audits), "maxTokens": max(row["applicationRequestTokens"] for row in audits)}), flush=True)
        return
    all_scores = {1: {}, 2: {}, 3: {}}
    decisions = []
    semaphore = asyncio.Semaphore(judge_concurrency)
    random.Random(20260905).shuffle(jobs)

    async def judge(job, number):
        identifier, targets, visible, request = job
        async with semaphore:
            if any(file_sha(Path(path)) != digest for path, digest in sources.items()):
                raise ValueError("review_source_drift")
            client = SubscriptionClient(output / f"{identifier}-chunk-{targets[0]:02d}-judge-{number}",
                max_calls=1, application_input_budget=budget)
            response = await client.chat.completions.create(**request)
            value = json.loads(response.choices[0].message.content)
            scores = validate_judgment(value, [visible], targets)
            write_new(output / f"{identifier}-chunk-{targets[0]:02d}-judgment-{number}.json", value)
            all_scores[number].update(scores)
            print(canonical({"sample": identifier, "turns": targets, "judge": number, "scores": len(scores)}), flush=True)
            return scores

    # Two independent calls per packet; every call is a new isolated session.
    # Serial packet scheduling bounds host concurrency at two judge calls.
    for job in jobs:
        first, second = await independent_pair(judge(job, 1), judge(job, 2))
        disputed = disagreements(first, second)
        decisions.append({"sampleId": job[0], "turns": job[1], "disputed": [list(key) for key in disputed]})
        if disputed:
            await judge(job, 3)
    aggregates = []
    for sample in samples:
        keys = [(sample["sampleId"], turn) for turn in range(1, turns + 1)]
        aggregates.append({"sampleId": sample["sampleId"], "perJudge": [{"judge": number,
            "meanAllTurns": {dim: sum(all_scores[number][key][dim] for key in keys) / turns for dim in DIMENSIONS},
            "seriousClaimTurns": [key[1] for key in keys if all_scores[number][key]["seriousErrors"]]} for number in (1, 2)]})
    if any(file_sha(Path(path)) != digest for path, digest in sources.items()):
        raise ValueError("review_source_drift")
    write_new(output / "result.json", {"status": "DEVELOPMENT_EVERY_TURN_REVIEW_REQUIRES_CLAIM_AUDIT",
        "turnsPerSample": turns, "decisions": decisions, "aggregates": aggregates,
        "allIndependentAndAnonymous": True, "humanGold": False, "formalAcceptance": False,
        "claimQuoteChecksPassed": True, "semanticClaimAuditRequired": True})


async def independent_pair(first, second):
    """Finish both bounded judges even if one judgment is rejected.

    External cancellation still cancels both. Ordinary single-judge failures
    do not cancel a paid sibling; accepted judgments are saved by judge().
    """
    results = await asyncio.gather(first, second, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


def validate_configuration(turns, chunk_size, packet_budget):
    if (any(type(value) is not int for value in (turns, chunk_size, packet_budget)) or
            not 1 <= turns <= 72 or not 1 <= chunk_size <= 12 or not 1000 <= packet_budget <= 192000):
        raise ValueError("review_configuration_out_of_bounds")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--attempts", nargs=3, required=True)
    parser.add_argument("--turns", type=int, default=48)
    parser.add_argument("--chunk-size", type=int, default=6)
    parser.add_argument("--packet-budget", type=int, default=96000)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--judge-concurrency", type=int, choices=[1, 2], default=2)
    args = parser.parse_args()
    validate_configuration(args.turns, args.chunk_size, args.packet_budget)
    try:
        asyncio.run(run(args.output.resolve(), args.attempts, args.turns, args.chunk_size, args.packet_budget, args.prepare_only, args.judge_concurrency))
    except Exception as exc:
        if args.output.exists():
            write_new(args.output / "failure.json", {"type": type(exc).__name__, "error": str(exc), "formalAcceptance": False})
        raise
