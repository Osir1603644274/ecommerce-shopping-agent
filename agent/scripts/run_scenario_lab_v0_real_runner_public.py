"""Public-only entrypoint for the Scenario Lab V0 real runner (REPAIR-004).

Spawned by the orchestrator ``run`` phase in a dedicated subprocess with a
clean argv/env/cwd.  This module imports ONLY the public runner/runtime module
``evaluation.scenario_lab_v0_real_runner`` plus the standard library.  The
private oracle/fault/score files never exist in its cwd and never appear in
its argv or environment; any injected private marker (path, env value, argv
value, or private sibling file in cwd) fails closed with exit code 4.

Drives the 12 frozen V0 scenarios with the deterministic in-memory stub world when
``--stub`` is given (offline protocol validation) or against the real durable
control plane otherwise.  Writes ``prediction.jsonl`` and ``evidence.json``
into the task-owned public runtime dir.

REPAIR-003: INF-001's session/task/thread identity is taken from the SIGNED
fixture receipt (never a generated ``sessions['inf001']``), and the real drive
performs an Agent active-backend read-back (health + owned durable trace)
that fails BLOCKED when the Agent's active execution is unobservable.

REPAIR-004: the real drive gates the WHOLE run on an owned active-backend
attestation — live /health must report the process's actually-loaded
``backendBaseUrl`` (``http://127.0.0.1:18082``) and ``productRetrievalMode``
(``bm25``) field-for-field, the owned durable ``agent-run-trace`` must match
the INF-001 identity from the receipt, and the owned persisted
``task-state:<taskId>`` ``stepExecutionResults`` must show a real successful
``search_products`` with ``retrievalTrace.channels.elasticsearch.status ==
active`` and ``bm25.status == active`` and no active ``mysqlFallback``.  Any
missing/unowned/degraded/executor-only/false-positive evidence is BLOCKED
(exit 3, no prediction.jsonl).  Works for the owned server AND an explicit
``--base-url`` pointing at an existing Agent (that process is never started
or stopped here).

Exit codes:
  0  twelve predictions + evidence.json written
  2  dependency missing (real drive without base_url/redis_url, or unhealthy owned server)
  3  blocked
  4  fail-closed isolation / input / receipt / schema violation
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

# Public-only import surface: the runner/runtime module (which itself imports
# only the accepted public evaluator + stdlib) + stdlib.  Nothing else.
from evaluation.scenario_lab_v0_real_runner import (  # noqa: E402
    FORBIDDEN_PRIVATE_FILENAMES,
    SEARCH_JAVA_BASE_URL,
    TASK_OWNED_SESSION_PREFIX,
    agent_active_backend_readback,
    probe_http,
    DurableChatClient,
    FakeTaskStateDriftAdapter,
    FixtureReceiptError,
    ObservationMapper,
    RedisCheckpointFixtureClient,
    RedisEvidenceAdapter,
    RedisTaskStateDriftAdapter,
    StubDurableClient,
    StubEvidenceAdapter,
    StubServerHost,
    StubWorld,
    TaskOwnedServer,
    find_free_port,
    load_fixture_receipt,
    load_public_input,
    path_has_private_truth_marker,
    prediction_record,
    public_environment_has_private_truth,
    run_scenarios,
    server_env_overrides,
    sha256_text,
    validate_record,
    write_json_atomic,
    write_jsonl_atomic,
)

SESSION_KEYS = (
    "lng001", "lng002", "lng003", "lng004", "lng005", "lng006", "lng007", "lng008",
    "inf001", "inf002", "inf003", "inf004",
)


def _isolation_violations(argv: Sequence[str], environ: Mapping[str, str]) -> list[str]:
    """Fail-closed argv/env/cwd scan for injected private truth (Repair 1)."""
    violations: list[str] = []
    for arg in argv[1:]:
        if path_has_private_truth_marker(arg):
            violations.append(f"argv: {arg}")
    for key in public_environment_has_private_truth(environ):
        violations.append(f"env: {key}")
    cwd = Path.cwd()
    try:
        names = [p.name for p in cwd.iterdir() if p.is_file()]
    except OSError:
        names = []
    for name in names:
        if name.lower() in FORBIDDEN_PRIVATE_FILENAMES:
            violations.append(f"cwd sibling: {name}")
    return violations


def _write_evidence(outdir: Path, **fields: Any) -> Path:
    path = outdir / "evidence.json"
    write_json_atomic(path, fields)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Scenario Lab V0 real runner — public-only entrypoint")
    parser.add_argument("--input", required=True, help="public scenario.input.jsonl (must pass the leak audit)")
    parser.add_argument("--outdir", required=True, help="task-owned public runtime dir (no private files)")
    parser.add_argument("--base-url", default=None, help="durable app base URL (real drive)")
    parser.add_argument("--redis-url", default=None, help="read-only task-owned Redis URL")
    parser.add_argument("--fixture-receipt", default=None, help="signed fixture receipt (prepare->freeze binding)")
    parser.add_argument("--stub", action="store_true", help="offline deterministic stub world (no backend, no model)")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())

    # ── Fail-closed isolation: argv / env / cwd must be clean ───────────────
    violations = _isolation_violations(sys.argv, os.environ)
    if violations:
        _write_evidence(
            outdir,
            provenance="asl-v0-real-runner-repair-004-public-subprocess",
            status="isolation_fail",
            stub=args.stub,
            isolationViolations=violations,
        )
        print("ISOLATION FAIL: public-only entrypoint detected private truth injection:", violations)
        return 4

    # ── Public input: accepted leak audit, frozen expansion cardinality ─────
    try:
        views = load_public_input(args.input)
    except Exception as exc:  # noqa: BLE001 - leak/format rejection is fail-closed
        _write_evidence(outdir, status="input_rejected", stub=args.stub, error=type(exc).__name__)
        print("INPUT REJECTED: public-only loader failed:", type(exc).__name__, exc)
        return 4
    if len(views) != 12:
        _write_evidence(outdir, status="input_rejected", stub=args.stub, viewCount=len(views))
        print(f"INPUT REJECTED: expected 12 public views, got {len(views)}")
        return 4

    # ── Fixture receipt: optional but fail-closed when present ──────────────
    receipt = None
    if args.fixture_receipt:
        try:
            receipt = load_fixture_receipt(args.fixture_receipt)
        except FixtureReceiptError as exc:
            _write_evidence(outdir, status="fixture_receipt_rejected", stub=args.stub, error=str(exc))
            print("RECEIPT REJECTED:", exc)
            return 4

    mapper = ObservationMapper(session_id="")
    sessions = {key: f"{TASK_OWNED_SESSION_PREFIX}-{key}-{stamp}" for key in SESSION_KEYS}
    # REPAIR-003: INF-001's session/task/thread identity comes from the SIGNED
    # receipt (never a generated sessions['inf001']); the INF-001 client, the
    # restart client and the recovery request all reuse that session.
    if receipt is not None:
        sessions["inf001"] = receipt.session_id

    stub = args.stub
    if stub:
        rc, evidence = _drive_stub(outdir, sessions, receipt, mapper, stamp, views)
    else:
        rc, evidence = _drive_real(
            outdir, sessions, receipt, mapper, stamp,
            base_url=args.base_url, redis_url=args.redis_url, scenarios=views,
        )
    if rc != 0:
        _write_evidence(outdir, provenance="asl-v0-real-runner-repair-004-public-subprocess", **evidence)
        print(f"ENTRYPOINT FAILED: status={evidence.get('status')} rc={rc}")
        return rc
    _write_evidence(outdir, provenance="asl-v0-real-runner-repair-004-public-subprocess", **evidence)
    print(f"ENTRYPOINT OK: {len(evidence.get('scenarioIds', []))} scenarios, stub={stub}")
    return 0


def _drive_stub(
    outdir: Path,
    sessions: Mapping[str, str],
    receipt: Any,
    mapper: ObservationMapper,
    stamp: int,
    scenarios: Sequence[Any],
) -> tuple[int, dict[str, Any]]:
    """Offline deterministic stub drive (no backend, no model, no Redis)."""
    world = StubWorld()
    if receipt is not None:
        world.register_fixture(fixture_id=receipt.fixture_id, created_code_sha256=receipt.created_code_sha256)
        ref, sha = world.checkpoints[receipt.thread_id]["ref"], world.checkpoints[receipt.thread_id]["sha"]
        if ref != receipt.checkpoint_ref or sha != receipt.checkpoint_sha256:
            return 4, {"status": "fixture_drift_stub", "stub": True,
                       "expectedRef": receipt.checkpoint_ref, "actualRef": ref}
    base_url = "http://stub"
    client_factory = lambda sid: StubDurableClient(base_url, sid, world=world)
    adapter = StubEvidenceAdapter(world)
    holder: dict[str, Any] = {}

    def restart_server_factory() -> Any:
        server = StubServerHost(world)
        holder["server"] = server
        return server

    def restart_client_factory(sid: str) -> Any:
        return StubDurableClient(base_url, sid, world=world)

    server_host = StubServerHost(world)
    results = run_scenarios(
        client_factory,
        adapter,
        mapper,
        dict(sessions),
        server_host=server_host,
        restart_server_factory=restart_server_factory,
        restart_client_factory=restart_client_factory,
        fixture_receipt=receipt,
        run_root=outdir,
        base_url=base_url,
        drift_adapter=FakeTaskStateDriftAdapter(world),
        scenarios=scenarios,
    )
    predictions = [_prediction(i + 1, r) for i, r in enumerate(results)]
    for pred in predictions:
        validate_record(pred, "prediction")
    write_jsonl_atomic(outdir / "prediction.jsonl", predictions)
    return 0, _evidence(outdir, sessions, results, predictions, receipt, stub=True,
                         base_url=base_url, ownership={
                             "sessions": dict(sessions),
                             "serverPorts": [],
                             "processPids": [],
                             "serversKilled": world.servers_killed,
                             "restartOccurred": world.restart_occurred,
                             "stub": True,
                         },
                         active_backend_readback={
                             "activeBackendReadable": False,
                             "reason": "stub/offline drive has no real Agent health or durable trace",
                             "checked": False,
                         })


def _drive_real(
    outdir: Path,
    sessions: Mapping[str, str],
    receipt: Any,
    mapper: ObservationMapper,
    stamp: int,
    *,
    base_url: str | None,
    redis_url: str | None,
    scenarios: Sequence[Any],
) -> tuple[int, dict[str, Any]]:
    """Real durable drive: task-owned uvicorn + real HTTP + read-only Redis."""
    if not redis_url:
        return 2, {"status": "dependency_missing", "stub": False, "note": "real drive requires --redis-url"}
    adapter = RedisEvidenceAdapter(redis_url)
    drift_adapter = RedisTaskStateDriftAdapter(redis_url)
    server_host: Any | None = None
    base_url_used = base_url
    if base_url is None:
        p1 = TaskOwnedServer(find_free_port(), outdir, server_env_overrides(redis_url=redis_url, backend_base_url=SEARCH_JAVA_BASE_URL))
        p1.start()
        if not p1.wait_health():
            p1.stop()
            adapter.close()
            return 2, {"status": "owned_server_unhealthy", "stub": False,
                       "stdoutTail": _tail(p1.stdout_log), "stderrTail": _tail(p1.stderr_log)}
        base_url_used = p1.base_url
        server_host = p1

    holder: dict[str, Any] = {}

    def restart_server_factory() -> Any:
        server = TaskOwnedServer(find_free_port(), outdir, server_env_overrides(redis_url=redis_url, backend_base_url=SEARCH_JAVA_BASE_URL))
        holder["server"] = server
        return server

    def client_factory(sid: str) -> DurableChatClient:
        return DurableChatClient(base_url_used, sid, timeout_seconds=120.0)

    readback: dict[str, Any] = {
        "activeBackendReadable": False,
        "reason": "no owned INF-001 run observed; active backend unobservable",
        "checked": False,
    }
    results: list[dict[str, Any]] = []
    try:
        results = run_scenarios(
            client_factory,
            adapter,
            mapper,
            dict(sessions),
            server_host=server_host,
            restart_server_factory=restart_server_factory,
            fixture_receipt=receipt,
            run_root=outdir,
            base_url=base_url_used,
            drift_adapter=drift_adapter,
            keep_restart_server_alive=True,
            scenarios=scenarios,
        )
        # REPAIR-004: Agent active-backend read-back — reconcile the task-owned
        # Agent's ACTUAL active execution from OBSERVED live /health (exact
        # backendBaseUrl + productRetrievalMode), the owned durable trace AND
        # the owned persisted search_products channel attestation.  Works for
        # BOTH the owned server and an explicit ``--base-url`` pointing at an
        # existing Agent (no start/stop of that external process).
        inf001_result = next(
            (r for r in results if r["scenarioId"] == "ASL-V0-INF-REAL-001"), None
        )
        if inf001_result and inf001_result.get("runId"):
            task_id = str(inf001_result.get("taskId") or "")
            run_id = str(inf001_result["runId"])
            session_id = sessions["inf001"]
            # REPAIR-005 Repair 3/4: the threadId returned by the driver is the
            # READ-BACK-CONFIRMED server-owned thread (TaskState v2RunMarker /
            # cursor) — never silently re-derived here from taskId/runId.
            thread_id = str(inf001_result.get("threadId") or "")
            if not thread_id:
                readback["reason"] = (
                    "owned INF-001 runId present but threadId missing; "
                    "active backend unobservable"
                )
            else:
                readback_base_url = base_url_used
                active_restart = holder.get("server")
                if (
                    active_restart is not None
                    and getattr(active_restart, "proc", None) is not None
                    and active_restart.proc.poll() is None
                ):
                    readback_base_url = active_restart.base_url
                health_entry = probe_http(
                    f"{readback_base_url.rstrip('/')}/health", timeout=3.0, kind="control-plane"
                )
                trace_record: Any = None
                try:
                    trace_record = adapter.trace_record(
                        run_id,
                        owner_task_id=task_id,
                        owner_session_id=session_id,
                    )
                except Exception as exc:  # noqa: BLE001 - unobservable trace is BLOCKED evidence
                    trace_record = {"traceReadError": type(exc).__name__}
                retrieval_channels: dict[str, Any] | None = None
                try:
                    retrieval_channels = adapter.retrieval_channel_projection(
                        task_id, session_id=session_id, thread_id=thread_id, run_id=run_id
                    )
                except Exception as exc:  # noqa: BLE001 - unobservable attestation is BLOCKED
                    retrieval_channels = {
                        "present": False,
                        "searchProductCalls": [],
                        "runId": run_id,
                        "reason": f"projection error: {type(exc).__name__}",
                    }
                readback = agent_active_backend_readback(
                    health_entry, trace_record,
                    expected_backend_base_url=SEARCH_JAVA_BASE_URL,
                    expected_retrieval_mode="bm25",
                    owned_task_id=task_id,
                    owned_session_id=session_id,
                    owned_run_id=run_id,
                    owned_thread_id=thread_id,
                    retrieval_channels=retrieval_channels,
                )
                readback["checked"] = True
                readback["healthHttpStatus"] = health_entry.get("httpStatus")
                readback["agentHealthStatus"] = health_entry.get("healthStatus")
                readback["retrievalChannelsProjection"] = retrieval_channels
        else:
            readback["reason"] = (
                "no owned INF-001 runId observed; active backend unobservable"
            )
    finally:
        if server_host is not None:
            server_host.stop()
        if holder.get("server") is not None:
            holder["server"].stop()
        adapter.close()

    ports = [server_host.port] if server_host is not None else []
    pids = [server_host.proc.pid if server_host is not None and server_host.proc is not None else None]
    if holder.get("server") is not None:
        restart = holder["server"]
        ports.append(getattr(restart, "port", None))
        pids.append(getattr(getattr(restart, "proc", None), "pid", None))
    ownership = {
        "sessions": dict(sessions),
        "serverPorts": [p for p in ports if p is not None],
        "processPids": [p for p in pids if p is not None],
        "stub": False,
    }

    # REPAIR-004 Repair 2e: a BLOCKED active-backend read-back gates the WHOLE
    # run — non-zero exit, NO prediction.jsonl written.  A bare /health, a wrong
    # backend/mode, a missing/unowned trace, executor-only evidence, ES
    # fallback/degraded, BM25 missing, or a foreign task/trace all fail closed.
    if not readback.get("activeBackendReadable"):
        blocked_evidence = _evidence(
            outdir, sessions, results, [], receipt, stub=False,
            base_url=base_url_used, ownership=ownership,
            active_backend_readback=readback,
        )
        blocked_evidence["status"] = "active_backend_blocked"
        blocked_evidence["blockedReason"] = readback.get("reason")
        return 3, blocked_evidence

    predictions = [_prediction(i + 1, r) for i, r in enumerate(results)]
    for pred in predictions:
        validate_record(pred, "prediction")
    write_jsonl_atomic(outdir / "prediction.jsonl", predictions)
    return 0, _evidence(outdir, sessions, results, predictions, receipt, stub=False,
                         base_url=base_url_used, ownership=ownership,
                         active_backend_readback=readback)


def _tail(log_path: Path, limit: int = 2000) -> str:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-limit:]


def _prediction(seed: int, result: dict[str, Any]) -> dict[str, Any]:
    return prediction_record(seed, result)


def _evidence(outdir: Path, sessions: Mapping[str, str], results: list[dict[str, Any]],
              predictions: list[dict[str, Any]], receipt: Any, *, stub: bool,
              base_url: str, ownership: Mapping[str, Any],
              active_backend_readback: Mapping[str, Any] | None = None) -> dict[str, Any]:
    runtime_records = []
    for result in results:
        request_id = result["attempts"][-1]["requestId"] if result["attempts"] else ""
        runtime_records.append(
            {
                "scenarioId": result["scenarioId"],
                "requestId": request_id,
                "observed": result["observed"],
                "status": "stub" if stub else "ran",
            }
        )
    fixture_ref = receipt.checkpoint_ref if receipt is not None else None
    fixture_sha = receipt.checkpoint_sha256 if receipt is not None else None
    inf001 = next((r for r in results if r["scenarioId"] == "ASL-V0-INF-REAL-001"), None)
    return {
        "status": "ok",
        "stub": stub,
        "baseUrl": base_url,
        "sessions": dict(sessions),
        "ownership": dict(ownership),
        "fixture": {
            "receiptLoaded": receipt is not None,
            "checkpointRef": fixture_ref,
            "checkpointSha256": fixture_sha,
            "recoverySourceRef": (inf001 or {}).get("observed", {}).get("recoverySourceFixtureRef"),
            "recoverySourceSha256": (inf001 or {}).get("observed", {}).get("recoverySourceFixtureSha256"),
        },
        "runtimeRecords": runtime_records,
        "scenarioIds": [r["scenarioId"] for r in results],
        "predictionSha256": sha256_text(
            (outdir / "prediction.jsonl").read_bytes() if (outdir / "prediction.jsonl").exists() else b""
        ),
        "observations": {r["scenarioId"]: r["observed"] for r in results},
        "activeBackendReadback": dict(active_backend_readback) if active_backend_readback is not None else None,
    }


if __name__ == "__main__":
    raise SystemExit(main())
