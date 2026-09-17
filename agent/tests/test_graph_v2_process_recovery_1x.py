import hashlib
import json

import pytest

from evaluation.graph_v2_process_recovery_1x import IDENTITY, ROOT, SOURCE_FILES, _sha, score_attempt_dir


def _write_attempt(tmp_path, mutate=None):
    identity = {"taskId": "task-1", "sessionId": "session-1"}
    result = {"boundary": "task_completed", "checkpointHash": "checkpoint-1", "mode": "restart", "restart": True, "revision": 10, "runId": "run-1", "threadId": "thread-1"}
    receipt = {"executionId": "execution-1", "fence": 1, "inboxStatus": "SUCCEEDED", "planId": "plan-1", "resultHash": "result-1", "runId": "run-1", "stateRevision": 4, "stepId": "step-1", "taskId": "task-1", "threadId": "thread-1", "toolName": "search_products", "toolOutcome": "tool_succeeded"}
    arguments = {"category": "手机", "query": "想找 iOS 二手机。"}
    obs = {
        "identity": identity, "expectedArguments": arguments,
        "pid1": {"pid": 101, "returnCode": 86, "stdout": "", "stderr": ""},
        "pid2": {"pid": 202, "returnCode": 0, "stdout": "worker output", "stderr": "", "result": result},
        "rtoMs": 1, "checkpointHash": "checkpoint-1",
        "ledger": [{"arguments": arguments, "executionId": "execution-1", "fence": 1, "pid": 303, "runId": "run-1", "taskId": "task-1", "threadId": "thread-1", "tool": "search_products"}],
        "finalState": {"taskId": "task-1", "sessionId": "session-1", "revision": 10, "activePlan": {"status": "completed"}, "domainState": {"v2ExecReceipt": receipt, "v2ExecReceiptProjection": {"projectionRevision": 9, "receiptHash": _sha(receipt)}, "stepExecutionResults": [{"planId": "plan-1", "stepId": "step-1", "outcome": "tool_succeeded", "resolvedArguments": arguments}]}},
    }
    if mutate:
        mutate(obs)
    sources = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCE_FILES}
    (tmp_path / "observations.json").write_text(json.dumps(obs, sort_keys=True), encoding="utf8")
    (tmp_path / "source-hashes.json").write_text(json.dumps(sources, sort_keys=True), encoding="utf8")
    manifest = {"identity": IDENTITY, "observationsSha256": _sha(obs), "sourceHashesSha256": _sha(sources)}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf8")
    return tmp_path


def test_dangerous_window_is_hold_until_tamper_and_revision_are_exercised(tmp_path):
    score = score_attempt_dir(_write_attempt(tmp_path))
    assert score["status"] == "HOLD"
    assert score["dangerousWindowStatus"] == "PASS"


@pytest.mark.parametrize("mutate", [
    lambda obs: obs["pid1"].update(returnCode=0),
    lambda obs: obs["ledger"][0].update(tool="compare_products"),
    lambda obs: obs["pid2"]["result"].update(boundary="stop_turn"),
    lambda obs: obs.pop("expectedArguments"),
])
def test_strict_scorer_rejects_forged_process_or_ledger_claims(tmp_path, mutate):
    score = score_attempt_dir(_write_attempt(tmp_path, mutate))
    assert score["status"] == "FAIL"


def test_strict_scorer_rejects_missing_source_hash(tmp_path):
    attempt = _write_attempt(tmp_path)
    sources = json.loads((attempt / "source-hashes.json").read_text(encoding="utf8"))
    sources.pop(next(iter(sources)))
    (attempt / "source-hashes.json").write_text(json.dumps(sources, sort_keys=True), encoding="utf8")
    manifest = json.loads((attempt / "manifest.json").read_text(encoding="utf8"))
    manifest["sourceHashesSha256"] = _sha(sources)
    (attempt / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf8")
    assert score_attempt_dir(attempt)["status"] == "FAIL"
