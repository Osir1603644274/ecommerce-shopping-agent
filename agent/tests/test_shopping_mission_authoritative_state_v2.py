"""Offline gates for authoritative-state prompt-contract repair V2."""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.evaluation import shopping_mission_authoritative_state_runner_v1 as v1
from agent.evaluation import shopping_mission_authoritative_state_runner_v2 as runner
from agent.evaluation import shopping_mission_authoritative_state_scorer_v2 as scorer
from agent.evaluation import shopping_mission_benchmark_v1 as benchmark


def _state():
    return {
        "missionFamily": "CONTRACT_RED", "currentGoal": "find commute shoes",
        "actions": [{
            "ref": "query_shoes", "kind": "QUERY_PRODUCTS", "category": "sneakers",
            "purchaseDisposition": "REQUIRED", "requiredEvidence": ["CATALOG_FACT"],
            "observationDependent": False,
        }],
        "dependencies": [], "requirements": [], "blockingUnknowns": [],
        "requiredOutputModes": ["PRODUCT_RECOMMENDATIONS"],
    }


def _call(*, fail_first=False):
    count = 0

    async def call_json(phase, messages, max_tokens):
        nonlocal count
        count += 1
        content = "not-json" if fail_first and count == 1 else json.dumps(_state())
        return runner.ModelCall(content, 10, 20)

    return call_json


def test_prompt_explicitly_contains_every_frozen_enum():
    for value in runner.ACTION_KINDS.split(", "):
        assert value in runner.STATE_PROMPT
    for value in runner.EVIDENCE_KINDS.split(", "):
        assert value in runner.STATE_PROMPT
    for value in runner.OUTPUT_MODES.split(", "):
        assert value in runner.STATE_PROMPT
    assert "合法形态示例" in runner.STATE_PROMPT


def test_repair_does_not_change_validator_compiler_or_request_contract():
    assert runner.validate_state is v1.validate_state
    assert runner.compile_state is v1.compile_state
    messages = [{"role": "user", "content": "x"}]
    assert runner.create_kwargs(model="deepseek-v4-flash", messages=messages) == v1.create_kwargs(
        model="deepseek-v4-flash", messages=messages,
    )


def test_runner_one_call_immutable_and_scorable(tmp_path):
    target = tmp_path / "attempt002"
    manifest = asyncio.run(runner.run_profile(
        run_id="fixture-v2", output_dir=target, model="fixture",
        endpoint="https://api.deepseek.com", call_json=_call(), concurrency=4,
    ))
    assert manifest["completedCount"] == 18
    assert manifest["promptVersion"] == "ENUMS_AND_SHAPE_EXPLICIT_V2"
    assert scorer.score_run(target)["metrics"]["caseCompletionRate"] == 1.0
    with pytest.raises(runner.AuthoritativeStateError, match="already exists"):
        asyncio.run(runner.run_profile(
            run_id="fixture-v2-again", output_dir=target, model="fixture",
            endpoint="https://api.deepseek.com", call_json=_call(),
        ))


def test_failure_stays_in_denominator(tmp_path):
    target = tmp_path / "attempt002"
    asyncio.run(runner.run_profile(
        run_id="fixture-v2", output_dir=target, model="fixture",
        endpoint="https://api.deepseek.com", call_json=_call(fail_first=True), concurrency=1,
    ))
    report = scorer.score_run(target)
    assert report["metrics"]["caseFailureRate"] == pytest.approx(1 / 18)


def test_digest_fails_before_private_oracle(tmp_path, monkeypatch):
    target = tmp_path / "attempt002"
    asyncio.run(runner.run_profile(
        run_id="fixture-v2", output_dir=target, model="fixture",
        endpoint="https://api.deepseek.com", call_json=_call(), concurrency=4,
    ))
    with (target / "cases.jsonl").open("ab") as handle:
        handle.write(b"\n")
    touched = False

    def forbidden():
        nonlocal touched
        touched = True
        raise AssertionError("private oracle must not be opened")

    monkeypatch.setattr(benchmark, "load_and_validate", forbidden)
    with pytest.raises(scorer.AuthoritativeScoreError, match="digest"):
        scorer.score_run(target)
    assert touched is False
