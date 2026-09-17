"""Lifecycle wrapper for an already closed three-arm development cohort review."""
import argparse
import json
from pathlib import Path
import runpy
import sys
import uuid

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, verify_sources
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise


def closed_attempts(cohort):
    if cohort.is_symlink():
        raise ValueError("linked_cohort_rejected")
    cohort = cohort.resolve()
    if cohort.parent != HERE.resolve():
        raise ValueError("direct_study_cohort_required")
    started = json.loads((cohort / "started.json").read_text(encoding="utf-8"))
    terminal = json.loads((cohort / "result.json").read_text(encoding="utf-8"))
    outer = json.loads(cohort.with_name(cohort.name + "_supervisor").joinpath("result.json").read_text(encoding="utf-8"))
    if (started.get("kind") != "SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL" or
            terminal.get("completeMatchedCollection") is not True or outer.get("childExitCode") != 0):
        raise ValueError("closed_complete_cohort_required")
    jobs = started.get("jobs", [])
    if len(jobs) != 3 or {job.get("label") for job in jobs} != {"A", "B", "C"}:
        raise ValueError("three_unique_arms_required")
    attempts = []
    for job in sorted(jobs, key=lambda job: job["label"]):
        raw = Path(job["output"])
        directory = raw.resolve()
        if raw.is_symlink() or directory.parent != cohort:
            raise ValueError("declared_direct_child_required")
        attempts.append(directory.relative_to(HERE.resolve()).as_posix())
    verify_sources(started["sources"])
    turns = started["plannedTurnsEach"]
    if not isinstance(turns, int) or not 1 <= turns <= 72:
        raise ValueError("invalid_cohort_length")
    return attempts, turns


def worker_arguments(output, attempts, turns, packet_budget=96000, judge_concurrency=1):
    if type(packet_budget) is not int or packet_budget not in (96000, 192000):
        raise ValueError("unapproved_review_packet_budget")
    if type(judge_concurrency) is not int or judge_concurrency not in (1, 2):
        raise ValueError("unapproved_judge_concurrency")
    return ["review", str(output), "--attempts", *attempts,
        "--turns", str(turns), "--chunk-size", "6", "--packet-budget", str(packet_budget),
        "--judge-concurrency", str(judge_concurrency)]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cohort", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--worker-token")
    parser.add_argument("--packet-budget", type=int, choices=(96000, 192000), default=96000)
    parser.add_argument("--judge-concurrency", type=int, choices=(1, 2), default=1)
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.exists():
        raise ValueError("new_absolute_review_required")
    if args.worker_token and sys.stdin.readline().strip() != args.worker_token:
        raise RuntimeError("review_gate_not_released")
    attempts, turns = closed_attempts(args.cohort)
    if args.worker_token:
        sys.argv = worker_arguments(args.output, attempts, turns, args.packet_budget, args.judge_concurrency)
        runpy.run_module("agent.evaluation.context_history_review_v2_20260905.run", run_name="__main__")
        return 0
    token = uuid.uuid4().hex
    command = [sys.executable, "-X", "utf8", "-B", "-m",
        "agent.evaluation.context_history_diagnostics_v1_20260905.supervised_review",
        str(args.cohort.resolve()), str(args.output), "--worker-token", token,
        "--packet-budget", str(args.packet_budget), "--judge-concurrency", str(args.judge_concurrency)]
    # Two judges per six-turn packet; at most one third judge per packet.
    # Each native call has its own 300-second limit. This is not a SUT latency.
    timeout = ((turns + 5) // 6) * 3 * 3 * 300 + 1200
    return supervise(command, args.output.with_name(args.output.name + "_supervisor"),
        timeout=timeout, start_token=token)


if __name__ == "__main__":
    raise SystemExit(main())
