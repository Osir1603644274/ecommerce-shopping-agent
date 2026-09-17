from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_place_agent_demo.py"
SPEC = importlib.util.spec_from_file_location("run_place_agent_demo", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_place_demo_contains_six_named_scenarios():
    payload = MODULE.load_scenarios()

    assert payload["demoVersion"] == "place-agent-demo-v2"
    assert len(payload["scenarios"]) == 6
    assert {scenario["id"] for scenario in payload["scenarios"]} == {
        "demo-01-structured-filter",
        "demo-02-search-detail-chain",
        "demo-03-ambiguity-follow-up",
        "demo-04-evidence-boundary",
        "demo-05-place-shop-routing",
        "demo-06-museum-search-detail",
    }


def test_turn_pass_requires_tool_sequence_answer_and_isolation():
    turn = {
        "expectedToolSequences": [["search_places"]],
        "forbiddenTools": ["search_knowledge"],
        "requiredAnswerAll": ["当前演示目录", "7"],
        "requiredAnswerAny": ["找到", "发现"],
    }

    assert MODULE._turn_passed(turn, "当前演示目录中找到7个", ["search_places"])
    assert not MODULE._turn_passed(turn, "当前演示目录中找到7个", ["search_knowledge"])
    assert not MODULE._turn_passed(turn, "海淀区共有7个", ["search_places"])
