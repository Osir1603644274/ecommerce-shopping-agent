"""Offline gates for authoritative MissionState and deterministic compiler V1."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from agent.evaluation import shopping_mission_authoritative_state_runner_v1 as runner
from agent.evaluation import shopping_mission_authoritative_state_scorer_v1 as scorer
from agent.evaluation import shopping_mission_benchmark_v1 as benchmark


def _state(*, kind="QUERY_PRODUCTS", unknowns=None, observation=False):
    action = {
        "ref": "primary_action", "kind": kind,
        "purchaseDisposition": "REQUIRED" if kind == "QUERY_PRODUCTS" else "NOT_APPLICABLE",
        "requiredEvidence": ["CATALOG_FACT"] if kind == "QUERY_PRODUCTS" else ["EXTERNAL_STABLE"],
        "observationDependent": observation,
    }
    if kind == "QUERY_PRODUCTS":
        action["category"] = "sneakers"
    return {
        "missionFamily": "CONTRACT_RED", "currentGoal": "满足当前购物任务",
        "actions": [action], "dependencies": [],
        "requirements": [{
            "scopeRef": "primary_action", "key": "budget_cny", "operator": "lte",
            "value": 1000, "priority": "hard", "polarity": "positive",
        }] if kind == "QUERY_PRODUCTS" else [],
        "blockingUnknowns": list(unknowns or []),
        "requiredOutputModes": ["PRODUCT_RECOMMENDATIONS"] if kind == "QUERY_PRODUCTS" else ["RESEARCH_SUMMARY"],
    }


def test_schemas_are_meta_valid():
    for path in (runner.STATE_SCHEMA_PATH, runner.SCORE_SCHEMA_PATH):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_request_contract_disables_thinking():
    messages = [{"role": "user", "content": "x"}]
    assert runner.create_kwargs(model="deepseek-v4-flash", messages=messages) == {
        "model": "deepseek-v4-flash", "messages": messages, "temperature": 0,
        "max_tokens": 6000, "response_format": {"type": "json_object"},
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def test_valid_state_compiles_deterministically():
    first = runner.compile_state(_state(), "SMB-V1-MVP-018")
    second = runner.compile_state(copy.deepcopy(_state()), "SMB-V1-MVP-018")
    assert first == second
    assert first["scenarioId"] == "SMB-V1-MVP-018"
    graph = first["predictedGraph"]
    assert graph["routeClass"] == "DIRECT"
    assert graph["constraints"][0]["scope"] == graph["nodes"][0]["nodeKey"]


@pytest.mark.parametrize("mutation,match", [
    (lambda value: value["actions"].append(copy.deepcopy(value["actions"][0])), "duplicate action ref"),
    (lambda value: value["requirements"][0].update(scopeRef="missing_action"), "scope"),
    (lambda value: value["actions"][0].pop("category"), "category contract"),
])
def test_validator_rejects_invalid_state(mutation, match):
    value = _state()
    mutation(value)
    with pytest.raises(runner.AuthoritativeStateError, match=match):
        runner.validate_state(value)


def test_validator_rejects_cycle_and_clarification_mismatch():
    value = _state()
    second = copy.deepcopy(value["actions"][0])
    second["ref"] = "second_action"
    value["actions"].append(second)
    value["dependencies"] = [
        {"beforeRef": "primary_action", "afterRef": "second_action", "reason": "INFORMS"},
        {"beforeRef": "second_action", "afterRef": "primary_action", "reason": "INFORMS"},
    ]
    with pytest.raises(runner.AuthoritativeStateError, match="cycle"):
        runner.validate_state(value)
    value = _state(unknowns=["size"])
    with pytest.raises(runner.AuthoritativeStateError, match="clarification"):
        runner.validate_state(value)


def test_route_derivation_closes_all_profiles():
    clarify = _state(kind="CLARIFY", unknowns=["size"])
    clarify["requiredOutputModes"] = ["CLARIFICATION_QUESTION"]
    assert runner._route(clarify) == "CLARIFY"
    assert runner._route(_state(observation=True)) == "BOUNDED_REACT"
    assert runner._route(_state()) == "DIRECT"
    assert runner._route(_state(kind="RESEARCH")) == "ANSWER_ONLY"
    planned = _state()
    extra = copy.deepcopy(planned["actions"][0])
    extra["ref"] = "second_query"
    planned["actions"].append(extra)
    assert runner._route(planned) == "PLANNED"


def _fixture_call(*, fail_first=False):
    calls = 0

    async def call_json(phase, messages, max_tokens):
        nonlocal calls
        calls += 1
        if fail_first and calls == 1:
            return runner.ModelCall("not-json", 3, 2)
        return runner.ModelCall(json.dumps(_state(), ensure_ascii=False), 3, 2)

    return call_json


def test_runner_is_one_call_per_case_and_output_is_immutable(tmp_path):
    target = tmp_path / "attempt"
    manifest = asyncio.run(runner.run_profile(
        run_id="fixture-run", output_dir=target, model="fixture",
        endpoint="https://api.deepseek.com", call_json=_fixture_call(), concurrency=4,
    ))
    assert manifest["profile"] == runner.PROFILE
    assert manifest["completedCount"] == 18
    receipts = [json.loads(line) for line in (target / "receipts.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {item["modelCalls"] for item in receipts} == {1}
    with pytest.raises(runner.AuthoritativeStateError, match="already exists"):
        asyncio.run(runner.run_profile(
            run_id="fixture-run-2", output_dir=target, model="fixture",
            endpoint="https://api.deepseek.com", call_json=_fixture_call(),
        ))


def test_scorer_works_and_failed_case_stays_in_denominator(tmp_path):
    target = tmp_path / "attempt"
    asyncio.run(runner.run_profile(
        run_id="fixture-run", output_dir=target, model="fixture",
        endpoint="https://api.deepseek.com", call_json=_fixture_call(fail_first=True), concurrency=1,
    ))
    report = scorer.score_run(target)
    assert report["profile"] == runner.PROFILE
    assert report["metrics"]["caseFailureRate"] == pytest.approx(1 / 18)


def test_invalid_digest_is_rejected_before_private_oracle_access(tmp_path, monkeypatch):
    target = tmp_path / "attempt"
    asyncio.run(runner.run_profile(
        run_id="fixture-run", output_dir=target, model="fixture",
        endpoint="https://api.deepseek.com", call_json=_fixture_call(), concurrency=4,
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
