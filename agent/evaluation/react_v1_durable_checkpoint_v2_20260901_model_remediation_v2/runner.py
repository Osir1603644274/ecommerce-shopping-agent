"""Exactly-three, no-retry real-model reachability remediation runner."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import redis

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from evaluation.react_v1_durable_checkpoint_v2_20260901_v1.runner import (
    _bootstrap,
    _free_port,
    _run_process,
    _state_summary,
    _worker_command,
)

IDENTITY = "react-v1-durable-real-model-reachability-remediation-20260901-v2"
SOURCE_FILES = (
    "app/control/react_context.py",
    "app/control/react_decision.py",
    "app/graph/nodes/react_policy.py",
    "app/graph/resume.py",
    "app/graph/checkpoint.py",
    "evaluation/react_v1_durable_checkpoint_v2_20260901_v1/runner.py",
    "evaluation/react_v1_durable_checkpoint_v2_20260901_model_remediation_v2/runner.py",
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _write(path: Path, value: object) -> None:
    path.write_bytes(_canonical(value) + b"\n")


def run(out_dir: Path) -> dict[str, Any]:
    out_dir = out_dir.resolve()
    if out_dir.exists():
        raise FileExistsError(f"new-only attempt already exists: {out_dir}")
    out_dir.mkdir(parents=True)
    redis_server = shutil.which("redis-server")
    if not redis_server:
        raise RuntimeError("redis-server is required")
    port = _free_port()
    server = subprocess.Popen(
        [redis_server, "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    sync = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    observations: dict[str, Any] = {
        "identity": IDENTITY, "startedAt": datetime.now(UTC).isoformat(),
        "redis": {"pid": server.pid, "port": port, "realProcess": True},
        "runs": [],
    }
    specs = (
        ("R01", "phone", "请基于当前条件继续；之前提到的旧手机候选已失效。"),
        ("R02", "laptop", "请继续推荐笔记本；之前引用的旧候选不再属于当前范围。"),
        ("R03", "headphones", "请继续推荐耳机；上轮候选引用已经过期。"),
    )
    try:
        for _ in range(100):
            try:
                if sync.ping():
                    break
            except redis.RedisError:
                time.sleep(0.05)
        for case_id, category, message in specs:
            identity = asyncio.run(_bootstrap(
                port, reason="stale_candidate_reference",
                category=category, message=message,
            ))
            process = _run_process(
                _worker_command(
                    port=port, identity=identity,
                    run_id=f"run-remediation-{case_id.lower()}",
                    mode="fresh", real_model=True, user_message=message,
                ),
                timeout=60,
            )
            observations["runs"].append({
                "id": case_id, "category": category, "identity": identity,
                "process": process,
                "finalState": _state_summary(port, identity["taskId"]),
            })
        observations["completedAt"] = datetime.now(UTC).isoformat()
        source_hashes = {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in SOURCE_FILES
        }
        errors: list[str] = []
        calls: list[int] = []
        failures = 0
        tokens = 0
        latency: list[float] = []
        for row in observations["runs"]:
            process = row["process"]
            result = process.get("result") or {}
            events = result.get("modelEvents") or []
            calls.append(len(events))
            if process.get("returnCode") != 0 or not 1 <= len(events) <= 2:
                errors.append(f"contract:{row['id']}")
            if result.get("boundary") not in {
                "task_completed", "stop_turn", "clarification", "state_diverged"
            }:
                errors.append(f"boundary:{row['id']}")
            for event in events:
                failures += int(bool(event.get("failed")))
                tokens += int(event.get("totalTokens") or 0)
                latency.append(float(event.get("durationMs") or 0))
                if not event.get("modelCallId") or not event.get("contextBindingHash"):
                    errors.append(f"binding:{row['id']}")
        freeze = json.loads(
            (PACKAGE / "IMPLEMENTATION_FREEZE_RECEIPT.json").read_text(encoding="utf-8")
        )
        if source_hashes != freeze.get("sourceSha256"):
            errors.append("source_drift")
        score = {
            "schemaVersion": 1, "identity": IDENTITY,
            "status": "LAYER_B_REMEDIATION_ACCEPT" if not errors else "FAIL",
            "runCount": len(observations["runs"]),
            "modelCallsPerRun": calls,
            "providerFailureCount": failures,
            "totalTokens": tokens,
            "modelLatencyMs": latency,
            "errors": sorted(set(errors)),
            "originalAttemptStatusUnchanged": "FAIL",
        }
        _write(out_dir / "observations.json", observations)
        _write(out_dir / "source-hashes.json", source_hashes)
        _write(out_dir / "score.json", score)
        _write(out_dir / "manifest.json", {
            "schemaVersion": 1, "identity": IDENTITY,
            "createdAt": datetime.now(UTC).isoformat(),
            "observationsSha256": hashlib.sha256(_canonical(observations)).hexdigest(),
            "sourceHashesSha256": hashlib.sha256(_canonical(source_hashes)).hexdigest(),
            "scoreSha256": hashlib.sha256(_canonical(score)).hexdigest(),
            "preregistrationSha256": hashlib.sha256(
                (PACKAGE / "preregistration.json").read_bytes()
            ).hexdigest(),
            "scenarioMatrixSha256": hashlib.sha256(
                (PACKAGE / "scenario-matrix.json").read_bytes()
            ).hexdigest(),
            "implementationFreezeSha256": hashlib.sha256(
                (PACKAGE / "IMPLEMENTATION_FREEZE_RECEIPT.json").read_bytes()
            ).hexdigest(),
        })
        return score
    finally:
        sync.close()
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    score = run(args.out_dir)
    print(json.dumps(score, sort_keys=True))
    return 0 if score.get("status") == "LAYER_B_REMEDIATION_ACCEPT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
