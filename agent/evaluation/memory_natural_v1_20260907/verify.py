"""Append-only zero-model verification record for the first edition."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[3]
OUT = Path("D:/agent-experiments/memory-natural-v1-20260907")


def main():
    directory = OUT / sys.argv[1]
    directory.mkdir(exist_ok=False)
    files = sorted(str(p.relative_to(ROOT / "agent"))
                   for p in (ROOT / "agent/tests").glob("test_memory*.py"))
    files += ["tests/test_long_term_memory_contract.py", "tests/test_react_context.py",
              "tests/test_react_decision.py", "tests/test_graph_v2_react_policy.py",
              "tests/test_graph_v2_react_durable_integration.py", "tests/test_react_runtime_config.py"]
    command = [sys.executable, "-m", "pytest", *files, "-q", "--disable-warnings",
               "--junitxml=" + str(directory / "junit.xml")]
    before = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (ROOT / "agent/app").rglob("*.py")}
    started = time.monotonic()
    with (directory / "stdout.txt").open("x", encoding="utf-8") as stdout:
        result = subprocess.run(command, cwd=ROOT / "agent", stdout=stdout,
            stderr=subprocess.STDOUT, timeout=300,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    changed = [path for path, expected in before.items()
               if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected]
    receipt = {"command": command, "exitCode": result.returncode,
               "durationSeconds": time.monotonic()-started, "modelCalls": 0,
               "sourceSha256": before, "sourcesChangedDuringTests": changed}
    (directory / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in receipt.items() if key != "sourceSha256"}), flush=True)
    print((directory / "stdout.txt").read_text(encoding="utf-8")[-6000:])
    raise SystemExit(result.returncode or bool(changed))


if __name__ == "__main__":
    main()
