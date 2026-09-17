"""Frozen, serial 24-turn development cohort. Never tunes or promotes results."""
import argparse
import json
from pathlib import Path
import sys
import uuid

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, ROOT, capture_sources, file_sha, now, verify_sources, write_new
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise
from agent.evaluation.context_history_strategies_v1_20260905.describe import describe
from agent.evaluation.context_history_diagnostics_v1_20260905.stream_audit import audit

SCRIPT = HERE / "core_dataset24_iphone001/script.json"
SCRIPT_SHA = "f3312ecebbfec3463bd6b9f46c44da42746319888215eea5297e5e71b6ee7ec5"


def schedule(output):
    common = ["--script", str(SCRIPT), "--input-budget", "96000", "--attempt-timeout", "7200"]
    variants = (("A", "A_FULL_HISTORY", []),
        ("B", "B_PACK_VIEW", ["--working-budget", "16000", "--fill-history-budget"]),
        ("C", "C_LLM_THRESHOLD", ["--working-budget", "32000", "--trigger-fraction", ".6",
            "--target-fraction", ".45", "--adaptive-summary-items"]))
    return [{"label": label, "arm": arm, "output": str(output / (label + "001")),
        "supervisor": str(output / (label + "001_supervisor")),
        "arguments": ["--arm", arm, *common, *extra]} for label, arm, extra in variants]


def run(output):
    if file_sha(SCRIPT) != SCRIPT_SHA:
        raise ValueError("short_development_script_drift")
    output.mkdir(parents=True, exist_ok=False)
    hashes = capture_sources(output / "source_snapshot")
    hashes[Path(__file__).relative_to(ROOT).as_posix()] = file_sha(__file__)
    jobs = schedule(output)
    write_new(output / "started.json", {"at": now(), "kind": "SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL",
        "script": str(SCRIPT), "scriptSha256": SCRIPT_SHA, "sources": hashes, "jobs": jobs,
        "order": "ABC", "plannedTurnsEach": 24, "maxRepeats": 1,
        "latencyClaim": "DEVELOPMENT_DESCRIPTIVE_ONLY_SINGLE_ORDER_SINGLE_FAMILY",
        "stopRule": "Preserve failures; no retries or edits inside cohort. No production promotion.",
        "budget": "Native7200s per episode and outer observed7320s; no API key fallback"})
    results = []
    for job in jobs:
        verify_sources(hashes)
        if file_sha(SCRIPT) != SCRIPT_SHA:
            raise ValueError("script_changed_during_cohort")
        attempt = Path(job["output"])
        token = uuid.uuid4().hex
        command = [sys.executable, "-X", "utf8", "-B", "-m",
            "agent.evaluation.context_history_strategies_v1_20260905.agent_smoke", str(attempt),
            "--supervised-start-token", token, *job["arguments"]]
        code = supervise(command, Path(job["supervisor"]), timeout=7320, start_token=token)
        record = {"label": job["label"], "arm": job["arm"], "childExitCode": code, "attempt": str(attempt)}
        if (attempt / "started.json").exists():
            census = describe(attempt)
            write_new(output / (job["label"] + "001_census.json"), census)
            record.update({key: census[key] for key in ("observedTurns", "dataCollectionComplete",
                "completeNativeTokenTotal", "unknownUsageCalls", "unsafeTerminalTurns")})
        if (attempt / "result.json").exists():
            # Use the existing streaming audit rather than retaining giant snapshots.
            audited = audit(attempt)
            write_new(output / (job["label"] + "001_audit.json"), audited)
            record["executionAuditStatus"] = audited["status"]
        verify_sources(hashes)
        results.append(record)
        write_new(output / (job["label"] + "001_closed.json"), record)
        print(json.dumps(record), flush=True)
    write_new(output / "result.json", {"status": "DEVELOPMENT_COHORT_COLLECTED_REQUIRES_QUALITY_REVIEW",
        "episodes": results, "sourceDrift": [], "formalAcceptance": False, "qualityAcceptance": False,
        "completeMatchedCollection": all(row.get("dataCollectionComplete") for row in results),
        "note": "Single short source family, serial ABC, no parameter search. Check C trigger counts before claiming summary impact; failed episodes remain visible."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--worker-token")
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.exists():
        raise ValueError("new_absolute_cohort_required")
    if args.worker_token:
        if sys.stdin.readline().strip() != args.worker_token:
            raise RuntimeError("start_gate_not_released")
        run(args.output)
    else:
        token = uuid.uuid4().hex
        command = [sys.executable, "-X", "utf8", "-B", "-m",
            "agent.evaluation.context_history_diagnostics_v1_20260905.serial_short_cohort",
            str(args.output), "--worker-token", token]
        raise SystemExit(supervise(command, args.output.with_name(args.output.name + "_supervisor"),
            timeout=22200, start_token=token))
