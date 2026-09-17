"""Persist no-model review path/byte-equivalence verification."""
import json
from pathlib import Path
import subprocess
import sys
import time

from agent.evaluation.context_history_review_v2_20260905.run import load_samples, prepare
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, ROOT, file_sha, write_new


def run(output):
    source_paths = [*Path(ROOT / "agent/evaluation/context_history_review_v2_20260905").glob("*.py"),
        ROOT / "agent/evaluation/context_history_diagnostics_v1_20260905/resume_review.py", Path(__file__),
        HERE / "review_conversation.py", HERE / "review_evidence.py"]
    sources = {str(path): file_sha(path) for path in source_paths}
    command = [sys.executable, "-X", "utf8", "-B", "-m", "unittest",
        "agent.evaluation.context_history_review_v2_20260905.test_cohort_paths",
        "agent.evaluation.context_history_review_v2_20260905.test_loader", "-v"]
    started = time.perf_counter()
    tests = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=180)
    samples, mapping = load_samples(["core24_v7_iphone_cohort001/A001"], 8)
    _, _, audit = prepare(samples[0], [7, 8])
    drift = [path for path, digest in sources.items() if file_sha(path) != digest]
    write_new(output, {"kind": "NO_MODEL_REVIEW_PATH_AND_INPUT_EQUIVALENCE_VERIFICATION",
        "status": "PASS" if tests.returncode == 0 and not drift else "HOLD",
        "sourceHashes": sources, "sourceDrift": drift, "command": command,
        "testExitCode": tests.returncode, "stdout": tests.stdout, "stderr": tests.stderr,
        "durationSeconds": time.perf_counter() - started, "newCohortClosedPrefixMapping": mapping,
        "newCohortPacketAudit": audit, "modelCalls": 0,
        "note": "Legacy complete24 review packets/mapping remain byte-hash equal; declared nested A closed-prefix loads and losslessly encodes. No B/C future turns or review judgments generated."})
    print(json.dumps({"testExitCode": tests.returncode, "sourceDrift": drift,
        "newCohortPacketTokens": audit["applicationRequestTokens"], "modelCalls": 0}))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
