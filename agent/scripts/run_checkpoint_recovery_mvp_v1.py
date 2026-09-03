"""Run the bounded checkpoint-recovery idempotency smoke as new-only evidence.

The runner owns observations: it runs the real graph tests that inject the
after-receipt fault, restart the same durable thread, resume an interrupt, and
exercise an expired inbox lease.  A scenario name or test target disappearing
is itself a failed experiment; this script never silently shortens the matrix.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from datetime import UTC, datetime


ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = {
    "after_receipt_kill_restart": (
        "agent/tests/test_graph_v2_process_restart.py::"
        "GraphV2ProcessRestartTests::"
        "test_restart_after_dangerous_window_keeps_live_tool_count_one"
    ),
    "exact_interrupt_resume_replay": (
        "agent/tests/test_graph_v2_idempotent_resume.py::"
        "GraphV2IdempotentResumeTests::"
        "test_exact_answer_replay_is_idempotent_no_new_revision_tool_or_checkpoint"
    ),
    "expired_inbox_unknown": (
        "agent/tests/test_executor.py::ExecutorStepClaimTests::"
        "test_expired_claim_is_marked_unknown_and_never_requeued"
    ),
}
SOURCE_FILES = (
    "agent/app/executor.py",
    "agent/app/graph/nodes/executor.py",
    "agent/app/graph/resume.py",
    "agent/tests/test_executor.py",
    "agent/tests/test_graph_v2_process_restart.py",
    "agent/tests/test_graph_v2_idempotent_resume.py",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_dump(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=True, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    out_dir = args.out_dir.resolve()
    if out_dir.exists():
        raise SystemExit(f"new-only evidence directory already exists: {out_dir}")
    out_dir.mkdir(parents=True)

    missing_sources = [name for name in SOURCE_FILES if not (ROOT / name).is_file()]
    observations: list[dict[str, object]] = []
    if missing_sources:
        observations.append({"scenario": "source_inventory", "status": "FAILED", "missing": missing_sources})
    else:
        for scenario, target in SCENARIOS.items():
            command = [sys.executable, "-m", "pytest", target, "-q"]
            completed = subprocess.run(
                command, cwd=ROOT, text=True, capture_output=True, check=False
            )
            observations.append({
                "scenario": scenario,
                "status": "PASSED" if completed.returncode == 0 else "FAILED",
                "runnerOwned": True,
                "target": target,
                "returnCode": completed.returncode,
                "stdoutSha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
                "stderrSha256": hashlib.sha256(completed.stderr.encode("utf-8")).hexdigest(),
            })
            (out_dir / f"{scenario}.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (out_dir / f"{scenario}.stderr.txt").write_text(completed.stderr, encoding="utf-8")

    sources = {name: _sha256(ROOT / name) for name in SOURCE_FILES if (ROOT / name).is_file()}
    _canonical_dump(out_dir / "source-hashes.json", sources)
    _canonical_dump(out_dir / "observations.json", observations)
    manifest = {
        "identity": "checkpoint-recovery-mvp-v1",
        "createdAt": datetime.now(UTC).isoformat(),
        "runnerOwned": True,
        "sourceHashesSha256": _sha256(out_dir / "source-hashes.json"),
        "observationsSha256": _sha256(out_dir / "observations.json"),
        "status": "PASS" if len(observations) == len(SCENARIOS) and all(item["status"] == "PASSED" for item in observations) else "FAIL",
        "scope": "read_only_tools_only",
    }
    _canonical_dump(out_dir / "manifest.json", manifest)
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
