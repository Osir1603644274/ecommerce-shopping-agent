"""Only permitted projection from public scenario records to SUT input."""

from __future__ import annotations

import json
import sys
from typing import Any


PROJECTION_MODE = "turn_text_only_v1"


def project_sut_input(scenario: dict[str, Any]) -> list[str]:
    turns = scenario.get("turns")
    if not isinstance(turns, list) or not turns:
        raise ValueError("scenario.turns must be a non-empty list")
    result: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict) or set(turn) != {"text"}:
            raise ValueError("each turn must contain exactly text")
        text = turn["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("turn text must be non-empty")
        result.append(text)
    return result


def main() -> int:
    for line in sys.stdin:
        if line.strip():
            print(json.dumps(project_sut_input(json.loads(line)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
