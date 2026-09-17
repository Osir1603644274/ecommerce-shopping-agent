"""Offline gates for the V2 thinking-disabled adapter."""

from __future__ import annotations

import asyncio
import json

from agent.evaluation import shopping_mission_baseline_runner_v1 as v1
from agent.evaluation import shopping_mission_baseline_runner_v2 as v2


def test_v2_request_changes_only_thinking_adapter():
    messages = [{"role": "user", "content": "x"}]
    kwargs = v2.create_kwargs(model="deepseek-v4-flash", messages=messages, max_tokens=6000)
    assert kwargs == {
        "model": "deepseek-v4-flash", "messages": messages, "temperature": 0,
        "max_tokens": 6000, "response_format": {"type": "json_object"},
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def test_v2_reuses_frozen_v1_prompt_and_profile_contracts():
    assert v2.PROFILES is v1.PROFILES
    assert "goalKey/nodeKey 只是图内本地" in v1.GRAPH_CONTRACT
    assert v1.GRAPH_CONTRACT == v1.GRAPH_CONTRACT


def test_v2_runner_binds_new_preregistration_and_restores_v1(tmp_path):
    original = v1.PREREGISTRATION_PATH

    async def call_json(phase, messages, max_tokens):
        graph = {
            "missionFamily": "CONTRACT_RED",
            "predictedGraph": {
                "goalKey": "goal", "routeClass": "DIRECT",
                "nodes": [{"nodeKey": "query_shoes", "kind": "QUERY_PRODUCTS", "category": "sneakers", "purchaseDisposition": "REQUIRED", "requiredEvidence": ["CATALOG_FACT"]}],
                "dependencies": [], "constraints": [], "blockingUnknowns": [],
                "clarification": {"required": False, "focus": None, "maxQuestions": 0},
                "requiredOutputModes": ["PRODUCT_RECOMMENDATIONS"],
            },
        }
        return v2.ModelCall(json.dumps(graph), 1, 1)

    target = tmp_path / "v2"
    manifest = asyncio.run(v2.run_profile(
        profile="DIRECT_ONE_SHOT", run_id="v2-fixture", output_dir=target,
        model="fixture", endpoint="https://api.deepseek.com", call_json=call_json, concurrency=4,
    ))
    assert manifest["preregistrationSha256"] == v1._sha256_path(v2.PREREGISTRATION_PATH)
    assert manifest["completedCount"] == 18
    assert v1.PREREGISTRATION_PATH == original

    from agent.evaluation.shopping_mission_baseline_scorer_v2 import score_run

    report = score_run(target)
    assert report["metrics"]["caseCompletionRate"] == 1.0
