"""Two fixed v7 long development baselines; no tuning, retries or holdout use."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, ROOT, capture_sources, file_sha, now, verify_sources, write_new
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise
from agent.evaluation.context_history_strategies_v1_20260905.describe import describe
from .stream_audit import audit

PROFILES = {
    "general72": ("core_dataset72_003/script.json", "540ec27743f180a007f37cfe50364b6b6a4a66fb35a3ad1b0d28d676a88ab130", 72, "BCA"),
    "vivo48": ("core_dataset48_vivo002/script.json", "60e945655d27f36a9ffe10d5e1e39e99d458b0370a948e561440db1f91573485", 48, "CAB"),
    "firstedition48": ("core_dataset48_vivo002/script.json", "60e945655d27f36a9ffe10d5e1e39e99d458b0370a948e561440db1f91573485", 48, "ABC"),
}


def schedule(output, profile):
    name, expected, turns, order = PROFILES[profile]
    script = HERE / name
    if file_sha(script) != expected:
        raise ValueError("approved_development_script_drift")
    if len(json.loads(script.read_text(encoding="utf-8"))["turns"]) != turns:
        raise ValueError("planned_length_mismatch")
    common = ["--script", str(script), "--input-budget", "96000", "--attempt-timeout", "7200"]
    variants = {
        "A": ("A_FULL_HISTORY", []),
        "B": ("B_PACK_VIEW", ["--working-budget", "16000", "--fill-history-budget"]),
        "C": ("C_LLM_THRESHOLD", ["--working-budget", "32000", "--trigger-fraction", ".6",
            "--target-fraction", ".45", "--adaptive-summary-items"]),
    }
    if profile == "firstedition48":
        variants["B"] = ("B_PACK_VIEW", ["--working-budget", "24000", "--fill-history-budget"])
        variants["C"] = ("C_LLM_THRESHOLD", ["--working-budget", "32000", "--trigger-fraction", ".6",
            "--target-fraction", ".55", "--adaptive-summary-items"])
    jobs = [{"label": label, "arm": variants[label][0], "output": str(output / (label + "001")),
        "supervisor": str(output / (label + "001_supervisor")),
        "arguments": ["--arm", variants[label][0], *common, *variants[label][1]]} for label in order]
    return script, expected, turns, order, jobs


def run(output, profile):
    script, expected, turns, order, jobs = schedule(output, profile)
    # All SUT files must still match the current short cohort, not merely HEAD.
    release = ("first_edition_release002.json" if profile == "firstedition48"
        else "core24_v7_iphone_cohort001/started.json")
    short = json.loads((HERE / release).read_text(encoding="utf-8"))
    if profile == "firstedition48" and short.get("kind") != "FIRST_EDITION_SOURCE_FREEZE":
        raise ValueError("first_edition_release_required")
    verify_sources(short["sources"])
    if shutil.disk_usage(HERE).free < 2_000_000_000:
        raise RuntimeError("prestart_storage_reserve_below_2GB")
    output.mkdir(parents=True, exist_ok=False)
    # New directory only. NTFS inheritance applies equally to all three arms.
    storage = subprocess.run(["compact.exe", "/c", "/i", "/q", str(output)],
        capture_output=True, text=True, errors="replace", timeout=30)
    write_new(output / "storage_policy.json", {"at": now(), "kind": "NEW_COHORT_NTFS_COMPRESSION_INHERITANCE_ALL_ARMS",
        "command": ["compact.exe", "/c", "/i", "/q", str(output)], "exitCode": storage.returncode,
        "stdout": storage.stdout, "stderr": storage.stderr, "deletedFiles": 0})
    if storage.returncode:
        raise RuntimeError("new_cohort_compression_setup_failed_no_model_calls")
    hashes = capture_sources(output / "source_snapshot")
    hashes[Path(__file__).relative_to(ROOT).as_posix()] = file_sha(__file__)
    if profile == "firstedition48":
        hashes.update(short["sources"])
    write_new(output / "started.json", {"at": now(), "kind": "SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL",
        "script": str(script), "scriptSha256": expected, "sources": hashes, "jobs": jobs,
        "order": order, "plannedTurnsEach": turns, "maxRepeats": 1, "profile": profile,
        "releaseManifest": release, "releaseSha256": file_sha(HERE / release),
        "latencyClaim": "DEVELOPMENT_DESCRIPTIVE_ONLY_SINGLE_ORDER_SINGLE_FAMILY",
        "stopRule": "Preserve failures; no retries or edits inside cohort. No production promotion.",
        "storagePolicy": "NTFS compression inherited by all arms from new parent; no concurrent closed-file maintenance planned.",
        "budget": "Episode7200s, observed7320s, native300s, Agent900s; subscription only."})
    results = []
    for job in jobs:
        verify_sources(hashes)
        if file_sha(script) != expected:
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
        "note": "Fixed development baseline. Costs and failures retained; quality and trigger counts require separate review."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=PROFILES)
    parser.add_argument("output", type=Path)
    parser.add_argument("--worker-token")
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.exists() or args.output.parent.resolve() != HERE.resolve():
        raise ValueError("new_absolute_direct_cohort_required")
    if args.worker_token:
        if sys.stdin.readline().strip() != args.worker_token:
            raise RuntimeError("start_gate_not_released")
        run(args.output, args.profile)
    else:
        token = uuid.uuid4().hex
        command = [sys.executable, "-X", "utf8", "-B", "-m",
            "agent.evaluation.context_history_diagnostics_v1_20260905.serial_long_cohort",
            args.profile, str(args.output), "--worker-token", token]
        raise SystemExit(supervise(command, args.output.with_name(args.output.name + "_supervisor"),
            timeout=22200, start_token=token))
