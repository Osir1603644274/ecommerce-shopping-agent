"""Offline contracts for Shopping Mission baseline comparison V1."""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent.evaluation import shopping_mission_baseline_runner_v1 as runner
from agent.evaluation import shopping_mission_baseline_scorer_v1 as scorer
from agent.evaluation import shopping_mission_benchmark_v1 as benchmark


def _model_graph():
    return {
        "missionFamily": "CONTRACT_RED",
        "predictedGraph": {
            "goalKey": "buy_commute_shoes",
            "routeClass": "DIRECT",
            "nodes": [{
                "nodeKey": "query_shoes", "kind": "QUERY_PRODUCTS", "category": "sneakers",
                "purchaseDisposition": "REQUIRED", "requiredEvidence": ["CATALOG_FACT"],
            }],
            "dependencies": [], "constraints": [], "blockingUnknowns": [],
            "clarification": {"required": False, "focus": None, "maxQuestions": 0},
            "requiredOutputModes": ["PRODUCT_RECOMMENDATIONS"],
        },
    }


def _state():
    return {
        "currentGoal": "buy commute shoes", "missionFamily": "CONTRACT_RED",
        "purchaseNeeds": [], "researchNeeds": [], "ownedItems": [],
        "nonPurchaseActions": [], "comparisonNeeds": [], "requirements": [],
        "blockingUnknowns": [], "revokedNeeds": ["camping"],
        "requiredOutputModes": ["PRODUCT_RECOMMENDATIONS"],
    }


def test_schemas_are_meta_valid():
    for path in runner.SCHEMA_DIR.glob("shopping_mission_baseline_*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_public_and_human_review_preconditions_are_closed():
    rows = runner._validate_public_preconditions()
    assert len(rows) == 18
    assert len({row["scenarioId"] for row in rows}) == 18


def test_runner_has_no_scorer_or_private_data_dependency():
    source = inspect.getsource(runner)
    assert "shopping_mission_baseline_scorer" not in source
    assert "mission_oracle.private" not in source
    assert "load_and_validate" not in source


def test_direct_profile_makes_exactly_one_call_and_runner_owns_identity():
    row = runner._validate_public_preconditions()[-2]
    seen = []

    async def call_json(phase, messages, max_tokens):
        seen.append((phase, messages, max_tokens))
        return runner.ModelCall(json.dumps(_model_graph()), 10, 20)

    case, receipt = asyncio.run(runner._run_case(row, "DIRECT_ONE_SHOT", "run-direct", call_json))
    assert case["status"] == "COMPLETED"
    assert case["prediction"]["scenarioId"] == row["scenarioId"]
    assert [item[0] for item in seen] == ["DIRECT_GRAPH"]
    assert receipt["modelCalls"] == 1


def test_stateful_profile_makes_two_calls_and_projects_state():
    row = runner._validate_public_preconditions()[-2]
    seen = []

    async def call_json(phase, messages, max_tokens):
        seen.append((phase, messages, max_tokens))
        value = _state() if phase == "STATE_EXTRACT" else _model_graph()
        return runner.ModelCall(json.dumps(value), 10, 20)

    case, receipt = asyncio.run(runner._run_case(row, "STATEFUL_CONTEXT", "run-state", call_json))
    assert case["status"] == "COMPLETED"
    assert [item[0] for item in seen] == ["STATE_EXTRACT", "STATE_TO_GRAPH"]
    assert "可信 MissionState 投影" in seen[1][1][1]["content"]
    assert receipt["modelCalls"] == 2


def test_invalid_json_fails_without_repair_call():
    row = runner._validate_public_preconditions()[0]
    calls = 0

    async def call_json(phase, messages, max_tokens):
        nonlocal calls
        calls += 1
        return runner.ModelCall("not-json", 1, 1)

    case, receipt = asyncio.run(runner._run_case(row, "DIRECT_ONE_SHOT", "run-fail", call_json))
    assert case["status"] == "FAILED" and case["prediction"] is None
    assert calls == receipt["modelCalls"] == 1


def test_run_directory_is_new_only(tmp_path):
    target = tmp_path / "attempt"

    async def call_json(phase, messages, max_tokens):
        return runner.ModelCall(json.dumps(_model_graph()), 1, 1)

    asyncio.run(runner.run_profile(
        profile="DIRECT_ONE_SHOT", run_id="offline", output_dir=target,
        model="fixture", endpoint="https://api.deepseek.com", call_json=call_json, concurrency=4,
    ))
    assert target.is_dir()
    with pytest.raises(runner.BaselineRunnerError, match="already exists"):
        asyncio.run(runner.run_profile(
            profile="DIRECT_ONE_SHOT", run_id="offline-2", output_dir=target,
            model="fixture", endpoint="https://api.deepseek.com", call_json=call_json,
        ))


def _renamed_prediction(oracle):
    graph = copy.deepcopy(oracle["expectedGraph"])
    mapping = {node["nodeKey"]: f"local_{index}" for index, node in enumerate(graph["nodes"], start=1)}
    graph["goalKey"] = "different_local_goal_name"
    for node in graph["nodes"]:
        node["nodeKey"] = mapping[node["nodeKey"]]
    for edge in graph["dependencies"]:
        edge["before"] = mapping[edge["before"]]
        edge["after"] = mapping[edge["after"]]
    for constraint in graph["constraints"]:
        if constraint["scope"] != "GLOBAL":
            constraint["scope"] = mapping[constraint["scope"]]
    graph["blockingUnknowns"] = [scorer.UNKNOWN_ALIASES.get(value, value) for value in graph["blockingUnknowns"]]
    if graph["clarification"]["focus"] is not None:
        graph["clarification"]["focus"] = scorer.UNKNOWN_ALIASES.get(
            graph["clarification"]["focus"], graph["clarification"]["focus"]
        )
    return {
        "scenarioId": oracle["scenarioId"],
        "schemaVersion": "shopping-mission-prediction-v1-mvp",
        "missionFamily": oracle["missionFamily"], "predictedGraph": graph,
    }


def _write_run(run_dir: Path, failed_id: str | None = None):
    _, oracles, _ = benchmark.load_and_validate()
    cases = []
    receipts = []
    for oracle in oracles:
        scenario_id = oracle["scenarioId"]
        failed = scenario_id == failed_id
        cases.append({
            "scenarioId": scenario_id, "runId": "fixture-run", "profile": "DIRECT_ONE_SHOT",
            "status": "FAILED" if failed else "COMPLETED",
            "prediction": None if failed else _renamed_prediction(oracle),
            "errorCode": "fixture_failure" if failed else None,
            "rawResponseDigest": None if failed else "a" * 64,
        })
        call = {
            "phase": "DIRECT_GRAPH", "ok": not failed, "latencyMs": 5,
            "inputTokens": 10, "outputTokens": 20, "requestDigest": "b" * 64,
            "responseDigest": None if failed else "a" * 64,
            "errorCode": "fixture_failure" if failed else None,
        }
        receipts.append({
            "scenarioId": scenario_id, "runId": "fixture-run", "profile": "DIRECT_ONE_SHOT",
            "status": "FAILED" if failed else "COMPLETED", "modelCalls": 1,
            "inputTokens": 10, "outputTokens": 20, "latencyMs": 5,
            "usageTrust": "API_REPORTED", "calls": [call],
        })
    case_bytes = b"".join(runner._canonical_bytes(item) for item in cases)
    receipt_bytes = b"".join(runner._canonical_bytes(item) for item in receipts)
    manifest = {
        "schemaVersion": "shopping-mission-baseline-run-manifest-v1",
        "datasetId": runner.DATASET_ID, "runId": "fixture-run", "profile": "DIRECT_ONE_SHOT",
        "model": "fixture", "endpointOrigin": "https://api.deepseek.com", "temperature": 0,
        "startedAt": "2026-08-23T00:00:00+00:00", "completedAt": "2026-08-23T00:00:01+00:00",
        "scenarioCount": 18, "completedCount": 17 if failed_id else 18,
        "failedCount": 1 if failed_id else 0,
        "publicSha256": runner._sha256_path(runner.PUBLIC_PATH),
        "humanLanguageEvidenceSha256": runner.EXPECTED_LANGUAGE_EVIDENCE_SHA,
        "humanSemanticEvidenceSha256": runner.EXPECTED_SEMANTIC_EVIDENCE_SHA,
        "runnerSha256": runner._sha256_path(Path(runner.__file__).resolve()),
        "predictionSchemaSha256": runner._sha256_path(runner.PREDICTION_SCHEMA_PATH),
        "preregistrationSha256": runner._sha256_path(runner.PREREGISTRATION_PATH),
        "casesSha256": runner._sha256_bytes(case_bytes),
        "receiptsSha256": runner._sha256_bytes(receipt_bytes),
        "modelRepairCalls": 0, "toolCalls": 0,
    }
    run_dir.mkdir()
    (run_dir / "cases.jsonl").write_bytes(case_bytes)
    (run_dir / "receipts.jsonl").write_bytes(receipt_bytes)
    (run_dir / "manifest.json").write_bytes(runner._canonical_bytes(manifest))


def test_scorer_is_invariant_to_goal_and_node_local_names(tmp_path):
    run_dir = tmp_path / "perfect"
    _write_run(run_dir)
    report = scorer.score_run(run_dir)
    assert set(report["metrics"].values()) == {0.0, 1.0}
    assert report["metrics"]["graphExactRate"] == 1.0


def test_failed_case_stays_in_denominator(tmp_path):
    run_dir = tmp_path / "failed"
    _write_run(run_dir, failed_id="SMB-V1-MVP-001")
    report = scorer.score_run(run_dir)
    assert report["metrics"]["caseFailureRate"] == pytest.approx(1 / 18)
    assert report["metrics"]["graphExactRate"] == pytest.approx(17 / 18)


def test_invalid_run_is_rejected_before_private_oracle_access(tmp_path, monkeypatch):
    run_dir = tmp_path / "tampered"
    _write_run(run_dir)
    with (run_dir / "cases.jsonl").open("ab") as handle:
        handle.write(b"\n")
    touched = False

    def forbidden():
        nonlocal touched
        touched = True
        raise AssertionError("private oracle must not be opened")

    monkeypatch.setattr(benchmark, "load_and_validate", forbidden)
    with pytest.raises(scorer.BaselineScoreError, match="digest"):
        scorer.score_run(run_dir)
    assert touched is False
