"""Unit tests for the Scenario Lab V0 real runner (REPAIR-004).

These tests prove the PUBLIC runner's isolation, mapping, budget, ownership,
fail-closed and command-contract behaviour, plus the REPAIR-003 repairs:

1. Production provenance — fixture prepare goes through the REAL TaskState
   store + durable graph/checkpointer APIs under ``override_task_state_client``
   (no handwritten TaskState JSON / envelope / saver-key forgery), decodes via
   the REAL ``GraphV2CheckpointSaver.aget_tuple()`` public read path and enters
   REAL ``Command(resume=...)`` recovery on the same thread/config.
2. Ownership-before-Redis — every fixture/store/drift entry verifies the owned
   identity triple (task/session/thread, mutually consistent with
   ``v2-task:<taskId>:<runId>``) BEFORE any GET/WATCH/SET; the recording-fake
   tests prove foreign task / foreign thread / task-thread mismatch / double
   drift / dangling ref all perform 0 GET / 0 WATCH / 0 SET.
3. INF-001 identity — the INF-001 client/restart/recovery reuse the SIGNED
   receipt's session (never a generated ``sessions['inf001']``); the stub
   enforces the same identity guard and rejects an old mismatched session.
4. Semantic readiness — ``derive_retrieval_chain`` requires catalog==252,
   non-empty retrieval, ES not red, Redis reachable; 404/empty-200/wrong-JSON/
   251-253 catalog/empty retrieval/red ES all fail closed; the Agent active-
   backend read-back reconciles observed health + owned durable trace.

Plus the REPAIR-004 repairs (all frozen here):

5. Receipt identity — ``load_fixture_receipt`` applies the same
   ``_require_owned_triple()`` semantics as the live fixture/drift path
   (task/session/thread owned shape, thread strictly ``v2-task:<taskId>:
   <non-empty runId>``); ``do_run`` reconciles the passed receipt against the
   frozen freeze receipt + INF-001 private fault ref/SHA + run-manifest binding
   field-by-field BEFORE copying public input, spawning the public subprocess
   or issuing any Agent request (0 subprocess on any drift); the first real
   response's taskId/threadId/runId must exactly match the receipt.
6. Active-backend attestation — live /health must report the process's
   actually-loaded ``backendBaseUrl``/``productRetrievalMode`` (field names
   fixed in ``HealthResponse``) and the read-back reconciles them field-by-field
   against ``http://127.0.0.1:18082`` + ``bm25``; a read-only minimal projection
   of the owned persisted ``task-state:<taskId>`` ``stepExecutionResults``
   yields only successful ``search_products`` identity + ``retrievalTrace.
   channels`` status (never raw facts/prompt/candidates/private truth); PASS
   requires ES active + BM25 active + no active mysqlFallback; bare health,
   wrong backend/mode, missing/unowned search, executor-only, ES fallback/
   degraded and foreign TaskState/trace all BLOCK.

They deliberately do NOT fake a real end-to-end run: no real model, no real
retrieval, no real durable graph.  The offline pipeline test drives the REAL
public-only subprocess in ``--stub`` mode and the ACCEPTED scorer subprocess,
proving the orchestration contract end-to-end.  The real loop is executed by
the CLI's freeze/run/score phases and reported separately in the acceptance
document.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

AGENT_DIR = Path(__file__).resolve().parents[1]
if str(AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(AGENT_DIR))

from evaluation import scenario_lab_v0 as lab  # noqa: E402
from evaluation import scenario_lab_v0_real_runner as runner  # noqa: E402
from evaluation.scenario_lab_v0_real_runner import (  # noqa: E402
    CATALOG_EXPECTED_COUNT,
    DurableChatClient,
    DurableResponse,
    FakeCheckpointFixtureClient,
    FakeTaskStateDriftAdapter,
    FIXTURE_PENDING_QUESTION,
    FixtureReceipt,
    FixtureReceiptError,
    INF_001_MESSAGE,
    INF_004_MESSAGE,
    ObservationMapper,
    PhaseGuard,
    PhaseGuardError,
    PublicInputLeakError,
    RedisCheckpointFixtureClient,
    RedisEvidenceAdapter,
    RedisEvidenceError,
    RedisTaskStateDriftAdapter,
    SEARCH_JAVA_BASE_URL,
    StubDurableClient,
    StubEvidenceAdapter,
    StubServerHost,
    StubWorld,
    TASK_OWNED_KEY_PREFIXES,
    TASK_OWNED_SESSION_PREFIX,
    agent_active_backend_readback,
    derive_retrieval_chain,
    drive_language_view,
    load_fixture_receipt,
    load_public_input,
    normalize_body,
    path_has_private_truth_marker,
    preflight_probe,
    proposal_hash,
    public_environment_has_private_truth,
    public_subprocess_env,
    run_manifest_blueprint,
    server_env_overrides,
    session_owner_hash,
    sha256_text,
    verify_active_backend,
    write_fixture_receipt,
)
from scripts import run_scenario_lab_v0_real_runner as cli  # noqa: E402

DATA_DIR = (
    AGENT_DIR.parent / "data" / "derived" / "scenario_lab_v0_real_runner_20260819"
)

SCENARIO_IDS = {
    "ASL-V0-LNG-REAL-001",
    "ASL-V0-LNG-REAL-002",
    "ASL-V0-LNG-REAL-003",
    "ASL-V0-LNG-REAL-004",
    "ASL-V0-LNG-REAL-005",
    "ASL-V0-LNG-REAL-006",
    "ASL-V0-LNG-REAL-007",
    "ASL-V0-LNG-REAL-008",
    "ASL-V0-INF-REAL-001",
    "ASL-V0-INF-REAL-002",
    "ASL-V0-INF-REAL-003",
    "ASL-V0-INF-REAL-004",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def response(body: dict, request_body: dict | None = None) -> DurableResponse:
    request_body = request_body or {}
    raw_request = json.dumps(request_body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw_response = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return DurableResponse(
        request_id=(body.get("trace") or {}).get("requestId") or "req-test",
        http_status=200,
        body=body,
        request_body=request_body,
        raw_request_bytes=raw_request,
        raw_response_bytes=raw_response,
    )


def _task_state(*, revision: int = 2, questions: list[str] | None = None,
                task_id: str = "task-t1") -> dict:
    return {
        "taskId": task_id,
        "taskType": "ecommerce_guide",
        "sessionId": "slv0r1-test",
        "status": "in_progress",
        "revision": revision,
        "goal": "g",
        "facts": [],
        "constraints": [],
        "unknowns": [],
        "pendingQuestions": questions or [],
        "domainState": {},
        "createdAt": "2026-08-19T00:00:00Z",
        "updatedAt": "2026-08-19T00:00:00Z",
    }


def _trace_summary(*, final_action: str = "task_completed", agent_status: str = "ok",
                   phases: list[str] | None = None, tool_call_count: int = 0,
                   degraded: bool = False, failure_code: str | None = None) -> dict:
    return {
        "runId": "run-1",
        "phase_count": 1,
        "tool_call_count": tool_call_count,
        "totalDurationMs": 12.0,
        "degraded": degraded,
        "agentStatus": agent_status,
        "finalAction": final_action,
        "failureCode": failure_code,
        "phases": [{"phase": p, "outcome": "ok", "durationMs": 1.0} for p in (phases or [])],
    }


def _alias_body(**overrides) -> dict:
    """A realistic durable response using the server's serialized aliases."""
    body = {
        "answer": "推荐结果",
        "tool_trace": [{"tool_name": "search_products", "ok": True, "duration_ms": 3.0}],
        "trace": {"requestId": "req-alias-1", "runId": "run-1", "status": "ok", "agentStatus": "ok"},
        "runId": "run-1",
        "traceSummary": _trace_summary(tool_call_count=1, phases=["planner", "executor"]),
        "taskState": _task_state(),
        "guideResult": None,
        "taskRelation": None,
    }
    body.update(overrides)
    return body


def _fixture(fixture_id: str = "test") -> tuple[StubWorld, FixtureReceipt]:
    """Deterministic offline fixture: world + self-signed receipt."""
    world = StubWorld()
    client = FakeCheckpointFixtureClient(world)
    receipt = client.park_clarification_fixture(fixture_id=fixture_id)
    return world, receipt


def _freeze(tmp_path: Path, *, fixture_id: str = "test") -> tuple[StubWorld, Path]:
    """prepare -> freeze into tmp_path; returns (world, receipt path)."""
    world = StubWorld()
    client = FakeCheckpointFixtureClient(world)
    receipt_path = cli.prepare_fixture_to_receipt(
        client, tmp_path / "fixtures", fixture_id=fixture_id
    )
    assert cli.do_freeze(tmp_path, fixture_receipt_path=receipt_path) == 0
    return world, receipt_path


@pytest.fixture
def runtime(tmp_path, monkeypatch) -> Path:
    """Hermetic task-owned runtime root for the orchestrator run phase."""
    root = tmp_path / "runtime"
    monkeypatch.setattr(cli, "RUNTIME_ROOT", root)
    return root


def _up_probe() -> dict:
    return {
        "targets": {
            "searchJavaReadiness": {"name": "searchJavaReadiness", "kind": "backend", "reachable": True, "httpStatus": 200, "healthStatus": "UP", "healthParsed": True},
            "searchJavaCatalog": {"name": "searchJavaCatalog", "kind": "catalog", "reachable": True, "httpStatus": 200, "catalogDocCount": 252, "catalogParsed": True},
            "retrievalSmoke": {"name": "retrievalSmoke", "kind": "retrieval-smoke", "reachable": True, "httpStatus": 200, "retrievedCount": 3, "retrievalParsed": True},
            "elasticsearch": {"name": "elasticsearch", "kind": "index", "reachable": True, "httpStatus": 200, "esStatus": "green", "esHealthParsed": True},
            "benchmarkJava": {"name": "benchmarkJava", "kind": "control", "reachable": True, "httpStatus": 200, "healthStatus": "UP", "healthParsed": True},
            "redis": {"name": "redis", "kind": "store", "reachable": True},
        },
        "derived": {"retrievalChainUp": True, "backendBaseUrl": runner.SEARCH_JAVA_BASE_URL},
    }


def _down_probe() -> dict:
    return {
        "targets": {
            "searchJavaReadiness": {"name": "searchJavaReadiness", "kind": "backend", "reachable": False, "healthParsed": False},
            "searchJavaCatalog": {"name": "searchJavaCatalog", "kind": "catalog", "reachable": False, "catalogParsed": False},
            "retrievalSmoke": {"name": "retrievalSmoke", "kind": "retrieval-smoke", "reachable": False, "retrievalParsed": False},
            "elasticsearch": {"name": "elasticsearch", "kind": "index", "reachable": False, "esHealthParsed": False},
            "benchmarkJava": {"name": "benchmarkJava", "kind": "control", "reachable": False, "healthParsed": False},
            "redis": {"name": "redis", "kind": "store", "reachable": False},
        },
        "derived": {"retrievalChainUp": False, "backendBaseUrl": runner.SEARCH_JAVA_BASE_URL},
    }


def _run_entrypoint(input_path: Path, outdir: Path, *, extra_args: tuple = (), extra_env: dict | None = None):
    """Run the REAL public-only entrypoint with a hermetic clean env."""
    env: dict[str, str] = {}
    for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "COMSPEC", "PATHEXT",
                "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER",
                "OS", "WINDIR", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    env["PYTHONPATH"] = str(AGENT_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra_env:
        env.update(extra_env)
    outdir.mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        [sys.executable, str(cli.ENTRYPOINT), "--input", str(input_path),
         "--outdir", str(outdir), *extra_args],
        cwd=str(outdir), env=env, capture_output=True, text=True, timeout=300,
    )


# ---------------------------------------------------------------------------
# public/private isolation
# ---------------------------------------------------------------------------


def test_public_loader_accepts_clean_frozen_input(tmp_path):
    world, receipt = _fixture()
    inputs, oracles, faults = cli.build_frozen_records(cli.derive_catalog(), fixture_receipt=receipt)
    for record in inputs:
        lab.validate_record(record, "input")
    cli.write_jsonl_atomic(tmp_path / "scenario.input.jsonl", inputs)
    views = load_public_input(tmp_path / "scenario.input.jsonl")
    assert {v.scenario_id for v in views} == SCENARIO_IDS
    # LNG views carry user turns; INF views carry setupRef and no turns.
    lng = {v.scenario_id: v for v in views if v.track_type == "language_e2e"}
    inf = {v.scenario_id: v for v in views if v.track_type == "infrastructure_fault"}
    assert len(lng) == 8 and len(inf) == 4
    assert all(v.setup_ref is None for v in lng.values())
    assert all(v.setup_ref and not v.turns for v in inf.values())


def test_public_loader_fails_closed_on_smuggled_answer_face(tmp_path):
    record = cli._input_record(
        "ASL-V0-LNG-REAL-001", "language_e2e",
        [{"role": "user", "text": "acceptableProductId 不应该出现在公共输入", "turnId": "t1"}],
    )
    with pytest.raises(PublicInputLeakError):
        runner._assert_no_private_markers(record, record["scenarioId"])


def test_public_loader_fails_closed_on_private_path_marker(tmp_path):
    record = cli._input_record(
        "ASL-V0-LNG-REAL-002", "language_e2e",
        [{"role": "user", "text": "请读取 /private/oracle.private.jsonl 之后再回答", "turnId": "t1"}],
    )
    with pytest.raises(PublicInputLeakError):
        runner._assert_no_private_markers(record, record["scenarioId"])


def test_public_loader_allows_schema_enum_constants():
    # trackType "infrastructure_fault" and setupRef are schema constants, not leaks.
    record = cli._input_record(
        "ASL-V0-INF-REAL-001", "infrastructure_fault", [], setup_ref="fixture.slv0r1-inf001"
    )
    runner._assert_no_private_markers(record, record["scenarioId"])  # must not raise


def test_runner_module_never_opens_private_truth_or_scores():
    """The runner must never OPEN the private truth files nor invoke the
    scorer.  The private file NAMES appear ONLY inside the leak-detector
    constants (FORBIDDEN_PRIVATE_FILENAMES / the scan patterns) — that is the
    detector's surface, never a read/import of the private content."""
    source = Path(runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Lines belonging to the detector constants (multi-line set literal etc.).
    allowed_lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Name) and target.id in {
                "FORBIDDEN_PRIVATE_FILENAMES",
                "_PRIVATE_TRUTH_MARKER_PATTERN",
                "_PRIVATE_TRUTH_PATH_PATTERN",
            }:
                allowed_lines.update(range(node.lineno, node.end_lineno + 1))

    for i, line in enumerate(source.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue  # inert explanatory prose
        if "oracle.private" in line or "fault.private" in line:
            assert i in allowed_lines, f"line {i}: {line}"

    # No scorer entry points: the runner cannot compute against the oracle.
    for symbol in ("ScenarioSet", "score_scenario_set", "load_records"):
        assert symbol not in source, symbol


def test_run_manifest_blueprint_has_no_private_fields():
    blueprint = run_manifest_blueprint()
    assert blueprint["taskId"] == "AGENTIC-SCENARIO-LAB-V0-REAL-RUNNER-REPAIR-004"
    assert blueprint["attempt"] == "004"
    assert blueprint["phases"] == []
    dumped = json.dumps(blueprint, ensure_ascii=False)
    assert "acceptableProductIds" not in dumped


def test_path_and_env_private_truth_detectors():
    assert path_has_private_truth_marker(r"C:\truth\oracle.private.jsonl")
    assert path_has_private_truth_marker("expectedTerminal")
    assert not path_has_private_truth_marker(r"C:\data\products.jsonl")
    assert not path_has_private_truth_marker("default")  # "fault" substring must NOT trip
    bad = public_environment_has_private_truth(
        {"ORACLE_PRIVATE_PATH": r"C:\truth", "AGENT_ORACLE_FILE": r"C:\o",
         "PATH": r"C:\bin", "HOME": r"C:\home"})
    assert "ORACLE_PRIVATE_PATH" in bad and "AGENT_ORACLE_FILE" in bad
    assert public_environment_has_private_truth({"PATH": "ok", "HOME": "ok"}) == []


def test_public_entrypoint_import_closure_is_stdlib_plus_runner():
    """The public-only entrypoint may import ONLY the runner module + stdlib."""
    src = Path(cli.ENTRYPOINT).read_text(encoding="utf-8")
    tree = ast.parse(src)
    stdlib = {
        "argparse", "json", "os", "sys", "time", "pathlib", "typing",
        "dataclasses", "re", "hashlib", "uuid", "socket", "datetime",
        "collections", "subprocess", "functools", "itertools", "concurrent",
        "__future__",
    }
    allowed_top = {"evaluation.scenario_lab_v0_real_runner"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in stdlib, f"non-stdlib import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module in allowed_top:
                continue
            assert node.module is not None and node.module.split(".")[0] in stdlib, \
                f"non-stdlib import: {node.module}"


# ---------------------------------------------------------------------------
# mapping (synthetic real-shaped server responses)
# ---------------------------------------------------------------------------


def test_classify_task_completed_success():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = _alias_body(answer="推荐结果", traceSummary=_trace_summary(
        final_action="task_completed", phases=["planner", "executor", "validator"], tool_call_count=2))
    c = mapper.classify(body)
    assert c.terminal_code == "SUCCESS"
    assert c.tool_calls == 2
    assert c.model_calls == 1  # planner invokes the model; executor/validator do not
    assert c.rejected is False
    assert c.task_id == "task-t1"
    assert c.thread_id == "v2-task:task-t1:run-1"
    assert c.run_id == "run-1"
    assert c.revision_after == 2


def test_classify_accepts_field_name_spellings():
    # Some server responses historically used python field names; normalize must
    # accept both alias and field-name keys.
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = {
        "answer": "x",
        "run_id": "run-2",
        "trace_summary": _trace_summary(final_action="ask_user", phases=["planner"]),
        "task_state": _task_state(revision=3),
    }
    c = mapper.classify(body)
    assert c.terminal_code == "CLARIFICATION_REQUIRED"
    assert c.run_id == "run-2"
    assert c.revision_after == 3


def test_classify_resume_rejected_fail_closed():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = _alias_body(
        answer="当前续答被拒绝",
        traceSummary=_trace_summary(final_action="resume_rejected", agent_status="failed", failure_code="revision_mismatch"),
    )
    c = mapper.classify(body, task_id="task-t1")
    assert c.terminal_code == "ABSTAIN_OR_EXPLAIN"
    assert c.rejected is True
    assert c.failure_code == "revision_mismatch"
    assert c.tool_calls == 0


def test_classify_idempotent_replay_placeholder_and_override():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = _alias_body(
        answer="已应用的答案",
        traceSummary=_trace_summary(final_action="idempotent_resume_replay", phases=[]),
    )
    c = mapper.classify(body, task_id="task-t1")
    assert c.terminal_code == ""  # placeholder; caller supplies referenced terminal
    attempt = mapper.build_attempt(
        attempt_kind="exact_replay", classified=c, request=response(body),
        payload_sha256="a" * 64, result_sha256="b" * 64,
        snapshot_before={"revision": 2, "checkpointCount": 3},
        snapshot_after={"revision": 2, "checkpointCount": 3},
        thread_id="v2-task:task-t1:run-1", referenced_terminal="SUCCESS",
    )
    assert attempt["terminalCode"] == "SUCCESS"
    assert attempt["toolCalls"] == 0 and attempt["modelCalls"] == 0


def test_classify_ownership_rejection():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = {"answer": runner._OWNERSHIP_REJECTED_ANSWER,
            "trace": {"requestId": "req-own", "status": "error"}}
    c = mapper.classify(body)
    assert c.terminal_code == "ABSTAIN_OR_EXPLAIN"
    assert c.rejected is True
    assert c.rejection_code == "durable_session_ownership_rejected"


def test_classify_degraded_no_terminal():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = _alias_body(traceSummary=_trace_summary(
        final_action=None, degraded=True, failure_code="retrieval_unavailable"))
    c = mapper.classify(body)
    assert c.terminal_code == "ABSTAIN_OR_EXPLAIN"


def test_result_hash_deterministic_and_terminal_override():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = _alias_body(answer="同一答案")
    c = mapper.classify(body, task_id="task-t1")
    h1 = mapper.result_hash(body, c)
    h2 = mapper.result_hash(body, c)
    assert h1 == h2
    assert len(h1) == 64
    # terminal_override replaces the empty idempotent placeholder.
    replay = _alias_body(answer="同一答案", traceSummary=_trace_summary(final_action="idempotent_resume_replay", phases=[]))
    c_replay = mapper.classify(replay, task_id="task-t1")
    assert mapper.result_hash(replay, c_replay, terminal_override="SUCCESS") == mapper.result_hash(
        _alias_body(answer="同一答案"), mapper.classify(_alias_body(answer="同一答案"), task_id="task-t1")
    )


def test_result_hash_and_clarifications_fail_closed_on_missing_http_body():
    mapper = ObservationMapper(session_id="slv0r1-test")
    classified = mapper.classify(None)
    assert classified.terminal_code == "FAILURE"
    digest = mapper.result_hash(None, classified)
    assert re.fullmatch(r"[a-f0-9]{64}", digest)
    assert mapper.result_hash({}, classified) == digest
    assert mapper.build_clarifications(None) == []


def test_proposal_hash_mirrors_server():
    # Deterministic over the visible identity — used to echo the server proposal.
    h = proposal_hash("task-1", "预算多少？", 2)
    assert isinstance(h, str) and len(h) == 16
    assert h == proposal_hash("task-1", "预算多少？", 2)


def test_build_tool_calls_reads_tool_name():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = _alias_body(tool_trace=[
        {"tool_name": "search_products", "ok": True},
        {"tool_name": "compare_products", "ok": False},
    ])
    calls = mapper.build_tool_calls(body)
    assert [c["toolName"] for c in calls] == ["search_products", "compare_products"]
    assert calls[1]["ok"] is False


def test_build_ranked_uses_validator_published_order_not_candidate_pool():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = _alias_body(guideResult={
        "products": [
            {"product": {"id": "P100", "title": "x", "brand": "y"}},
            {"product": {"id": "P200", "title": "z", "brand": "q"}},
        ],
        "candidatePoolIds": ["P100", "P200", "P-ELIMINATED"],
    })
    assert mapper.build_ranked(body) == ["P100", "P200"]


def test_build_claimed_constraints_projects_server_owned_brand_avoidance():
    mapper = ObservationMapper(session_id="slv0r1-test")
    task = _task_state()
    task["domainState"] = {
        "shoppingGuide": {
            "requirements": [],
            "brandAvoidances": [{
                "values": ["apple"], "strength": "hard", "source": "user",
            }],
        }
    }
    body = _alias_body(taskState=task)

    assert mapper.build_claimed_constraints(body) == {
        "hard": [{
            "group": "brand",
            "operator": "NOT_IN",
            "allowedValues": ["苹果/apple"],
        }],
        "soft": [],
    }


def test_build_clarifications_reads_pending_questions():
    mapper = ObservationMapper(session_id="slv0r1-test")
    body = _alias_body(taskState=_task_state(questions=["预算多少？", "要多大内存？"]))
    clar = mapper.build_clarifications(body)
    assert [c["question"] for c in clar] == ["预算多少？", "要多大内存？"]
    assert [c["turnIndex"] for c in clar] == [0, 1]


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------


def test_redis_adapter_refuses_non_owned_key():
    adapter = RedisEvidenceAdapter("redis://127.0.0.1:6379/0")
    try:
        with pytest.raises(RedisEvidenceError):
            adapter._require_owned_key("user-session:abc")
        with pytest.raises(RedisEvidenceError):
            adapter._require_owned_key("")
        # owned prefixes are accepted at the guard layer
        assert adapter._require_owned_key("task-state:task-t1") == "task-state:task-t1"
    finally:
        adapter.close()


def test_redis_adapter_refuses_key_outside_owned_scope():
    adapter = RedisEvidenceAdapter("redis://127.0.0.1:6379/0")
    try:
        with pytest.raises(RedisEvidenceError):
            adapter._require_owned_key("task-state:task-t1", owner_scope="task-OTHER")
    finally:
        adapter.close()


def test_task_owned_prefix_is_narrow():
    assert TASK_OWNED_SESSION_PREFIX == "slv0r1"
    for prefix in TASK_OWNED_KEY_PREFIXES:
        assert prefix in {
            "task-state:", "task-state-session:", "graph-v2:cp:", "graph-v2:cursor:",
            "graph-v2:terminal:", "agent-run-trace:",
        }


def test_owned_store_refuses_keys_outside_task_namespace():
    """REPAIR-003: the store's guard verifies the OWNED identity ATOM embedded
    in each task-owned key — a bare prefix match on a foreign/non-owned key is
    refused (ownership-before-I/O)."""
    store = runner._OwnedDurableStore(redis_url=None)
    owned_task = "task-" + "a" * 16  # production store id shape
    owned_session = f"{TASK_OWNED_SESSION_PREFIX}-sess"
    owned_thread = f"v2-task:{owned_task}:run-1"
    try:
        with pytest.raises(RedisEvidenceError):
            store._guard("user-session:abc")
        with pytest.raises(RedisEvidenceError):
            store._guard("")
        # Bare prefix match on a foreign/non-owned task id is refused.
        with pytest.raises(RedisEvidenceError):
            store._guard("task-state:foreign-user-task")
        # A production-shaped-but-unregistered id is still OWNED-shaped, so the
        # store accepts it at the key-shape layer (the THIS-run ledger is the
        # drift adapter's job, not the read-only store's).
        assert store._guard(f"task-state:{owned_task}") == f"task-state:{owned_task}"
        assert store._guard(f"task-state-session:{owned_session}") == f"task-state-session:{owned_session}"
        assert store._guard(f"graph-v2:cp:{owned_thread}::cp:c1") == f"graph-v2:cp:{owned_thread}::cp:c1"
        assert store._guard(f"graph-v2:cursor:{owned_task}") == f"graph-v2:cursor:{owned_task}"
        # Opaque identity-keys (terminal / agent-run-trace) are refused by shape
        # alone — their identity is only verifiable from the stored VALUE.
        with pytest.raises(RedisEvidenceError):
            store._guard("graph-v2:terminal:abc")
        with pytest.raises(RedisEvidenceError):
            store._guard("agent-run-trace:abc")
    finally:
        store.close()


def test_durable_client_validates_session_length():
    with pytest.raises(ValueError):
        DurableChatClient("http://127.0.0.1:1", "")
    with pytest.raises(ValueError):
        DurableChatClient("http://127.0.0.1:1", "x" * 65)


# ---------------------------------------------------------------------------
# REPAIR-002 Repair 1/2 — process boundary + backend probing
# ---------------------------------------------------------------------------


def test_public_subprocess_env_is_allowlisted_and_no_private_markers():
    base = {
        "PATH": r"C:\Python", "HOME": r"C:\Users\x",
        "ORACLE_PRIVATE_PATH": r"C:\truth", "AGENT_ORACLE_FILE": r"C:\o",
    }
    env = public_subprocess_env(base, base_url=runner.SEARCH_JAVA_BASE_URL,
                                redis_url="redis://127.0.0.1:6379/0")
    for key in ("HOME", "ORACLE_PRIVATE_PATH", "AGENT_ORACLE_FILE"):
        assert key not in env
    assert env["BACKEND_BASE_URL"] == runner.SEARCH_JAVA_BASE_URL
    assert env["PRODUCT_RETRIEVAL_MODE"] == "bm25"
    assert env["REDIS_URL"] == "redis://127.0.0.1:6379/0"
    assert env[runner.EXECUTOR_FAULT_POINT_ENV] == ""
    assert env["PYTHONPATH"] == str(AGENT_DIR)
    assert env["PYTHONIOENCODING"] == "utf-8"
    for value in env.values():
        assert not path_has_private_truth_marker(value)
    assert public_environment_has_private_truth(env) == []


def test_server_env_overrides_points_to_real_durable_backend():
    env = server_env_overrides(redis_url="redis://127.0.0.1:6379/0",
                               backend_base_url=runner.SEARCH_JAVA_BASE_URL)
    assert env["BACKEND_BASE_URL"] == runner.SEARCH_JAVA_BASE_URL == "http://127.0.0.1:18082"
    assert env["PRODUCT_RETRIEVAL_MODE"] == "bm25"
    assert env["REDIS_URL"] == "redis://127.0.0.1:6379/0"
    assert env["AGENT_GRAPH_V2_DURABLE_ENABLED"] == "true"
    assert env[runner.EXECUTOR_FAULT_POINT_ENV] == ""
    assert env["USED_PHONE_SYNTHETIC_PRICE_POLICY"] == "budget_and_ranking"
    assert Path(env["USED_PHONE_SYNTHETIC_PRICE_DIR"]).resolve() == (
        AGENT_DIR.parent
        / "data"
        / "derived"
        / "ecommerce"
        / "used_phone_synthetic_reference_price_v1"
    ).resolve()
    # Never the pydantic default.
    assert env["BACKEND_BASE_URL"] != "http://localhost:8080"
    # Real model key is read from the repo .env but never printed here.
    if "DEEPSEEK_API_KEY" in env:
        assert env["DEEPSEEK_API_KEY"].startswith("sk-")


def test_probe_targets_point_to_real_chain_not_localhost8080():
    for target in runner.DEFAULT_PROBE_TARGETS:
        assert not target.url.startswith("http://localhost:8080")
    assert any(runner.SEARCH_JAVA_BASE_URL in t.url for t in runner.DEFAULT_PROBE_TARGETS)
    assert any(runner.ES_BASE_URL in t.url for t in runner.DEFAULT_PROBE_TARGETS)
    assert any(runner.BENCHMARK_JAVA_BASE_URL in t.url for t in runner.DEFAULT_PROBE_TARGETS)
    catalog = next(t for t in runner.DEFAULT_PROBE_TARGETS if t.name == "searchJavaCatalog")
    retrieval = next(t for t in runner.DEFAULT_PROBE_TARGETS if t.name == "retrievalSmoke")
    assert catalog.method == "GET"
    assert catalog.url.startswith(f"{runner.SEARCH_JAVA_BASE_URL}/api/products?")
    assert "category=" in catalog.url and "limit=1500" in catalog.url
    assert retrieval.method == "GET"
    assert retrieval.url.startswith(f"{runner.SEARCH_JAVA_BASE_URL}/api/products/retrieval?")
    assert "query=" in retrieval.url and "category=" in retrieval.url and "limit=3" in retrieval.url
    assert "/api/products/catalog" not in catalog.url


def test_verify_active_backend_derives_conclusions_from_evidence():
    assert verify_active_backend(
        _up_probe(), base_url=runner.SEARCH_JAVA_BASE_URL,
        redis_url="redis://127.0.0.1:6379/0") is True
    # A probed chain at :18082 configured as localhost:8080 fails closed.
    assert verify_active_backend(_up_probe(), base_url="http://localhost:8080", redis_url=None) is False
    # ES down -> retrievalChainUp false -> fail closed.
    es_down = _up_probe()
    es_down["targets"]["elasticsearch"]["reachable"] = False
    es_down["derived"]["retrievalChainUp"] = False
    assert verify_active_backend(es_down, base_url=runner.SEARCH_JAVA_BASE_URL, redis_url=None) is False
    # Redis down -> fail closed when Redis is supplied.
    redis_down = _up_probe()
    redis_down["targets"]["redis"]["reachable"] = False
    assert verify_active_backend(
        redis_down, base_url=runner.SEARCH_JAVA_BASE_URL,
        redis_url="redis://127.0.0.1:6379/0") is False
    # A probe that never concluded (missing derived) fails closed.
    assert verify_active_backend({"targets": {}}, base_url=runner.SEARCH_JAVA_BASE_URL) is False


def test_preflight_probe_derives_conclusion_from_evidence_only(monkeypatch):
    """Each probe conclusion reuses ONE captured response — the fake records
    exactly one request per target (no probe-then-second-request race)."""
    captured: list[str] = []

    def _fake_probe(url, *, method="GET", payload=None, timeout=3.0, kind=""):
        captured.append(url)
        result = {"url": url, "method": method, "kind": kind, "reachable": True,
                  "httpStatus": 200, "errorType": None, "latencyMs": 1.0,
                  "timestamp": "2026-08-19T00:00:00+00:00", "source": "probe_http"}
        if kind == "catalog":
            result["catalogDocCount"] = 252
            result["catalogParsed"] = True
        elif kind == "retrieval-smoke":
            result["retrievedCount"] = 3
            result["retrievalParsed"] = True
        elif kind == "index":
            result["esStatus"] = "green"
            result["esHealthParsed"] = True
        elif kind in ("backend", "control", "control-plane"):
            result["healthStatus"] = "UP"
            result["healthParsed"] = True
        return result

    monkeypatch.setattr(runner, "probe_http", _fake_probe)
    targets = [
        runner.BackendProbeTarget("searchJavaReadiness", f"{runner.SEARCH_JAVA_BASE_URL}/actuator/health/readiness", "backend", "GET"),
        runner.BackendProbeTarget("searchJavaCatalog", f"{runner.SEARCH_JAVA_BASE_URL}/api/products?category=phone&limit=1500", "catalog", "GET"),
        runner.BackendProbeTarget("retrievalSmoke", f"{runner.SEARCH_JAVA_BASE_URL}/api/products/retrieval?query=phone&limit=3", "retrieval-smoke", "GET"),
        runner.BackendProbeTarget("elasticsearch", f"{runner.ES_BASE_URL}/_cluster/health", "index", "GET"),
    ]
    evidence = preflight_probe(targets=targets, redis_url=None, app_base_url=None)
    assert evidence["derived"]["retrievalChainUp"] is True
    assert evidence["derived"]["backendBaseUrl"] == runner.SEARCH_JAVA_BASE_URL
    for name in ("searchJavaReadiness", "searchJavaCatalog", "retrievalSmoke", "elasticsearch"):
        assert evidence["targets"][name]["reachable"] is True
        assert evidence["targets"][name]["healthParsed" if name in ("searchJavaReadiness",) else
               ("catalogParsed" if name == "searchJavaCatalog" else
                ("retrievalParsed" if name == "retrievalSmoke" else "esHealthParsed"))] is True
    # Each target captured EXACTLY once — the derivation never re-probes.
    for name in ("searchJavaReadiness", "searchJavaCatalog", "retrievalSmoke", "elasticsearch"):
        url = evidence["targets"][name]["url"]
        assert captured.count(url) == 1


# ---------------------------------------------------------------------------
# REPAIR-002 Repair 3 — checkpoint fixture (prepare -> freeze -> run binding)
# ---------------------------------------------------------------------------


def test_fixture_receipt_round_trips_signed(tmp_path):
    world, receipt = _fixture()
    assert receipt.owner_prefix == TASK_OWNED_SESSION_PREFIX
    assert re.fullmatch(r"[a-f0-9]{64}", receipt.checkpoint_sha256)
    assert receipt.checkpoint_ref.startswith("cp-")
    path = write_fixture_receipt(tmp_path / "receipt.json", receipt)
    loaded = load_fixture_receipt(path)
    assert loaded.checkpoint_ref == receipt.checkpoint_ref
    assert loaded.checkpoint_sha256 == receipt.checkpoint_sha256
    assert loaded.receipt_sha256 == receipt.receipt_sha256


def test_fixture_prepare_freeze_binds_real_ref_sha(tmp_path):
    world, receipt_path = _freeze(tmp_path)
    receipt = load_fixture_receipt(receipt_path)
    assert receipt.checkpoint_ref.startswith("cp-")
    assert re.fullmatch(r"[a-f0-9]{64}", receipt.checkpoint_sha256)
    faults = lab.load_records(tmp_path / "fault.private.jsonl", "fault")
    inf001 = next(r for r in faults if r["scenarioId"] == "ASL-V0-INF-REAL-001")
    # The old always-mismatching SENTINEL is gone: the fault freezes the REAL
    # fixture ref/SHA from the receipt.
    assert inf001["faultPlan"]["setupFixture"]["checkpointFixtureRef"] == receipt.checkpoint_ref
    assert inf001["faultPlan"]["setupFixture"]["expectedCheckpointContentsSha256"] == receipt.checkpoint_sha256


def test_build_frozen_records_fails_closed_without_receipt():
    with pytest.raises(FixtureReceiptError):
        cli.build_frozen_records(cli.derive_catalog(), fixture_receipt=None)


def test_do_freeze_fails_closed_without_receipt(tmp_path):
    with pytest.raises(FixtureReceiptError):
        cli.do_freeze(tmp_path, fixture_receipt_path=None)


def test_fixture_receipt_fails_closed_on_missing(tmp_path):
    with pytest.raises(FixtureReceiptError):
        load_fixture_receipt(tmp_path / "missing.json")


def test_fixture_receipt_fails_closed_on_signature_drift(tmp_path):
    world, receipt = _fixture()
    path = tmp_path / "receipt.json"
    data = dict(receipt.to_dict())
    data["checkpointRef"] = "cp-tampered"  # tamper WITHOUT re-signing
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(FixtureReceiptError):
        load_fixture_receipt(path)


def test_fixture_receipt_fails_closed_on_cross_owner(tmp_path):
    world, receipt = _fixture()
    cross = FixtureReceipt.from_dict({**receipt.to_dict(), "ownerPrefix": "other", "receiptSha256": ""})
    path = cli.write_fixture_receipt(tmp_path / "receipt2.json", cross)
    with pytest.raises(FixtureReceiptError) as excinfo:
        load_fixture_receipt(path)
    assert "cross-owner" in str(excinfo.value)


def test_fixture_receipt_fails_closed_on_bad_sha_shape(tmp_path):
    world, receipt = _fixture()
    bad = FixtureReceipt.from_dict({**receipt.to_dict(), "checkpointSha256": "xyz", "receiptSha256": ""})
    path = cli.write_fixture_receipt(tmp_path / "receipt3.json", bad)
    with pytest.raises(FixtureReceiptError) as excinfo:
        load_fixture_receipt(path)
    assert "64 hex" in str(excinfo.value)


def test_prepare_fixture_receipt_resigns_created_code_sha(tmp_path):
    world = StubWorld()
    client = FakeCheckpointFixtureClient(world, created_code_sha256="a" * 64)
    path = cli.prepare_fixture_to_receipt(client, tmp_path / "fixtures", fixture_id="rc",
                                          created_code_sha256="b" * 64)
    receipt = load_fixture_receipt(path)
    assert receipt.created_code_sha256 == "b" * 64


def test_recovery_source_checkpoint_binds_only_on_exact_ref():
    world, receipt = _fixture()
    snap = {"revision": 1, "checkpointCount": 1,
            "checkpointRef": receipt.checkpoint_ref, "checkpointSha256": receipt.checkpoint_sha256}
    assert runner._recovery_source_checkpoint(receipt, snap) == (receipt.checkpoint_ref, receipt.checkpoint_sha256)
    # A LATER checkpoint (not the frozen fixture ref) fails the binding closed.
    later = dict(snap, checkpointRef="cp-after", checkpointSha256="a" * 64)
    assert runner._recovery_source_checkpoint(receipt, later) == (None, None)
    # Missing receipt / missing snapshot / non-mapping all fail closed.
    assert runner._recovery_source_checkpoint(None, snap) == (None, None)
    assert runner._recovery_source_checkpoint(receipt, None) == (None, None)
    assert runner._recovery_source_checkpoint(receipt, "not-a-mapping") == (None, None)


def test_inf001_stub_restart_binds_recovered_fixture_ref(tmp_path):
    """REPAIR-003: INF-001's client/restart/recovery reuse the SIGNED receipt's
    session (never a generated ``sessions['inf001']``)."""
    world, receipt = _fixture()
    mapper = ObservationMapper(session_id="")
    adapter = StubEvidenceAdapter(world)

    def client_factory(sid):
        return StubDurableClient("http://stub", sid, world=world)

    server1 = StubServerHost(world)
    restart_server = StubServerHost(world)
    result = runner.drive_inf001(
        server1, restart_server, client_factory(receipt.session_id), adapter, mapper, tmp_path,
        fixture_receipt=receipt,
        restart_client_factory=client_factory,
    )
    observed = result["observed"]
    assert result["taskId"] == receipt.task_id
    assert result["threadId"] == receipt.thread_id
    assert observed["recoverySourceFixtureRef"] == receipt.checkpoint_ref
    assert observed["recoverySourceFixtureSha256"] == receipt.checkpoint_sha256
    restart = [a for a in result["attempts"] if a["attemptKind"] == "restart_recovery"][0]
    assert restart["checkpointRef"] == receipt.checkpoint_ref
    assert restart["checkpointSha256"] == receipt.checkpoint_sha256
    assert restart["terminalCode"] == "SUCCESS"
    assert world.servers_killed == 1 and world.restart_occurred is True


def test_inf001_stub_restart_without_receipt_fails_binding_closed(tmp_path):
    world = StubWorld()
    mapper = ObservationMapper(session_id="")
    adapter = StubEvidenceAdapter(world)

    def client_factory(sid):
        return StubDurableClient("http://stub", sid, world=world)

    server1 = StubServerHost(world)
    restart_server = StubServerHost(world)
    result = runner.drive_inf001(
        server1, restart_server, client_factory("slv0r1-inf001"), adapter, mapper, tmp_path,
        fixture_receipt=None, restart_client_factory=client_factory,
    )
    observed = result["observed"]
    # No frozen fixture receipt -> no recovery-source binding: the observed
    # evidence simply carries no recovery ref/SHA (treated as None upstream).
    assert observed.get("recoverySourceFixtureRef") is None
    assert observed.get("recoverySourceFixtureSha256") is None


# ---------------------------------------------------------------------------
# REPAIR-002 Repair 4 — real external TaskState revision drift
# ---------------------------------------------------------------------------


def test_inf004_real_occ_drift_observed_and_rejected():
    world = StubWorld()
    mapper = ObservationMapper(session_id="slv0r1-inf004")
    adapter = StubEvidenceAdapter(world)
    client = StubDurableClient("http://stub", "slv0r1-inf004", world=world)
    drift = FakeTaskStateDriftAdapter(world)
    result = runner.drive_inf004(client, adapter, mapper, drift_adapter=drift)
    observed = result["observed"]
    assert observed["parkedRevision"] == 1
    assert observed["driftRevisionBefore"] == 1
    assert observed["driftRevisionAfter"] == 2
    assert observed["staleRevisionSent"] == 1
    assert observed["rejectedZeroDelta"] is True
    assert observed["redisRevisionBefore"] == 2
    assert observed["redisRevisionAfter"] == 2
    assert observed["driftReceipt"]["actor"] == runner.DRIFT_ACTOR
    assert observed["driftReceipt"]["revisionBefore"] == 1
    assert observed["driftReceipt"]["revisionAfter"] == 2
    assert result["terminalClass"] == "ABSTAIN_OR_EXPLAIN"
    conflict = [a for a in result["attempts"] if a["attemptKind"] == "revision_conflict"][0]
    assert conflict["rejected"] is True
    assert conflict["terminalCode"] == "ABSTAIN_OR_EXPLAIN"
    # The stale receipt's revision was NOT bumped to match the drift.
    assert conflict["revisionBefore"] == 2 and conflict["revisionAfter"] == 2


def test_inf004_parked_revision_minus_one_implementation_deleted():
    """The packet's Repair 4 demanded DELETE of the ``parked_revision - 1``
    implementation; sending n-1 is not equivalent to a real n -> n+1 drift.
    Only the module docstring may still explain that the old approach was
    removed — no executable ``parked_revision - 1`` expression may survive."""
    source = Path(runner.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _is_parked_minus_one(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Sub)
            and isinstance(node.left, ast.Name)
            and node.left.id == "parked_revision"
            and isinstance(node.right, ast.Constant)
            and node.right.value == 1
        )

    assert [n for n in ast.walk(tree) if _is_parked_minus_one(n)] == []


def test_fake_drift_adapter_fails_closed():
    world = StubWorld()
    client = StubDurableClient("http://stub", "slv0r1-inf004", world=world)
    r1 = client.post({"message": INF_004_MESSAGE, "domainHint": "ecommerce"})
    identity = runner._require_task_identity(r1.body)
    task_id, _, thread_id = identity
    drift = FakeTaskStateDriftAdapter(world)
    # REPAIR-003: the drift adapter only operates on a triple registered THIS run.
    drift.register_owned(task_id=task_id, session_id="slv0r1-inf004", thread_id=thread_id)
    # Wrong expected revision fails closed.
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(task_id=task_id, session_id="slv0r1-inf004", thread_id=thread_id, expected_revision=2)
    # Correct OCC patch succeeds.
    drift.drift_revision(task_id=task_id, session_id="slv0r1-inf004", thread_id=thread_id, expected_revision=1)
    # Double drift (expected 1 again) fails closed.
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(task_id=task_id, session_id="slv0r1-inf004", thread_id=thread_id, expected_revision=1)
    # Cross-owner session fails closed (registered under a different triple).
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(task_id=task_id, session_id="slv0r1-other", thread_id=thread_id, expected_revision=2)
    # Dangling task fails closed (never registered THIS run).
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(task_id=f"{TASK_OWNED_SESSION_PREFIX}-missing",
                             session_id="slv0r1-inf004",
                             thread_id=f"v2-task:{TASK_OWNED_SESSION_PREFIX}-missing:run",
                             expected_revision=1)


# ---------------------------------------------------------------------------
# REPAIR-003 — production fixture (real TaskState store + durable
#               graph/checkpointer), real saver round-trip, Command(resume=...)
#               recovery, ownership zero-I/O matrix, INF-001 receipt identity,
#               semantic probes, Agent active-backend read-back.
# ---------------------------------------------------------------------------


class InFileRedis:
    """Loop-agnostic async plain-Redis fake (mirrors the committed
    ``test_graph_v2_interrupt_resume.InFileRedis``): strings/lists/hashes/
    zsets + ``eval`` dispatching on ``numkeys`` (TaskState CAS = 1 key + 4
    args; saver CAS = 3 keys + 7 args).  Per-key TTLs are recorded."""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sorted_sets: dict[str, dict[str, int]] = {}
        self.ttls: dict[str, int] = {}

    # ── strings ──────────────────────────────────────────────────────────────
    async def get(self, key: str) -> str | None:
        return self.strings.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.strings.pop(key, None)

    async def set(self, key: str, value: str, **kwargs) -> bool:
        self.strings[key] = value
        ex = kwargs.get("ex") or kwargs.get("EX")
        if ex:
            self.ttls[key] = int(ex)
        return True

    # ── lists ────────────────────────────────────────────────────────────────
    @staticmethod
    def _slice(values: list[str], start: int, end: int) -> list[str]:
        length = len(values)
        start = max(length + start, 0) if start < 0 else start
        end = length + end if end < 0 else end
        if start >= length or start > end:
            return []
        return values[start : end + 1]

    async def rpush(self, key: str, value: str) -> int:
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self._slice(self.lists.get(key, []), start, end)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.lists[key] = self._slice(self.lists.get(key, []), start, end)

    # ── hashes ───────────────────────────────────────────────────────────────
    async def hset(self, key: str, field: str, value: str) -> int:
        created = field not in self.hashes.setdefault(key, {})
        self.hashes[key][field] = value
        return int(created)

    async def hsetnx(self, key: str, field: str, value: str) -> int:
        fields = self.hashes.setdefault(key, {})
        if field in fields:
            return 0
        fields[field] = value
        return 1

    async def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))

    async def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        fields = self.hashes.setdefault(key, {})
        current = int(fields.get(field, 0))
        fields[field] = str(current + amount)
        return current + amount

    # ── sorted sets ──────────────────────────────────────────────────────────
    async def zadd(self, key: str, mapping: dict[str, int]) -> int:
        values = self.sorted_sets.setdefault(key, {})
        new_members = sum(member not in values for member in mapping)
        values.update(mapping)
        return new_members

    async def zrange(self, key: str, start: int, end: int) -> list[str]:
        members = [
            member
            for member, _ in sorted(
                self.sorted_sets.get(key, {}).items(), key=lambda item: item[1]
            )
        ]
        return self._slice(members, start, end)

    async def zrem(self, key: str, *members: str) -> int:
        values = self.sorted_sets.get(key, {})
        removed = sum(values.pop(member, None) is not None for member in members)
        return removed

    # ── generic ──────────────────────────────────────────────────────────────
    async def expire(self, key: str, seconds: int) -> bool:
        exists = (
            key in self.strings
            or key in self.lists
            or key in self.hashes
            or key in self.sorted_sets
        )
        if exists:
            self.ttls[key] = int(seconds)
        return exists

    async def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            deleted += int(self.strings.pop(key, None) is not None)
            deleted += int(self.lists.pop(key, None) is not None)
            deleted += int(self.hashes.pop(key, None) is not None)
            deleted += int(self.sorted_sets.pop(key, None) is not None)
            self.ttls.pop(key, None)
        return deleted

    async def scan_iter(self, match: str = "*", count: int = 10):
        pattern = re.compile("^" + match.replace("*", ".*") + "$")
        for key in (
            set(self.strings)
            | set(self.lists)
            | set(self.hashes)
            | set(self.sorted_sets)
        ):
            if pattern.match(key):
                yield key

    # ── Lua CAS dispatch (numkeys distinguishes TaskState vs saver) ───────────
    async def eval(self, script: str, numkeys: int, *args):
        if "GRAPH_V2_PUT_WRITES" in script and numkeys == 1:
            return self._put_writes(*args)
        if numkeys == 1 and len(args) == 4:
            return self._task_state_cas(*args)
        if numkeys == 3 and len(args) == 7:
            return self._saver_cas(*args)
        raise NotImplementedError(
            f"InFileRedis cannot eval numkeys={numkeys} args={len(args)}"
        )

    def _put_writes(self, key: str, ttl_raw: str, count_raw: str, *args):
        fields = self.hashes.setdefault(key, {})
        call_ord = int(fields.get("__ord", 0)) + 1
        fields["__ord"] = str(call_ord)
        count = int(count_raw)
        for within in range(count):
            mode, field, encoded = args[within * 3 : within * 3 + 3]
            envelope = json.loads(encoded)
            envelope["o"] = f"{call_ord:05d}:{within:05d}"
            rewritten = json.dumps(envelope, separators=(",", ":"))
            if mode != "nx" or field not in fields:
                fields[field] = rewritten
        self.ttls[key] = int(ttl_raw)
        return call_ord

    def _task_state_cas(self, key: str, expected_raw: str, payload: str, ttl: str):
        raw = self.strings.get(key)
        if raw is None:
            return [-1, -1]
        actual = int(json.loads(raw)["revision"])
        expected = int(expected_raw)
        if actual != expected:
            return [0, actual]
        self.strings[key] = payload
        self.ttls[key] = int(ttl)
        return [1, expected + 1]

    def _saver_cas(
        self,
        cp_key: str,
        latest_key: str,
        step_key: str,
        envelope: str,
        checkpoint_id: str,
        step_raw: str,
        ttl_raw: str,
    ):
        self.strings[cp_key] = envelope
        self.ttls[cp_key] = int(ttl_raw)
        cur = self.strings.get(step_key)
        step = int(step_raw)
        if cur is None or step >= int(cur):
            self.strings[latest_key] = checkpoint_id
            self.strings[step_key] = step_raw
            self.ttls[latest_key] = int(ttl_raw)
            self.ttls[step_key] = int(ttl_raw)
            return 1
        return 0


class _RecordingRedis:
    """Counts GET/WATCH/SET to prove ownership-before-Redis: every identity
    rejection must perform ZERO GET/WATCH/SET.  ``eval`` (the CAS patch) is
    passed through un-counted — reaching it means the guards already passed."""

    def __init__(self, inner):
        self._inner = inner
        self.ops = {"get": 0, "watch": 0, "set": 0}

    async def get(self, *args, **kwargs):
        self.ops["get"] += 1
        return await self._inner.get(*args, **kwargs)

    async def set(self, *args, **kwargs):
        self.ops["set"] += 1
        return await self._inner.set(*args, **kwargs)

    async def watch(self, *args, **kwargs):
        self.ops["watch"] += 1
        return await self._inner.watch(*args, **kwargs)

    async def eval(self, *args, **kwargs):
        return await self._inner.eval(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _resume_tool_schemas():
    from app.tools import TOOL_SCHEMAS

    wanted = {"search_products", "get_product_details"}
    return [s for s in TOOL_SCHEMAS if s["function"]["name"] in wanted]


def _resume_fake_tool(name: str, arguments: dict):
    from app.schemas import ToolTrace
    from tests.two_stage_ranking_fixtures import two_stage_search_detail

    if name == "search_products":
        return ToolTrace(tool=name, ok=True, detail=two_stage_search_detail([101, 102]))
    if name == "get_product_details":
        return ToolTrace(
            tool=name, ok=True,
            detail={"productIds": [101, 102], "products": [{"id": 101}, {"id": 102}]},
        )
    raise AssertionError(f"unexpected tool dispatch: {name}")


def _resume_fake_client():
    from types import SimpleNamespace

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock())),
    )


def _park_fixture_on_infile_redis(fixture_id: str = "saver-rt") -> tuple[InFileRedis, FixtureReceipt]:
    """REAL fixture via production APIs (TaskState store + durable graph +
    checkpointer) over a controlled InFileRedis — no network, model or tool."""
    redis = InFileRedis()
    fixture_client = RedisCheckpointFixtureClient(client=redis)
    receipt = fixture_client.park_clarification_fixture(fixture_id=fixture_id)
    return redis, receipt


def test_production_fixture_goes_through_real_saver_and_decodes():
    """REPAIR-003 Repair 1: the fixture is produced by the REAL TaskState store +
    durable graph/checkpointer under ``override_task_state_client`` (no handwritten
    TaskState JSON, no handwritten checkpoint envelope, no direct saver-key
    forgery) and its checkpoint decodes through the REAL
    ``GraphV2CheckpointSaver.aget_tuple()`` public read path — calling only the
    runner's own ``read_checkpoint`` does not count.  Receipt fields keep their
    distinct semantics: checkpointRef == the actual checkpoint id,
    checkpointDigest == the production saver 16-hex digest, checkpointSha256 ==
    the full envelope SHA-256 (64 hex)."""
    from app import task_state as ts
    from app.graph.checkpoint import GraphV2CheckpointSaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    redis, receipt = _park_fixture_on_infile_redis()
    assert receipt.checkpoint_ref
    assert re.fullmatch(r"[a-f0-9]{16}", receipt.checkpoint_digest)
    assert re.fullmatch(r"[a-f0-9]{64}", receipt.checkpoint_sha256)
    assert receipt.thread_id.startswith("v2-task:")
    assert receipt.task_id == receipt.thread_id.split(":")[1]
    # Fixture writes landed under the production saver's real key scheme.
    assert f"graph-v2:cp:{receipt.thread_id}::latest" in redis.strings

    async def _read_back():
        async with ts.override_task_state_client(redis):
            saver = GraphV2CheckpointSaver(serde=JsonPlusSerializer())
            tup = await saver.aget_tuple(
                {"configurable": {"thread_id": receipt.thread_id}}
            )
            digest = await saver.alatest_checkpoint_hash(receipt.thread_id)
            raw = await redis.get(
                f"graph-v2:cp:{receipt.thread_id}::cp:{receipt.checkpoint_ref}"
            )
            return tup, digest, raw

    tup, digest, raw = asyncio.run(_read_back())
    assert tup is not None
    assert tup.config["configurable"]["checkpoint_id"] == receipt.checkpoint_ref
    assert digest == receipt.checkpoint_digest  # production saver digest (16-hex)
    assert raw is not None
    assert sha256_text(raw.encode("utf-8")) == receipt.checkpoint_sha256  # full envelope SHA-256
    # The production digest is NOT conflated with a prefix of the envelope SHA-256.
    assert receipt.checkpoint_digest != receipt.checkpoint_sha256[:16]


def test_prepare_fixture_fails_closed_when_production_api_unavailable():
    """REPAIR-003 Repair 1 (4): live ``prepare-fixtures`` FAILS CLOSED when the
    production API cannot be called safely — a broken controlled client (real
    Redis write refused) surfaces as ``FixtureReceiptError``, never a half-built
    hand-written "production-shaped" fixture."""

    class _BrokenRedis:
        async def set(self, *args, **kwargs):
            raise RuntimeError("redis connection refused")

    client = RedisCheckpointFixtureClient(client=_BrokenRedis())
    with pytest.raises(FixtureReceiptError) as exc:
        client.park_clarification_fixture(fixture_id="broken")
    assert "FAILED CLOSED" in str(exc.value)


def test_production_fixture_parked_checkpoint_resumes_via_command():
    """REPAIR-003 Repair 1: the SAME thread/config resumes through REAL
    ``Command(resume=...)`` recovery (``run_graph_v2_durable`` with a resume
    payload) against the REAL parked envelope — proving the production fixture
    enters real recovery, not merely a runner-internal read."""
    from app import task_state as ts
    from app.agent_trace import TraceBuilder
    from app.graph.checkpoint import GraphV2CheckpointSaver
    from app.graph.resume import run_graph_v2_durable
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    redis, receipt = _park_fixture_on_infile_redis(fixture_id="resume-rt")
    run_id = receipt.thread_id.split(":")[-1]
    answer = "预算 3000 元以内。"
    async def _resume_fake_tool_v2(name, arguments, _context):
        return _resume_fake_tool(name, arguments)

    tool_caller = AsyncMock(side_effect=_resume_fake_tool_v2)

    async def _resume():
        async with ts.override_task_state_client(redis):
            saver = GraphV2CheckpointSaver(serde=JsonPlusSerializer())
            payload = {
                "taskId": receipt.task_id,
                "runId": run_id,
                "threadId": receipt.thread_id,
                "revision": receipt.revision,
                "proposalHash": proposal_hash(
                    receipt.task_id, FIXTURE_PENDING_QUESTION, receipt.revision
                ),
                "answer": answer,
            }
            return await run_graph_v2_durable(
                task_id=receipt.task_id,
                session_id=receipt.session_id,
                resume=payload,
                run_id=run_id,
                thread_id=receipt.thread_id,
                user_message=INF_001_MESSAGE,
                client=_resume_fake_client(),
                model="fixture-no-model",
                resolve_tool_schemas=lambda _schemas: _resume_tool_schemas(),
                tool_caller=tool_caller,
                tool_caller_v2=tool_caller,
                tool_inbox=runner._FixtureToolInbox(),
                trace_builder=TraceBuilder(
                    f"resume-{receipt.fixture_id}", mode="context_pack"
                ),
                max_transitions=8,
                checkpointer=saver,
            )

    resumed = asyncio.run(_resume())
    assert resumed.mode == "resume"
    assert resumed.boundary == "task_completed"
    assert resumed.thread_id == receipt.thread_id
    assert resumed.run_id == run_id
    # The recovery consumed the REAL parked envelope and ran the REAL durable
    # path to completion: exactly ONE live search executed, the apply-answer
    # bumped the task-state revision past the parked revision.
    assert resumed.revision > receipt.revision
    searches = [c for c in tool_caller.await_args_list if c.args[0] == "search_products"]
    assert len(searches) == 1

    # REPAIR-006 Phase 0: inspect the REAL production-written completion state,
    # not a pair of hand-written adjacent integers.
    live = json.loads(redis.strings[f"task-state:{receipt.task_id}"])
    domain = live["domainState"]
    exec_receipt = domain["v2ExecReceipt"]
    exec_projection = domain["v2ExecReceiptProjection"]
    exec_revision = exec_projection["projectionRevision"]
    validation = domain["validationResult"]
    cursor = json.loads(redis.strings[f"graph-v2:cursor:{receipt.task_id}"])
    assert live["revision"] == exec_revision + 1
    assert validation["basedOnRevision"] == exec_revision
    assert validation["taskId"] == receipt.task_id
    assert validation["planId"] == exec_receipt["planId"]
    assert validation["outcome"] == "passed"
    assert len([
        item for item in validation["stepResults"]
        if item["stepId"] == exec_receipt["stepId"]
        and item["outcome"] == "satisfied"
    ]) == 1
    assert live["activePlan"]["planId"] == exec_receipt["planId"]
    assert live["activePlan"]["status"] == "completed"
    marker = domain["v2RunMarker"]
    owner_hash = session_owner_hash(receipt.session_id)
    for witness in (marker, cursor):
        assert witness["runId"] == run_id
        assert witness["threadId"] == receipt.thread_id
        assert witness["sessionOwnerHash"] == owner_hash
    assert type(cursor["revision"]) is int
    assert 0 < cursor["revision"] <= exec_revision

    sync_adapter = RedisEvidenceAdapter(
        "redis://production-adjacent",
        client=_SyncRedis(dict(redis.strings)),
    )
    assert sync_adapter.read_owned_thread(
        receipt.task_id, run_id, session_id=receipt.session_id
    ) == receipt.thread_id
    projection = sync_adapter.retrieval_channel_projection(
        receipt.task_id,
        session_id=receipt.session_id,
        thread_id=receipt.thread_id,
        run_id=run_id,
    )
    assert projection["taskStateRevision"] == exec_revision + 1
    assert projection["validatorBasedOnRevision"] == exec_revision
    assert projection["validatorStepOutcomeSatisfied"] is True
    assert projection["activePlanCompleted"] is True


def test_production_saver_rejects_handwritten_old_envelope():
    """REPAIR-003 frozen counterexample: a handwritten OLD-format envelope
    ``{c, h, ...}`` (the pre-v2 shape, no ``b``/``t`` base64 payload) is rejected
    by the REAL production saver decode path with
    ``GraphV2CheckpointIntegrityError`` — so a stale/directly-forged envelope can
    never decode as a real checkpoint."""
    from app import task_state as ts
    from app.graph.checkpoint import (
        GraphV2CheckpointIntegrityError,
        GraphV2CheckpointSaver,
    )
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    redis = InFileRedis()
    thread_id = "v2-task:task-0000000000000000:run-counter"

    async def _seed_and_read():
        await redis.set(f"graph-v2:cp:{thread_id}::latest", "old-cp")
        await redis.set(
            f"graph-v2:cp:{thread_id}::cp:old-cp",
            json.dumps({"c": {}, "h": "a" * 16}),
        )
        async with ts.override_task_state_client(redis):
            saver = GraphV2CheckpointSaver(serde=JsonPlusSerializer())
            return await saver.aget_tuple(
                {"configurable": {"thread_id": thread_id}}
            )

    with pytest.raises(GraphV2CheckpointIntegrityError):
        asyncio.run(_seed_and_read())


def test_drift_identity_rejections_zero_redis_io():
    """REPAIR-003 Repair 2: every ownership rejection happens BEFORE any Redis
    GET/WATCH/SET.  The recording fake proves 0 GET / 0 WATCH / 0 SET for a
    foreign task + matching foreign session, a foreign thread, a task/thread
    mismatch, a double drift, and a dangling ref; the ONE real OCC drift does
    exactly 1 GET (the CAS pre-read) + 1 EVAL — never a SET."""
    from app import task_state as ts

    redis = InFileRedis()

    async def _seed(session_id: str):
        async with ts.override_task_state_client(redis):
            state = await ts.create_task_state(
                ts.TaskStateCreateRequest(
                    task_type="ecommerce_guide",
                    goal=INF_001_MESSAGE,
                    session_id=session_id,
                    pending_questions=[FIXTURE_PENDING_QUESTION],
                    domain_state={},
                )
            )
        return state

    state = asyncio.run(_seed(f"{TASK_OWNED_SESSION_PREFIX}-drift-session"))
    task_id = state.task_id
    session_id = state.session_id
    thread_id = f"v2-task:{task_id}:run-1"

    recording = _RecordingRedis(redis)
    drift = RedisTaskStateDriftAdapter(client=recording)
    drift.register_owned(task_id=task_id, session_id=session_id, thread_id=thread_id)

    def _expect_zero():
        assert recording.ops == {"get": 0, "watch": 0, "set": 0}

    # 1) Foreign task + its MATCHING foreign session: never registered THIS run.
    foreign_task = "task-" + "f" * 16
    foreign_session = f"{TASK_OWNED_SESSION_PREFIX}-external-session"
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(
            task_id=foreign_task, session_id=foreign_session,
            thread_id=f"v2-task:{foreign_task}:run-9", expected_revision=1,
        )
    _expect_zero()
    # 2) Foreign thread: registered triple but a different owned thread.
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(
            task_id=task_id, session_id=session_id,
            thread_id=f"v2-task:{task_id}:run-2", expected_revision=1,
        )
    _expect_zero()

    # 3) Task/thread mismatch: thread embeds a different task than registered.
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(
            task_id=task_id, session_id=session_id,
            thread_id=f"v2-task:{TASK_OWNED_SESSION_PREFIX}-other:run-1",
            expected_revision=1,
        )
    _expect_zero()

    # 4) The one REAL OCC drift succeeds (n -> n+1) with exactly 1 GET + 1 EVAL.
    drifted = drift.drift_revision(
        task_id=task_id, session_id=session_id,
        thread_id=thread_id, expected_revision=1,
    )
    assert drifted.revision_before == 1 and drifted.revision_after == 2
    assert recording.ops == {"get": 1, "watch": 0, "set": 0}

    # 5) Double drift is rejected BEFORE any Redis I/O.
    recording.ops = {"get": 0, "watch": 0, "set": 0}
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(
            task_id=task_id, session_id=session_id,
            thread_id=thread_id, expected_revision=1,
        )
    _expect_zero()

    # 6) Dangling ref: owned-shaped but never registered THIS run.
    with pytest.raises(RedisEvidenceError):
        drift.drift_revision(
            task_id=f"{TASK_OWNED_SESSION_PREFIX}-missing",
            session_id=f"{TASK_OWNED_SESSION_PREFIX}-missing-session",
            thread_id=f"v2-task:{TASK_OWNED_SESSION_PREFIX}-missing:run",
            expected_revision=1,
        )
    _expect_zero()


def test_real_drift_adapter_uses_async_redis_client():
    drift = RedisTaskStateDriftAdapter(redis_url="redis://127.0.0.1:6379/0")
    try:
        assert drift._redis_client is not None
        assert type(drift._redis_client).__module__.startswith("redis.asyncio")
    finally:
        # No connection was opened; close the async client without touching
        # Redis so this remains a pure client-type regression test.
        asyncio.run(drift._redis_client.aclose())


def test_inf001_receipt_session_reused_and_mismatch_fails_in_stub(tmp_path):
    """REPAIR-003 Repair 3: INF-001 reuses the SIGNED receipt's session
    end-to-end; a freshly generated ``slv0r1-inf001`` session (the OLD approach)
    fails closed in the STUB too — the frozen counterexample.  No session/task/
    thread drift can reach the first request."""
    world, receipt = _fixture()
    mapper = ObservationMapper(session_id="")
    adapter = StubEvidenceAdapter(world)
    client_factory = lambda sid: StubDurableClient("http://stub", sid, world=world)

    server1 = StubServerHost(world)
    restart_server = StubServerHost(world)
    result = runner.drive_inf001(
        server1, restart_server, client_factory(receipt.session_id), adapter, mapper,
        tmp_path, fixture_receipt=receipt, restart_client_factory=client_factory,
    )
    # Receipt session is used for client/restart/recovery end-to-end.
    assert result["taskId"] == receipt.task_id
    assert result["threadId"] == receipt.thread_id
    assert result["observed"]["recoverySourceFixtureRef"] == receipt.checkpoint_ref

    # Old generated session is NOT the receipt session -> the stub fails closed
    # with no task identity (runId empty).
    stale_client = StubDurableClient(
        "http://stub", f"{TASK_OWNED_SESSION_PREFIX}-inf001", world=world
    )
    stale = stale_client.post({"message": INF_001_MESSAGE, "domainHint": "ecommerce"})
    assert stale.body.get("runId") == ""
    assert runner._require_task_identity(stale.body) is None


def _semantic_probe(**overrides) -> dict:
    probe = {
        "url": "http://probe", "method": "GET", "kind": "",
        "reachable": True, "httpStatus": 200, "errorType": None,
        "latencyMs": 1.0, "timestamp": "2026-08-19T00:00:00+00:00",
        "source": "probe_http",
    }
    probe.update(overrides)
    return probe


def test_derive_retrieval_chain_semantic_fail_closed():
    """REPAIR-003 Repair 4: the chain is UP only when EVERY semantic requirement
    holds on the CAPTURED responses — catalog 251/253, empty/unparsed retrieval,
    ES red (yellow OK), readiness not-UP/unparsed, HTTP 404 / 200-invalid, and
    non-mapping evidence all fail closed."""
    def up(**mutations) -> dict:
        targets = {
            "searchJavaReadiness": _semantic_probe(
                kind="backend", healthStatus="UP", healthParsed=True),
            "searchJavaCatalog": _semantic_probe(
                kind="catalog", catalogDocCount=CATALOG_EXPECTED_COUNT, catalogParsed=True),
            "retrievalSmoke": _semantic_probe(
                kind="retrieval-smoke", retrievedCount=3, retrievalParsed=True),
            "elasticsearch": _semantic_probe(
                kind="index", esStatus="green", esHealthParsed=True),
        }
        for key, value in mutations.items():
            targets[key].update(value)
        return targets

    assert derive_retrieval_chain(up()) is True
    # catalog must be EXACTLY 252.
    assert derive_retrieval_chain(up(searchJavaCatalog={"catalogDocCount": 251, "catalogParsed": True})) is False
    assert derive_retrieval_chain(up(searchJavaCatalog={"catalogDocCount": 253, "catalogParsed": True})) is False
    # HTTP 404 / wrong-JSON-200 catalog: unparsed -> fail closed.
    assert derive_retrieval_chain(up(searchJavaCatalog={"httpStatus": 404, "catalogParsed": False})) is False
    assert derive_retrieval_chain(up(searchJavaCatalog={"httpStatus": 200, "catalogDocCount": None, "catalogParsed": False})) is False
    # retrieval must be non-empty AND parsed.
    assert derive_retrieval_chain(up(retrievalSmoke={"retrievedCount": 0, "retrievalParsed": True})) is False
    assert derive_retrieval_chain(up(retrievalSmoke={"retrievedCount": None, "retrievalParsed": False})) is False
    # ES: red fails closed, yellow passes (not red), unparsed fails.
    assert derive_retrieval_chain(up(elasticsearch={"esStatus": "red", "esHealthParsed": True})) is False
    assert derive_retrieval_chain(up(elasticsearch={"esStatus": "yellow", "esHealthParsed": True})) is True
    assert derive_retrieval_chain(up(elasticsearch={"esStatus": None, "esHealthParsed": False})) is False
    # readiness must be reachable + parsed + UP.
    assert derive_retrieval_chain(up(searchJavaReadiness={"healthStatus": "DOWN", "healthParsed": True})) is False
    assert derive_retrieval_chain(up(searchJavaReadiness={"healthStatus": "UP", "healthParsed": False})) is False
    assert derive_retrieval_chain(up(searchJavaReadiness={"reachable": False})) is False
    # Redis (when present) must be reachable.
    assert derive_retrieval_chain({**up(), "redis": _semantic_probe(kind="store", reachable=True)}) is True
    assert derive_retrieval_chain({**up(), "redis": _semantic_probe(kind="store", reachable=False)}) is False
    # Non-mapping / missing evidence fails closed.
    assert derive_retrieval_chain(None) is False
    assert derive_retrieval_chain("garbage") is False
    assert derive_retrieval_chain({}) is False


def test_probe_http_kind_derivation_single_capture(monkeypatch):
    """Each kind-derived conclusion reuses the ONE captured response body — no
    probe-then-second-request race (exactly one request per target)."""
    import httpx

    captured: list[tuple[str, str]] = []

    class _FakeResponse:
        def __init__(self, status_code: int, data: dict | list):
            self.status_code = status_code
            self._data = data

        def json(self):
            return self._data

    def _fake_get(url: str, **kwargs):
        captured.append(("GET", url))
        if url.endswith("/actuator/health/readiness"):
            return _FakeResponse(200, {"status": "UP", "retrievalMode": "bm25", "esStatus": "green"})
        if url.endswith("/api/products?category=phone&limit=1500"):
            return _FakeResponse(200, {"data": [{"id": 1} for _ in range(252)]})
        if url.endswith("/api/products/retrieval?query=phone&limit=3"):
            return _FakeResponse(200, {"data": {"products": [{"id": 1}, {"id": 2}]}})
        if url.endswith("/_cluster/health"):
            return _FakeResponse(200, {"status": "green", "number_of_nodes": 1})
        return _FakeResponse(200, {})

    def _fake_post(url: str, **kwargs):
        captured.append(("POST", url))
        return _FakeResponse(200, {"data": [{"id": 1}, {"id": 2}]})

    monkeypatch.setattr(httpx, "get", _fake_get)
    monkeypatch.setattr(httpx, "post", _fake_post)

    catalog = runner.probe_http("http://x/api/products?category=phone&limit=1500", kind="catalog")
    assert catalog["catalogDocCount"] == 252 and catalog["catalogParsed"] is True
    readiness = runner.probe_http("http://x/actuator/health/readiness", kind="backend")
    assert readiness["healthStatus"] == "UP" and readiness["healthParsed"] is True
    assert readiness["retrievalMode"] == "bm25" and readiness["esStatus"] == "green"
    retrieval = runner.probe_http("http://x/api/products/retrieval?query=phone&limit=3", kind="retrieval-smoke")
    assert retrieval["retrievedCount"] == 2 and retrieval["retrievalParsed"] is True
    es = runner.probe_http("http://x/_cluster/health", kind="index")
    assert es["esStatus"] == "green" and es["esHealthParsed"] is True
    # Exactly one request per target: the derivation never re-probes.
    assert captured == [
        ("GET", "http://x/api/products?category=phone&limit=1500"),
        ("GET", "http://x/actuator/health/readiness"),
        ("GET", "http://x/api/products/retrieval?query=phone&limit=3"),
        ("GET", "http://x/_cluster/health"),
    ]


def _owned_channel_calls(task_id: str, *, es: str = "active", bm25: str = "active",
                         mysql: str | None = None, omit_es: bool = False,
                         omit_bm25: bool = False, run_id: str = "run-abc",
                         plan_id: str = "plan-1", step_id: str = "step-1",
                         call_task: str | None = None,
                         call_plan: str | None = None,
                         call_step: str | None = None,
                         extra_calls: tuple = ()) -> dict:
    """A persisted search_products channel attestation projection for tests.
    REPAIR-005: carries the current runId + server-owned v2ExecReceipt
    plan/step so the read-back can bind the call to the SAME run; drift knobs
    (call_task/call_plan/call_step/extra_calls) exercise the stale/foreign
    counterexamples."""
    channels: dict[str, dict] = {}
    if not omit_es:
        channels["elasticsearch"] = {"status": es, "count": 3}
    if not omit_bm25:
        channels["bm25"] = {"status": bm25, "count": 3}
    if mysql is not None:
        channels["mysqlFallback"] = {"status": mysql, "count": 3}
    calls = [
        {"taskId": call_task or task_id, "planId": call_plan or plan_id,
         "stepId": call_step or step_id, "toolName": "search_products",
         "channels": channels},
    ]
    calls.extend(extra_calls)
    return {
        "present": True,
        "searchProductCalls": calls,
        "runId": run_id,
        "v2RunMarkerMatched": True,
        "v2ExecReceipt": {
            "runId": run_id,
            "planId": plan_id,
            "stepId": step_id,
            "revision": 3,
        },
        "taskStateRevision": 4,
        "execReceiptRevision": 3,
        "validatorBasedOnRevision": 3,
        "validatorOutcome": "passed",
        "validatorTaskMatched": True,
        "validatorPlanMatched": True,
        "validatorStepMatched": True,
        "validatorStepOutcomeSatisfied": True,
        "activePlanMatched": True,
        "activePlanCompleted": True,
        "activePlanStepMatched": True,
    }


def test_agent_active_backend_readback_repair004_positive_negative():
    """REPAIR-004 Repair 2c + REPAIR-005: the Agent active-backend read-back
    requires the OBSERVED live /health to carry the process's ACTUALLY loaded
    ``backendBaseUrl`` == :18082 and ``productRetrievalMode`` == bm25, an OWNED
    durable trace whose STORED runId == the current run, and a REAL persisted
    search_products channel attestation (ES active + BM25 active + no
    mysqlFallback) whose task/plan/step match the server-owned v2ExecReceipt.
    The REPAIR-003 bare-health / executor-only vacuous passes are DELETED and a
    stale/historical/foreign-run search or wrong-run trace is BLOCKED."""
    owned_task = f"{TASK_OWNED_SESSION_PREFIX}-rt-task"
    owned_session = f"{TASK_OWNED_SESSION_PREFIX}-rt-session"
    owned_run = "run-abc"
    owned_thread = f"v2-task:{owned_task}:{owned_run}"
    health_up = {
        "url": "http://x/health", "kind": "control-plane", "reachable": True,
        "httpStatus": 200, "healthStatus": "UP", "healthParsed": True,
        "backendBaseUrl": SEARCH_JAVA_BASE_URL, "productRetrievalMode": "bm25",
    }
    trace_ok = {
        "runId": owned_run, "taskId": owned_task, "sessionId": owned_session,
        "graphV2Events": [
            {"node": "entry", "checkpointHash": "a" * 16},
            {"node": "executor", "checkpointHash": "a" * 16},
            {"node": "validator", "checkpointHash": "a" * 16},
        ],
    }

    def _ok(health=health_up, trace=trace_ok, channels=None, task=owned_task,
            session=owned_session, run=owned_run, thread=owned_thread):
        return agent_active_backend_readback(
            health, trace,
            expected_backend_base_url=SEARCH_JAVA_BASE_URL,
            expected_retrieval_mode="bm25",
            owned_task_id=task, owned_session_id=session,
            owned_run_id=run, owned_thread_id=thread,
            retrieval_channels=channels if channels is not None else _owned_channel_calls(task, run_id=run),
        )

    ok = _ok()
    assert ok["activeBackendReadable"] is True
    assert ok["backendBaseUrlObserved"] == SEARCH_JAVA_BASE_URL
    assert ok["productRetrievalModeObserved"] == "bm25"
    assert ok["attestedChannels"] == {"elasticsearch": "active", "bm25": "active", "mysqlFallback": "inactive"}
    assert ok["searchProductCallCount"] == 1
    assert ok["v2ExecReceiptPlanId"] == "plan-1"
    assert ok["v2ExecReceiptStepId"] == "step-1"
    assert ok["traceOwned"] is True
    # The real app reports 运行中 (running); accepted as up.
    assert _ok(health={**health_up, "healthStatus": "运行中"})["activeBackendReadable"] is True

    def _blocked(health=health_up, trace=trace_ok, channels=None, task=owned_task,
                 session=owned_session, run=owned_run, thread=owned_thread):
        return agent_active_backend_readback(
            health, trace,
            expected_backend_base_url=SEARCH_JAVA_BASE_URL,
            expected_retrieval_mode="bm25",
            owned_task_id=task, owned_session_id=session,
            owned_run_id=run, owned_thread_id=thread,
            retrieval_channels=channels if channels is not None else _owned_channel_calls(task, run_id=run),
        )["reason"]

    # Bare /health (no loaded-config fields) is now BLOCKED — the vacuous
    # REPAIR-003 positive is inverted.
    bare = {k: v for k, v in health_up.items() if k not in ("backendBaseUrl", "productRetrievalMode")}
    assert "backendBaseUrl" in _blocked(health=bare)
    assert _blocked(health={})
    assert _blocked(health={**health_up, "reachable": False})
    assert _blocked(health={**health_up, "healthParsed": False})
    assert _blocked(health={**health_up, "healthStatus": "DOWN"})
    assert _blocked(health={**health_up, "backendBaseUrl": ""})
    assert _blocked(health={**health_up, "backendBaseUrl": "http://wrong:9999"})
    assert _blocked(health={**health_up, "productRetrievalMode": "hybrid"})
    # Wrong mode via the ES-watch compatibility field is irrelevant: the loaded
    # productRetrievalMode must be exactly bm25.
    assert "productRetrievalMode" in _blocked(health={**health_up, "productRetrievalMode": "rrf"})
    # Missing / unreadable / foreign / executor-only / checkpoint-only trace.
    assert _blocked(trace=None)
    assert _blocked(trace={})
    assert _blocked(trace={"traceReadError": "ConnectionError"})
    assert _blocked(trace={**trace_ok, "taskId": f"{TASK_OWNED_SESSION_PREFIX}-other"})
    assert _blocked(trace={**trace_ok, "sessionId": f"{TASK_OWNED_SESSION_PREFIX}-other"})
    assert _blocked(trace={"graphV2Events": [{"node": "executor"}]})
    assert _blocked(trace={"graphV2Events": [{"node": "entry", "checkpointHash": "a" * 16}]})
    assert _blocked(trace={"graphV2Events": "nope"})
    # Missing owned identity / channel attestation / thread derivation.
    assert "ownership" in _blocked(task=None)
    assert _blocked(session=None)
    assert _blocked(run=None)
    assert _blocked(thread=None)
    assert _blocked(thread=f"v2-task:{TASK_OWNED_SESSION_PREFIX}-other:{owned_run}")
    assert _blocked(channels=None)
    assert _blocked(channels={"present": False, "searchProductCalls": []})
    assert _blocked(channels={"present": True, "searchProductCalls": [], "reason": "no persisted search"})
    # REPAIR-005: wrong-run trace — stored runId drifts from the requested run.
    assert "runId" in _blocked(trace={**trace_ok, "runId": "run-foreign-current-mismatch"})
    # REPAIR-005: stale/historical search — current executor/checkpoint trace
    # present but the ONLY persisted search belongs to another plan/step.
    stale = _owned_channel_calls(owned_task, call_plan="plan-9", call_step="step-9")
    assert "stale/historical" in _blocked(channels=stale)
    # REPAIR-005: foreign-task search with matching plan/step is still BLOCKED.
    foreign_call = _owned_channel_calls(owned_task, call_task=f"{TASK_OWNED_SESSION_PREFIX}-other")
    assert "matching the current-run" in _blocked(channels=foreign_call)
    # REPAIR-005: projection runId drift / missing v2ExecReceipt.
    assert "runId" in _blocked(channels={**_owned_channel_calls(owned_task), "runId": "run-other"})
    assert "v2ExecReceipt" in _blocked(channels={k: v for k, v in _owned_channel_calls(owned_task).items() if k != "v2ExecReceipt"})
    assert "v2ExecReceipt" in _blocked(channels={**_owned_channel_calls(owned_task), "v2ExecReceipt": {"runId": owned_run, "planId": "", "stepId": ""}})
    # mysqlFallback active / ES degraded / BM25 missing → BLOCKED.
    assert "mysqlFallback" in _blocked(channels=_owned_channel_calls(owned_task, mysql="active"))
    assert _blocked(channels=_owned_channel_calls(owned_task, es="failed"))
    assert _blocked(channels=_owned_channel_calls(owned_task, omit_bm25=True))
    assert _blocked(channels=_owned_channel_calls(owned_task, omit_es=True))


def test_fixture_receipt_identity_guard_rejects_drift_before_use(tmp_path):
    """REPAIR-004 Repair 1a: ``load_fixture_receipt`` runs the task-owned
    identity guard — taskId/sessionId owned-shaped, thread strictly
    ``v2-task:<taskId>:<non-empty runId>`` and consistent with taskId — and
    rejects ANY mismatch, including a REPLACED receipt whose self-hash was
    recomputed over drifted content, BEFORE the receipt can drive any request.
    """
    world, receipt = _fixture()
    valid_path = tmp_path / "valid.json"
    write_fixture_receipt(valid_path, receipt)
    assert load_fixture_receipt(valid_path).receipt_sha256 == receipt.receipt_sha256

    def _write_and_load(**changes):
        data = receipt.to_dict()
        data.update({k: v for k, v in changes.items() if v is not None})
        data["receiptSha256"] = ""
        drifted = FixtureReceipt.from_dict(data)
        path = tmp_path / "drifted.json"
        write_fixture_receipt(path, drifted)  # re-signs over the drifted content
        with pytest.raises(FixtureReceiptError):
            load_fixture_receipt(path)

    _write_and_load(taskId="foreign-user-task")
    _write_and_load(sessionId="foreign-session")
    _write_and_load(threadId="v1-thread:whatever")
    _write_and_load(threadId=f"v2-task:{receipt.task_id}:")
    other_task = f"{TASK_OWNED_SESSION_PREFIX}-other-task"
    _write_and_load(threadId=f"v2-task:{other_task}:some-run")


def test_inf001_receipt_identity_drift_fails_closed_single_request(tmp_path):
    """REPAIR-004 Repair 1c: when the server-side session binding resolves the
    first INF-001 response to ANOTHER owned task/thread (not the signed
    receipt's), ``drive_inf001`` returns an explicit identity failure after
    EXACTLY 1 initial request — 0 kill, 0 restart, 0 recovery request, no
    recovery ref."""
    world = StubWorld()
    receipt = world.register_fixture(fixture_id="drift")
    world.inf001_identity_drift = True
    mapper = ObservationMapper(session_id="")
    adapter = StubEvidenceAdapter(world)

    def client_factory(sid):
        return StubDurableClient("http://stub", sid, world=world)

    server1 = StubServerHost(world)
    restart_server = StubServerHost(world)
    result = runner.drive_inf001(
        server1, restart_server, client_factory(receipt.session_id), adapter, mapper, tmp_path,
        fixture_receipt=receipt,
        restart_client_factory=client_factory,
    )
    observed = result["observed"]
    assert observed.get("identityFailure") is True
    assert "taskId" in observed.get("identityReason", "")
    assert result["attempts"] == []
    assert result["runId"] == ""
    assert result["terminalClass"] == "FAILURE"
    assert world.request_count == 1
    assert world.servers_killed == 0
    assert world.restart_occurred is False
    assert observed.get("recoverySourceFixtureRef") is None


class _SyncRedis:
    """Minimal SYNC redis fake for RedisEvidenceAdapter (get -> raw JSON)."""

    def __init__(self, raw: dict[str, str] | None = None):
        self._raw = raw or {}
        self.get_count = 0

    def get(self, key):
        self.get_count += 1
        return self._raw.get(key)


def _projection_task_state(task_id: str, session_id: str) -> str:
    return json.dumps({
        "taskId": task_id, "sessionId": session_id, "revision": 7, "status": "completed",
        "facts": [{"id": "fact-secret", "value": "private truth"}],
        "constraints": [{"id": "constraint-secret"}],
        "pendingQuestions": [],
        "activePlan": {
            "planId": "plan-1",
            "status": "completed",
            "steps": [{"stepId": "step-1", "status": "executed"}],
        },
        "domainState": {
            # REPAIR-005: server-owned same-run markers must be present.
            "v2RunMarker": {
                "runId": "run-abc",
                "threadId": f"v2-task:{task_id}:run-abc",
                "sessionOwnerHash": session_owner_hash(session_id),
                "startedAt": "2026-08-19T00:00:00+00:00",
            },
            "v2ExecReceipt": {
                "runId": "run-abc", "planId": "plan-1", "stepId": "step-1",
                "stateRevision": 1, "at": "2026-08-19T00:00:00+00:00",
            },
            "v2ExecReceiptProjection": {"projectionRevision": 6},
            "validationResult": {
                "outcome": "passed",
                "taskId": task_id,
                "planId": "plan-1",
                "basedOnRevision": 6,
                "stepResults": [
                    {"stepId": "step-1", "outcome": "satisfied"},
                ],
            },
            "stepExecutionResults": [
                {
                    "taskId": task_id, "planId": "plan-1", "stepId": "step-1",
                    "toolName": "search_products", "outcome": "tool_succeeded",
                    "resolvedArguments": {"query": "预算 3000 二手手机"},
                    "toolTrace": {
                        "tool": "search_products", "ok": True, "durationMs": 42.0,
                        "detail": {
                            "contractVersion": "two-stage-ranking-v1",
                            "candidateIds": [101, 102, 103],
                            "candidates": [{"id": 101, "ref": "secret-ref"}],
                            "evidence": [{"ref": "secret-ref", "fact": "private"}],
                            "retrievalTrace": {
                                "channels": {
                                    "elasticsearch": {"status": "active", "count": 3},
                                    "bm25": {"status": "active", "count": 3},
                                    "structuredRequirements": {"status": "active", "count": 2},
                                },
                                "fusedTop50": [101, 102, 103],
                            },
                        },
                    },
                    "startedAt": "2026-08-19T00:00:00+00:00",
                    "finishedAt": "2026-08-19T00:00:00+00:00",
                    "durationMs": 42.0,
                },
                {
                    "taskId": task_id, "planId": "plan-1", "stepId": "step-2",
                    "toolName": "search_products", "outcome": "tool_failed",
                    "resolvedArguments": {},
                    "toolTrace": {"tool": "search_products", "ok": False,
                                  "detail": {"code": "product_search_unavailable"}},
                    "startedAt": "2026-08-19T00:00:00+00:00",
                    "finishedAt": "2026-08-19T00:00:00+00:00",
                    "durationMs": 1.0,
                },
                {
                    "taskId": task_id, "planId": "plan-1", "stepId": "step-3",
                    "toolName": "get_product_details", "outcome": "tool_succeeded",
                    "resolvedArguments": {},
                    "toolTrace": {"tool": "get_product_details", "ok": True, "detail": {}},
                    "startedAt": "2026-08-19T00:00:00+00:00",
                    "finishedAt": "2026-08-19T00:00:00+00:00",
                    "durationMs": 1.0,
                },
            ]
        },
    }, ensure_ascii=False)


def test_retrieval_channel_projection_read_only_safe():
    """REPAIR-004 Repair 2b + REPAIR-005: the read-only projection of
    ``task-state`` returns ONLY the CURRENT-RUN successful ``search_products``
    tool identity (verified against the server-owned v2RunMarker/v2ExecReceipt)
    + per-channel status/count.  Raw facts, prompts, candidates, fused pools,
    evidence and private truth are NEVER present in the projection."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-proj-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-proj-session"
    thread_id = f"v2-task:{task_id}:run-abc"
    redis = _SyncRedis({f"task-state:{task_id}": _projection_task_state(task_id, session_id)})
    adapter = RedisEvidenceAdapter("redis://x", client=redis)

    projection = adapter.retrieval_channel_projection(
        task_id, session_id=session_id, thread_id=thread_id, run_id="run-abc"
    )
    assert projection["present"] is True
    assert projection["runId"] == "run-abc"
    assert projection["v2RunMarkerMatched"] is True
    assert projection["v2ExecReceipt"]["planId"] == "plan-1"
    assert projection["v2ExecReceipt"]["stepId"] == "step-1"
    assert projection["taskStateRevision"] == 7
    assert projection["execReceiptRevision"] == 6
    assert projection["execReceiptClaimedRevision"] == 1
    assert projection["validatorBasedOnRevision"] == 6
    assert projection["validatorOutcome"] == "passed"
    assert projection["validatorStepOutcomeSatisfied"] is True
    assert projection["activePlanCompleted"] is True
    calls = projection["searchProductCalls"]
    assert len(calls) == 1  # only the current-run successful search_products survives
    call = calls[0]
    assert call["taskId"] == task_id and call["toolName"] == "search_products"
    assert call["planId"] == "plan-1" and call["stepId"] == "step-1"
    assert call["channels"]["elasticsearch"] == {"status": "active", "count": 3}
    assert call["channels"]["bm25"] == {"status": "active", "count": 3}
    assert "mysqlFallback" not in call["channels"]
    assert "structuredRequirements" in call["channels"]

    blob = json.dumps(projection, ensure_ascii=False)
    for forbidden in ("private truth", "fact-secret", "secret-ref", "candidateIds",
                      "fusedTop50", "candidates", "evidence", "resolvedArguments",
                      "query", "预算"):
        assert forbidden not in blob, f"projection leaked {forbidden!r}"
    assert redis.get_count == 1  # exactly one read for the owned identity


def test_retrieval_channel_projection_foreign_identity_zero_redis_io():
    """REPAIR-004 Repair 2b/4 + REPAIR-005: a foreign task/session/thread/RUN
    identity is refused by ``retrieval_channel_projection`` BEFORE any Redis GET
    — the recording-fake proves 0 I/O on every rejection (the four-way guard
    refuses a missing/foreign run exactly like a foreign task/session/thread)."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-proj-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-proj-session"
    thread_id = f"v2-task:{task_id}:run-abc"

    def _reject(*, task=task_id, session=session_id, thread=thread_id, run="run-abc") -> int:
        redis = _SyncRedis({"task-state:whatever": "{}"})
        adapter = RedisEvidenceAdapter("redis://x", client=redis)
        with pytest.raises(RedisEvidenceError):
            adapter.retrieval_channel_projection(task, session_id=session, thread_id=thread, run_id=run)
        return redis.get_count

    assert _reject(task="foreign-user-task") == 0
    assert _reject(session="foreign-session") == 0
    assert _reject(thread="v1-thread:whatever") == 0
    assert _reject(thread=f"v2-task:{task_id}:") == 0
    assert _reject(thread=f"v2-task:{TASK_OWNED_SESSION_PREFIX}-other:run-x") == 0
    assert _reject(run="") == 0
    assert _reject(run="foreign-run") == 0
    assert _reject(run="run-x") == 0  # thread carries run-abc; run must match it
    # Owned identity + stored-record drift (taskId/sessionId mismatch inside the
    # persisted value) → refused (this still performs exactly one read).
    redis = _SyncRedis({f"task-state:{task_id}": json.dumps({
        "taskId": f"{TASK_OWNED_SESSION_PREFIX}-other", "sessionId": session_id,
        "domainState": {},
    })})
    adapter = RedisEvidenceAdapter("redis://x", client=redis)
    with pytest.raises(RedisEvidenceError):
        adapter.retrieval_channel_projection(task_id, session_id=session_id, thread_id=thread_id, run_id="run-abc")


# ---------------------------------------------------------------------------
# REPAIR-005 — current-run same-run evidence chain (single run, no borrowing)
# ---------------------------------------------------------------------------


def test_retrieval_channel_projection_marker_receipt_drift_blocked():
    """REPAIR-005 frozen #2 (marker/receipt half): ANY drift in the server-owned
    ``v2RunMarker`` (run/thread) or ``v2ExecReceipt`` (run/plan/step/revision)
    inside the persisted task-state is BLOCKED by the projection
    (``RedisEvidenceError``) — a foreign or historical run can never borrow the
    current-run markers."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-proj-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-proj-session"
    thread_id = f"v2-task:{task_id}:run-abc"

    def _state(**domain_changes):
        base = json.loads(_projection_task_state(task_id, session_id))
        base["domainState"].update(domain_changes)
        return json.dumps(base)

    def _reject(raw):
        redis = _SyncRedis({f"task-state:{task_id}": raw})
        adapter = RedisEvidenceAdapter("redis://x", client=redis)
        with pytest.raises(RedisEvidenceError):
            adapter.retrieval_channel_projection(
                task_id, session_id=session_id, thread_id=thread_id, run_id="run-abc"
            )

    marker = {
        "runId": "run-abc",
        "threadId": thread_id,
        "sessionOwnerHash": session_owner_hash(session_id),
    }
    receipt = {"runId": "run-abc", "planId": "plan-1", "stepId": "step-1", "stateRevision": 1}
    # v2RunMarker missing.
    _reject(_state(v2RunMarker=None, v2ExecReceipt=receipt))
    # v2RunMarker run/thread drift.
    _reject(_state(v2RunMarker={**marker, "runId": "run-other"}, v2ExecReceipt=receipt))
    _reject(_state(v2RunMarker={**marker, "threadId": f"v2-task:{task_id}:run-other"}, v2ExecReceipt=receipt))
    # v2ExecReceipt missing.
    _reject(_state(v2RunMarker=marker, v2ExecReceipt=None))
    # v2ExecReceipt run/plan/step/revision drift.
    _reject(_state(v2RunMarker=marker, v2ExecReceipt={**receipt, "runId": "run-other"}))
    _reject(_state(v2RunMarker=marker, v2ExecReceipt={**receipt, "planId": ""}))
    _reject(_state(v2RunMarker=marker, v2ExecReceipt={**receipt, "stepId": ""}))
    _reject(_state(v2RunMarker=marker, v2ExecReceipt={**receipt, "stateRevision": "1"}))


def test_retrieval_channel_projection_stepexec_identity_drift_excluded():
    """REPAIR-005 frozen #2 (StepExecutionResult half): a ``search_products``
    StepExecutionResult whose task/plan/step drifts from the owned task +
    server-owned v2ExecReceipt plan/step — or that is technically unsuccessful —
    is EXCLUDED from the current-run attestation; the read-back then BLOCKS the
    stale/historical search."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-proj-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-proj-session"
    thread_id = f"v2-task:{task_id}:run-abc"
    base = json.loads(_projection_task_state(task_id, session_id))
    good = base["domainState"]["stepExecutionResults"][0]

    def _state_for(item):
        state = dict(base)
        state["domainState"] = dict(base["domainState"])
        state["domainState"]["stepExecutionResults"] = [item]
        return json.dumps(state)

    cases = [
        {**good, "taskId": f"{TASK_OWNED_SESSION_PREFIX}-other"},
        {**good, "planId": "plan-9"},
        {**good, "stepId": "step-9"},
        {**good, "outcome": "tool_failed"},
        {**good, "toolTrace": {"tool": "search_products", "ok": False, "detail": {}}},
    ]
    health = {
        "reachable": True, "healthParsed": True, "healthStatus": "UP",
        "backendBaseUrl": SEARCH_JAVA_BASE_URL, "productRetrievalMode": "bm25",
    }
    for item in cases:
        redis = _SyncRedis({f"task-state:{task_id}": _state_for(item)})
        adapter = RedisEvidenceAdapter("redis://x", client=redis)
        projection = adapter.retrieval_channel_projection(
            task_id, session_id=session_id, thread_id=thread_id, run_id="run-abc"
        )
        assert projection["searchProductCalls"] == []
        reason = agent_active_backend_readback(
            health,
            {"runId": "run-abc", "taskId": task_id, "sessionId": session_id,
             "graphV2Events": [{"node": "executor", "checkpointHash": "a" * 16}]},
            expected_backend_base_url=SEARCH_JAVA_BASE_URL,
            expected_retrieval_mode="bm25",
            owned_task_id=task_id, owned_session_id=session_id,
            owned_run_id="run-abc", owned_thread_id=thread_id,
            retrieval_channels=projection,
        )["reason"]
        assert "persisted" in reason or "stale/historical" in reason


def test_agent_active_backend_readback_stale_search_counterexample_blocked():
    """REPAIR-005 frozen #1 (Codex minimal counterexample): exact
    :18082+bm25 health, a same-task/session executor+checkpoint trace whose
    STORED runId is ``run-foreign-current-mismatch``, and ONLY a stale-plan/
    stale-step historical search_products.  REPAIR-004 returned
    ``activeBackendReadable=True``; REPAIR-005 must BLOCK it (wrong-run trace +
    no matching current-run receipt/search)."""
    owned_task = f"{TASK_OWNED_SESSION_PREFIX}-rt-task"
    owned_session = f"{TASK_OWNED_SESSION_PREFIX}-rt-session"
    owned_run = "run-abc"
    owned_thread = f"v2-task:{owned_task}:{owned_run}"
    health = {
        "reachable": True, "healthParsed": True, "healthStatus": "UP",
        "backendBaseUrl": SEARCH_JAVA_BASE_URL, "productRetrievalMode": "bm25",
    }
    trace = {
        "runId": "run-foreign-current-mismatch",  # wrong-run trace
        "taskId": owned_task, "sessionId": owned_session,
        "graphV2Events": [
            {"node": "entry", "checkpointHash": "a" * 16},
            {"node": "executor", "checkpointHash": "a" * 16},
        ],
    }
    channels = _owned_channel_calls(
        owned_task, run_id=owned_run, call_plan="plan-9", call_step="step-9"
    )
    result = agent_active_backend_readback(
        health, trace,
        expected_backend_base_url=SEARCH_JAVA_BASE_URL, expected_retrieval_mode="bm25",
        owned_task_id=owned_task, owned_session_id=owned_session,
        owned_run_id=owned_run, owned_thread_id=owned_thread,
        retrieval_channels=channels,
    )
    assert result["activeBackendReadable"] is False
    assert "runId" in result["reason"]


def test_agent_active_backend_readback_same_run_chain_unique_positive():
    """REPAIR-005 frozen #3: live health, server-owned marker/thread,
    trace stored runId, v2ExecReceipt AND the matching search_products
    task/plan/step are ALL consistent — the UNIQUE positive PASS of the
    same-run evidence chain, proven both through the read-back and through the
    projection's marker/receipt verification."""
    owned_task = f"{TASK_OWNED_SESSION_PREFIX}-rt-task"
    owned_session = f"{TASK_OWNED_SESSION_PREFIX}-rt-session"
    owned_run = "run-abc"
    owned_thread = f"v2-task:{owned_task}:{owned_run}"
    health = {
        "reachable": True, "healthParsed": True, "healthStatus": "运行中",
        "backendBaseUrl": SEARCH_JAVA_BASE_URL, "productRetrievalMode": "bm25",
    }
    trace = {
        "runId": owned_run, "taskId": owned_task, "sessionId": owned_session,
        "graphV2Events": [
            {"node": "executor", "checkpointHash": "a" * 16},
            {"node": "validator", "checkpointHash": "a" * 16},
        ],
    }
    result = agent_active_backend_readback(
        health, trace,
        expected_backend_base_url=SEARCH_JAVA_BASE_URL, expected_retrieval_mode="bm25",
        owned_task_id=owned_task, owned_session_id=owned_session,
        owned_run_id=owned_run, owned_thread_id=owned_thread,
        retrieval_channels=_owned_channel_calls(owned_task, run_id=owned_run),
    )
    assert result["activeBackendReadable"] is True
    assert result["attestedChannels"] == {"elasticsearch": "active", "bm25": "active", "mysqlFallback": "inactive"}
    # The same chain through the projection: marker/thread + receipt + the
    # matching search survive together and only together.
    redis = _SyncRedis({f"task-state:{owned_task}": _projection_task_state(owned_task, owned_session)})
    adapter = RedisEvidenceAdapter("redis://x", client=redis)
    projection = adapter.retrieval_channel_projection(
        owned_task, session_id=owned_session, thread_id=owned_thread, run_id=owned_run
    )
    assert projection["v2RunMarkerMatched"] is True
    assert projection["searchProductCalls"][0]["planId"] == "plan-1"
    assert projection["searchProductCalls"][0]["stepId"] == "step-1"


def test_read_owned_thread_marker_and_cursor_readback():
    """REPAIR-006: marker and cursor are a conjunction, never fallbacks."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-thread-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-thread-session"
    thread_id = f"v2-task:{task_id}:run-abc"
    marker_state = json.dumps({
        "taskId": task_id, "sessionId": session_id, "revision": 4,
        "domainState": {
            "v2RunMarker": {
                "runId": "run-abc",
                "threadId": thread_id,
                "sessionOwnerHash": session_owner_hash(session_id),
            },
        },
    })
    cursor = json.dumps({
        "runId": "run-abc",
        "threadId": thread_id,
        "sessionOwnerHash": session_owner_hash(session_id),
        "revision": 2,
    })
    # Unique positive: owner + marker + cursor all agree.
    redis = _SyncRedis({
        f"task-state:{task_id}": marker_state,
        f"graph-v2:cursor:{task_id}": cursor,
    })
    adapter = RedisEvidenceAdapter("redis://x", client=redis)
    assert adapter.read_owned_thread(task_id, "run-abc", session_id=session_id) == thread_id

    # Marker drift cannot be repaired by a matching cursor.
    redis = _SyncRedis({
        f"task-state:{task_id}": json.dumps({
            "taskId": task_id, "sessionId": session_id, "revision": 4,
            "domainState": {"v2RunMarker": {
                "runId": "run-other", "threadId": "v2-task:x:y",
                "sessionOwnerHash": session_owner_hash(session_id),
            }},
        }),
        f"graph-v2:cursor:{task_id}": cursor,
    })
    adapter = RedisEvidenceAdapter("redis://x", client=redis)
    assert adapter.read_owned_thread(task_id, "run-abc", session_id=session_id) is None

    # Cursor drift cannot be repaired by a matching marker.
    redis = _SyncRedis({
        f"task-state:{task_id}": marker_state,
        f"graph-v2:cursor:{task_id}": json.dumps({
            "runId": "run-other", "threadId": thread_id,
            "sessionOwnerHash": session_owner_hash(session_id), "revision": 2,
        }),
    })
    adapter = RedisEvidenceAdapter("redis://x", client=redis)
    assert adapter.read_owned_thread(task_id, "run-abc", session_id=session_id) is None

    # Either witness missing, owner drift, malformed/future revision => blocked.
    variants = [
        {f"task-state:{task_id}": marker_state},
        {f"graph-v2:cursor:{task_id}": cursor},
        {
            f"task-state:{task_id}": marker_state,
            f"graph-v2:cursor:{task_id}": json.dumps({
                "runId": "run-abc", "threadId": thread_id,
                "sessionOwnerHash": "wrong-owner", "revision": 2,
            }),
        },
        {
            f"task-state:{task_id}": marker_state,
            f"graph-v2:cursor:{task_id}": json.dumps({
                "runId": "run-abc", "threadId": thread_id,
                "sessionOwnerHash": session_owner_hash(session_id), "revision": True,
            }),
        },
        {
            f"task-state:{task_id}": marker_state,
            f"graph-v2:cursor:{task_id}": json.dumps({
                "runId": "run-abc", "threadId": thread_id,
                "sessionOwnerHash": session_owner_hash(session_id), "revision": 5,
            }),
        },
    ]
    for raw in variants:
        adapter = RedisEvidenceAdapter("redis://x", client=_SyncRedis(raw))
        assert adapter.read_owned_thread(task_id, "run-abc", session_id=session_id) is None


def test_repair006_old_byte_numeric_revision_false_pass_is_blocked():
    """Old bytes accepted any integer receipt, including impossible 999."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-numeric-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-numeric-session"
    thread_id = f"v2-task:{task_id}:run-abc"
    state = json.loads(_projection_task_state(task_id, session_id))
    state["revision"] = 1000
    state["domainState"]["v2ExecReceiptProjection"]["projectionRevision"] = 999
    state["domainState"]["validationResult"]["basedOnRevision"] = 999
    adapter = RedisEvidenceAdapter(
        "redis://x",
        client=_SyncRedis({f"task-state:{task_id}": json.dumps(state)}),
    )
    with pytest.raises(RedisEvidenceError):
        adapter.retrieval_channel_projection(
            task_id,
            session_id=session_id,
            thread_id=thread_id,
            run_id="run-abc",
        )


def test_repair006_completion_witness_single_field_matrix_blocked():
    """Every completion/owner field is independently fail-closed."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-matrix-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-matrix-session"
    thread_id = f"v2-task:{task_id}:run-abc"
    delete = object()
    cases = [
        (("taskId",), f"{TASK_OWNED_SESSION_PREFIX}-other"),
        (("sessionId",), f"{TASK_OWNED_SESSION_PREFIX}-other"),
        (("revision",), 3),
        (("revision",), 5),
        (("revision",), "4"),
        (("revision",), True),
        (("revision",), 0),
        (("domainState", "v2RunMarker"), delete),
        (("domainState", "v2RunMarker", "runId"), "run-other"),
        (("domainState", "v2RunMarker", "threadId"), "v2-task:x:y"),
        (("domainState", "v2RunMarker", "sessionOwnerHash"), "wrong-owner"),
        (("domainState", "v2ExecReceipt"), delete),
        (("domainState", "v2ExecReceipt", "runId"), "run-other"),
        (("domainState", "v2ExecReceipt", "planId"), ""),
        (("domainState", "v2ExecReceipt", "stepId"), ""),
        (("domainState", "v2ExecReceipt", "stateRevision"), 2),
        (("domainState", "v2ExecReceipt", "stateRevision"), 4),
        (("domainState", "v2ExecReceipt", "stateRevision"), 999),
        (("domainState", "v2ExecReceipt", "stateRevision"), "1"),
        (("domainState", "v2ExecReceipt", "stateRevision"), True),
        (("domainState", "v2ExecReceipt", "stateRevision"), 0),
        (("domainState", "v2ExecReceiptProjection", "projectionRevision"), 2),
        (("domainState", "v2ExecReceiptProjection", "projectionRevision"), 999),
        (("domainState", "validationResult"), delete),
        (("domainState", "validationResult", "taskId"), "other-task"),
        (("domainState", "validationResult", "planId"), "plan-other"),
        (("domainState", "validationResult", "basedOnRevision"), 2),
        (("domainState", "validationResult", "basedOnRevision"), "3"),
        (("domainState", "validationResult", "basedOnRevision"), True),
        (("domainState", "validationResult", "outcome"), "validation_failed"),
        (("domainState", "validationResult", "stepResults", 0, "stepId"), "step-other"),
        (("domainState", "validationResult", "stepResults", 0, "outcome"), "invalid"),
        (("activePlan",), delete),
        (("activePlan", "planId"), "plan-other"),
        (("activePlan", "status"), "active"),
        (("activePlan", "steps", 0, "stepId"), "step-other"),
        (("activePlan", "steps", 0, "status"), "pending"),
    ]

    def _mutate(value, path, replacement):
        parent = value
        for key in path[:-1]:
            parent = parent[key]
        key = path[-1]
        if replacement is delete:
            del parent[key]
        else:
            parent[key] = replacement

    for path, replacement in cases:
        state = json.loads(_projection_task_state(task_id, session_id))
        _mutate(state, path, replacement)
        adapter = RedisEvidenceAdapter(
            "redis://x",
            client=_SyncRedis({f"task-state:{task_id}": json.dumps(state)}),
        )
        with pytest.raises(RedisEvidenceError):
            adapter.retrieval_channel_projection(
                task_id,
                session_id=session_id,
                thread_id=thread_id,
                run_id="run-abc",
            )


def test_repair006_direct_readback_completion_projection_matrix_blocked():
    """A forged/good channel cannot mask one bad completion projection field."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-readback-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-readback-session"
    run_id = "run-abc"
    thread_id = f"v2-task:{task_id}:{run_id}"
    health = {
        "reachable": True,
        "healthParsed": True,
        "healthStatus": "UP",
        "backendBaseUrl": SEARCH_JAVA_BASE_URL,
        "productRetrievalMode": "bm25",
    }
    trace = {
        "runId": run_id,
        "taskId": task_id,
        "sessionId": session_id,
        "graphV2Events": [{"node": "executor", "checkpointHash": "a" * 16}],
    }
    cases = [
        ("v2RunMarkerMatched", False),
        ("taskStateRevision", 3),
        ("taskStateRevision", 5),
        ("taskStateRevision", "4"),
        ("taskStateRevision", True),
        ("execReceiptRevision", 2),
        ("execReceiptRevision", "3"),
        ("execReceiptRevision", True),
        ("validatorBasedOnRevision", 2),
        ("validatorBasedOnRevision", "3"),
        ("validatorOutcome", "validation_failed"),
        ("validatorTaskMatched", False),
        ("validatorPlanMatched", False),
        ("validatorStepMatched", False),
        ("validatorStepOutcomeSatisfied", False),
        ("activePlanMatched", False),
        ("activePlanCompleted", False),
        ("activePlanStepMatched", False),
    ]
    for key, value in cases:
        projection = _owned_channel_calls(task_id, run_id=run_id)
        projection[key] = value
        result = agent_active_backend_readback(
            health,
            trace,
            expected_backend_base_url=SEARCH_JAVA_BASE_URL,
            expected_retrieval_mode="bm25",
            owned_task_id=task_id,
            owned_session_id=session_id,
            owned_run_id=run_id,
            owned_thread_id=thread_id,
            retrieval_channels=projection,
        )
        assert result["activeBackendReadable"] is False, key

    forged_receipt = _owned_channel_calls(task_id, run_id=run_id)
    forged_receipt["v2ExecReceipt"] = {
        **forged_receipt["v2ExecReceipt"],
        "revision": 999,
    }
    assert agent_active_backend_readback(
        health,
        trace,
        expected_backend_base_url=SEARCH_JAVA_BASE_URL,
        expected_retrieval_mode="bm25",
        owned_task_id=task_id,
        owned_session_id=session_id,
        owned_run_id=run_id,
        owned_thread_id=thread_id,
        retrieval_channels=forged_receipt,
    )["activeBackendReadable"] is False


def test_read_owned_thread_foreign_identity_zero_redis_io():
    """REPAIR-005 frozen #5 (read-back half): a foreign task/session or an
    EMPTY run is refused by ``read_owned_thread`` BEFORE any Redis GET — the
    recording-fake proves 0 I/O (four-way guard runs first)."""
    task_id = f"{TASK_OWNED_SESSION_PREFIX}-thread-task"
    session_id = f"{TASK_OWNED_SESSION_PREFIX}-thread-session"

    def _reject(*, task=task_id, session=session_id, run="run-abc") -> int:
        redis = _SyncRedis({"task-state:whatever": "{}"})
        adapter = RedisEvidenceAdapter("redis://x", client=redis)
        with pytest.raises(RedisEvidenceError):
            adapter.read_owned_thread(task, run, session_id=session)
        return redis.get_count

    assert _reject(task="foreign-user-task") == 0
    assert _reject(session="foreign-session") == 0
    assert _reject(run="") == 0


def test_inf001_persisted_thread_missing_or_drift_fails_closed_single_request(tmp_path):
    """REPAIR-005 frozen #4: the first INF-001 response's taskId/runId MATCH the
    signed receipt, but the SERVER-OWNED persisted thread (TaskState v2RunMarker
    / cursor) is MISSING or DRIFTED — the driver fails closed with EXACTLY 1
    request and 0 kill / 0 restart / 0 recovery / no recovery ref."""
    for mode in ("missing", "drift"):
        world = StubWorld()
        receipt = world.register_fixture(fixture_id=f"thread-{mode}")
        if mode == "missing":
            world.inf001_thread_missing = True
        else:
            world.inf001_thread_drift = True
        mapper = ObservationMapper(session_id="")
        adapter = StubEvidenceAdapter(world)
        server1 = StubServerHost(world)
        restart_server = StubServerHost(world)
        result = runner.drive_inf001(
            server1, restart_server,
            StubDurableClient("http://stub", receipt.session_id, world=world),
            adapter, mapper, tmp_path,
            fixture_receipt=receipt,
            restart_client_factory=lambda sid: StubDurableClient("http://stub", sid, world=world),
        )
        observed = result["observed"]
        assert observed.get("identityFailure") is True
        if mode == "missing":
            assert "server-owned" in observed.get("identityReason", "")
        else:
            assert "persisted thread" in observed.get("identityReason", "")
        assert result["attempts"] == []
        assert result["runId"] == ""
        assert result["terminalClass"] == "FAILURE"
        assert world.request_count == 1
        assert world.servers_killed == 0
        assert world.restart_occurred is False
        assert observed.get("recoverySourceFixtureRef") is None


def test_do_run_reconciles_receipt_and_fault_before_any_subprocess(tmp_path, runtime, monkeypatch):
    """REPAIR-004 Repair 1b: ``do_run`` field-by-field reconciles the passed
    receipt against the frozen receipt + INF-001 private fault ref/SHA + freeze
    manifest binding BEFORE copying public input / spawning the public
    subprocess / issuing any Agent request.  Missing/replaced/re-signed/
    task-session-thread drift all fail closed with 0 subprocess calls."""
    world, receipt_path = _freeze(tmp_path)

    def _boom(*args, **kwargs):
        raise AssertionError("public subprocess must NOT be spawned on receipt/fault drift")

    monkeypatch.setattr(cli.subprocess, "run", _boom)
    frozen = load_fixture_receipt(tmp_path / cli.FIXTURE_RECEIPT_FILENAME)

    # 1) Passed receipt replaced + re-signed with a drifted checkpointRef.
    drifted = FixtureReceipt.from_dict(
        {**frozen.to_dict(), "checkpointRef": "cp-replaced-0000000000", "receiptSha256": ""}
    )
    drifted_path = tmp_path / "passed-drifted.json"
    write_fixture_receipt(drifted_path, drifted)
    with pytest.raises(FixtureReceiptError):
        cli.do_run(tmp_path, stub=True, fixture_receipt_path=drifted_path)

    # 2) Passed receipt re-signed with task/thread drift (owned but wrong).
    drifted2 = FixtureReceipt.from_dict(
        {**frozen.to_dict(),
         "threadId": f"v2-task:{TASK_OWNED_SESSION_PREFIX}-other:run-other",
         "receiptSha256": ""}
    )
    drifted_path2 = tmp_path / "passed-drifted2.json"
    write_fixture_receipt(drifted_path2, drifted2)
    with pytest.raises(FixtureReceiptError):
        cli.do_run(tmp_path, stub=True, fixture_receipt_path=drifted_path2)

    # 3) INF-001 private fault drifted from the frozen receipt.
    faults = lab.load_records(tmp_path / "fault.private.jsonl", "fault")
    for record in faults:
        if record["scenarioId"] == "ASL-V0-INF-REAL-001":
            record["faultPlan"]["setupFixture"]["checkpointFixtureRef"] = "cp-drifted-0000000000"
    runner.write_jsonl_atomic(tmp_path / "fault.private.jsonl", faults)
    with pytest.raises(FixtureReceiptError):
        cli.do_run(tmp_path, stub=True)

    # 4) Freeze manifest receipt binding drifted.
    sub = tmp_path / "sub"
    _freeze(sub)
    manifest_path = sub / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fixtureReceipt"]["checkpointSha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FixtureReceiptError):
        cli.do_run(sub, stub=True)

    # 5) A valid frozen dir does NOT over-block: every reconciliation passes.
    sub2 = tmp_path / "sub2"
    _freeze(sub2)
    sub2_receipt = load_fixture_receipt(sub2 / cli.FIXTURE_RECEIPT_FILENAME)
    cli._reconcile_receipts(sub2_receipt, sub2_receipt, source="same")
    cli._reconcile_inf001_fault(sub2, sub2_receipt)
    cli._reconcile_manifest_binding(sub2, sub2_receipt)


def test_health_endpoint_exposes_actually_loaded_retrieval_config(monkeypatch):
    """REPAIR-004 Repair 2a: the live Agent /health returns the process's
    ACTUALLY loaded non-secret retrieval config (backendBaseUrl +
    productRetrievalMode, fixed field names) alongside the legacy
    service/status; the read-back can reconcile it field-by-field."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.system import create_system_router
    from app.settings import Settings

    # Pin a KNOWN actually-loaded config so the test is independent of ambient
    # env, then assert /health exposes EXACTLY that observed config.
    monkeypatch.setattr("app.api.system.settings", Settings(
        backend_base_url="http://127.0.0.1:18082",
        product_retrieval_mode="bm25",
        _env_file=None,
    ))
    app = FastAPI()
    app.include_router(create_system_router(Path(".")))
    client = TestClient(app)
    body = client.get("/health").json()
    assert body["service"]
    assert body["status"]
    assert body["backendBaseUrl"] == "http://127.0.0.1:18082"
    assert body["productRetrievalMode"] == "bm25"


def test_health_response_schema_field_names_fixed():
    """REPAIR-004 Repair 2a: HealthResponse serializes the two new fields by
    their FIXED camelCase names and keeps legacy construction valid (empty
    default is unobservable → read-back BLOCKS, never echoes)."""
    from app.schemas import HealthResponse

    full = HealthResponse(
        service="本地生活智能体服务",
        status="运行中",
        backend_base_url="http://127.0.0.1:18082",
        product_retrieval_mode="bm25",
    )
    dumped = full.model_dump(by_alias=True)
    assert dumped["backendBaseUrl"] == "http://127.0.0.1:18082"
    assert dumped["productRetrievalMode"] == "bm25"
    assert "backend_base_url" not in dumped
    # A legacy construction (no config fields) stays valid but observable-empty.
    legacy = HealthResponse(service="s", status="ok").model_dump(by_alias=True)
    assert legacy["backendBaseUrl"] == "" and legacy["productRetrievalMode"] == ""


def test_probe_http_captures_loaded_health_config(monkeypatch):
    """REPAIR-004 Repair 2a: the control-plane /health probe captures the
    observed backendBaseUrl/productRetrievalMode from the body; a body WITHOUT
    them captures nothing (expected config is never echoed as an observation)."""
    import httpx

    class _FakeResponse:
        def __init__(self, status_code: int, data: dict):
            self.status_code = status_code
            self._data = data

        def json(self):
            return self._data

    monkeypatch.setattr(
        httpx, "get",
        lambda url, **kw: _FakeResponse(200, {
            "service": "本地生活智能体服务", "status": "运行中",
            "backendBaseUrl": "http://127.0.0.1:18082",
            "productRetrievalMode": "bm25",
        }),
    )
    probe = runner.probe_http("http://x/health", kind="control-plane")
    assert probe["healthParsed"] is True
    assert probe["backendBaseUrl"] == "http://127.0.0.1:18082"
    assert probe["productRetrievalMode"] == "bm25"

    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: _FakeResponse(200, {"status": "运行中"})
    )
    bare = runner.probe_http("http://x/health", kind="control-plane")
    assert "backendBaseUrl" not in bare
    assert "productRetrievalMode" not in bare


def test_fixture_receipt_fails_closed_on_bad_checkpoint_digest_shape(tmp_path):
    """REPAIR-003: the receipt validates checkpointDigest as the production
    saver digest (16 hex), not conflated with the 64-hex envelope SHA."""
    world = StubWorld()
    client = FakeCheckpointFixtureClient(world)
    receipt = client.park_clarification_fixture(fixture_id="digest-shape")
    d = receipt.to_dict()
    assert re.fullmatch(r"[a-f0-9]{16}", d["checkpointDigest"])
    assert re.fullmatch(r"[a-f0-9]{64}", d["checkpointSha256"])

    bad = dict(d)
    bad["checkpointDigest"] = "z" * 16
    bad["receiptSha256"] = ""
    p = tmp_path / "bad-digest-receipt.json"
    write_fixture_receipt(p, FixtureReceipt.from_dict(bad))
    with pytest.raises(FixtureReceiptError):
        load_fixture_receipt(p)

    conflated = dict(d)
    conflated["checkpointDigest"] = "a" * 64  # envelope SHA in the digest slot
    conflated["receiptSha256"] = ""
    p2 = tmp_path / "conflated-receipt.json"
    write_fixture_receipt(p2, FixtureReceipt.from_dict(conflated))
    with pytest.raises(FixtureReceiptError):
        load_fixture_receipt(p2)


# ---------------------------------------------------------------------------
# budget / command contract
# ---------------------------------------------------------------------------


def test_frozen_scenario_budget_is_fixed():
    world, receipt = _fixture()
    inputs, oracles, faults = cli.build_frozen_records(cli.derive_catalog(), fixture_receipt=receipt)
    by_id = {r["scenarioId"]: r for r in inputs}
    # LNG-001: exactly one natural-language turn, no setupRef.
    assert len(by_id["ASL-V0-LNG-REAL-001"]["turns"]) == 1
    assert by_id["ASL-V0-LNG-REAL-001"]["turns"][0]["text"] == cli.LNG_001_MESSAGE
    # LNG-002: exactly one open question + one fixed answer, no setupRef.
    assert [t["text"] for t in by_id["ASL-V0-LNG-REAL-002"]["turns"]] == [
        cli.LNG_002_QUESTION, cli.LNG_002_ANSWER,
    ]
    assert by_id["ASL-V0-LNG-REAL-003"]["turns"][0]["text"] == cli.LNG_003_MESSAGE
    assert by_id["ASL-V0-LNG-REAL-004"]["turns"][0]["text"] == cli.LNG_004_MESSAGE
    assert [t["text"] for t in by_id["ASL-V0-LNG-REAL-005"]["turns"]] == [
        cli.LNG_002_QUESTION, cli.LNG_005_ANSWER,
    ]
    assert [t["text"] for t in by_id["ASL-V0-LNG-REAL-006"]["turns"]] == [
        cli.LNG_006_FIRST, cli.LNG_006_CORRECTION,
    ]
    assert [t["text"] for t in by_id["ASL-V0-LNG-REAL-007"]["turns"]] == [
        cli.LNG_007_FIRST, cli.LNG_007_FOLLOWUP,
    ]
    assert [t["text"] for t in by_id["ASL-V0-LNG-REAL-008"]["turns"]] == [
        cli.LNG_008_FIRST, cli.LNG_008_CORRECTION,
    ]
    # INF scenarios: no turns, each with a setupRef (no pre-set TaskState).
    for sid in ("ASL-V0-INF-REAL-001", "ASL-V0-INF-REAL-002",
                "ASL-V0-INF-REAL-003", "ASL-V0-INF-REAL-004"):
        assert by_id[sid]["turns"] == []
        assert by_id[sid]["setupRef"]


def test_expansion_scope_followup_uses_one_final_observation_and_rerank_tool(tmp_path):
    world, receipt = _fixture()
    inputs, _oracles, _faults = cli.build_frozen_records(
        cli.derive_catalog(), fixture_receipt=receipt,
    )
    path = tmp_path / "scenario.input.jsonl"
    cli.write_jsonl_atomic(path, inputs)
    view = next(
        item for item in load_public_input(path)
        if item.scenario_id == "ASL-V0-LNG-REAL-007"
    )
    session_id = "slv0r1-expansion-lng007"
    result = drive_language_view(
        view,
        StubDurableClient("http://stub", session_id, world=world),
        StubEvidenceAdapter(world),
        ObservationMapper(session_id=session_id),
    )

    assert len(result["attempts"]) == 1
    assert [item["kind"] for item in result["roundtrips"]] == ["fresh", "followup"]
    assert [item["toolName"] for item in result["toolCalls"]] == [
        "rerank_products_in_scope"
    ]
    assert result["observed"]["publicTurnCount"] == 2


def test_expansion_clarification_uses_server_receipt_and_one_final_observation(tmp_path):
    world, receipt = _fixture()
    inputs, _oracles, _faults = cli.build_frozen_records(
        cli.derive_catalog(), fixture_receipt=receipt,
    )
    path = tmp_path / "scenario.input.jsonl"
    cli.write_jsonl_atomic(path, inputs)
    view = next(
        item for item in load_public_input(path)
        if item.scenario_id == "ASL-V0-LNG-REAL-005"
    )
    session_id = "slv0r1-expansion-lng005"
    result = drive_language_view(
        view,
        StubDurableClient("http://stub", session_id, world=world),
        StubEvidenceAdapter(world),
        ObservationMapper(session_id=session_id),
    )

    assert len(result["attempts"]) == 1
    assert [item["kind"] for item in result["roundtrips"]] == ["fresh", "resume"]
    assert result["observed"]["clarificationResumeUsed"] is True
    assert result["attempts"][0]["taskId"] == result["taskId"]
    assert result["attempts"][0]["threadId"] == result["threadId"]
    assert result["attempts"][0]["runId"] == result["runId"]


def test_driver_source_has_no_retry_loops():
    """Budget: no auto-retry / temperature sweep loops may exist in the CLI."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    # No retry helpers, no repeated-LMG loops, no score-driven re-run.
    assert "for retry" not in source
    assert "retries" not in source
    assert "temperature" not in source


def test_phase_guard_enforces_freeze_run_score(tmp_path):
    guard = PhaseGuard(tmp_path / "phase")
    guard.mark("freeze")
    with pytest.raises(PhaseGuardError):
        guard.assert_can("freeze")  # each phase exactly once
    guard.mark("run")
    with pytest.raises(PhaseGuardError):
        guard.assert_can("run")  # repeat refused
    with pytest.raises(PhaseGuardError):
        guard.assert_can("freeze")  # earlier phase after a later one refused
    guard.mark("score")
    with pytest.raises(PhaseGuardError):
        guard.assert_can("score")  # repeat refused
    with pytest.raises(PhaseGuardError):
        guard.assert_can("run")  # earlier phase after a later one refused
    assert guard.phases_done == ["freeze", "run", "score"]


def test_cli_auto_commands_manifest_order():
    """The auto command must drive freeze -> run -> score (and the manifest
    records the completedAt order).  Static proof of the wiring, scoped to the
    dispatcher block so earlier appearances in function bodies are ignored."""
    source = Path(cli.__file__).read_text(encoding="utf-8")
    dispatcher = source[source.index("if args.command == \"auto\":"):]
    freeze_call = dispatcher.index("rc = do_freeze(data_dir, fixture_receipt_path=args.fixture_receipt)")
    run_call = dispatcher.index("rc = do_run(data_dir, base_url=args.base_url")
    score_call = dispatcher.index("rc = do_score(data_dir)")
    assert freeze_call < run_call < score_call
    assert dispatcher.index("do_verify(data_dir") > score_call


def test_do_run_requires_freeze_first(tmp_path):
    with pytest.raises(Exception):
        cli.do_run(tmp_path, stub=True)


def test_do_run_spawns_public_only_subprocess_clean_argv_env_cwd(tmp_path, runtime, monkeypatch):
    _freeze(tmp_path)
    captured: dict[str, object] = {}

    def _capturing_run(argv, **kwargs):
        captured["argv"] = list(argv)
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        # Do NOT execute the real subprocess here: the point is to inspect the
        # spawn contract (argv/env/cwd) while the cwd is still pre-run (it must
        # hold ONLY the public input copy at spawn time).
        return subprocess.CompletedProcess(args=[], returncode=0)

    monkeypatch.setattr(cli.subprocess, "run", _capturing_run)
    # The fabricated subprocess wrote no prediction.jsonl, so do_run reports the
    # missing artifact (4).  The point of THIS test is the spawn contract, not
    # the pipeline completion (covered by test_offline_full_pipeline_*).
    assert cli.do_run(tmp_path, stub=True) in (0, 4)

    argv = captured["argv"]
    assert argv[0] == sys.executable
    assert argv[1] == str(cli.ENTRYPOINT)
    assert "--stub" in argv
    assert "--input" in argv
    # The subprocess receives ONLY public paths + config — never a private file.
    for arg in argv:
        assert "oracle.private" not in arg
        assert "fault.private" not in arg
        assert "score-report" not in arg
    env = captured["env"]
    assert public_environment_has_private_truth(env) == []
    assert env["BACKEND_BASE_URL"] == runner.SEARCH_JAVA_BASE_URL
    assert env["PRODUCT_RETRIEVAL_MODE"] == "bm25"
    # The subprocess cwd is the task-owned public runtime dir, which at spawn
    # holds ONLY the public input copy.
    cwd = Path(captured["cwd"])
    assert cwd.name == "public"
    assert {p.name for p in cwd.iterdir()} == {"scenario.input.jsonl"}


def test_do_run_fails_closed_when_real_retrieval_unavailable(tmp_path, runtime, monkeypatch):
    """When the real retrieval chain is unreachable, run must return the
    BLOCKED_REAL_DEPENDENCY exit code WITHOUT executing any scenario or writing
    a prediction.  The conclusion is DERIVED from probe evidence — no hardcoded
    'Docker down' string.  (Fail-closed logic test; no real scenario runs.)"""
    _freeze(tmp_path)
    monkeypatch.setattr(cli, "preflight_probe", lambda **kw: _down_probe())
    rc = cli.do_run(tmp_path, base_url="http://127.0.0.1:1", redis_url="redis://127.0.0.1:6379/0")
    assert rc == 3  # BLOCKED_REAL_DEPENDENCY
    assert not (tmp_path / "prediction.jsonl").exists()
    manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["phases"][-1]["blockReason"] == "BLOCKED_REAL_DEPENDENCY"
    assert manifest["phases"][-1]["probeEvidence"] == _down_probe()
    assert [p["phase"] for p in manifest["phases"]] == ["freeze", "run"]
    detail = manifest["phases"][-1]["detail"]
    assert isinstance(detail, list) and any(
        "retrievalChainUp=False" in d for d in detail
    ), detail
    # Run was NOT completed (freeze stamp untouched; run phase not marked).
    guard = PhaseGuard(tmp_path)
    assert guard.phases_done == ["freeze"]


def test_verify_reports_blocked_state_honestly(tmp_path, runtime, monkeypatch):
    """verify in the blocked state validates the frozen subset and reports the
    blocker as verified-blocked with a NON-ZERO exit — never a WARN + exit 0."""
    _freeze(tmp_path)
    monkeypatch.setattr(cli, "preflight_probe", lambda **kw: _down_probe())
    assert cli.do_run(tmp_path, base_url="http://127.0.0.1:1",
                      redis_url="redis://127.0.0.1:6379/0") == 3
    rc = cli.do_verify(tmp_path)
    assert rc == 3
    manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest.get("verdict") == "verified-blocked"
    assert manifest["secretScan"] == {"checked": True, "hits": []}


def test_five_schemas_pass_draft202012_metaschema():
    meta = Draft202012Validator.META_SCHEMA
    assert set(lab.SCHEMA_FILENAMES) == {
        "agentic_scenario_input_v0", "agentic_scenario_oracle_private_v0",
        "agentic_scenario_fault_private_v0", "agentic_scenario_prediction_v0",
        "agentic_scenario_score_report_v0",
    }
    for name in lab.SCHEMA_FILENAMES:
        schema = json.loads((lab.SCHEMAS_DIR / lab.SCHEMA_FILENAMES[name]).read_text(encoding="utf-8"))
        assert list(Draft202012Validator(meta).iter_errors(schema)) == [], name


def test_freeze_produces_valid_records(tmp_path):
    world, receipt_path = _freeze(tmp_path)
    for kind in ("input", "oracle", "fault"):
        records = lab.load_records(tmp_path / f"{'scenario.input' if kind=='input' else kind + '.private'}.jsonl", kind)
        assert len(records) == len(SCENARIO_IDS)
    assert (tmp_path / "README.md").exists()
    assert (tmp_path / cli.FIXTURE_RECEIPT_FILENAME).exists()
    manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
    assert [p["phase"] for p in manifest["phases"]] == ["freeze"]
    assert len(manifest["files"]) >= 4
    # Per-phase code SHA triple is recorded in the freeze phase.
    code = manifest["phases"][0]["code"]
    assert set(code) == {"runnerModuleSha256", "cliSha256", "acceptedEvaluatorSha256"}
    assert code["runnerModuleSha256"] == cli._code_shas()["runnerModuleSha256"]


def test_freeze_acceptable_universes_are_hard_rule_derived():
    catalog = cli.derive_catalog()
    docs = [json.loads(line) for line in cli.CATALOG_DOCS.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {d["product_id"]: d for d in docs}
    for pid in catalog["acceptableLng001"] + catalog["acceptableLng002"]:
        doc = by_id[pid]
        assert not cli._is_apple(doc.get("brand"))
    # Every LNG-002-acceptable id is within 3000 元 and a superset of LNG-001.
    assert set(catalog["acceptableLng001"]) <= set(catalog["acceptableLng002"])


def test_default_data_dir_is_repair004_and_old_dir_untouched():
    assert cli.DEFAULT_DATA_DIR.name == "scenario_lab_v0_real_runner_repair004_20260819"
    # The old 001 freeze dir must never be a default write target of this CLI.
    assert DATA_DIR != cli.DEFAULT_DATA_DIR
    assert not DATA_DIR.is_relative_to(cli.DEFAULT_DATA_DIR)


# ---------------------------------------------------------------------------
# REPAIR-002 Repair 5 — offline full pipeline via the REAL public subprocess
# ---------------------------------------------------------------------------


def test_offline_full_pipeline_prepare_freeze_run_score_verify(tmp_path, runtime):
    _freeze(tmp_path)
    assert cli.do_run(tmp_path, stub=True) == 0
    assert cli.do_score(tmp_path) == 0
    assert cli.do_verify(tmp_path) == 0
    manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
    assert [p["phase"] for p in manifest["phases"]] == ["freeze", "run", "score"]
    assert manifest["ownership"]["stub"] is True
    assert manifest["secretScan"] == {"checked": True, "hits": []}
    assert "verdict" not in manifest or manifest["verdict"] != "verified-blocked"
    # Twelve schema-valid predictions aligned to the frozen scenario ids.
    preds = lab.load_records(tmp_path / "prediction.jsonl", "prediction")
    assert {r["scenarioId"] for r in preds} == SCENARIO_IDS
    # INF-001 restart_recovery attempt bound the 'actually recovered from'
    # frozen fixture ref/SHA with a SUCCESS terminal.
    receipt = load_fixture_receipt(tmp_path / cli.FIXTURE_RECEIPT_FILENAME)
    inf001 = next(r for r in preds if r["scenarioId"] == "ASL-V0-INF-REAL-001")
    restart = [a for a in inf001["observations"]["attempts"] if a["attemptKind"] == "restart_recovery"][0]
    assert restart["checkpointRef"] == receipt.checkpoint_ref
    assert restart["checkpointSha256"] == receipt.checkpoint_sha256
    assert restart["terminalCode"] == "SUCCESS"
    # INF-004 real OCC drift is observable in the prediction evidence.
    inf004 = next(r for r in preds if r["scenarioId"] == "ASL-V0-INF-REAL-004")
    obs = inf004["observations"]["attempts"]
    assert any(a["attemptKind"] == "revision_conflict" and a.get("rejected") for a in obs)
    # The subprocess evidence.json proves the drive stayed public + stub.
    runs = sorted((tmp_path / "runtime").glob("run-*"), key=lambda p: p.name)
    evidence = json.loads((runs[-1] / "public" / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "ok"
    assert evidence["stub"] is True
    assert evidence["fixture"]["receiptLoaded"] is True
    assert evidence["fixture"]["recoverySourceRef"] == receipt.checkpoint_ref
    assert evidence["fixture"]["recoverySourceSha256"] == receipt.checkpoint_sha256


def test_offline_public_dir_never_holds_private_files(tmp_path, runtime):
    _freeze(tmp_path)
    assert cli.do_run(tmp_path, stub=True) == 0
    runs = sorted((tmp_path / "runtime").glob("run-*"), key=lambda p: p.name)
    public_dir = runs[-1] / "public"
    assert {p.name for p in public_dir.iterdir()} == {
        "scenario.input.jsonl", "prediction.jsonl", "evidence.json",
    }


def test_entrypoint_stub_drive_writes_twelve_predictions(tmp_path):
    world, receipt_path = _freeze(tmp_path)
    outdir = tmp_path / "public"
    proc = _run_entrypoint(tmp_path / "scenario.input.jsonl", outdir,
                           extra_args=("--stub", "--fixture-receipt", str(receipt_path)))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    preds = lab.load_records(outdir / "prediction.jsonl", "prediction")
    assert len(preds) == len(SCENARIO_IDS)
    assert {r["scenarioId"] for r in preds} == SCENARIO_IDS
    evidence = json.loads((outdir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "ok"
    assert evidence["stub"] is True
    assert evidence["fixture"]["receiptLoaded"] is True
    assert evidence["fixture"]["recoverySourceRef"] == evidence["fixture"]["checkpointRef"]


def test_entrypoint_fails_closed_on_private_env_injection(tmp_path):
    world, receipt_path = _freeze(tmp_path)
    outdir = tmp_path / "public-env"
    proc = _run_entrypoint(tmp_path / "scenario.input.jsonl", outdir,
                           extra_args=("--stub",),
                           extra_env={"ORACLE_PRIVATE_PATH": r"C:\truth\oracle.private.jsonl"})
    assert proc.returncode == 4
    evidence = json.loads((outdir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "isolation_fail"
    assert any("ORACLE_PRIVATE_PATH" in v for v in evidence["isolationViolations"])


def test_entrypoint_fails_closed_on_private_argv(tmp_path):
    world, receipt_path = _freeze(tmp_path)
    outdir = tmp_path / "public-argv"
    proc = _run_entrypoint(tmp_path / "scenario.input.jsonl", outdir,
                           extra_args=("--stub", "--base-url", "http://x/oracle.private/fault"))
    assert proc.returncode == 4
    evidence = json.loads((outdir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "isolation_fail"


def test_entrypoint_fails_closed_on_private_cwd_sibling(tmp_path):
    world, receipt_path = _freeze(tmp_path)
    outdir = tmp_path / "public-cwd"
    outdir.mkdir()
    (outdir / "oracle.private.jsonl").write_text("{}", encoding="utf-8")
    proc = _run_entrypoint(tmp_path / "scenario.input.jsonl", outdir, extra_args=("--stub",))
    assert proc.returncode == 4
    evidence = json.loads((outdir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "isolation_fail"
    assert any("cwd sibling" in v for v in evidence["isolationViolations"])


def test_entrypoint_stub_fails_closed_on_fixture_drift(tmp_path):
    """A fixture receipt whose ref no longer matches the re-registered fixture
    (drift) must fail closed at the subprocess boundary, not silently bind."""
    world, receipt_path = _freeze(tmp_path)
    data = json.loads(receipt_path.read_text(encoding="utf-8"))
    data["checkpointRef"] = "cp-drifted-0000000000"
    data["receiptSha256"] = ""
    drifted = FixtureReceipt.from_dict(data)
    write_fixture_receipt(receipt_path, drifted)  # re-sign over the drifted content
    outdir = tmp_path / "public-drift"
    proc = _run_entrypoint(tmp_path / "scenario.input.jsonl", outdir,
                           extra_args=("--stub", "--fixture-receipt", str(receipt_path)))
    assert proc.returncode == 4
    evidence = json.loads((outdir / "evidence.json").read_text(encoding="utf-8"))
    assert evidence["status"] == "fixture_drift_stub"


# ---------------------------------------------------------------------------
# Repair 5 — fail-closed verify on mismatch (never WARN + exit 0)
# ---------------------------------------------------------------------------


def test_verify_fails_closed_on_code_sha_drift(tmp_path, runtime):
    _freeze(tmp_path)
    assert cli.do_run(tmp_path, stub=True) == 0
    assert cli.do_score(tmp_path) == 0
    manifest_path = tmp_path / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["phases"][0]["code"]["cliSha256"] = "0" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert cli.do_verify(tmp_path) == 4


def test_verify_fails_closed_on_secret_scan_hit(tmp_path, runtime):
    _freeze(tmp_path)
    assert cli.do_run(tmp_path, stub=True) == 0
    assert cli.do_score(tmp_path) == 0
    with open(tmp_path / "README.md", "a", encoding="utf-8") as fh:
        fh.write("\nDEEPSEEK_API_KEY=sk-fake-secret-1234567890\n")
    assert cli.do_verify(tmp_path) == 4


def test_secret_scan_distinguishes_internal_step_ids_from_credentials():
    assert runner.scan_secrets('"stepId":"sk-11f27d4ac7ce4f90"') == []
    assert runner.scan_secrets("sk-" + "a" * 32) == ["sk-" + "a" * 32]


# ---------------------------------------------------------------------------
# six-artifact validation (only when the real run produced them)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not (DATA_DIR / "prediction.jsonl").exists(),
                    reason="real run artifacts not produced yet")
def test_real_artifacts_pass_schema_and_cardinality():
    assert (DATA_DIR / "scenario.input.jsonl").exists()
    assert (DATA_DIR / "oracle.private.jsonl").exists()
    assert (DATA_DIR / "fault.private.jsonl").exists()
    assert (DATA_DIR / "score-report.json").exists()
    assert (DATA_DIR / "run-manifest.json").exists()

    # Four JSONL kinds validate against the accepted evaluator.
    for kind in ("input", "oracle", "fault", "prediction"):
        records = lab.load_records(DATA_DIR / _kind_filename(kind), kind)
        assert records, kind
    report = json.loads((DATA_DIR / "score-report.json").read_text(encoding="utf-8"))
    lab.validate_report(report)

    # Six scenario ids aligned across the four JSONL files.
    sets = {}
    for kind in ("input", "oracle", "fault", "prediction"):
        sets[kind] = {r["scenarioId"] for r in lab.load_records(DATA_DIR / _kind_filename(kind), kind)}
    assert sets["input"] == SCENARIO_IDS == sets["oracle"] == sets["fault"] == sets["prediction"]


def _kind_filename(kind: str) -> str:
    return {
        "input": "scenario.input.jsonl",
        "oracle": "oracle.private.jsonl",
        "fault": "fault.private.jsonl",
        "prediction": "prediction.jsonl",
    }[kind]
