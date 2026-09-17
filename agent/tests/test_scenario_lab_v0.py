"""Directed tests for the Agentic Scenario Lab V0 contract / evaluator.

Covers CONTRACT-002 repair surfaces (fault/oracle single truth, injection↔
expectation closure, durable metrics derived from runner-owned observations,
hard constraints bound to private product truth, claim→evidence/provenance
binding, per-track metric aggregates, LNG/INF prefix↔track identity) and the
CONTRACT-003 strict single-occurrence contract: exactly one initial attempt,
exactly one observation per required injection, globally-unique requestId,
server-owned durable task/thread/run identity across all attempts, no
unplanned attemptKind, no good attempt masking a bad one, per-ranked-product
hard-constraint verification, SUCCESS product scenarios returning a non-empty
ranking, and an always-emitted unsupported-claim gate summary so every enabled
metric family forms eligibility on both the success and the failure side.
"""

import base64
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from agent.evaluation import scenario_lab_v0 as lab


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

def _input(sid="ASL-V0-LNG-001", track="language_e2e", catalog="catalog-v0-001", env="env-v0-001", **over):
    record = {
        "scenarioId": sid,
        "trackType": track,
        "schemaVersion": lab.INPUT_SCHEMA_VERSION,
        "catalogRevision": catalog,
        "environmentRevision": env,
        "turns": [{"role": "user", "text": "Help me find a suitable laptop.", "turnId": "t1"}],
    }
    record.update(over)
    return record


def _oracle(sid="ASL-V0-LNG-001", track="language_e2e", catalog="catalog-v0-001", env="env-v0-001", **over):
    record = {
        "scenarioId": sid,
        "trackType": track,
        "schemaVersion": lab.ORACLE_SCHEMA_VERSION,
        "catalogRevision": catalog,
        "environmentRevision": env,
        "successConditions": {"expectedTerminalClasses": ["SUCCESS"]},
    }
    record.update(over)
    return record


def _fault(sid="ASL-V0-LNG-001", track="language_e2e", catalog="catalog-v0-001", env="env-v0-001", **over):
    record = {
        "scenarioId": sid,
        "trackType": track,
        "schemaVersion": lab.FAULT_SCHEMA_VERSION,
        "catalogRevision": catalog,
        "environmentRevision": env,
        "faultPlan": {"injectionPoints": []},
    }
    record.update(over)
    return record


def _prediction(sid="ASL-V0-LNG-001", track="language_e2e", catalog="catalog-v0-001", env="env-v0-001", pid="pred-001", **over):
    record = {
        "predictionId": pid,
        "taskId": "task-1",
        "threadId": "thread-1",
        "runId": "run-1",
        "scenarioId": sid,
        "trackType": track,
        "schemaVersion": lab.PREDICTION_SCHEMA_VERSION,
        "catalogRevision": catalog,
        "environmentRevision": env,
        "terminalClass": "SUCCESS",
        "toolCalls": [{"toolName": "search_products", "ok": True, "turnIndex": 0}],
        "claims": [],
        "claimExtraction": {"provenance": "runner-claims-v1", "version": "1", "claimsComplete": True},
        "observations": {
            "attempts": [
                {
                    "attemptKind": "initial",
                    "requestId": "req-1",
                    "runId": "run-1",
                    "taskId": "task-1",
                    "threadId": "thread-1",
                    "payloadSha256": "b" * 64,
                    "resultSha256": "c" * 64,
                    "toolCalls": 1,
                    "modelCalls": 1,
                    "revisionBefore": 0,
                    "revisionAfter": 1,
                    "historyCountBefore": 0,
                    "historyCountAfter": 1,
                }
            ]
        },
        "latencyMs": 250.0,
    }
    record.update(over)
    return record


def _infra_input(sid="ASL-V0-INF-001", **over):
    record = {"setupRef": "fixture.cp-7", "turns": []}
    return _input(sid=sid, track="infrastructure_fault", **{**record, **over})


def _infra_oracle(sid="ASL-V0-INF-001", **over):
    record = {
        "successConditions": {
            "expectedTerminalClasses": ["SUCCESS"],
            "faultExpectations": {
                "checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True},
                "exact_replay": {"required": True, "expectedExactReplayZeroTool": True},
                "mutated_replay": {"required": True, "expectedMutatedReplayRejected": True},
                "revision_conflict": {"required": True, "expectedRevisionDivergenceFailClosed": True},
            },
        }
    }
    return _oracle(sid=sid, track="infrastructure_fault", **{**record, **over})


def _infra_fault(sid="ASL-V0-INF-001", **over):
    record = {
        "faultPlan": {
            "injectionPoints": ["checkpoint_restart", "exact_replay", "mutated_replay", "revision_conflict"],
            "setupFixture": {
                "checkpointFixtureRef": "fixture://cp-7",
                "expectedCheckpointContentsSha256": "a" * 64,
            },
            "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
        }
    }
    return _fault(sid=sid, track="infrastructure_fault", **{**record, **over})


def _infra_prediction(sid="ASL-V0-INF-001", pid="pred-inf-001", **over):
    record = {
        "predictionId": pid,
        "taskId": "task-inf-1",
        "threadId": "thread-inf-1",
        "runId": "run-inf-1",
        "scenarioId": sid,
        "trackType": "infrastructure_fault",
        "schemaVersion": lab.PREDICTION_SCHEMA_VERSION,
        "catalogRevision": "catalog-v0-001",
        "environmentRevision": "env-v0-001",
        "terminalClass": "SUCCESS",
        "toolCalls": [],
        "claims": [],
        "claimExtraction": {"provenance": "runner-claims-v1", "version": "1", "claimsComplete": True},
        "observations": {
            "attempts": [
                {
                    "attemptKind": "initial",
                    "requestId": "req-init",
                    "runId": "run-inf-1",
                    "taskId": "task-inf-1",
                    "threadId": "thread-inf-1",
                    "payloadSha256": "b" * 64,
                    "resultSha256": "c" * 64,
                    "toolCalls": 0,
                    "modelCalls": 0,
                    "revisionBefore": 0,
                    "revisionAfter": 0,
                    "historyCountBefore": 0,
                    "historyCountAfter": 0,
                },
                {
                    "attemptKind": "restart_recovery",
                    "requestId": "req-restart",
                    "runId": "run-inf-1",
                    "taskId": "task-inf-1",
                    "threadId": "thread-inf-1",
                    "payloadSha256": "b" * 64,
                    "resultSha256": "c" * 64,
                    "toolCalls": 0,
                    "modelCalls": 0,
                    "revisionBefore": 0,
                    "revisionAfter": 0,
                    "historyCountBefore": 0,
                    "historyCountAfter": 0,
                    "checkpointRef": "fixture://cp-7",
                    "checkpointSha256": "a" * 64,
                    "terminalCode": "SUCCESS",
                },
                {
                    "attemptKind": "exact_replay",
                    "requestId": "req-replay",
                    "runId": "run-inf-1",
                    "taskId": "task-inf-1",
                    "threadId": "thread-inf-1",
                    "payloadSha256": "b" * 64,
                    "resultSha256": "c" * 64,
                    "toolCalls": 0,
                    "modelCalls": 0,
                    "revisionBefore": 0,
                    "revisionAfter": 0,
                    "historyCountBefore": 0,
                    "historyCountAfter": 0,
                },
                {
                    "attemptKind": "mutated_replay",
                    "requestId": "req-mut",
                    "runId": "run-inf-1",
                    "taskId": "task-inf-1",
                    "threadId": "thread-inf-1",
                    "payloadSha256": "d" * 64,
                    "resultSha256": "e" * 64,
                    "toolCalls": 0,
                    "modelCalls": 0,
                    "rejected": True,
                    "rejectionCode": "controlled_rejection",
                    "revisionBefore": 0,
                    "revisionAfter": 0,
                    "historyCountBefore": 0,
                    "historyCountAfter": 0,
                },
                {
                    "attemptKind": "revision_conflict",
                    "requestId": "req-conf",
                    "runId": "run-inf-1",
                    "taskId": "task-inf-1",
                    "threadId": "thread-inf-1",
                    "payloadSha256": "f" * 64,
                    "resultSha256": "a" * 64,
                    "toolCalls": 0,
                    "modelCalls": 0,
                    "rejected": True,
                    "rejectionCode": "revision_divergence",
                    "revisionBefore": 0,
                    "revisionAfter": 0,
                    "historyCountBefore": 0,
                    "historyCountAfter": 0,
                },
            ]
        },
        "latencyMs": 300.0,
    }
    record.update(over)
    return record


def _scenario(input_record, oracle_record, fault_record, prediction_record):
    return {
        "input": input_record,
        "oracle": oracle_record,
        "fault": fault_record,
        "prediction": prediction_record,
    }


def _write_jsonl(path, records):
    path = Path(path)
    with open(path, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def _write_set(tmp_path, scenarios):
    input_path = tmp_path / "scenario.input.jsonl"
    oracle_path = tmp_path / "oracle.private.jsonl"
    fault_path = tmp_path / "fault.private.jsonl"
    prediction_path = tmp_path / "prediction.jsonl"
    _write_jsonl(input_path, [s["input"] for s in scenarios])
    _write_jsonl(oracle_path, [s["oracle"] for s in scenarios])
    _write_jsonl(fault_path, [s["fault"] for s in scenarios])
    _write_jsonl(prediction_path, [s["prediction"] for s in scenarios])
    return input_path, oracle_path, fault_path, prediction_path


def _load(paths):
    return lab.ScenarioSet.load(*paths)


def _score(tmp_path, scenarios):
    scenario_set = lab.ScenarioSet.load(*_write_set(tmp_path, scenarios))
    return lab.score_scenario_set(scenario_set)


def _check_by_name(result, name):
    for check in result["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(
        f"check {name!r} not found in {[c['name'] for c in result['checks']]}"
    )


def _empty_track(track_type):
    return {
        "trackType": track_type,
        "scenarioCount": 0,
        "passed": 0,
        "failed": 0,
        "latencyMs": {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None},
        "metrics": {family: {"eligible": 0, "pass": 0, "fail": 0} for family in lab.METRIC_FAMILIES},
        "counts": {
            "toolCallCount": {"count": 0, "total": 0, "mean": None},
            "duplicateToolCallCount": {"count": 0, "total": 0, "mean": None},
        },
    }


def _minimal_report(**over):
    report = {
        "reportId": "report-test",
        "schemaVersion": lab.REPORT_SCHEMA_VERSION,
        "exploratory": True,
        "statisticalSignificanceClaimed": False,
        "sourceInputs": {
            "inputFileSha256": "b" * 64,
            "oracleFileSha256": "b" * 64,
            "faultFileSha256": "b" * 64,
            "predictionFileSha256": "b" * 64,
        },
        "tracks": {
            "language_e2e": _empty_track("language_e2e"),
            "infrastructure_fault": _empty_track("infrastructure_fault"),
        },
        "perScenario": [],
    }
    report.update(over)
    return report


# ---------------------------------------------------------------------------
# Schema validity (draft 2020-12) and field contract
# ---------------------------------------------------------------------------

def test_all_schemas_pass_draft202012_metaschema():
    meta = Draft202012Validator.META_SCHEMA
    for name in lab.SCHEMA_FILENAMES:
        with open(lab.SCHEMAS_DIR / lab.SCHEMA_FILENAMES[name], encoding="utf-8") as fh:
            schema = json.load(fh)
        errors = list(Draft202012Validator(meta).iter_errors(schema))
        assert not errors, (name, [e.message for e in errors])


def test_minimal_language_set_loads(tmp_path):
    scenario_set = _load(_write_set(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())]))
    assert [s.scenario_id for s in scenario_set.scenarios] == ["ASL-V0-LNG-001"]
    assert scenario_set.scenarios[0].track_type == "language_e2e"


def test_input_schema_rejects_extra_field():
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(_input(extra="x"), "input")


def test_oracle_schema_rejects_extra_field():
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(_oracle(extra="x"), "oracle")


def test_fault_schema_rejects_extra_field():
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(_fault(extra="x"), "fault")


def test_prediction_schema_rejects_extra_field():
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(_prediction(extra="x"), "prediction")


def test_prediction_schema_requires_observations():
    prediction = _prediction()
    del prediction["observations"]
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(prediction, "prediction")


def test_prediction_observations_requires_at_least_one_attempt():
    prediction = _prediction()
    prediction["observations"]["attempts"] = []
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(prediction, "prediction")


def test_prediction_schema_requires_claims_and_extraction():
    prediction = _prediction()
    del prediction["claims"]
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(prediction, "prediction")
    prediction2 = _prediction()
    del prediction2["claimExtraction"]
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(prediction2, "prediction")


def test_old_self_reported_bool_fields_schema_rejected():
    """The durable-fault booleans are not even representable in the new
    prediction contract — the runner must produce observations instead."""
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(
            _prediction(
                sid="ASL-V0-INF-001",
                track="infrastructure_fault",
                pid="pred-x",
                restart={"recoveredFromRestart": True, "restartCount": 1},
                replay={"exactReplayZeroTool": True, "mutatedReplayRejected": True},
                revisionDivergenceFailedClosed=True,
            ),
            "prediction",
        )


def test_oracle_schema_version_drift_rejected():
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(_oracle(schemaVersion=lab.INPUT_SCHEMA_VERSION), "oracle")


def test_prediction_schema_version_drift_rejected():
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(_prediction(schemaVersion=lab.INPUT_SCHEMA_VERSION), "prediction")


def test_report_schema_rejects_extra_field():
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_report(_minimal_report(extra="x"))


# ---------------------------------------------------------------------------
# Input answer-face leak audit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field",
    [
        "expectedAnswer",
        "acceptableProductIds",
        "goldRankedIds",
        "oracle",
        "threshold",
        "terminalClass",
        "correctIds",
        "answerIds",
        "expectedToolSequence",
        "taskStateDelta",
        "preinitializedState",
        "label",
    ],
)
def test_input_leak_detects_direct_field(field):
    record = _input()
    record["leaky"] = {field: "x"}
    with pytest.raises(lab.ScenarioContractError, match="leaks answer face"):
        lab.audit_input_leaks(record)


def test_input_leak_detects_nested_metadata():
    record = _input()
    record["metadata"] = {"expectedRankedIds": ["1"]}
    with pytest.raises(lab.ScenarioContractError, match="leaks answer face"):
        lab.audit_input_leaks(record)


def test_input_leak_detects_case_variant():
    record = _input()
    record["ExpectedAnswerId"] = "1"
    with pytest.raises(lab.ScenarioContractError, match="leaks answer face"):
        lab.audit_input_leaks(record)


def test_input_leak_detects_renamed_alias():
    record = _input()
    record["gold_ids"] = ["1001"]
    with pytest.raises(lab.ScenarioContractError, match="leaks answer face"):
        lab.audit_input_leaks(record)


def test_input_leak_detects_inline_text():
    record = _input()
    record["turns"][0]["text"] = "expectedAnswer is hidden in the turn"
    with pytest.raises(lab.ScenarioContractError, match="leaks answer face"):
        lab.audit_input_leaks(record)


def test_input_leak_detects_base64():
    record = _input()
    record["payload"] = base64.b64encode(b"goldRankedIds=['1']").decode("ascii")
    with pytest.raises(lab.ScenarioContractError, match="leaks answer face"):
        lab.audit_input_leaks(record)


def test_input_leak_detects_base64url():
    record = _input()
    record["payload"] = base64.urlsafe_b64encode(b"gold product ids").decode("ascii")
    with pytest.raises(lab.ScenarioContractError, match="leaks answer face"):
        lab.audit_input_leaks(record)


def test_input_leak_rejected_at_load(tmp_path):
    record = _input()
    record["turns"][0]["text"] = "answerIds leak"
    with pytest.raises(lab.ScenarioContractError, match="leaks answer face"):
        _load(_write_set(tmp_path, [_scenario(record, _oracle(), _fault(), _prediction())]))


# ---------------------------------------------------------------------------
# Runner-facing API: public views only
# ---------------------------------------------------------------------------

def test_runner_input_views_expose_only_public_fields(tmp_path):
    input_path = _write_jsonl(
        tmp_path / "input.jsonl",
        [_input(), _infra_input(sid="ASL-V0-INF-001")],
    )
    views = lab.load_runner_input(input_path)
    assert [v.scenario_id for v in views] == ["ASL-V0-LNG-001", "ASL-V0-INF-001"]
    language_view = views[0]
    assert language_view.track_type == "language_e2e"
    assert language_view.setup_ref is None
    assert language_view.turns[0]["text"]
    assert not hasattr(language_view, "oracle")
    assert not hasattr(language_view, "fault")
    assert not hasattr(language_view, "successConditions")


def test_runner_input_rejects_private_oracle_file(tmp_path):
    oracle_path = _write_jsonl(tmp_path / "oracle.jsonl", [_oracle()])
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.load_runner_input(oracle_path)


def test_runner_input_rejects_private_fault_file(tmp_path):
    fault_path = _write_jsonl(tmp_path / "fault.jsonl", [_fault()])
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.load_runner_input(fault_path)


# ---------------------------------------------------------------------------
# Identity alignment
# ---------------------------------------------------------------------------

def test_empty_input_rejected(tmp_path):
    paths = _write_set(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    _write_jsonl(paths[0], [])
    with pytest.raises(lab.ScenarioContractError, match="input file is empty"):
        _load(paths)


def test_duplicate_input_id_rejected(tmp_path):
    input_path = _write_jsonl(tmp_path / "dup.input.jsonl", [_input(), _input()])
    oracle_path = _write_jsonl(tmp_path / "oracle.jsonl", [_oracle()])
    fault_path = _write_jsonl(tmp_path / "fault.jsonl", [_fault()])
    pred_path = _write_jsonl(tmp_path / "prediction.jsonl", [_prediction()])
    with pytest.raises(lab.ScenarioContractError, match="duplicate scenarioId"):
        lab.ScenarioSet.load(input_path, oracle_path, fault_path, pred_path)


def test_duplicate_oracle_id_rejected(tmp_path):
    input_path = _write_jsonl(tmp_path / "input.jsonl", [_input()])
    oracle_path = _write_jsonl(tmp_path / "oracle.jsonl", [_oracle(), _oracle()])
    fault_path = _write_jsonl(tmp_path / "fault.jsonl", [_fault()])
    pred_path = _write_jsonl(tmp_path / "prediction.jsonl", [_prediction()])
    with pytest.raises(lab.ScenarioContractError, match="duplicate scenarioId"):
        lab.ScenarioSet.load(input_path, oracle_path, fault_path, pred_path)


def test_duplicate_fault_id_rejected(tmp_path):
    input_path = _write_jsonl(tmp_path / "input.jsonl", [_input()])
    oracle_path = _write_jsonl(tmp_path / "oracle.jsonl", [_oracle()])
    fault_path = _write_jsonl(tmp_path / "fault.jsonl", [_fault(), _fault()])
    pred_path = _write_jsonl(tmp_path / "prediction.jsonl", [_prediction()])
    with pytest.raises(lab.ScenarioContractError, match="duplicate scenarioId"):
        lab.ScenarioSet.load(input_path, oracle_path, fault_path, pred_path)


def test_duplicate_prediction_id_rejected(tmp_path):
    input_path = _write_jsonl(tmp_path / "input.jsonl", [_input()])
    oracle_path = _write_jsonl(tmp_path / "oracle.jsonl", [_oracle()])
    fault_path = _write_jsonl(tmp_path / "fault.jsonl", [_fault()])
    pred_path = _write_jsonl(
        tmp_path / "prediction.jsonl",
        [_prediction(pid="pred-001"), _prediction(sid="ASL-V0-LNG-002", pid="pred-001")],
    )
    with pytest.raises(lab.ScenarioContractError, match="duplicate predictionId"):
        lab.ScenarioSet.load(input_path, oracle_path, fault_path, pred_path)


def test_missing_oracle_rejected(tmp_path):
    paths = _write_set(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    _write_jsonl(paths[1], [])
    with pytest.raises(lab.ScenarioContractError, match="oracle_missing"):
        _load(paths)


def test_extra_oracle_rejected(tmp_path):
    input_path = _write_jsonl(tmp_path / "input.jsonl", [_input()])
    oracle_path = _write_jsonl(tmp_path / "oracle.jsonl", [_oracle(), _oracle(sid="ASL-V0-LNG-002")])
    fault_path = _write_jsonl(tmp_path / "fault.jsonl", [_fault()])
    pred_path = _write_jsonl(tmp_path / "prediction.jsonl", [_prediction()])
    with pytest.raises(lab.ScenarioContractError, match="oracle_extra"):
        lab.ScenarioSet.load(input_path, oracle_path, fault_path, pred_path)


def test_missing_prediction_rejected(tmp_path):
    paths = _write_set(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    _write_jsonl(paths[3], [])
    with pytest.raises(lab.ScenarioContractError, match="prediction_missing"):
        _load(paths)


def test_extra_prediction_rejected(tmp_path):
    input_path = _write_jsonl(tmp_path / "input.jsonl", [_input()])
    oracle_path = _write_jsonl(tmp_path / "oracle.jsonl", [_oracle()])
    fault_path = _write_jsonl(tmp_path / "fault.jsonl", [_fault()])
    pred_path = _write_jsonl(
        tmp_path / "prediction.jsonl",
        [_prediction(), _prediction(sid="ASL-V0-LNG-002", pid="pred-002")],
    )
    with pytest.raises(lab.ScenarioContractError, match="prediction_extra"):
        lab.ScenarioSet.load(input_path, oracle_path, fault_path, pred_path)


def test_catalog_revision_mismatch_rejected(tmp_path):
    scenario = _scenario(_input(), _oracle(catalog="catalog-v0-999"), _fault(), _prediction())
    with pytest.raises(lab.ScenarioContractError, match="catalogRevision mismatch"):
        _load(_write_set(tmp_path, [scenario]))


def test_environment_revision_mismatch_rejected(tmp_path):
    scenario = _scenario(_input(), _oracle(env="env-v0-999"), _fault(), _prediction())
    with pytest.raises(lab.ScenarioContractError, match="environmentRevision mismatch"):
        _load(_write_set(tmp_path, [scenario]))


def test_track_mismatch_rejected(tmp_path):
    prediction = _prediction(sid="ASL-V0-LNG-001", track="infrastructure_fault", pid="pred-x")
    with pytest.raises(lab.ScenarioContractError, match="track mismatch"):
        _load(_write_set(tmp_path, [_scenario(_input(), _oracle(), _fault(), prediction)]))


# ---------------------------------------------------------------------------
# LNG/INF prefix ↔ track identity, per file kind
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["input", "oracle", "fault", "prediction"])
def test_scenario_id_prefix_track_mismatch_rejected_per_file(kind, tmp_path):
    builders = {
        "input": lambda sid, track: _input(sid=sid, track=track),
        "oracle": lambda sid, track: _oracle(sid=sid, track=track),
        "fault": lambda sid, track: _fault(sid=sid, track=track),
        "prediction": lambda sid, track: _prediction(sid=sid, track=track),
    }
    for sid, track in (
        ("ASL-V0-LNG-001", "infrastructure_fault"),
        ("ASL-V0-INF-001", "language_e2e"),
    ):
        path = _write_jsonl(tmp_path / f"{kind}-{track}.jsonl", [builders[kind](sid, track)])
        with pytest.raises(lab.ScenarioContractError, match="track mismatch.*scenarioId-prefix"):
            lab.load_records(path, kind)


# ---------------------------------------------------------------------------
# Track contract rules (load-time)
# ---------------------------------------------------------------------------

def test_language_input_rejects_setup_ref(tmp_path):
    record = _input(setupRef="fixture.x")
    with pytest.raises(lab.ScenarioContractError, match="setupRef"):
        lab.load_records(_write_jsonl(tmp_path / "in.jsonl", [record]), "input")


def test_language_input_requires_turns(tmp_path):
    record = _input(turns=[])
    with pytest.raises(lab.ScenarioContractError, match="one user turn"):
        lab.load_records(_write_jsonl(tmp_path / "in.jsonl", [record]), "input")


def test_infra_fault_requires_injection_points(tmp_path):
    fault = _fault(sid="ASL-V0-INF-001", track="infrastructure_fault")
    with pytest.raises(lab.ScenarioContractError, match="at least one injection"):
        _load(_write_set(tmp_path, [_scenario(_infra_input(), _infra_oracle(), fault, _infra_prediction())]))


def test_infra_fault_requires_expected_fault_outcome(tmp_path):
    fault = _infra_fault(faultPlan={"injectionPoints": ["checkpoint_restart"]})
    with pytest.raises(lab.ScenarioContractError, match="expectedFaultOutcome"):
        _load(_write_set(tmp_path, [_scenario(_infra_input(), _infra_oracle(), fault, _infra_prediction())]))


def test_language_fault_with_injections_rejected(tmp_path):
    fault = _fault(faultPlan={"injectionPoints": ["checkpoint_restart"]})
    with pytest.raises(lab.ScenarioContractError, match="empty injectionPoints"):
        _load(_write_set(tmp_path, [_scenario(_input(), _oracle(), fault, _prediction())]))


def test_language_oracle_with_required_fault_expectations_rejected(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "faultExpectations": {"exact_replay": {"required": True, "expectedExactReplayZeroTool": True}},
    })
    with pytest.raises(lab.ScenarioContractError, match="required fault expectations"):
        _load(_write_set(tmp_path, [_scenario(_input(), oracle, _fault(), _prediction())]))


def test_infra_oracle_missing_fault_expectations_rejected(tmp_path):
    oracle = _oracle(sid="ASL-V0-INF-001", track="infrastructure_fault")
    with pytest.raises(lab.ScenarioContractError, match="missing.*faultExpectations"):
        _load(_write_set(tmp_path, [_scenario(_infra_input(), oracle, _infra_fault(), _infra_prediction())]))


# ---------------------------------------------------------------------------
# Fault/oracle single truth + injection↔expectation closure
# ---------------------------------------------------------------------------

def test_frozen_contradictory_fault_terminal_load_fail(tmp_path):
    oracle = _infra_oracle()  # oracle expects SUCCESS
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["checkpoint_restart", "exact_replay", "mutated_replay", "revision_conflict"],
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-7",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": ["FAILURE"]},  # contradicts oracle
    })
    with pytest.raises(lab.ScenarioContractError, match="contradicts oracle"):
        _load(_write_set(tmp_path, [_scenario(_infra_input(), oracle, fault, _infra_prediction())]))


@pytest.mark.parametrize(
    "injections,expectations,ok",
    [
        # exact closure → ok
        (
            ["checkpoint_restart"],
            {"checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True}},
            True,
        ),
        # extra expectation not injected → reject
        (
            ["checkpoint_restart"],
            {
                "checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True},
                "exact_replay": {"required": True, "expectedExactReplayZeroTool": True},
            },
            False,
        ),
        # missing expectation for an injected fault → reject
        (
            ["checkpoint_restart", "exact_replay"],
            {"checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True}},
            False,
        ),
        # non-required expectation → reject (declared-but-ungraded is not allowed)
        (
            ["checkpoint_restart"],
            {"checkpoint_restart": {"required": False, "expectedRecoveredAfterRestart": True}},
            False,
        ),
        # unsupported expectation key → reject
        (
            ["checkpoint_restart"],
            {
                "checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True},
                "other_fault": {"required": True},
            },
            False,
        ),
        # duplicate injection point → reject
        (
            ["checkpoint_restart", "checkpoint_restart"],
            {"checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True}},
            False,
        ),
    ],
)
def test_fault_injection_expectation_closure(tmp_path, injections, expectations, ok):
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "faultExpectations": expectations,
    })
    fault = _infra_fault(faultPlan={
        "injectionPoints": injections,
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-7",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    paths = _write_set(tmp_path, [_scenario(_infra_input(), oracle, fault, _infra_prediction())])
    if ok:
        _load(paths)
    else:
        with pytest.raises(lab.ScenarioContractError):
            _load(paths)


def test_frozen_unexecuted_faults_self_reported_load_fail(tmp_path):
    """Oracle requires all four fault expectations but the fault only injects
    checkpoint_restart → the other three properties are declared but can never
    be executed/graded, so the set must fail closed at load."""
    oracle = _infra_oracle()  # requires all four
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["checkpoint_restart"],
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-7",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    with pytest.raises(lab.ScenarioContractError, match="closure mismatch"):
        _load(_write_set(tmp_path, [_scenario(_infra_input(), oracle, fault, _infra_prediction())]))


def test_frozen_unexecuted_faults_score_fail(tmp_path):
    """Fault and oracle both require all four, but observations only show an
    initial attempt → none of the durable conclusions can be derived, so the
    scenario must score passed=false (not PASS=True)."""
    prediction = _prediction(
        sid="ASL-V0-INF-001",
        track="infrastructure_fault",
        pid="pred-inf-001",
        toolCalls=[],
        observations={
            "attempts": [
                {
                    "attemptKind": "initial",
                    "requestId": "req-init",
                    "runId": "run-1",
                    "taskId": "task-1",
                    "threadId": "thread-1",
                    "payloadSha256": "b" * 64,
                    "resultSha256": "c" * 64,
                    "toolCalls": 0,
                    "modelCalls": 0,
                    "revisionBefore": 0,
                    "revisionAfter": 0,
                    "historyCountBefore": 0,
                    "historyCountAfter": 0,
                }
            ]
        },
    )
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "restart_recovery")["passed"] is False
    assert _check_by_name(result, "exact_replay_zero_tool")["passed"] is False
    assert _check_by_name(result, "mutated_replay_rejection")["passed"] is False
    assert _check_by_name(result, "revision_divergence_fail_closed")["passed"] is False


# ---------------------------------------------------------------------------
# Durable metrics derived from runner-owned observations
# ---------------------------------------------------------------------------

def test_infra_scenario_passes(tmp_path):
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), _infra_prediction())],
    )["perScenario"][0]
    assert result["passed"] is True
    assert result["reason"] == "ok"
    names = {c["name"] for c in result["checks"]}
    assert "terminal_class" in names
    assert "restart_recovery" in names
    assert "exact_replay_zero_tool" in names
    assert "mutated_replay_rejection" in names
    assert "revision_divergence_fail_closed" in names
    assert "evidence_sufficiency" not in names
    assert "clarification_required" not in names


def test_infra_restart_recovery_negative(tmp_path):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "restart_recovery":
            attempt.pop("checkpointRef", None)
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "restart_recovery")["passed"] is False


def test_infra_restart_recovery_terminal_outside_expected_fails(tmp_path):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "restart_recovery":
            attempt["terminalCode"] = "FAILURE"
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "restart_recovery")["passed"] is False


def test_infra_restart_tool_bound_fails(tmp_path):
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "faultExpectations": {
            "checkpoint_restart": {
                "required": True,
                "expectedRecoveredAfterRestart": True,
                "maxToolCallsAfterRestart": 0,
            },
            "exact_replay": {"required": True, "expectedExactReplayZeroTool": True},
            "mutated_replay": {"required": True, "expectedMutatedReplayRejected": True},
            "revision_conflict": {"required": True, "expectedRevisionDivergenceFailClosed": True},
        },
    })
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "restart_recovery":
            attempt["toolCalls"] = 2
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), oracle, _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "restart_tool_bound")["passed"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(payloadSha256="d" * 64),
        lambda a: a.update(resultSha256="d" * 64),
        lambda a: a.update(toolCalls=1),
        lambda a: a.update(modelCalls=1),
        lambda a: a.update(revisionAfter=2),
        lambda a: a.update(historyCountAfter=3),
    ],
)
def test_infra_exact_replay_each_field_tamper_fails(tmp_path, mutate):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "exact_replay":
            mutate(attempt)
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "exact_replay_zero_tool")["passed"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(payloadSha256="b" * 64),  # same payload as initial → not mutated
        lambda a: a.update(rejected=False),
        lambda a: a.pop("rejectionCode", None),  # no controlled rejection code
        lambda a: a.update(toolCalls=1),
        lambda a: a.update(modelCalls=1),
        lambda a: a.update(revisionAfter=2),
    ],
)
def test_infra_mutated_replay_each_field_tamper_fails(tmp_path, mutate):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "mutated_replay":
            mutate(attempt)
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "mutated_replay_rejection")["passed"] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(rejected=False),
        lambda a: a.update(toolCalls=1),
        lambda a: a.update(modelCalls=1),
        lambda a: a.pop("rejectionCode", None),
        lambda a: a.update(revisionAfter=2),
    ],
)
def test_infra_revision_conflict_each_field_tamper_fails(tmp_path, mutate):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "revision_conflict":
            mutate(attempt)
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "revision_divergence_fail_closed")["passed"] is False


def test_infra_checkpoint_loss_fail_closed_positive(tmp_path):
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": ["FAILURE"],
        "faultExpectations": {
            "redis_checkpoint_loss": {"required": True, "expectedCheckpointLossFailClosed": True},
        },
    })
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["redis_checkpoint_loss"],
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-9",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": ["FAILURE"]},
    })
    prediction = _infra_prediction(terminalClass="FAILURE")
    # V0 strict cardinality: the fault only declares redis_checkpoint_loss, so
    # the prediction must carry exactly the initial + checkpoint_loss_fail_closed
    # attempts (any other durable attemptKind would be an unplanned one).
    prediction["observations"]["attempts"] = [
        attempt
        for attempt in prediction["observations"]["attempts"]
        if attempt["attemptKind"] == "initial"
    ]
    prediction["observations"]["attempts"].append(
        {
            "attemptKind": "checkpoint_loss_fail_closed",
            "requestId": "req-loss",
            "runId": "run-inf-1",
            "taskId": "task-inf-1",
            "threadId": "thread-inf-1",
            "payloadSha256": "b" * 64,
            "resultSha256": "c" * 64,
            "rejected": True,
            "rejectionCode": "checkpoint_unavailable",
            "toolCalls": 0,
            "modelCalls": 0,
            "revisionBefore": 0,
            "revisionAfter": 0,
            "historyCountBefore": 0,
            "historyCountAfter": 0,
            "checkpointRef": "fixture://cp-9",
            "checkpointSha256": "a" * 64,
            "terminalCode": "FAILURE",
        }
    )
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), oracle, fault, prediction)],
    )["perScenario"][0]
    assert result["passed"] is True
    assert _check_by_name(result, "checkpoint_loss_fail_closed")["passed"] is True
    # The aligned checks are emitted only on violation (like attempt_identity_aligned);
    # on a fully bound pass neither ref nor sha check appears.
    names = {c["name"] for c in result["checks"]}
    assert "checkpoint_loss_checkpoint_ref_aligned" not in names
    assert "checkpoint_loss_checkpoint_sha_aligned" not in names


def test_infra_checkpoint_loss_fail_closed_negative(tmp_path):
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": ["FAILURE"],
        "faultExpectations": {
            "redis_checkpoint_loss": {"required": True, "expectedCheckpointLossFailClosed": True},
        },
    })
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["redis_checkpoint_loss"],
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-9",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": ["FAILURE"]},
    })
    prediction = _infra_prediction(terminalClass="FAILURE")
    # V0 strict cardinality: the fault only declares redis_checkpoint_loss, so
    # the prediction must carry exactly the initial attempt; the required
    # checkpoint_loss_fail_closed observation is missing → fail closed.
    prediction["observations"]["attempts"] = [
        attempt
        for attempt in prediction["observations"]["attempts"]
        if attempt["attemptKind"] == "initial"
    ]
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), oracle, fault, prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "checkpoint_loss_fail_closed")["passed"] is False
    assert _check_by_name(result, "redis_checkpoint_loss_observation_present")["passed"] is False


def test_report_exposes_durable_deltas_from_observations(tmp_path):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "exact_replay":
            attempt["revisionBefore"] = 3
            attempt["revisionAfter"] = 4
            attempt["historyCountBefore"] = 10
            attempt["historyCountAfter"] = 13
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["durableDeltas"]["exactReplayRevisionDelta"] == 1
    assert result["durableDeltas"]["exactReplayHistoryDelta"] == 3
    assert result["durableDeltas"]["revisionConflictRevisionDelta"] == 0
    assert result["durableDeltas"]["revisionConflictHistoryDelta"] == 0


# ---------------------------------------------------------------------------
# Hard constraints bound to private product truth
# ---------------------------------------------------------------------------

def test_hard_constraint_missing_product_truth_load_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    with pytest.raises(lab.ScenarioContractError, match="no verifiable product truth"):
        _load(_write_set(tmp_path, [_scenario(_input(), oracle, _fault(), _prediction())]))


def test_frozen_self_reported_hard_constraint_fails(tmp_path):
    """The prediction self-reports a satisfied iOS constraint but ranks a
    product that is not in the private acceptable universe → must fail."""
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001", "1002"],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        rankedProductIds=["1001", "9999"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "ranked_ids_allowed")["passed"] is False


def test_frozen_self_reported_hard_constraint_android_fact_fails(tmp_path):
    """The prediction claims the product is iOS but the private fact atom says
    Android → hard_constraint_fact_verified must fail."""
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "productFactAtoms": [{"productId": "1001", "field": "os", "value": "Android"}],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        rankedProductIds=["1001"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "hard_constraint_fact_verified")["passed"] is False


def test_hard_constraint_private_truth_all_satisfying_passes(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001", "1002"],
        "productFactAtoms": [
            {"productId": "1001", "field": "os", "value": "iOS"},
            {"productId": "1002", "field": "os", "value": "iOS"},
        ],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        rankedProductIds=["1001", "1002"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is True
    assert _check_by_name(result, "hard_constraint_fact_verified")["passed"] is True
    assert _check_by_name(result, "ranked_ids_allowed")["passed"] is True
    assert _check_by_name(result, "hard_constraint_match")["passed"] is True


def test_hard_constraint_pass(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001", "1002"],
        "productFactAtoms": [
            {"productId": "1001", "field": "os", "value": "iOS"},
            {"productId": "1002", "field": "os", "value": "iOS"},
        ],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        rankedProductIds=["1001", "1002"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is True
    assert _check_by_name(result, "hard_constraint_match")["passed"] is True


def test_hard_constraint_missing_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001", "1002"],
        "productFactAtoms": [{"productId": "1001", "field": "os", "value": "iOS"}],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(rankedProductIds=["1001"])  # claims nothing
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "hard_constraint_match")["passed"] is False


def test_hard_constraint_weakened_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001"],
        "productFactAtoms": [{"productId": "1001", "field": "os", "value": "iOS"}],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        rankedProductIds=["1001"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS", "Android"]}]},
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "hard_constraint_match")["passed"] is False


def test_ranked_ids_allowed_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001", "1002"],
    })
    prediction = _prediction(rankedProductIds=["1001", "9999"])
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "ranked_ids_allowed")["passed"] is False


def test_success_with_empty_ranking_fails_when_universe_pinned(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001", "1002"],
    })
    prediction = _prediction()  # no rankedProductIds but SUCCESS
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "ranked_ids_nonempty")["passed"] is False


# ---------------------------------------------------------------------------
# Unsupported claims: claim→evidence/provenance binding
# ---------------------------------------------------------------------------

def _forbid_claims_oracle():
    return _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "evidenceRules": {"forbidUnsupportedClaims": True},
    })


def _bound_claim_prediction():
    return _prediction(
        rankedProductIds=["1001"],
        claims=[{"claimId": "c1", "subject": "1001", "field": "os", "value": "iOS", "evidenceRefs": ["ev-1"]}],
        evidenceCitations=[
            {
                "evidenceId": "ev-1",
                "productId": "1001",
                "field": "os",
                "rawValue": "iOS 17",
                "normalizedValue": "iOS",
                "sourceRef": "catalog://1001",
                "revision": "env-v0-001",
            }
        ],
    )


def test_unsupported_claim_full_binding_passes(tmp_path):
    result = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), _bound_claim_prediction())],
    )["perScenario"][0]
    assert result["passed"] is True
    # The gate always emits exactly one summary check when forbidUnsupportedClaims
    # is enabled, so the unsupported-claim metric forms eligibility on the
    # success side too (a fully-bound pass must not collapse to eligible=0).
    gate = _check_by_name(result, "unsupported_claim_gate")
    assert gate["passed"] is True
    assert len([c for c in result["checks"] if c["name"] == "unsupported_claim_gate"]) == 1


def test_frozen_unsupported_claim_unrepresentable_fails(tmp_path):
    """A structured claim with no evidence binding cannot pass the
    unsupported-claim gate."""
    prediction = _prediction(
        rankedProductIds=["1001"],
        claims=[{"claimId": "c1", "subject": "1001", "field": "os", "value": "iOS", "evidenceRefs": []}],
    )
    result = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "claim_has_evidence")["passed"] is False


def test_frozen_unsupported_claim_incomplete_extraction_fails(tmp_path):
    prediction = _prediction(
        rankedProductIds=["1001"],
        claims=[],
        claimExtraction={"provenance": "runner-claims-v1", "version": "1", "claimsComplete": False},
    )
    result = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "claim_extraction_complete")["passed"] is False


def test_unsupported_claim_missing_provenance_schema_rejected():
    """A claimExtraction without provenance is not even representable: the
    prediction schema requires it (minLength 1), so the loader rejects it at
    load time rather than trusting it at score time."""
    prediction = _prediction(
        rankedProductIds=["1001"],
        claims=[],
        claimExtraction={"version": "1", "claimsComplete": True},
    )
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        lab.validate_record(prediction, "prediction")


@pytest.mark.parametrize(
    "mutate_evidence,check",
    [
        (lambda e: e.update(productId="9999"), "claim_evidence_subject_aligned"),
        (lambda e: e.update(field="price"), "claim_evidence_field_aligned"),
        (lambda e: e.update(normalizedValue="iOS 18"), "claim_evidence_value_consistent"),
        (lambda e: e.update(revision="env-v0-999"), "claim_evidence_revision_aligned"),
    ],
)
def test_unsupported_claim_evidence_mismatch_negative(tmp_path, mutate_evidence, check):
    prediction = _bound_claim_prediction()
    mutate_evidence(prediction["evidenceCitations"][0])
    result = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, check)["passed"] is False


def test_unsupported_claim_dangling_ref_fails(tmp_path):
    prediction = _bound_claim_prediction()
    prediction["claims"][0]["evidenceRefs"] = ["ev-99"]
    result = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "claim_evidence_refs_resolve")["passed"] is False


def test_unsupported_claim_duplicate_evidence_ref_schema_rejected(tmp_path):
    prediction = _bound_claim_prediction()
    prediction["claims"][0]["evidenceRefs"] = ["ev-1", "ev-1"]
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        _score(tmp_path, [_scenario(_input(), _forbid_claims_oracle(), _fault(), prediction)])


def test_unsupported_claims_fail(tmp_path):
    prediction = _prediction(
        rankedProductIds=["1001"],
        evidenceCitations=[
            {
                "evidenceId": "ev-1",
                "productId": "9999",
                "field": "os",
                "rawValue": "iOS 17",
                "normalizedValue": "iOS",
                "sourceRef": "catalog://9999",
                "revision": "env-v0-001",
            }
        ],
    )
    result = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "unsupported_claims_absent")["passed"] is False
    assert _check_by_name(result, "evidence_bound_to_claim")["passed"] is False


def test_unsupported_success_without_citation_fail(tmp_path):
    prediction = _prediction(rankedProductIds=["1001"])
    result = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "unsupported_claims_absent")["passed"] is False


# ---------------------------------------------------------------------------
# Language scoring (evidence, clarification, tools)
# ---------------------------------------------------------------------------

def test_language_scenario_passes(tmp_path):
    result = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])["perScenario"][0]
    assert result["passed"] is True
    assert result["reason"] == "ok"
    names = {c["name"] for c in result["checks"]}
    assert "terminal_class" in names
    assert "evidence_sufficiency" in names


def test_language_terminal_fail(tmp_path):
    prediction = _prediction(terminalClass="FAILURE")
    result = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "terminal_class")["passed"] is False


def test_evidence_sufficiency_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "evidenceRules": {"minCitationCount": 2},
    })
    prediction = _prediction(
        evidenceCitations=[
            {
                "evidenceId": "ev-1",
                "productId": "1001",
                "field": "os",
                "rawValue": "iOS 17",
                "normalizedValue": "iOS",
                "sourceRef": "catalog://1001",
                "revision": "env-v0-001",
            }
        ]
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "evidence_sufficiency")["passed"] is False


def test_evidence_min_cited_products_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "evidenceRules": {"minCitedProductIds": ["1001", "1002"]},
    })
    prediction = _prediction(
        evidenceCitations=[
            {
                "evidenceId": "ev-1",
                "productId": "1001",
                "field": "os",
                "rawValue": "iOS 17",
                "normalizedValue": "iOS",
                "sourceRef": "catalog://1001",
                "revision": "env-v0-001",
            }
        ]
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "evidence_min_cited_products")["passed"] is False


def test_clarification_pass(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["CLARIFICATION_REQUIRED"],
        "clarificationRules": {"requiredQuestions": ["What is your budget?"]},
    })
    prediction = _prediction(
        terminalClass="CLARIFICATION_REQUIRED",
        clarifications=[{"question": "What is your budget?"}],
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is True


def test_clarification_required_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["CLARIFICATION_REQUIRED"],
        "clarificationRules": {"requiredQuestions": ["What is your budget?"]},
    })
    prediction = _prediction(terminalClass="CLARIFICATION_REQUIRED", clarifications=[])
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "clarification_required")["passed"] is False


def test_clarification_not_unnecessary_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "clarificationRules": {},
    })
    prediction = _prediction(
        clarifications=[{"question": "What is your budget?"}],
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "clarification_not_unnecessary")["passed"] is False


def test_clarification_forbidden_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "clarificationRules": {"forbiddenQuestions": ["What is your budget?"]},
    })
    prediction = _prediction(
        clarifications=[{"question": "What is your budget?"}],
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "clarification_forbidden_absent")["passed"] is False


def test_duplicate_tool_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "toolRules": {"maxDuplicateToolCalls": 0},
    })
    prediction = _prediction(toolCalls=[
        {"toolName": "search_products", "ok": True, "turnIndex": 0},
        {"toolName": "search_products", "ok": True, "turnIndex": 0},
    ])
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "duplicate_tool_limit")["passed"] is False


def test_tool_call_count_limit_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "toolRules": {"maxToolCalls": 1},
    })
    prediction = _prediction(toolCalls=[
        {"toolName": "search_products", "ok": True, "turnIndex": 0},
        {"toolName": "compare_products", "ok": True, "turnIndex": 0},
    ])
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "tool_call_count_limit")["passed"] is False


# ---------------------------------------------------------------------------
# Per-track report: aggregate consistency, no mixing, no search metrics
# ---------------------------------------------------------------------------

def test_report_separates_tracks_and_never_mixes(tmp_path):
    scenarios = [
        _scenario(
            _input(sid="ASL-V0-LNG-001"),
            _oracle(sid="ASL-V0-LNG-001"),
            _fault(sid="ASL-V0-LNG-001"),
            _prediction(sid="ASL-V0-LNG-001"),
        ),
        _scenario(_infra_input(), _infra_oracle(), _infra_fault(), _infra_prediction()),
    ]
    report = _score(tmp_path, scenarios)
    assert set(report["tracks"]) == {"language_e2e", "infrastructure_fault"}
    for summary in report["tracks"].values():
        assert set(summary) == {
            "trackType", "scenarioCount", "passed", "failed", "latencyMs", "metrics", "counts",
        }
    assert report["tracks"]["language_e2e"]["scenarioCount"] == 1
    assert report["tracks"]["infrastructure_fault"]["scenarioCount"] == 1
    assert {r["trackType"] for r in report["perScenario"]} == {"language_e2e", "infrastructure_fault"}


def test_report_empty_track_summary_shape(tmp_path):
    report = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    infra = report["tracks"]["infrastructure_fault"]
    assert infra["scenarioCount"] == 0
    assert infra["passed"] == 0
    assert infra["failed"] == 0
    assert infra["latencyMs"] == {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None}
    assert infra["metrics"]["terminal"] == {"eligible": 0, "pass": 0, "fail": 0}
    assert infra["counts"]["toolCallCount"] == {"count": 0, "total": 0, "mean": None}
    assert report["tracks"]["language_e2e"]["scenarioCount"] == 1


def test_report_contains_no_search_metrics(tmp_path):
    report = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    for summary in report["tracks"].values():
        assert "hit" not in summary["metrics"]
        assert "recall" not in summary["metrics"]
        assert "ndcg" not in summary["metrics"]


def test_track_metrics_aggregate_consistent_with_per_scenario(tmp_path):
    scenarios = []
    for index in range(3):
        sid = f"ASL-V0-LNG-{index + 1:03d}"
        scenarios.append(
            _scenario(
                _input(sid=sid),
                _oracle(sid=sid),
                _fault(sid=sid),
                _prediction(sid=sid, pid=f"pred-{index + 1:03d}"),
            )
        )
    scenarios[2]["oracle"]["successConditions"]["toolRules"] = {"maxToolCalls": 1}
    scenarios[2]["prediction"]["toolCalls"] = [
        {"toolName": "search_products", "ok": True, "turnIndex": 0},
        {"toolName": "compare_products", "ok": True, "turnIndex": 0},
    ]
    report = _score(tmp_path, scenarios)
    summary = report["tracks"]["language_e2e"]
    assert summary["metrics"]["terminal"] == {"eligible": 3, "pass": 3, "fail": 0}
    assert summary["metrics"]["tool_count"] == {"eligible": 3, "pass": 2, "fail": 1}
    assert summary["metrics"]["restart"] == {"eligible": 0, "pass": 0, "fail": 0}
    assert summary["metrics"]["evidence"] == {"eligible": 3, "pass": 3, "fail": 0}
    total_tools = sum(r["metricCounts"]["toolCallCount"] for r in report["perScenario"])
    assert total_tools == 4
    assert summary["counts"]["toolCallCount"] == {"count": 3, "total": total_tools, "mean": total_tools / 3}
    assert summary["counts"]["duplicateToolCallCount"] == {"count": 3, "total": 0, "mean": 0}


def test_per_scenario_metric_counts_reflect_runner_owned_tool_calls(tmp_path):
    prediction = _prediction(toolCalls=[
        {"toolName": "search_products", "ok": True, "turnIndex": 0},
        {"toolName": "search_products", "ok": True, "turnIndex": 0},
        {"toolName": "compare_products", "ok": True, "turnIndex": 0},
    ])
    result = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), prediction)])["perScenario"][0]
    assert result["metricCounts"]["toolCallCount"] == 3
    assert result["metricCounts"]["duplicateToolCallCount"] == 1


def test_report_flags_exploratory(tmp_path):
    report = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    assert report["exploratory"] is True
    assert report["statisticalSignificanceClaimed"] is False


def test_report_source_inputs_hashes(tmp_path):
    paths = _write_set(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    report = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    assert report["sourceInputs"] == {
        "inputFileSha256": lab.sha256_file(paths[0]),
        "oracleFileSha256": lab.sha256_file(paths[1]),
        "faultFileSha256": lab.sha256_file(paths[2]),
        "predictionFileSha256": lab.sha256_file(paths[3]),
    }


def test_report_latency_stats_and_flagged_exploratory(tmp_path):
    report = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    language = report["tracks"]["language_e2e"]
    assert language["latencyMs"]["count"] == 1
    assert language["latencyMs"]["min"] == 250.0
    assert language["latencyMs"]["mean"] == 250.0
    assert report["exploratory"] is True


def test_report_deterministic(tmp_path):
    scenarios = [
        _scenario(
            _input(sid="ASL-V0-LNG-001"),
            _oracle(sid="ASL-V0-LNG-001"),
            _fault(sid="ASL-V0-LNG-001"),
            _prediction(sid="ASL-V0-LNG-001"),
        ),
        _scenario(_infra_input(), _infra_oracle(), _infra_fault(), _infra_prediction()),
    ]
    assert _score(tmp_path, scenarios) == _score(tmp_path, scenarios)


def test_report_meta_schema_validates_scored_output(tmp_path):
    report = _score(tmp_path, [_scenario(_input(), _oracle(), _fault(), _prediction())])
    lab.validate_report(report)  # must not raise


# ---------------------------------------------------------------------------
# CONTRACT-003: frozen counterexamples
# ---------------------------------------------------------------------------

def test_003_frozen_empty_ranking_with_product_facts_fails(tmp_path):
    """SUCCESS with hard constraints and private product facts must return a
    non-empty ranking even when the truth source is productFactAtoms only —
    ranked_ids_nonempty must not be scoped to acceptableProductIds."""
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "productFactAtoms": [{"productId": "1001", "field": "os", "value": "iOS"}],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    )  # SUCCESS with no rankedProductIds
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "ranked_ids_nonempty")["passed"] is False


def test_003_frozen_required_replay_without_observation_expected_false_load_fail(tmp_path):
    """A required injection declaring expected=False must fail closed at load:
    absence of a counter-observation is not evidence, so `derived=false ==
    expected=false` must never pass."""
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "faultExpectations": {
            "exact_replay": {"required": True, "expectedExactReplayZeroTool": False},
        },
    })
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["exact_replay"],
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-7",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    prediction = _infra_prediction()
    prediction["observations"]["attempts"] = [
        a for a in prediction["observations"]["attempts"] if a["attemptKind"] == "initial"
    ]
    with pytest.raises(lab.ScenarioContractError, match="must be true"):
        _load(_write_set(tmp_path, [_scenario(_infra_input(), oracle, fault, prediction)]))


def test_003_frozen_good_replay_masks_bad_replay_fails(tmp_path):
    """One qualified exact-replay plus one tool/model/delta-violating replay
    must fail the whole scenario — a good attempt must never mask a bad one."""
    prediction = _infra_prediction()
    good = next(
        a for a in prediction["observations"]["attempts"] if a["attemptKind"] == "exact_replay"
    )
    prediction["observations"]["attempts"].append(
        dict(good, requestId="req-replay-2", toolCalls=1, modelCalls=1)
    )
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "attempt_cardinality")["passed"] is False
    assert _check_by_name(result, "exact_replay_zero_tool")["passed"] is False


def test_003_frozen_supported_claim_aggregate_pass(tmp_path):
    """A fully-bound unsupported-claim scenario must form positive eligibility
    (unsupported_claim = {eligible: 1, pass: 1, fail: 0}), not eligible=0."""
    report = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), _bound_claim_prediction())],
    )
    assert report["tracks"]["language_e2e"]["metrics"]["unsupported_claim"] == {
        "eligible": 1, "pass": 1, "fail": 0,
    }


# ---------------------------------------------------------------------------
# CONTRACT-003: strict cardinality and durable identity
# ---------------------------------------------------------------------------

def test_003_duplicate_initial_fails(tmp_path):
    prediction = _infra_prediction()
    initial = next(
        a for a in prediction["observations"]["attempts"] if a["attemptKind"] == "initial"
    )
    prediction["observations"]["attempts"].append(dict(initial, requestId="req-init-2"))
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "initial_unique")["passed"] is False


def test_003_missing_initial_fails(tmp_path):
    prediction = _infra_prediction()
    prediction["observations"]["attempts"] = [
        a for a in prediction["observations"]["attempts"] if a["attemptKind"] != "initial"
    ]
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "initial_unique")["passed"] is False


def test_003_duplicate_request_id_fails(tmp_path):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "exact_replay":
            attempt["requestId"] = "req-init"  # duplicates the initial requestId
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "request_id_unique")["passed"] is False


def test_003_cross_run_attempt_fails(tmp_path):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "exact_replay":
            attempt["runId"] = "run-other"
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "attempt_identity_aligned")["passed"] is False


def test_003_cross_task_attempt_fails(tmp_path):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "exact_replay":
            attempt["taskId"] = "task-other"
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "attempt_identity_aligned")["passed"] is False


def test_003_cross_thread_attempt_fails(tmp_path):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "exact_replay":
            attempt["threadId"] = "thread-other"
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "attempt_identity_aligned")["passed"] is False


def test_003_unplanned_attempt_kind_fails(tmp_path):
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "faultExpectations": {
            "checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True},
        },
    })
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["checkpoint_restart"],
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-7",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    prediction = _infra_prediction()  # carries exact/mutated/revision attempts the plan never declared
    result = _score(tmp_path, [_scenario(_infra_input(), oracle, fault, prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "unplanned_attempt_kind")["passed"] is False


def test_003_missing_required_attempt_fails(tmp_path):
    prediction = _infra_prediction()
    prediction["observations"]["attempts"] = [
        a for a in prediction["observations"]["attempts"] if a["attemptKind"] == "initial"
    ]
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    for name in (
        "checkpoint_restart_observation_present",
        "exact_replay_observation_present",
        "mutated_replay_observation_present",
        "revision_conflict_observation_present",
    ):
        assert _check_by_name(result, name)["passed"] is False


def test_003_duplicate_required_observation_fails(tmp_path):
    prediction = _infra_prediction()
    replay = next(
        a for a in prediction["observations"]["attempts"] if a["attemptKind"] == "exact_replay"
    )
    prediction["observations"]["attempts"].append(dict(replay, requestId="req-replay-2"))
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "attempt_cardinality")["passed"] is False
    assert _check_by_name(result, "exact_replay_zero_tool")["passed"] is False


# ---------------------------------------------------------------------------
# CONTRACT-003: per-ranked-product hard-constraint verification
# ---------------------------------------------------------------------------

def test_003_ranked_product_missing_fact_fails(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "productFactAtoms": [],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        rankedProductIds=["1001"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "hard_constraint_products_verified")["passed"] is False


def test_003_ranked_product_contradicted_fact_fails(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "productFactAtoms": [{"productId": "1001", "field": "os", "value": "Android"}],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        rankedProductIds=["1001"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "hard_constraint_products_verified")["passed"] is False


def test_003_acceptable_vs_facts_contradiction_load_fail(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001", "1002"],
        "productFactAtoms": [{"productId": "9999", "field": "os", "value": "iOS"}],
    })
    with pytest.raises(lab.ScenarioContractError, match="outside acceptableProductIds"):
        _load(_write_set(tmp_path, [_scenario(_input(), oracle, _fault(), _prediction())]))


def test_003_product_facts_full_satisfying_passes(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "productFactAtoms": [
            {"productId": "1001", "field": "os", "value": "iOS"},
            {"productId": "1002", "field": "os", "value": "iOS"},
        ],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(
        rankedProductIds=["1001", "1002"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    )
    result = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])["perScenario"][0]
    assert result["passed"] is True
    assert _check_by_name(result, "hard_constraint_products_verified")["passed"] is True


# ---------------------------------------------------------------------------
# CONTRACT-003: unsupported-claim gate summary and metric eligibility
# ---------------------------------------------------------------------------

def test_003_unsupported_claim_positive_aggregate(tmp_path):
    report = _score(
        tmp_path,
        [_scenario(_input(), _forbid_claims_oracle(), _fault(), _bound_claim_prediction())],
    )
    assert report["tracks"]["language_e2e"]["metrics"]["unsupported_claim"] == {
        "eligible": 1, "pass": 1, "fail": 0,
    }


def test_003_unsupported_claim_negative_aggregate(tmp_path):
    prediction = _bound_claim_prediction()
    prediction["claims"][0]["evidenceRefs"] = ["ev-99"]  # dangling → gate fails
    report = _score(tmp_path, [_scenario(_input(), _forbid_claims_oracle(), _fault(), prediction)])
    assert report["tracks"]["language_e2e"]["metrics"]["unsupported_claim"] == {
        "eligible": 1, "pass": 0, "fail": 1,
    }


# ---------------------------------------------------------------------------
# CONTRACT-003: every enabled metric family forms eligibility both ways
# ---------------------------------------------------------------------------

def test_003_infra_gates_positive_aggregate(tmp_path):
    """Every enabled infra gate family must form positive eligibility; gates
    that the oracle did not enable must stay eligible=0."""
    report = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), _infra_prediction())],
    )
    metrics = report["tracks"]["infrastructure_fault"]["metrics"]
    assert metrics["terminal"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["restart"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["exact_replay"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["mutated_replay"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["revision_conflict"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["tool_count"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["unsupported_claim"] == {"eligible": 0, "pass": 0, "fail": 0}
    assert metrics["evidence"] == {"eligible": 0, "pass": 0, "fail": 0}
    assert metrics["clarification"] == {"eligible": 0, "pass": 0, "fail": 0}
    assert metrics["duplicate_tool"] == {"eligible": 0, "pass": 0, "fail": 0}
    assert metrics["hard_constraint"] == {"eligible": 0, "pass": 0, "fail": 0}
    assert metrics["checkpoint_loss"] == {"eligible": 0, "pass": 0, "fail": 0}


def test_003_durable_gate_failing_aggregate(tmp_path):
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "exact_replay":
            attempt["toolCalls"] = 1
    report = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )
    assert report["tracks"]["infrastructure_fault"]["metrics"]["exact_replay"] == {
        "eligible": 1, "pass": 0, "fail": 1,
    }


def test_003_language_all_gates_positive_aggregate(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001"],
        "productFactAtoms": [{"productId": "1001", "field": "os", "value": "iOS"}],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
        "evidenceRules": {"forbidUnsupportedClaims": True, "minCitationCount": 1},
        "clarificationRules": {},
        "toolRules": {"maxToolCalls": 1},
    })
    prediction = _prediction(
        rankedProductIds=["1001"],
        claimedConstraints={"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
        claims=[{"claimId": "c1", "subject": "1001", "field": "os", "value": "iOS", "evidenceRefs": ["ev-1"]}],
        evidenceCitations=[
            {
                "evidenceId": "ev-1",
                "productId": "1001",
                "field": "os",
                "rawValue": "iOS 17",
                "normalizedValue": "iOS",
                "sourceRef": "catalog://1001",
                "revision": "env-v0-001",
            }
        ],
    )
    report = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])
    metrics = report["tracks"]["language_e2e"]["metrics"]
    assert metrics["terminal"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["hard_constraint"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["evidence"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["unsupported_claim"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["clarification"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["tool_count"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["duplicate_tool"] == {"eligible": 0, "pass": 0, "fail": 0}


def test_003_language_gate_failing_aggregate(tmp_path):
    oracle = _oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "acceptableProductIds": ["1001"],
        "hardConstraints": {"hard": [{"group": "os", "operator": "IN", "allowedValues": ["iOS"]}]},
    })
    prediction = _prediction(rankedProductIds=["9999"])  # outside the universe
    report = _score(tmp_path, [_scenario(_input(), oracle, _fault(), prediction)])
    assert report["tracks"]["language_e2e"]["metrics"]["hard_constraint"] == {
        "eligible": 1, "pass": 0, "fail": 1,
    }


# ---------------------------------------------------------------------------
# CONTRACT-004: per-attempt durable identity (schema-required, unconditional)
# ---------------------------------------------------------------------------

def _loss_fc_oracle(**over):
    """Oracle that requires the checkpoint-loss fail-closed gate."""
    record = {
        "successConditions": {
            "expectedTerminalClasses": ["FAILURE"],
            "faultExpectations": {
                "redis_checkpoint_loss": {
                    "required": True,
                    "expectedCheckpointLossFailClosed": True,
                },
            },
        }
    }
    return _infra_oracle(**{**record, **over})


def _loss_fc_fault(**over):
    """Fault that injects redis_checkpoint_loss against the authoritative
    fixture fixture://cp-9 (expected contents sha 'a'*64)."""
    record = {
        "faultPlan": {
            "injectionPoints": ["redis_checkpoint_loss"],
            "setupFixture": {
                "checkpointFixtureRef": "fixture://cp-9",
                "expectedCheckpointContentsSha256": "a" * 64,
            },
            "expectedFaultOutcome": {"expectedTerminalClasses": ["FAILURE"]},
        }
    }
    return _infra_fault(**{**record, **over})


def _loss_fc_prediction(**over):
    """Prediction that passes the checkpoint-loss fail-closed contract: exactly
    the initial attempt plus one controlled checkpoint_loss_fail_closed attempt
    carrying the exact fixture ref/sha and no side effects."""
    prediction = _infra_prediction(terminalClass="FAILURE")
    prediction["observations"]["attempts"] = [
        a for a in prediction["observations"]["attempts"] if a["attemptKind"] == "initial"
    ]
    prediction["observations"]["attempts"].append(
        {
            "attemptKind": "checkpoint_loss_fail_closed",
            "requestId": "req-loss",
            "runId": "run-inf-1",
            "taskId": "task-inf-1",
            "threadId": "thread-inf-1",
            "payloadSha256": "b" * 64,
            "resultSha256": "c" * 64,
            "rejected": True,
            "rejectionCode": "checkpoint_unavailable",
            "toolCalls": 0,
            "modelCalls": 0,
            "revisionBefore": 0,
            "revisionAfter": 0,
            "historyCountBefore": 0,
            "historyCountAfter": 0,
            "checkpointRef": "fixture://cp-9",
            "checkpointSha256": "a" * 64,
            "terminalCode": "FAILURE",
        }
    )
    prediction.update(over)
    return prediction


@pytest.mark.parametrize("field", ["taskId", "threadId", "runId"])
@pytest.mark.parametrize(
    "kind",
    ["initial", "restart_recovery", "exact_replay", "mutated_replay", "revision_conflict", "all"],
)
def test_004_missing_attempt_identity_field_load_fail(tmp_path, field, kind):
    """Removing taskId/threadId/runId from any attempt — initial, each durable
    kind, or all attempts — must fail closed at load: the fields are required
    in the prediction schema, so there is no 'field-present-then-compare'
    space and no truthy fallback."""
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if kind == "all" or attempt["attemptKind"] == kind:
            attempt.pop(field, None)
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        _load(_write_set(tmp_path, [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)]))


@pytest.mark.parametrize("field, value", [
    ("taskId", "task-other"),
    ("threadId", "thread-other"),
    ("runId", "run-other"),
])
@pytest.mark.parametrize(
    "kind",
    ["initial", "restart_recovery", "exact_replay", "mutated_replay", "revision_conflict"],
)
def test_004_cross_identity_attempt_fails(tmp_path, field, value, kind):
    """Changing any identity field on any attempt kind to a cross-identity
    value must fail closed at score time (unconditional comparison)."""
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == kind:
            attempt[field] = value
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "attempt_identity_aligned")["passed"] is False


def test_004_full_identity_explicit_triple_passes(tmp_path):
    """Every attempt explicitly carries taskId/threadId/runId strictly equal to
    the prediction triple and requestId stays globally unique → passes."""
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        assert attempt["taskId"] == prediction["taskId"] == "task-inf-1"
        assert attempt["threadId"] == prediction["threadId"] == "thread-inf-1"
        assert attempt["runId"] == prediction["runId"] == "run-inf-1"
    request_ids = [a["requestId"] for a in prediction["observations"]["attempts"]]
    assert len(request_ids) == len(set(request_ids))
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is True


# ---------------------------------------------------------------------------
# CONTRACT-004: checkpoint-loss is fail-closed, never a recovery
# ---------------------------------------------------------------------------

def test_004_old_checkpoint_loss_oracle_field_rejected(tmp_path):
    """expectedCheckpointLossRecovered is no longer part of the contract and is
    rejected by additionalProperties=false."""
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": ["FAILURE"],
        "faultExpectations": {
            "redis_checkpoint_loss": {"required": True, "expectedCheckpointLossRecovered": True},
        },
    })
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(), oracle, _loss_fc_fault(), _loss_fc_prediction()),
        ]))


def test_004_old_checkpoint_loss_attempt_kind_rejected(tmp_path):
    """checkpoint_loss_recovery is no longer a valid attemptKind and is rejected
    by the schema enum (no dual semantics)."""
    prediction = _loss_fc_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "checkpoint_loss_fail_closed":
            attempt["attemptKind"] = "checkpoint_loss_recovery"
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction),
        ]))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(rejected=False),
        lambda a: a.pop("rejected", None),
        lambda a: a.update(rejectionCode=""),
        lambda a: a.pop("rejectionCode", None),
        lambda a: a.update(toolCalls=1),
        lambda a: a.update(modelCalls=1),
        lambda a: a.update(revisionBefore=1, revisionAfter=2),
        lambda a: a.update(historyCountBefore=1, historyCountAfter=2),
        lambda a: a.pop("revisionBefore", None),
        lambda a: a.pop("historyCountBefore", None),
        lambda a: a.update(terminalCode="SUCCESS"),
        lambda a: a.pop("terminalCode", None),
        lambda a: a.update(checkpointRef="attacker://unrelated"),
        lambda a: a.update(checkpointSha256="b" * 64),
        lambda a: a.pop("checkpointRef", None),
        lambda a: a.pop("checkpointSha256", None),
    ],
)
def test_004_checkpoint_loss_fail_closed_each_tamper_fails(tmp_path, mutate):
    """Every field that proves fail-closed behaviour — controlled rejection,
    zero tool/model calls, no revision/history delta, terminal in the expected
    set, exact fixture ref/sha binding — is required; tampering any one of them
    fails the gate.  The conclusion is derived from runner-owned fields, never
    from SUT self-reported booleans."""
    prediction = _loss_fc_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "checkpoint_loss_fail_closed":
            mutate(attempt)
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "checkpoint_loss_fail_closed")["passed"] is False


def test_004_checkpoint_loss_top_level_terminal_class_outside_expected_fails(tmp_path):
    """The prediction top-level terminalClass must also stay within the
    fail-closed expected set; a SUCCESS 'recovery' terminal is never valid for a
    loss scenario."""
    prediction = _loss_fc_prediction(terminalClass="SUCCESS")
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "terminal_class")["passed"] is False


# ---------------------------------------------------------------------------
# CONTRACT-004: fixture/ref/hash verbatim binding (restart + loss)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mutate, check_name",
    [
        (lambda a: a.pop("checkpointRef", None), "restart_checkpoint_ref_aligned"),
        (lambda a: a.update(checkpointRef="attacker://unrelated"), "restart_checkpoint_ref_aligned"),
        (lambda a: a.update(checkpointRef="fixture://cp-other"), "restart_checkpoint_ref_aligned"),
        (lambda a: a.update(checkpointRef="fixture://cp-7x"), "restart_checkpoint_ref_aligned"),
        (lambda a: a.pop("checkpointSha256", None), "restart_checkpoint_sha_aligned"),
        (lambda a: a.update(checkpointSha256="b" * 64), "restart_checkpoint_sha_aligned"),
    ],
)
def test_004_restart_fixture_binding_each_tamper_fails(tmp_path, mutate, check_name):
    """Restart recovery requires the attempt's checkpointRef to equal the
    setupFixture's checkpointFixtureRef verbatim and checkpointSha256 to equal
    expectedCheckpointContentsSha256 verbatim.  Missing, forged, cross-fixture
    and merely prefix-similar refs/hashes all fail closed — no basename,
    prefix/suffix or URI normalization guessing."""
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "restart_recovery":
            mutate(attempt)
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "restart_recovery")["passed"] is False
    assert _check_by_name(result, check_name)["passed"] is False


@pytest.mark.parametrize(
    "mutate, check_name",
    [
        (lambda a: a.pop("checkpointRef", None), "checkpoint_loss_checkpoint_ref_aligned"),
        (lambda a: a.update(checkpointRef="attacker://unrelated"), "checkpoint_loss_checkpoint_ref_aligned"),
        (lambda a: a.update(checkpointRef="fixture://cp-other"), "checkpoint_loss_checkpoint_ref_aligned"),
        (lambda a: a.update(checkpointRef="fixture://cp-9x"), "checkpoint_loss_checkpoint_ref_aligned"),
        (lambda a: a.pop("checkpointSha256", None), "checkpoint_loss_checkpoint_sha_aligned"),
        (lambda a: a.update(checkpointSha256="b" * 64), "checkpoint_loss_checkpoint_sha_aligned"),
    ],
)
def test_004_loss_fixture_binding_each_tamper_fails(tmp_path, mutate, check_name):
    prediction = _loss_fc_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "checkpoint_loss_fail_closed":
            mutate(attempt)
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "checkpoint_loss_fail_closed")["passed"] is False
    assert _check_by_name(result, check_name)["passed"] is False


@pytest.mark.parametrize(
    "point, terminal, expectation_key, expectation_value, fault_plan",
    [
        (
            "checkpoint_restart",
            "SUCCESS",
            "checkpoint_restart",
            {"required": True, "expectedRecoveredAfterRestart": True},
            {
                "injectionPoints": ["checkpoint_restart"],
                "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
            },
        ),
        (
            "redis_checkpoint_loss",
            "FAILURE",
            "redis_checkpoint_loss",
            {"required": True, "expectedCheckpointLossFailClosed": True},
            {
                "injectionPoints": ["redis_checkpoint_loss"],
                "expectedFaultOutcome": {"expectedTerminalClasses": ["FAILURE"]},
            },
        ),
    ],
)
def test_004_checkpoint_injection_missing_setup_fixture_load_fail(
    tmp_path, point, terminal, expectation_key, expectation_value, fault_plan
):
    """checkpoint_restart / redis_checkpoint_loss injections require the
    authoritative setupFixture; a fault without one fails closed at load."""
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": [terminal],
        "faultExpectations": {expectation_key: expectation_value},
    })
    fault = _infra_fault(faultPlan=fault_plan)
    prediction = _infra_prediction(terminalClass=terminal)
    with pytest.raises(lab.ScenarioContractError, match="require setupFixture"):
        _load(_write_set(tmp_path, [_scenario(_infra_input(), oracle, fault, prediction)]))


@pytest.mark.parametrize(
    "setup_fixture, match",
    [
        ({}, "missing checkpointFixtureRef"),
        ({"checkpointFixtureRef": "fixture://cp-7"}, "missing expectedCheckpointContentsSha256"),
        ({"expectedCheckpointContentsSha256": "a" * 64}, "missing checkpointFixtureRef"),
    ],
)
def test_004_checkpoint_setup_partial_missing_load_fail(tmp_path, setup_fixture, match):
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["checkpoint_restart"],
        "setupFixture": setup_fixture,
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    with pytest.raises(lab.ScenarioContractError, match=match):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(), _infra_oracle(), fault, _infra_prediction()),
        ]))


def test_004_non_checkpoint_fault_does_not_require_setup_fixture(tmp_path):
    """A fault that never touches a checkpoint is not required to carry a
    setupFixture — the requirement is scoped to checkpoint injections only."""
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "faultExpectations": {"exact_replay": {"required": True, "expectedExactReplayZeroTool": True}},
    })
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["exact_replay"],
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    loaded = _load(_write_set(tmp_path, [
        _scenario(_infra_input(), oracle, fault, _infra_prediction()),
    ]))
    assert loaded is not None


# ---------------------------------------------------------------------------
# CONTRACT-004: frozen counterexamples must not PASS=True
# ---------------------------------------------------------------------------

def test_004_frozen_missing_attempt_task_thread_identity_not_pass(tmp_path):
    """Frozen counterexample: a prediction whose attempts all omit taskId/
    threadId (only the top level carries identity) previously scored PASS=True.
    Now the schema requires the triple on every attempt, so the set fails
    closed at load and can never pass."""
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        attempt.pop("taskId", None)
        attempt.pop("threadId", None)
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction),
        ]))


def test_004_frozen_checkpoint_loss_with_side_effects_not_pass(tmp_path):
    """Frozen counterexample: a loss attempt with toolCalls=99, modelCalls=77
    and revision/history deltas previously still scored PASS=True.  Fail-closed
    requires zero side effects and no delta, so it must now fail."""
    prediction = _loss_fc_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "checkpoint_loss_fail_closed":
            attempt.update(
                toolCalls=99,
                modelCalls=77,
                revisionBefore=0,
                revisionAfter=9,
                historyCountBefore=0,
                historyCountAfter=8,
            )
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "checkpoint_loss_fail_closed")["passed"] is False


def test_004_frozen_restart_with_unrelated_checkpoint_ref_not_pass(tmp_path):
    """Frozen counterexample: a legitimate restart whose checkpointRef was
    replaced with attacker://unrelated previously scored PASS=True (truthy
    checkpointRef).  The verbatim fixture binding now fails it."""
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "restart_recovery":
            attempt["checkpointRef"] = "attacker://unrelated"
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "restart_recovery")["passed"] is False
    assert _check_by_name(result, "restart_checkpoint_ref_aligned")["passed"] is False


# ---------------------------------------------------------------------------
# CONTRACT-004: checkpoint-loss aggregate eligibility (positive + negative)
# ---------------------------------------------------------------------------

def test_004_checkpoint_loss_positive_aggregate(tmp_path):
    report = _score(
        tmp_path,
        [_scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), _loss_fc_prediction())],
    )
    metrics = report["tracks"]["infrastructure_fault"]["metrics"]
    assert metrics["checkpoint_loss"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["terminal"] == {"eligible": 1, "pass": 1, "fail": 0}


def test_004_checkpoint_loss_negative_aggregate(tmp_path):
    prediction = _loss_fc_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "checkpoint_loss_fail_closed":
            attempt["rejected"] = False
    report = _score(
        tmp_path,
        [_scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction)],
    )
    assert report["tracks"]["infrastructure_fault"]["metrics"]["checkpoint_loss"] == {
        "eligible": 1, "pass": 0, "fail": 1,
    }


# ---------------------------------------------------------------------------
# CONTRACT-005: restart recovery is SUCCESS-only; restart and checkpoint loss
# cannot share a scenario; checkpoint-loss identity matrix is frozen
# ---------------------------------------------------------------------------

def _restart_failure_records(sid="ASL-V0-INF-CE1"):
    """checkpoint_restart-only scenario whose shared expected terminal and
    restart attempt terminal are both FAILURE (frozen counterexample 1 shape:
    every identity/ref/hash/cardinality field is otherwise legal)."""
    base = {
        "trackType": "infrastructure_fault",
        "catalogRevision": "catalog-v0-001",
        "environmentRevision": "env-v0-001",
    }
    inp = {
        "scenarioId": sid, **base, "schemaVersion": lab.INPUT_SCHEMA_VERSION,
        "setupRef": "fixture.cp-7", "turns": [],
    }
    orc = {
        "scenarioId": sid, **base, "schemaVersion": lab.ORACLE_SCHEMA_VERSION,
        "successConditions": {
            "expectedTerminalClasses": ["FAILURE"],
            "faultExpectations": {
                "checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True},
            },
        },
    }
    flt = {
        "scenarioId": sid, **base, "schemaVersion": lab.FAULT_SCHEMA_VERSION,
        "faultPlan": {
            "injectionPoints": ["checkpoint_restart"],
            "setupFixture": {
                "checkpointFixtureRef": "fixture://cp-7",
                "expectedCheckpointContentsSha256": "a" * 64,
            },
            "expectedFaultOutcome": {"expectedTerminalClasses": ["FAILURE"]},
        },
    }
    pred = {
        "predictionId": "pred-ce1", "taskId": "task-inf-1", "threadId": "thread-inf-1",
        "runId": "run-inf-1", "scenarioId": sid, **base,
        "schemaVersion": lab.PREDICTION_SCHEMA_VERSION, "terminalClass": "FAILURE",
        "toolCalls": [], "claims": [],
        "claimExtraction": {"provenance": "runner-claims-v1", "version": "1", "claimsComplete": True},
        "observations": {"attempts": [
            {
                "attemptKind": "initial", "requestId": "req-init", "runId": "run-inf-1",
                "taskId": "task-inf-1", "threadId": "thread-inf-1",
                "payloadSha256": "b" * 64, "resultSha256": "c" * 64,
                "toolCalls": 0, "modelCalls": 0,
            },
            {
                "attemptKind": "restart_recovery", "requestId": "req-restart",
                "runId": "run-inf-1", "taskId": "task-inf-1", "threadId": "thread-inf-1",
                "payloadSha256": "b" * 64, "resultSha256": "c" * 64,
                "toolCalls": 0, "modelCalls": 0,
                "checkpointRef": "fixture://cp-7", "checkpointSha256": "a" * 64,
                "terminalCode": "FAILURE",
            },
        ]},
    }
    return inp, orc, flt, pred


def _restart_loss_combo_records(sid="ASL-V0-INF-CE2", restart_first=True):
    """checkpoint_restart + redis_checkpoint_loss in one scenario with a shared
    FAILURE terminal (frozen counterexample 2 shape).  restart_first toggles the
    declaration order of the two injections/expectations."""
    keys = ["checkpoint_restart", "redis_checkpoint_loss"]
    if not restart_first:
        keys = keys[::-1]
    expectations = {
        "checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True},
        "redis_checkpoint_loss": {"required": True, "expectedCheckpointLossFailClosed": True},
    }
    base = {
        "trackType": "infrastructure_fault",
        "catalogRevision": "catalog-v0-001",
        "environmentRevision": "env-v0-001",
    }
    inp = {
        "scenarioId": sid, **base, "schemaVersion": lab.INPUT_SCHEMA_VERSION,
        "setupRef": "fixture.cp-7", "turns": [],
    }
    orc = {
        "scenarioId": sid, **base, "schemaVersion": lab.ORACLE_SCHEMA_VERSION,
        "successConditions": {
            "expectedTerminalClasses": ["FAILURE"],
            "faultExpectations": {key: expectations[key] for key in keys},
        },
    }
    flt = {
        "scenarioId": sid, **base, "schemaVersion": lab.FAULT_SCHEMA_VERSION,
        "faultPlan": {
            "injectionPoints": list(keys),
            "setupFixture": {
                "checkpointFixtureRef": "fixture://cp-7",
                "expectedCheckpointContentsSha256": "a" * 64,
            },
            "expectedFaultOutcome": {"expectedTerminalClasses": ["FAILURE"]},
        },
    }
    pred = {
        "predictionId": "pred-ce2", "taskId": "task-inf-1", "threadId": "thread-inf-1",
        "runId": "run-inf-1", "scenarioId": sid, **base,
        "schemaVersion": lab.PREDICTION_SCHEMA_VERSION, "terminalClass": "FAILURE",
        "toolCalls": [], "claims": [],
        "claimExtraction": {"provenance": "runner-claims-v1", "version": "1", "claimsComplete": True},
        "observations": {"attempts": [
            {
                "attemptKind": "initial", "requestId": "req-init", "runId": "run-inf-1",
                "taskId": "task-inf-1", "threadId": "thread-inf-1",
                "payloadSha256": "b" * 64, "resultSha256": "c" * 64,
                "toolCalls": 0, "modelCalls": 0,
            },
            {
                "attemptKind": "restart_recovery", "requestId": "req-restart",
                "runId": "run-inf-1", "taskId": "task-inf-1", "threadId": "thread-inf-1",
                "payloadSha256": "b" * 64, "resultSha256": "c" * 64,
                "toolCalls": 0, "modelCalls": 0,
                "checkpointRef": "fixture://cp-7", "checkpointSha256": "a" * 64,
                "terminalCode": "FAILURE",
            },
            {
                "attemptKind": "checkpoint_loss_fail_closed", "requestId": "req-loss",
                "runId": "run-inf-1", "taskId": "task-inf-1", "threadId": "thread-inf-1",
                "payloadSha256": "b" * 64, "resultSha256": "c" * 64,
                "rejected": True, "rejectionCode": "checkpoint_unavailable",
                "toolCalls": 0, "modelCalls": 0,
                "revisionBefore": 0, "revisionAfter": 0,
                "historyCountBefore": 0, "historyCountAfter": 0,
                "checkpointRef": "fixture://cp-7", "checkpointSha256": "a" * 64,
                "terminalCode": "FAILURE",
            },
        ]},
    }
    return inp, orc, flt, pred


def test_005_frozen_restart_failure_misclassified_as_recovered(tmp_path):
    """Frozen counterexample: a restart-only scenario whose shared expected
    terminal is FAILURE and whose restart attempt ends FAILURE was previously
    scored PASS=True.  V0 restart recovery is SUCCESS-only, so this shape must
    fail closed at load with the exact shared-terminal contract error."""
    with pytest.raises(lab.ScenarioContractError, match="checkpoint_restart fixes the shared "
                                                       "expected terminal to exactly \\['SUCCESS'\\]"):
        _load(_write_set(tmp_path, [
            _scenario(*_restart_failure_records()),
        ]))


def test_005_frozen_restart_and_checkpoint_loss_shared_failure_terminal(tmp_path):
    """Frozen counterexample: checkpoint_restart and redis_checkpoint_loss in
    one scenario previously both passed on a shared FAILURE terminal.  V0 does
    not support the combination — the loader must reject it with a stable
    contract error naming both injections."""
    with pytest.raises(lab.ScenarioContractError, match="V0 does not support checkpoint_restart "
                                                       "together with redis_checkpoint_loss"):
        _load(_write_set(tmp_path, [
            _scenario(*_restart_loss_combo_records()),
        ]))


@pytest.mark.parametrize(
    "terminal, expected_passed",
    [
        ("SUCCESS", True),
        (None, False),
        ("FAILURE", False),
        ("ABSTAIN_OR_EXPLAIN", False),
        ("CLARIFICATION_REQUIRED", False),
        ("unknown-terminal", False),
    ],
)
def test_005_restart_terminal_matrix(tmp_path, terminal, expected_passed):
    """In a valid restart scenario (shared expected terminal ['SUCCESS']), the
    restart attempt's terminalCode must be exactly 'SUCCESS'.  Missing,
    FAILURE, ABSTAIN, CLARIFICATION, or any other string fails the derived
    restart_recovery gate on its own — the top-level terminal check staying
    green must not rescue a non-SUCCESS attempt terminal."""
    prediction = _infra_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "restart_recovery":
            if terminal is None:
                attempt.pop("terminalCode", None)
            else:
                attempt["terminalCode"] = terminal
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _infra_oracle(), _infra_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is expected_passed
    assert _check_by_name(result, "restart_recovery")["passed"] is expected_passed


@pytest.mark.parametrize(
    "terminal, ok",
    [
        (["SUCCESS"], True),
        (["FAILURE"], False),
        (["ABSTAIN_OR_EXPLAIN"], False),
        (["CLARIFICATION_REQUIRED"], False),
        (["SUCCESS", "FAILURE"], False),
    ],
)
def test_005_restart_shared_terminal_matrix(tmp_path, terminal, ok):
    """required checkpoint_restart fixes the shared expected terminal to exactly
    ['SUCCESS']: the strict equality must hold, not mere inclusion, so any
    single non-SUCCESS class or a mixed set fails closed at load."""
    oracle = _infra_oracle(successConditions={
        "expectedTerminalClasses": terminal,
        "faultExpectations": {
            "checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True},
        },
    })
    fault = _infra_fault(faultPlan={
        "injectionPoints": ["checkpoint_restart"],
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-7",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": terminal},
    })
    if ok:
        loaded = _load(_write_set(tmp_path, [
            _scenario(_infra_input(), oracle, fault, _infra_prediction(terminalClass="SUCCESS")),
        ]))
        assert loaded is not None
    else:
        with pytest.raises(lab.ScenarioContractError, match="checkpoint_restart fixes the shared "
                                                           "expected terminal to exactly"):
            _load(_write_set(tmp_path, [
                _scenario(_infra_input(), oracle, fault, _infra_prediction(terminalClass=terminal[0])),
            ]))


@pytest.mark.parametrize("restart_first", [True, False])
def test_005_restart_loss_combination_order_stable(tmp_path, restart_first):
    """Restart+loss is rejected by the same stable contract error regardless of
    the declaration order of injections/expectations — never a random set
    order and never a silently ignored combination."""
    with pytest.raises(lab.ScenarioContractError) as excinfo:
        _load(_write_set(tmp_path, [
            _scenario(*_restart_loss_combo_records(restart_first=restart_first)),
        ]))
    message = str(excinfo.value)
    assert "V0 does not support checkpoint_restart together with redis_checkpoint_loss" in message


def test_005_restart_loss_combination_error_identical_across_orders(tmp_path):
    """The two declaration orders produce byte-identical contract errors, so
    no set-order randomness can change which error a runner sees."""
    def error(restart_first):
        with pytest.raises(lab.ScenarioContractError) as excinfo:
            _load(_write_set(tmp_path, [
                _scenario(*_restart_loss_combo_records(restart_first=restart_first)),
            ]))
        return str(excinfo.value)
    assert error(True) == error(False)


@pytest.mark.parametrize("field", ["taskId", "threadId", "runId"])
def test_005_checkpoint_loss_identity_missing_field_load_fail(tmp_path, field):
    """checkpoint_loss_fail_closed must be inside the per-field identity matrix:
    removing taskId/threadId/runId from the loss attempt fails closed at load
    (schema required), exactly like every other attempt kind."""
    prediction = _loss_fc_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "checkpoint_loss_fail_closed":
            attempt.pop(field, None)
    with pytest.raises(lab.ScenarioContractError, match="violates"):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction),
        ]))


@pytest.mark.parametrize("field, value", [
    ("taskId", "task-other"),
    ("threadId", "thread-other"),
    ("runId", "run-other"),
])
def test_005_checkpoint_loss_identity_cross_value_fails(tmp_path, field, value):
    """A cross-identity value on the checkpoint_loss_fail_closed attempt fails
    closed at score time with the unconditional per-attempt identity check."""
    prediction = _loss_fc_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "checkpoint_loss_fail_closed":
            attempt[field] = value
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is False
    assert _check_by_name(result, "attempt_identity_aligned")["passed"] is False


def test_005_checkpoint_loss_identity_full_triple_passes(tmp_path):
    """The checkpoint_loss_fail_closed attempt explicitly carrying the exact
    task/thread/run triple still passes the full fail-closed contract."""
    prediction = _loss_fc_prediction()
    for attempt in prediction["observations"]["attempts"]:
        if attempt["attemptKind"] == "checkpoint_loss_fail_closed":
            assert attempt["taskId"] == prediction["taskId"] == "task-inf-1"
            assert attempt["threadId"] == prediction["threadId"] == "thread-inf-1"
            assert attempt["runId"] == prediction["runId"] == "run-inf-1"
    result = _score(
        tmp_path,
        [_scenario(_infra_input(), _loss_fc_oracle(), _loss_fc_fault(), prediction)],
    )["perScenario"][0]
    assert result["passed"] is True
    assert _check_by_name(result, "checkpoint_loss_fail_closed")["passed"] is True


def test_005_restart_and_loss_positive_aggregates(tmp_path):
    """Restart and checkpoint-loss stay cleanly separable in one set: each
    single-injection positive scores with the correct eligible/pass/fail and
    a non-checkpoint fault (exact_replay only) is untouched by the new
    restart/loss terminal rules."""
    restart_oracle = _infra_oracle("ASL-V0-INF-R", successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "faultExpectations": {
            "checkpoint_restart": {"required": True, "expectedRecoveredAfterRestart": True},
        },
    })
    restart_fault = _infra_fault("ASL-V0-INF-R", faultPlan={
        "injectionPoints": ["checkpoint_restart"],
        "setupFixture": {
            "checkpointFixtureRef": "fixture://cp-7",
            "expectedCheckpointContentsSha256": "a" * 64,
        },
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    restart_prediction = _infra_prediction("ASL-V0-INF-R", pid="pred-inf-005-r")
    restart_prediction["observations"]["attempts"] = [
        attempt for attempt in restart_prediction["observations"]["attempts"]
        if attempt["attemptKind"] in ("initial", "restart_recovery")
    ]
    restart_scenario = _scenario(_infra_input("ASL-V0-INF-R"), restart_oracle, restart_fault,
                                 restart_prediction)
    loss_prediction = _loss_fc_prediction()
    loss_prediction["scenarioId"] = "ASL-V0-INF-L"
    loss_prediction["predictionId"] = "pred-inf-005-l"
    loss_scenario = _scenario(_infra_input("ASL-V0-INF-L"), _loss_fc_oracle(sid="ASL-V0-INF-L"),
                              _loss_fc_fault(sid="ASL-V0-INF-L"), loss_prediction)
    replay_oracle = _infra_oracle("ASL-V0-INF-ER", successConditions={
        "expectedTerminalClasses": ["SUCCESS"],
        "faultExpectations": {"exact_replay": {"required": True, "expectedExactReplayZeroTool": True}},
    })
    replay_fault = _infra_fault("ASL-V0-INF-ER", faultPlan={
        "injectionPoints": ["exact_replay"],
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    replay_prediction = _infra_prediction("ASL-V0-INF-ER", pid="pred-inf-005-er")
    replay_prediction["observations"]["attempts"] = [
        attempt for attempt in replay_prediction["observations"]["attempts"]
        if attempt["attemptKind"] in ("initial", "exact_replay")
    ]
    replay_scenario = _scenario(_infra_input("ASL-V0-INF-ER"), replay_oracle, replay_fault,
                                replay_prediction)
    report = _score(tmp_path, [restart_scenario, loss_scenario, replay_scenario])
    metrics = report["tracks"]["infrastructure_fault"]["metrics"]
    assert metrics["restart"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["checkpoint_loss"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["exact_replay"] == {"eligible": 1, "pass": 1, "fail": 0}
    assert metrics["terminal"] == {"eligible": 3, "pass": 3, "fail": 0}


# ---------------------------------------------------------------------------
# CONTRACT-006: expected-terminal arrays are unique before set comparison
# (fault schema uniqueItems + loader defensive duplicate detection)
# ---------------------------------------------------------------------------

def _restart_dup_terminal_records(sid="ASL-V0-INF-CE3"):
    """Frozen CONTRACT-006 counterexample shape: required checkpoint_restart,
    oracle expected terminal ['SUCCESS'], fault expected terminal
    ['SUCCESS','SUCCESS'].  Every identity/ref/hash/cardinality field is
    otherwise legal; before the fix the loader's set conversion collapsed the
    duplicate and the scenario scored PASS=True."""
    inp, orc, flt, pred = _restart_failure_records(sid=sid)
    orc["successConditions"]["expectedTerminalClasses"] = ["SUCCESS"]
    flt["faultPlan"]["expectedFaultOutcome"]["expectedTerminalClasses"] = ["SUCCESS", "SUCCESS"]
    pred["terminalClass"] = "SUCCESS"
    for attempt in pred["observations"]["attempts"]:
        if attempt["attemptKind"] == "restart_recovery":
            attempt["terminalCode"] = "SUCCESS"
    return inp, orc, flt, pred


def test_006_frozen_fault_terminal_duplicate_collapses_to_set(tmp_path):
    """Frozen counterexample: fault expectedTerminalClasses=['SUCCESS',
    'SUCCESS'] against oracle ['SUCCESS'] was previously set-collapsed to a
    singleton and scored PASS=True.  The fault schema's uniqueItems now rejects
    the malformed fault record at load, before any set comparison or scoring."""
    with pytest.raises(lab.ScenarioContractError, match="non-unique"):
        _load(_write_set(tmp_path, [
            _scenario(*_restart_dup_terminal_records()),
        ]))


def test_006_checkpoint_loss_fault_duplicate_load_fail(tmp_path):
    """A redis_checkpoint_loss fault that repeats its fail-closed terminal
    ['FAILURE','FAILURE'] must fail closed at load (uniqueItems) instead of
    collapsing to a single FAILURE and passing the fail-closed gate."""
    sid = "ASL-V0-INF-CE4"
    oracle = _loss_fc_oracle(sid=sid)
    fault = _loss_fc_fault(sid=sid)
    fault["faultPlan"]["expectedFaultOutcome"]["expectedTerminalClasses"] = ["FAILURE", "FAILURE"]
    prediction = _loss_fc_prediction()
    prediction["scenarioId"] = sid
    prediction["predictionId"] = "pred-inf-ce4"
    with pytest.raises(lab.ScenarioContractError, match="non-unique"):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(sid), oracle, fault, prediction),
        ]))


def test_006_non_restart_fault_duplicate_mixed_load_fail(tmp_path):
    """Cardinality enforcement is not restart/loss-specific: an exact_replay
    fault with a duplicated terminal inside a mixed expected set
    ['SUCCESS','FAILURE','FAILURE'] must also fail closed at load."""
    sid = "ASL-V0-INF-CE5"
    oracle = _infra_oracle(sid, successConditions={
        "expectedTerminalClasses": ["SUCCESS", "FAILURE"],
        "faultExpectations": {"exact_replay": {"required": True, "expectedExactReplayZeroTool": True}},
    })
    fault = _infra_fault(sid, faultPlan={
        "injectionPoints": ["exact_replay"],
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS", "FAILURE", "FAILURE"]},
    })
    prediction = _infra_prediction(sid, pid="pred-inf-ce5")
    prediction["observations"]["attempts"] = [
        a for a in prediction["observations"]["attempts"]
        if a["attemptKind"] in ("initial", "exact_replay")
    ]
    with pytest.raises(lab.ScenarioContractError, match="non-unique"):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(sid), oracle, fault, prediction),
        ]))


def test_006_oracle_duplicate_load_fail(tmp_path):
    """An oracle that repeats its expected terminal must fail closed at load:
    the oracle schema rejects duplicates, and the duplicated truth must never
    be collapsed to a singleton and compared equal to a unique fault set."""
    sid = "ASL-V0-INF-CE6"
    oracle = _infra_oracle(sid, successConditions={
        "expectedTerminalClasses": ["SUCCESS", "SUCCESS"],
        "faultExpectations": {"exact_replay": {"required": True, "expectedExactReplayZeroTool": True}},
    })
    fault = _infra_fault(sid, faultPlan={
        "injectionPoints": ["exact_replay"],
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS"]},
    })
    prediction = _infra_prediction(sid, pid="pred-inf-ce6")
    prediction["observations"]["attempts"] = [
        a for a in prediction["observations"]["attempts"]
        if a["attemptKind"] in ("initial", "exact_replay")
    ]
    with pytest.raises(lab.ScenarioContractError, match="non-unique"):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(sid), oracle, fault, prediction),
        ]))


def test_006_both_sides_same_duplicate_load_fail(tmp_path):
    """fault and oracle carrying the same duplicated terminal must still fail
    closed: set equality of the collapsed sets is never a valid pass, so the
    fault-side uniqueness violation is rejected before any comparison."""
    sid = "ASL-V0-INF-CE7"
    oracle = _infra_oracle(sid, successConditions={
        "expectedTerminalClasses": ["SUCCESS", "SUCCESS"],
        "faultExpectations": {"exact_replay": {"required": True, "expectedExactReplayZeroTool": True}},
    })
    fault = _infra_fault(sid, faultPlan={
        "injectionPoints": ["exact_replay"],
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS", "SUCCESS"]},
    })
    prediction = _infra_prediction(sid, pid="pred-inf-ce7")
    prediction["observations"]["attempts"] = [
        a for a in prediction["observations"]["attempts"]
        if a["attemptKind"] in ("initial", "exact_replay")
    ]
    with pytest.raises(lab.ScenarioContractError, match="non-unique"):
        _load(_write_set(tmp_path, [
            _scenario(_infra_input(sid), oracle, fault, prediction),
        ]))


def test_006_loader_defensive_duplicate_detection():
    """The module-level enforcement boundary rejects duplicated terminals even
    when schema validation is bypassed — the loader's defensive check is a
    direct gate, not just the jsonschema uniqueItems text.  fault, oracle and
    same-on-both-sides duplicates all produce a stable, locatable contract
    error before any set comparison; unique terminals still reach it."""
    def _records(terminals, oracle_terminals):
        return {
            "trackType": "infrastructure_fault",
            "successConditions": {
                "expectedTerminalClasses": oracle_terminals,
                "faultExpectations": {
                    "exact_replay": {"required": True, "expectedExactReplayZeroTool": True},
                },
            },
        }, {
            "trackType": "infrastructure_fault",
            "faultPlan": {
                "injectionPoints": ["exact_replay"],
                "expectedFaultOutcome": {"expectedTerminalClasses": terminals},
            },
        }

    # fault-side duplicate fires before set comparison
    oracle, fault = _records(["SUCCESS", "SUCCESS"], ["SUCCESS"])
    with pytest.raises(
        lab.ScenarioContractError,
        match="faultPlan.expectedFaultOutcome.expectedTerminalClasses must be unique",
    ):
        lab._enforce_fault_oracle_truth("ASL-V0-INF-006", oracle, fault)

    # oracle-side duplicate with a clean fault
    oracle, fault = _records(["FAILURE"], ["FAILURE", "FAILURE"])
    with pytest.raises(
        lab.ScenarioContractError,
        match="successConditions.expectedTerminalClasses must be unique",
    ):
        lab._enforce_fault_oracle_truth("ASL-V0-INF-006", oracle, fault)

    # both sides carry the same duplicate — the fault-side rejection fires
    # first, so set equality could never be reached
    oracle, fault = _records(["SUCCESS", "SUCCESS"], ["SUCCESS", "SUCCESS"])
    with pytest.raises(
        lab.ScenarioContractError,
        match="faultPlan.expectedFaultOutcome.expectedTerminalClasses must be unique",
    ):
        lab._enforce_fault_oracle_truth("ASL-V0-INF-006", oracle, fault)

    # unique terminals pass the defensive gate and reach set comparison
    oracle, fault = _records(["SUCCESS", "FAILURE"], ["FAILURE", "SUCCESS"])
    lab._enforce_fault_oracle_truth("ASL-V0-INF-006", oracle, fault)


def test_006_unique_reverse_ordered_terminals_load_and_score(tmp_path):
    """Set semantics are preserved after uniqueness passes: a non-restart/non-
    loss fault may declare ['SUCCESS','FAILURE'] against an oracle
    ['FAILURE','SUCCESS']; both load and the prediction is scored by the
    existing scorer when it ends in one of the allowed terminals."""
    sid = "ASL-V0-INF-CE8"
    oracle = _infra_oracle(sid, successConditions={
        "expectedTerminalClasses": ["FAILURE", "SUCCESS"],
        "faultExpectations": {"exact_replay": {"required": True, "expectedExactReplayZeroTool": True}},
    })
    fault = _infra_fault(sid, faultPlan={
        "injectionPoints": ["exact_replay"],
        "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS", "FAILURE"]},
    })
    prediction = _infra_prediction(sid, pid="pred-inf-ce8")
    prediction["observations"]["attempts"] = [
        a for a in prediction["observations"]["attempts"]
        if a["attemptKind"] in ("initial", "exact_replay")
    ]
    result = _score(tmp_path, [
        _scenario(_infra_input(sid), oracle, fault, prediction),
    ])["perScenario"][0]
    assert result["passed"] is True
    assert _check_by_name(result, "terminal_class")["passed"] is True
    assert _check_by_name(result, "exact_replay_zero_tool")["passed"] is True


def test_006_fault_schema_unique_positive_negative_samples():
    """Draft 2020-12 fault schema: duplicate expected terminals are rejected by
    uniqueItems and unique terminal arrays are accepted; minItems:1 and the
    terminal enum are preserved."""
    schema_path = lab.SCHEMAS_DIR / lab.SCHEMA_FILENAMES["agentic_scenario_fault_private_v0"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert Draft202012Validator.check_schema(schema) is None
    validator = Draft202012Validator(schema)
    outcome = validator.schema["properties"]["faultPlan"]["properties"] \
        ["expectedFaultOutcome"]["properties"]["expectedTerminalClasses"]
    assert outcome.get("uniqueItems") is True
    assert outcome.get("minItems") == 1
    base = {
        "scenarioId": "ASL-V0-INF-SCH",
        "trackType": "infrastructure_fault",
        "schemaVersion": lab.FAULT_SCHEMA_VERSION,
        "catalogRevision": "catalog-v0-001",
        "environmentRevision": "env-v0-001",
    }
    positive = {
        **base,
        "faultPlan": {
            "injectionPoints": ["exact_replay"],
            "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS", "FAILURE"]},
        },
    }
    negative = {
        **base,
        "faultPlan": {
            "injectionPoints": ["exact_replay"],
            "expectedFaultOutcome": {"expectedTerminalClasses": ["SUCCESS", "SUCCESS"]},
        },
    }
    assert validator.is_valid(positive)
    assert not validator.is_valid(negative)
    messages = [error.message for error in validator.iter_errors(negative)]
    assert any("non-unique" in message for message in messages)


