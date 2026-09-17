"""Real subscription repeated-summary probe on immutable old dev dialogue.

This isolates the mechanism, not end-to-end Agent quality or an A/B/C effect.
One explicitly corrupted summary tests fallback accounting after a REAL call.
"""
import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import traceback
import uuid

from agent.app.context_history import HistoryArchive
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, ROOT, append, capture_sources, file_sha, now, verify_sources, write_new
from agent.evaluation.context_history_strategies_v1_20260905.history_strategies import CodexSummarizer, HistoryPolicy, HistoryStrategies, tokens
from agent.evaluation.context_history_strategies_v1_20260905.subscription import SubscriptionClient
from agent.evaluation.context_history_strategies_v1_20260905.supervise_attempt import supervise


class PrefixArchive:
    def __init__(self, archive):
        self.archive = archive
        self.through = 0

    def records(self):
        return [row for row in self.archive.records() if row["turn"] <= self.through]


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    hashes = capture_sources(output / "source_snapshot")
    hashes[Path(__file__).relative_to(ROOT).as_posix()] = file_sha(__file__)
    source = HERE / "core72_v6_C001/archive"
    identity = json.loads((source / "identity.json").read_text(encoding="utf-8"))
    source_hashes = {name: file_sha(source / name) for name in ("identity.json", "messages.jsonl")}
    archive = HistoryArchive(source, session_id=identity["sessionId"], task_id=identity["taskId"])
    prefixes = (24, 48, 62)
    if max(row["turn"] for row in archive.records()) != 62:
        raise ValueError("unexpected_closed_archive_boundary")
    policy = HistoryPolicy(input_budget=96000, working_budget=16000,
        trigger_fraction=.6, target_fraction=.45, adaptive_summary_items=True)
    write_new(output / "started.json", {"at": now(), "kind": "DEVELOPMENT_MECHANISM_PROBE_NOT_FULL_AGENT",
        "source": str(source), "sourceSha256": source_hashes, "codeHashes": hashes,
        "prefixes": prefixes, "policy": vars(policy), "fixedTokensForMechanismProbe": 2500,
        "realNativeCallBudget": 8, "faultInjection": "after one new real summary, change its sourceId; preserve native original",
        "qualityAcceptance": False, "comparisonPermitted": False})
    client = SubscriptionClient(output / "model_calls", max_calls=8, application_input_budget=96000)
    summarize = CodexSummarizer(client, policy)
    prefix = PrefixArchive(archive)
    strategy = HistoryStrategies(prefix, policy)
    receipts = []
    try:
        for through in prefixes:
            verify_sources(hashes)
            prefix.through = through
            start = time.perf_counter()
            before = len(client.calls)
            receipt_before = len(strategy.receipts)
            rendered = await strategy.llm_history(summarize, fixed_tokens=2500)
            receipt = {"through": through, "nativeCallCount": len(client.calls) - before,
                "durationMs": (time.perf_counter() - start) * 1000,
                "fullHistoryTokens": tokens(strategy.full()), "renderedHistoryTokens": tokens(rendered),
                "newHistoryReceipts": deepcopy(strategy.receipts[receipt_before:]), "rendered": rendered}
            receipts.append(receipt)
            write_new(output / f"prefix-{through}.json", receipt)
            print(json.dumps({key: receipt[key] for key in ("through", "nativeCallCount", "durationMs", "fullHistoryTokens", "renderedHistoryTokens")}), flush=True)
        # Separate fresh mechanism state: do not contaminate committed summaries.
        failing = HistoryStrategies(prefix, policy)
        injected_calls = 0

        async def corrupted_summary(sources, target):
            nonlocal injected_calls
            original = await summarize(sources, target)
            injected_calls += 1
            altered = deepcopy(original)
            if not altered.get("items"):
                raise ValueError("cannot_inject_source_fault_without_summary_item")
            altered["items"][0]["sourceId"] = "fault-injection-unavailable-original"
            append(output / "fault_injections.jsonl", {"at": now(), "nativeOrdinal": len(client.calls),
                "kind": "EXPLICIT_TEST_ONLY_SOURCE_CORRUPTION", "original": original, "altered": altered})
            return altered

        start = time.perf_counter()
        fallback = await failing.llm_history(corrupted_summary, fixed_tokens=2500)
        after_failure = len(client.calls)
        cached = await failing.llm_history(corrupted_summary, fixed_tokens=2500)
        checks = {"multipleRealSummaryCommits": sum(row["kind"] == "C_LLM_SUMMARY" for row in strategy.receipts) >= 2,
            "failureWasInjectedAfterRealCall": injected_calls == 1,
            "failedSummaryNotCommitted": failing.summary is None,
            "fallbackEqualsCompleteOriginal": fallback == failing.full() == cached,
            "sameSourceFailureDoesNotCallAgain": len(client.calls) == after_failure,
            "fallbackWithinSharedHardLimit": tokens(fallback) + 2500 <= 96000,
            "originalArchiveUnchanged": all(file_sha(source / name) == value for name, value in source_hashes.items())}
        verify_sources(hashes)
        unknown = [row["ordinal"] for row in client.calls if not row.get("usage")]
        total = sum(row["usage"]["input_tokens"] + row["usage"]["output_tokens"] for row in client.calls if row.get("usage"))
        write_new(output / "result.json", {"status": "MECHANISM_PASS" if all(checks.values()) else "MECHANISM_HOLD",
            "checks": checks, "nativeCalls": len(client.calls), "knownNativeTokenSubtotal": total,
            "unknownUsageCalls": unknown, "sourceDrift": [], "fallbackReceipts": failing.receipts,
            "faultAndFallbackMs": (time.perf_counter() - start) * 1000,
            "independentSemanticReview": False, "fullAgentRecoveryVerified": False,
            "note": "Old development dialogue, real new summaries, separately labeled injected corruption. This is not a complete Agent episode, formal comparison, or semantic summary acceptance."})
        return all(checks.values())
    except BaseException as exc:
        write_new(output / "failure.json", {"at": now(), "type": type(exc).__name__, "error": str(exc), "traceback": traceback.format_exc()})
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--worker-token")
    args = parser.parse_args()
    if not args.output.is_absolute() or args.output.exists():
        raise ValueError("new_absolute_output_required")
    if args.worker_token:
        if sys.stdin.readline().strip() != args.worker_token:
            raise RuntimeError("start_gate_not_released")
        raise SystemExit(0 if asyncio.run(run(args.output)) else 2)
    token = uuid.uuid4().hex
    command = [sys.executable, "-X", "utf8", "-B", "-m",
        "agent.evaluation.context_history_diagnostics_v1_20260905.summary_recovery_probe",
        str(args.output), "--worker-token", token]
    raise SystemExit(supervise(command, args.output.with_name(args.output.name + "_supervisor"),
        timeout=1920, start_token=token))
