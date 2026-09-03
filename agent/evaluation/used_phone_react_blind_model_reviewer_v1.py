"""Run a mapping-blind external model review over the public A/B pack."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from app.model_compat import tool_choice_kwargs
from app.settings import settings


TOOL_NAME = "submit_blind_review"
DIMENSIONS = (
    "constraintFidelity", "evidenceDiscipline", "taskProgression", "usefulness"
)
SYSTEM_PROMPT = """你是严格的匿名电商对话评审者。你只会看到同一场景的候选A和候选B，不知道系统身份。
逐轮检查：硬约束是否被静默放宽；未知证据是否冒充已知；旧候选范围是否被错误复用；回答是否在证据边界内推进任务。
不要因为回答更长、商品更多或措辞更自信而加分。证据不足时，安全澄清或明确边界优于编造结论。
对A和B分别给四项1-5整数分，并给总体偏好A/B/tie/unjudgeable。只调用指定工具。"""


REVIEW_TOOL = {
    "type": "function",
    "function": {
        "name": TOOL_NAME,
        "description": "Submit one anonymous A/B dialogue review.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["candidateA", "candidateB", "overallPreference", "notes"],
            "properties": {
                "candidateA": {
                    "type": "object", "additionalProperties": False,
                    "required": list(DIMENSIONS),
                    "properties": {
                        name: {"type": "integer", "minimum": 1, "maximum": 5}
                        for name in DIMENSIONS
                    },
                },
                "candidateB": {
                    "type": "object", "additionalProperties": False,
                    "required": list(DIMENSIONS),
                    "properties": {
                        name: {"type": "integer", "minimum": 1, "maximum": 5}
                        for name in DIMENSIONS
                    },
                },
                "overallPreference": {
                    "type": "string", "enum": ["A", "B", "tie", "unjudgeable"]
                },
                "notes": {"type": "string", "maxLength": 800},
            },
        },
    },
}


def _read_public(path: Path) -> list[dict[str, Any]]:
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows or any(not isinstance(row, dict) or row.get("review") is not None for row in rows):
        raise ValueError("public pack must contain unreviewed object rows")
    if len({row.get("itemId") for row in rows}) != len(rows):
        raise ValueError("public pack item IDs must be unique")
    return rows


def _validate_review(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "candidateA", "candidateB", "overallPreference", "notes"
    }:
        raise ValueError("review payload has invalid fields")
    for side in ("candidateA", "candidateB"):
        scores = value[side]
        if not isinstance(scores, dict) or set(scores) != set(DIMENSIONS):
            raise ValueError(f"{side} has invalid dimensions")
        if any(type(scores[name]) is not int or not 1 <= scores[name] <= 5 for name in DIMENSIONS):
            raise ValueError(f"{side} scores must be integers from 1 to 5")
    if value["overallPreference"] not in {"A", "B", "tie", "unjudgeable"}:
        raise ValueError("overallPreference is invalid")
    if type(value["notes"]) is not str or len(value["notes"]) > 800:
        raise ValueError("notes is invalid")
    return value


async def review_pack(
    *, public_input: Path, completed_output: Path, manifest_output: Path,
    reviewer_id: str, timeout_seconds: float,
    base_completed: Path | None = None,
    item_ids: set[str] | None = None,
) -> dict[str, Any]:
    rows = _read_public(public_input)
    completed_by_id = {row["itemId"]: dict(row) for row in rows}
    if base_completed is not None:
        base_rows = [
            json.loads(line)
            for line in base_completed.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        base_by_id = {
            row.get("itemId"): row for row in base_rows if isinstance(row, dict)
        }
        if len(base_by_id) != len(rows) or set(base_by_id) != set(completed_by_id):
            raise ValueError("base completed review does not match public items")
        for item_id, row in base_by_id.items():
            if {k: v for k, v in row.items() if k != "review"} != {
                k: v for k, v in completed_by_id[item_id].items() if k != "review"
            }:
                raise ValueError(f"{item_id}: base completed content changed")
        completed_by_id = base_by_id
    known_ids = set(completed_by_id)
    if item_ids is not None and not item_ids <= known_ids:
        raise ValueError("requested item-id is not in the public pack")
    selected = [
        row for row in rows
        if (item_ids is None or row["itemId"] in item_ids)
        and completed_by_id[row["itemId"]].get("review") is None
    ]
    client = AsyncOpenAI(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        max_retries=0,
    )
    attempts: list[dict[str, Any]] = []
    for row in selected:
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=settings.deepseek_model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": json.dumps({
                            "candidateA": row.get("candidateA"),
                            "candidateB": row.get("candidateB"),
                            "rubric": row.get("rubric"),
                        }, ensure_ascii=False)},
                    ],
                    tools=[REVIEW_TOOL],
                    **tool_choice_kwargs(settings.deepseek_model, {
                        "type": "function", "function": {"name": TOOL_NAME}
                    }),
                ),
                timeout=timeout_seconds,
            )
            message = response.choices[0].message
            calls = list(message.tool_calls or [])
            if len(calls) != 1 or calls[0].function.name != TOOL_NAME:
                raise ValueError("model did not return the required single review tool")
            review = _validate_review(json.loads(calls[0].function.arguments))
            completed_by_id[row["itemId"]] = {
                **row,
                "review": {"reviewerId": reviewer_id, **review},
            }
            attempts.append({
                "itemId": row.get("itemId"), "status": "SUCCEEDED",
                "durationMs": round((time.perf_counter() - started) * 1000, 2),
            })
        except Exception as exc:
            attempts.append({
                "itemId": row.get("itemId"), "status": "FAILED",
                "durationMs": round((time.perf_counter() - started) * 1000, 2),
                "errorType": type(exc).__name__, "error": str(exc)[:500],
            })
    await client.close()
    completed = [completed_by_id[row["itemId"]] for row in rows]
    completed_output.parent.mkdir(parents=True, exist_ok=True)
    completed_output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in completed),
        encoding="utf-8",
    )
    succeeded = sum(item["status"] == "SUCCEEDED" for item in attempts)
    complete_count = sum(isinstance(row.get("review"), dict) for row in completed)
    manifest = {
        "schemaVersion": "used-phone-blind-model-review-manifest-v1",
        "status": "COMPLETE" if complete_count == len(rows) else "HOLD_EXTERNAL_REVIEW",
        "reviewerType": "external_model_blind_judge",
        "reviewerId": reviewer_id,
        "model": settings.deepseek_model,
        "itemCount": len(rows),
        "attemptedItems": len(selected),
        "succeededItems": succeeded,
        "failedItems": len(selected) - succeeded,
        "completeItems": complete_count,
        "sourcePublicSha256": hashlib.sha256(public_input.read_bytes()).hexdigest(),
        "completedReviewSha256": hashlib.sha256(completed_output.read_bytes()).hexdigest(),
        "sealedMappingRead": False,
        "attempts": attempts,
        "claimBoundary": {
            "isHumanReview": False,
            "provesReviewerIndependence": False,
            "provesGeneralSuperiority": False,
        },
    }
    manifest_output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-input", required=True, type=Path)
    parser.add_argument("--completed-output", required=True, type=Path)
    parser.add_argument("--manifest-output", required=True, type=Path)
    parser.add_argument("--reviewer-id", required=True)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--base-completed", type=Path)
    parser.add_argument("--item-id", action="append")
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        raise ValueError("timeout-seconds must be positive")
    manifest = asyncio.run(review_pack(
        public_input=args.public_input,
        completed_output=args.completed_output,
        manifest_output=args.manifest_output,
        reviewer_id=args.reviewer_id,
        timeout_seconds=args.timeout_seconds,
        base_completed=args.base_completed,
        item_ids=set(args.item_id) if args.item_id else None,
    ))
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
