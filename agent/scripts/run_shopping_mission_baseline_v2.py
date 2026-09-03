"""Run or score the thinking-disabled Shopping Mission baseline V2."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--profile", required=True, choices=["DIRECT_ONE_SHOT", "STATEFUL_CONTEXT"])
    run.add_argument("--run-id", required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--concurrency", type=int, default=3)
    score = sub.add_parser("score")
    score.add_argument("--run-dir", type=Path, required=True)
    return parser


async def _run(args):
    from openai import AsyncOpenAI

    from agent.app.settings import settings
    from agent.evaluation import shopping_mission_baseline_runner_v2 as runner

    if not settings.deepseek_api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured")
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url,
        timeout=120.0, max_retries=0,
    )

    async def call_json(phase, messages, max_tokens):
        response = await client.chat.completions.create(**runner.create_kwargs(
            model=settings.deepseek_model, messages=messages, max_tokens=max_tokens,
        ))
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("model returned empty content")
        usage = response.usage
        return runner.ModelCall(
            content=content,
            input_tokens=int(usage.prompt_tokens if usage is not None else 0),
            output_tokens=int(usage.completion_tokens if usage is not None else 0),
        )

    try:
        return await runner.run_profile(
            profile=args.profile, run_id=args.run_id, output_dir=args.output_dir.resolve(),
            model=settings.deepseek_model, endpoint=settings.deepseek_base_url,
            call_json=call_json, concurrency=args.concurrency,
        )
    finally:
        await client.close()


def main() -> int:
    args = _parser().parse_args()
    if args.command == "run":
        result = asyncio.run(_run(args))
    else:
        from agent.evaluation.shopping_mission_baseline_scorer_v2 import score_run, write_score_new

        result = score_run(args.run_dir.resolve())
        write_score_new(args.run_dir.resolve(), result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
