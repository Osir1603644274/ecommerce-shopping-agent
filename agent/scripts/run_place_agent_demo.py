from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any


AGENT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIOS_PATH = AGENT_ROOT / "place_data" / "demo" / "place_agent_demo_v2.json"
DEFAULT_REPORT_PATH = AGENT_ROOT / "place_data" / "demo" / "place_agent_demo_report.json"
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from app.llm import run_agent  # noqa: E402


def load_scenarios(path: Path = DEFAULT_SCENARIOS_PATH) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _turn_passed(turn: dict[str, Any], answer: str, tool_names: list[str]) -> bool:
    expected = turn["expectedToolSequences"]
    forbidden = set(turn.get("forbiddenTools", []))
    required_all = turn.get("requiredAnswerAll", [])
    required_any = turn.get("requiredAnswerAny", [])
    return (
        tool_names in expected
        and not forbidden.intersection(tool_names)
        and all(fragment in answer for fragment in required_all)
        and (not required_any or any(fragment in answer for fragment in required_any))
    )


async def run_scenario(scenario: dict[str, Any]) -> dict[str, Any]:
    history: list[dict] = []
    results = []
    for turn in scenario["turns"]:
        answer, traces, turn_messages = await run_agent(turn["user"], history=history)
        tool_names = [trace.tool for trace in traces]
        passed = _turn_passed(turn, answer, tool_names)
        results.append({
            "user": turn["user"],
            "answer": answer,
            "toolNames": tool_names,
            "toolTraces": [trace.model_dump(by_alias=True) for trace in traces],
            "passed": passed,
        })
        history.extend(turn_messages)
    return {
        "id": scenario["id"],
        "title": scenario["title"],
        "learningPoint": scenario["learningPoint"],
        "passed": all(turn["passed"] for turn in results),
        "turns": results,
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run reproducible Beijing place Agent demos.")
    parser.add_argument("--scenario", default="all", help="Scenario id, or 'all'.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()

    payload = load_scenarios(args.scenarios.resolve())
    scenarios = payload["scenarios"]
    if args.scenario != "all":
        scenarios = [scenario for scenario in scenarios if scenario["id"] == args.scenario]
        if not scenarios:
            available = ", ".join(item["id"] for item in payload["scenarios"])
            parser.error(f"unknown scenario {args.scenario!r}; available: {available}")

    results = [await run_scenario(scenario) for scenario in scenarios]
    report = {
        "demoVersion": payload["demoVersion"],
        "catalogScope": payload["catalogScope"],
        "scenarioCount": len(results),
        "passed": sum(result["passed"] for result in results),
        "scenarios": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Place Agent demos: {report['passed']}/{report['scenarioCount']} scenarios passed")
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  {status} {result['id']}: {result['title']}")
        for index, turn in enumerate(result["turns"], start=1):
            print(f"    turn {index}: tools={turn['toolNames']} passed={turn['passed']}")
    print(f"Wrote {args.output.resolve()}")
    return 0 if report["passed"] == report["scenarioCount"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
