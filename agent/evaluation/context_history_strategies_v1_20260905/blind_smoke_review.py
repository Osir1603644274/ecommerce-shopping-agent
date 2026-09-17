"""Independent fresh-session AI quality review of development smoke answers."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import random

import jsonschema

from .artifacts import HERE, canonical, file_sha, sha, write_new
from .subscription import SubscriptionClient

RUBRIC = {"type": "object", "additionalProperties": False,
    "properties": {"scores": {"type": "array", "items": {"type": "object", "additionalProperties": False,
        "properties": {"sampleId": {"type": "string"},
            **{key: {"type": "integer", "minimum": 0, "maximum": 4}
               for key in ("correctness", "constraints", "relevance", "usefulness")},
            "seriousErrors": {"type": "array", "items": {"type": "string"}},
            "rationale": {"type": "string"}},
        "required": ["sampleId", "correctness", "constraints", "relevance", "usefulness", "seriousErrors", "rationale"]}}},
    "required": ["scores"]}


def sample(directory, identifier):
    rows = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("turn-*.json"))]
    if len(rows) != 3 or rows[-1]["traceSummary"]["agentStatus"] != "ok":
        raise ValueError("incomplete_review_source")
    transcript = []
    for row in rows:
        transcript.extend([{"role": "user", "content": row["query"]}, {"role": "assistant", "content": row["answer"]}])
    evidence = []
    for trace in rows[-1]["toolTraces"]:
        if trace["tool"] == "compare_products" and trace["ok"]:
            evidence.append({"category": trace["detail"].get("category"),
                "products": [{"facts": product.get("facts"), "checks": product.get("checks")}
                             for product in trace["detail"].get("products", [])]})
    return {"sampleId": identifier, "dialogue": transcript, "verifiedEvidenceForFinalTurn": evidence}


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    sources = [HERE / "strategy_smoke_A002", HERE / "strategy_smoke_B002", HERE / "strategy_smoke_C001"]
    mappings = [{"id": "sample-" + sha(["blind-smoke", index])[:10], "directory": str(directory),
                 "sourceSha256": file_sha(directory / "turn-03.json")} for index, directory in enumerate(sources)]
    write_new(output / "mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json", mappings)
    values = [sample(Path(row["directory"]), row["id"]) for row in mappings]
    for judge in (1, 2):
        client = SubscriptionClient(output / f"judge-{judge}", max_calls=1)
        ordered = list(values)
        random.Random(20260905 + judge).shuffle(ordered)
        prompt = ("你是独立评审，只根据提供的对话和已核验商品证据，评价每份对话中最后一条助手回答。"
            "不知道各样本的实现或处理方式，请不要猜测。历史用户原话决定需求，助手旧回答不是需求真值。"
            "每项用0至4整数评分：correctness事实正确性，constraints当前条件遵守，relevance相关性，"
            "usefulness实际选购帮助。4=充分满足；3=小遗漏；2=明显不足；1=严重问题；0=完全失败。"
            "偏好不能擅自升级为硬条件；父亲微信视频/看新闻且不玩游戏应体现在取舍中，但不可捏造性能或拍照实测。"
            "本任务价格为明确标注的合成模拟参考价，可用于预算比较，不是实时报价。"
            "严重错误包括违反明确硬要求、错指商品、捏造证据；单纯少提一个偏好不自动等于严重错误。"
            "保留真实分歧，不看收益、不受任何上线目标影响。rationale用简短中文指出具体依据。"
            "只输出符合schema的JSON，所有sampleId必须各出现一次。\n"
            + canonical({"schema": RUBRIC, "samples": ordered}))
        response = await client.chat.completions.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": prompt}])
        value = json.loads(response.choices[0].message.content)
        jsonschema.validate(value, RUBRIC)
        if sorted(row["sampleId"] for row in value["scores"]) != sorted(row["id"] for row in mappings):
            raise ValueError("judge_sample_identity_mismatch")
        write_new(output / f"judgment-{judge}.json", value)
        print(canonical({"status": "INDEPENDENT_AI_REVIEW_RETURNED", "judge": judge}), flush=True)
    write_new(output / "result.json", {"status": "TWO_INDEPENDENT_AI_REVIEWS_RETURNED", "formalAcceptance": False,
        "sourceScope": "ONE_THREE_TURN_DEVELOPMENT_SCENARIO_PER_ARM", "humanGold": False,
        "latencyAndTokenHidden": True, "armNamesHidden": True,
        "note": "Distinct development source versions; not a formal paired comparison. Adjudication and source audit precede any quality claim."})


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    asyncio.run(run(parser.parse_args().output))
