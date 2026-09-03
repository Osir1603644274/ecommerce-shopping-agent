"""DeepSeek V4 JSON adapter repair for Shopping Mission baseline V2.

All public prompts, parsing, receipts, budgets and immutable output logic remain
owned by frozen V1.  V2 only pins a new preregistration and disables thinking
for structured JSON calls so the provider returns final ``content``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

from agent.evaluation import shopping_mission_baseline_runner_v1 as v1


PREREGISTRATION_PATH = Path(__file__).resolve().parents[2] / "docs" / "analysis-briefs" / "shopping-mission-baseline-adapter-repair-2026-08-23.md"


def create_kwargs(
    *, model: str, messages: Sequence[Mapping[str, str]], max_tokens: int,
) -> dict[str, Any]:
    """Return the exact provider request; only thinking differs from V1."""

    return {
        "model": model,
        "messages": list(messages),
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "extra_body": {"thinking": {"type": "disabled"}},
    }


async def run_profile(**kwargs):
    original = v1.PREREGISTRATION_PATH
    v1.PREREGISTRATION_PATH = PREREGISTRATION_PATH
    try:
        return await v1.run_profile(**kwargs)
    finally:
        v1.PREREGISTRATION_PATH = original


ModelCall = v1.ModelCall
PROFILES = v1.PROFILES
