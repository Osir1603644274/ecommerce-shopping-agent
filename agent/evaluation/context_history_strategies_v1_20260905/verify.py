"""Persist reproducible verification commands, source hashes and test output."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys
import time

from .artifacts import HERE, ROOT, canonical, file_sha, now, write_new


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    sources = sorted((ROOT / "agent/app").rglob("*.py")) + sorted(HERE.glob("*.py"))
    for name in ("context_history_diagnostics_v1_20260905", "context_history_review_v2_20260905"):
        sources += sorted((HERE.parent / name).glob("*.py"))
    hashes = {str(path.relative_to(ROOT)).replace("\\", "/"): file_sha(path) for path in sources}
    write_new(output / "source_manifest.json", {"at": now(), "sources": hashes})
    module = "agent.evaluation.context_history_strategies_v1_20260905."
    jobs = [(ROOT, ["-m", "unittest", *[module + name for name in
        ("test_subscription", "test_history", "test_repairs", "test_context_client", "test_history_lookup",
         "test_resume_reask", "test_patch_preflight", "test_answer_gate", "test_review_conversation", "test_audit", "test_context_semantics_v3", "test_runner_retention")],
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_next_repairs",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_budget_after_compare",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_budget_implementation",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_transport_recovery",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_prefix_census",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_static_grid",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_note_implementation",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_note_compare_route",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_vivo_state_audit",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_stream_audit",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_display_turn_lookup",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_refresh_request",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_capacity_boundaries",
        "agent.evaluation.context_history_diagnostics_v1_20260905.test_supervisor_lifecycle",
        "agent.evaluation.context_history_review_v2_20260905.test_loader",
        "agent.evaluation.context_history_review_v2_20260905.test_evidence", "-v"]),
        (ROOT, ["-m", "pytest", "agent/tests/test_context_pack.py", "agent/tests/test_context_view.py",
                "agent/tests/test_evaluation_context_arm.py", "-q"]),
        (ROOT / "agent", ["-m", "pytest", "tests/test_run_agent.py", "tests/test_planner.py",
                           "tests/test_replanner.py", "tests/test_reference_context.py", "tests/test_validator.py",
                           "tests/test_react_context.py", "tests/test_react_decision.py",
                           "tests/test_shopping_state_authority.py", "tests/test_shopping_state_update.py", "-q"]),
        (ROOT / "agent", ["-m", "pytest", "tests/test_graph_v2_interrupt_resume.py",
                           "tests/test_graph_v2_idempotent_resume.py", "tests/test_graph_v2_react_durable_integration.py", "-q"])]
    results = []
    for ordinal, (cwd, command) in enumerate(jobs, 1):
        args = [sys.executable, "-X", "utf8", "-B", *command]
        started = time.perf_counter()
        result = subprocess.run(args, cwd=cwd, capture_output=True, encoding="utf-8", timeout=240,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        row = {"command": args, "cwd": str(cwd), "exitCode": result.returncode,
               "stdout": result.stdout, "stderr": result.stderr,
               "durationSeconds": time.perf_counter() - started}
        write_new(output / f"test-{ordinal:02d}.json", row)
        results.append(row)
        print(canonical({"job": ordinal, "exitCode": result.returncode, "durationSeconds": row["durationSeconds"]}), flush=True)
    drift = [name for name, digest in hashes.items() if file_sha(ROOT / name) != digest]
    status = "VERIFICATION_PASS" if all(row["exitCode"] == 0 for row in results) and not drift else "VERIFICATION_HOLD"
    write_new(output / "result.json", {"status": status, "sourceDrift": drift, "jobs": len(results),
                                      "formalExperimentalAcceptance": False})
    return status == "VERIFICATION_PASS"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    raise SystemExit(0 if run(parser.parse_args().output) else 2)
