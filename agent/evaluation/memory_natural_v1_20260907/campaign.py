"""Sequential bounded batches; retains failures and never overwrites an attempt."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .trajectories import DEV, VALIDATION, VALIDATION_ATTEMPT

ROOT = Path(__file__).resolve().parents[3]
OUT = Path("D:/agent-experiments/memory-natural-v1-20260907")


def run_batch(batch):
    if batch == "dev":
        selected = DEV
    elif batch == "validation-a":
        selected = VALIDATION[:3]
    elif batch == "validation-b":
        selected = VALIDATION[3:]
    else:
        raise ValueError("unknown batch")
    if batch != "dev":
        for trajectory in DEV:
            for arm in ("M0", "M1", "M2"):
                report = json.loads((OUT / f"flow-{trajectory['id']}-{arm}-001/report.json").read_text(encoding="utf-8"))
                if report.get("complete") is not True:
                    raise RuntimeError("pilot incomplete")
                for episode in range(1, 4):
                    row = json.loads((OUT / f"flow-{trajectory['id']}-{arm}-001/episode-{episode}.json").read_text(encoding="utf-8"))
                    if (row["traceSummary"].get("agentStatus") != "ok" or not row["agentNativeOrdinals"]
                            or row["runId"] != row["expectedRunId"]):
                        raise RuntimeError("pilot real final model or identity gate failed")
        recovery = json.loads((OUT / "recovery001/report.json").read_text(encoding="utf-8"))
        if recovery["status"] != "PASS":
            raise RuntimeError("recovery not accepted")
    receipt = OUT / ("batch-" + batch + ("-" + VALIDATION_ATTEMPT if batch != "dev" else "") + ".json")
    if receipt.exists():
        raise RuntimeError("batch already consumed")
    started = time.monotonic()
    observed = []
    before = len(list(OUT.glob("*/model_calls/call-*")))
    for index, trajectory in enumerate(selected):
        for arm in (("M0", "M1", "M2"), ("M1", "M2", "M0"), ("M2", "M0", "M1"))[index % 3]:
            attempt = "001" if batch == "dev" else VALIDATION_ATTEMPT
            name = f"flow-{trajectory['id']}-{arm}-{attempt}"
            output = OUT / name
            if output.exists():
                # Only complete already-published attempts may be reused.
                report = json.loads((output / "report.json").read_text(encoding="utf-8"))
                if report.get("complete") is not True:
                    raise RuntimeError("existing failed attempt requires explicit repair version")
                observed.append({"name": name, "reused": True})
                continue
            if time.monotonic()-started >= 3600 or len(list(OUT.glob("*/model_calls/call-*")))-before >= 120:
                raise RuntimeError("batch hard ceiling reached")
            print("FLOW_STARTED " + name, flush=True)
            with (OUT / (name + ".log")).open("x", encoding="utf-8") as log:
                result = subprocess.run([sys.executable, "-m", "agent.evaluation.memory_natural_v1_20260907.flow",
                    name, trajectory["id"], arm], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                    timeout=min(1800, max(1, 3600-(time.monotonic()-started))),
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            observed.append({"name": name, "exitCode": result.returncode})
            print("FLOW_FINISHED " + json.dumps(observed[-1]), flush=True)
            complete = json.loads((output / "report.json").read_text(encoding="utf-8")).get("complete") if (output / "report.json").exists() else False
            if result.returncode or complete is not True:
                receipt.write_text(json.dumps({"batch": batch, "status": "FAILED_GATE", "runs": observed}, indent=2), encoding="utf-8")
                raise RuntimeError("flow failed runtime or real-model gate; batch stopped")
    receipt.write_text(json.dumps({"batch": batch, "runs": observed,
        "durationSeconds": time.monotonic()-started,
        "nativeCalls": len(list(OUT.glob("*/model_calls/call-*")))-before}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    run_batch(sys.argv[1])
