"""Persist targeted red/green tests and source bindings, without model calls."""
import argparse
from pathlib import Path
import subprocess
import sys
import time

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, ROOT, file_sha, write_new


def run(output, names=None):
    modules = ["agent.evaluation.context_history_diagnostics_v1_20260905." + name
        for name in (names or ("test_budget_implementation", "test_transport_recovery"))]
    sources = [ROOT / "agent/app/llm.py", ROOT / "agent/app/context_history.py",
        ROOT / "agent/app/control/react_context.py", ROOT / "agent/app/control/react_decision.py",
        HERE / "subscription.py", HERE / "history_lookup.py", HERE / "history_strategies.py",
        *Path(__file__).parent.glob("*.py")]
    hashes = {str(p.relative_to(ROOT)): file_sha(p) for p in sources}
    command = [sys.executable, "-X", "utf8", "-B", "-m", "unittest", *modules, "-v"]
    start = time.perf_counter()
    process = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", capture_output=True, timeout=180)
    value = {"kind": "TARGETED_REPAIR_TEST_RECORD", "exitCode": process.returncode,
        "command": command, "sourceHashes": hashes,
        "sourceDrift": [name for name, digest in hashes.items() if file_sha(ROOT / name) != digest],
        "stdout": process.stdout, "stderr": process.stderr, "durationSeconds": time.perf_counter() - start,
        "modelCalls": 0, "formalAcceptance": False}
    write_new(output, value)
    print({"exitCode": process.returncode, "sourceDrift": value["sourceDrift"]})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--modules", nargs="+")
    args = parser.parse_args()
    run(args.output, args.modules)
