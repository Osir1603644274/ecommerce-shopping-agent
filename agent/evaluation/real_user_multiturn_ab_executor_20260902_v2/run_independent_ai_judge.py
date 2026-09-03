"""Run one mapping-blind AI judge over exactly one public judge packet."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from jsonschema import Draft202012Validator
from openai import AsyncOpenAI

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.app.model_compat import tool_choice_kwargs
from agent.app.settings import settings


TOOL_NAME = "submit_context_blind_review"
DIMENSIONS = (
    "constraintFidelity",
    "referenceResolution",
    "evidenceDiscipline",
    "taskProgression",
    "usefulness",
)
SYSTEM_PROMPT = """你是独立、严格、映射盲的电商多轮对话评审者。只评当前提供的一个 A/B 项目，不知道系统身份，也不得推测身份。
完整阅读各自对话和公开证据，按以下五项分别给 1-5 整数分：约束保持、指代解析、证据纪律、任务推进、实用性。
不能因回答更长、更自信而加分。候选内容在实质上相同时必须判 tie；证据不足时安全澄清优于编造。
只调用指定工具一次，不输出额外文字。"""


REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "提交一个匿名 A/B 多轮对话评审。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["candidateA", "candidateB", "overallPreference", "reason"],
            "properties": {
                "candidateA": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(DIMENSIONS),
                    "properties": {
                        name: {"type": "integer", "minimum": 1, "maximum": 5}
                        for name in DIMENSIONS
                    },
                },
                "candidateB": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": list(DIMENSIONS),
                    "properties": {
                        name: {"type": "integer", "minimum": 1, "maximum": 5}
                        for name in DIMENSIONS
                    },
                },
                "overallPreference": {"type": "string", "enum": ["A", "B", "tie"]},
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
            },
        },
    },
}


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.output.exists():
        raise RuntimeError(f"refusing to overwrite {args.output}")
    args.output.mkdir(parents=True, exist_ok=False)
    rows = load_jsonl(args.packet)
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    started = {
        "schemaVersion": "real-user-multiturn-ai-judge-started-v1",
        "judgeRunId": args.judge_run_id,
        "judgeModel": settings.deepseek_model,
        "startedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "packetSha256": sha_file(args.packet),
        "schemaSha256": sha_file(args.schema),
        "automaticRetries": 0,
        "sealedMappingRead": False,
    }
    started_path = args.output / "started.json"
    started_path.write_text(json.dumps(started, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    output_path = args.output / "responses.jsonl"
    call_receipts_path = args.output / "call_receipts.jsonl"
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        max_retries=0,
    )
    responses: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []
    try:
        for row in rows:
            before = time.perf_counter()
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.deepseek_model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": canonical(row)},
                    ],
                    temperature=0,
                    tools=[REVIEW_TOOL],
                    **tool_choice_kwargs(
                        settings.deepseek_model,
                        {"type": "function", "function": {"name": TOOL_NAME}},
                    ),
                ),
                timeout=args.timeout_seconds,
            )
            duration_ms = round((time.perf_counter() - before) * 1000, 3)
            calls = list(response.choices[0].message.tool_calls or [])
            if len(calls) != 1 or calls[0].function.name != TOOL_NAME:
                raise RuntimeError(f"{row['itemId']}: required single tool call missing")
            review = json.loads(calls[0].function.arguments)
            result = {
                "schemaVersion": "real-user-multiturn-ai-judge-response-v4",
                "itemId": row["itemId"],
                "reviewerType": "independent_ai_judge",
                "judgeModel": settings.deepseek_model,
                "judgeRunId": args.judge_run_id,
                "review": review,
            }
            validator.validate(result)
            usage = response.usage
            receipt = {
                "itemId": row["itemId"],
                "durationMs": duration_ms,
                "promptTokens": int(getattr(usage, "prompt_tokens", 0) or 0),
                "completionTokens": int(getattr(usage, "completion_tokens", 0) or 0),
                "totalTokens": int(getattr(usage, "total_tokens", 0) or 0),
                "responseId": str(response.id),
                "responseSha256": hashlib.sha256(canonical(result).encode("utf-8")).hexdigest(),
            }
            responses.append(result)
            receipts.append(receipt)
            with output_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical(result) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            with call_receipts_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical(receipt) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
    finally:
        await client.close()
    if len(responses) != len(rows):
        raise RuntimeError(f"incomplete judge run: {len(responses)}/{len(rows)}")
    result = {
        "schemaVersion": "real-user-multiturn-ai-judge-receipt-v1",
        "status": "COMPLETE",
        "judgeRunId": args.judge_run_id,
        "judgeModel": settings.deepseek_model,
        "itemCount": len(rows),
        "packetSha256": sha_file(args.packet),
        "responsesSha256": sha_file(output_path),
        "callReceiptsSha256": sha_file(call_receipts_path),
        "observedTotalTokens": sum(item["totalTokens"] for item in receipts),
        "automaticRetries": 0,
        "sealedMappingRead": False,
    }
    result_path = args.output / "receipt.json"
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    witnesses = (started_path, output_path, call_receipts_path, result_path)
    (args.output / "SHA256SUMS.txt").write_text(
        "".join(f"{sha_file(path)}  {path.name}\n" for path in witnesses),
        encoding="utf-8",
        newline="\n",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--judge-run-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
