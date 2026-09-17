"""Diagnose recovered native transport warnings without changing old attempts."""
import argparse
import json
import re

from agent.evaluation.context_history_strategies_v1_20260905 import subscription
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, sha, write_new


def recoverable_transport_event(event):
    if event.get("type") == "error":
        return bool(re.fullmatch(
            r"Reconnecting\.\.\. [1-5]/5 \((?:stream disconnected before completion: tls handshake eof|Connection failed: error sending request)\)",
            event.get("message", "")))
    return (event.get("type") == "item.completed" and event.get("item", {}).get("type") == "error"
        and event["item"].get("message") == "Falling back from WebSockets to HTTPS transport. stream disconnected before completion: tls handshake eof")


def candidate_parse_events(lines, exit_code):
    events = [json.loads(line) for line in lines.splitlines() if line.strip()]
    filtered = [event for event in events if not recoverable_transport_event(event)]
    return subscription.parse_events("\n".join(json.dumps(event) for event in filtered), exit_code)


def terminal_usage(lines):
    events = [json.loads(line) for line in lines.splitlines() if line.strip()]
    terminals = [e for e in events if e.get("type") in {"turn.completed", "turn.failed"}]
    threads = [e for e in events if e.get("type") == "thread.started"]
    if len(terminals) != 1 or terminals[0]["type"] != "turn.completed" or len(threads) != 1:
        raise ValueError("native_usage_not_unambiguously_completed")
    usage = terminals[0].get("usage")
    if not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0 for key in ("input_tokens", "output_tokens")):
        raise ValueError("missing_native_usage")
    for key, parent in (("cached_input_tokens", "input_tokens"), ("reasoning_output_tokens", "output_tokens")):
        if key in usage and (type(usage[key]) is not int or not 0 <= usage[key] <= usage[parent]):
            raise ValueError("invalid_native_usage_subset")
    return usage, threads[0]["thread_id"]


def reconcile(output, attempt_name="core72_v4_A001", ordinal=165):
    attempt = (HERE / attempt_name).resolve()
    if attempt.parent != HERE.resolve():
        raise ValueError("attempt_outside_study")
    call = attempt / f"model_calls/call-{ordinal:03}"
    events = (call / "events.jsonl").read_text(encoding="utf-8")
    original = json.loads((call / "result.json").read_text(encoding="utf-8"))
    request = json.loads((call / "request.json").read_text(encoding="utf-8"))
    assert original["status"] == "FAILED" and original["usage"] is None
    assert sha(request) == original["requestSha256"]
    usage, thread_id = terminal_usage(events)
    answer, _, _ = candidate_parse_events(events, 0)
    subscription.decode_answer(answer, request)
    census_path = HERE / f"{attempt_name}_census001.json"
    census = json.loads(census_path.read_text(encoding="utf-8"))
    assert census["unknownUsageCalls"] == [ordinal]
    value = {"status": "NATIVE_USAGE_RECONCILED_ATTEMPT_REMAINS_FAILED",
        "attempt": str(attempt), "nativeOrdinal": ordinal, "threadId": thread_id,
        "originalAdapterStatus": "FAILED", "originalResultNotModified": True,
        "nativeTerminal": "turn.completed", "usage": usage,
        "recoveredNativeTokens": usage["input_tokens"] + usage["output_tokens"],
        "reconciledKnownNativeTotal": census["knownNativeTokenSubtotal"] + usage["input_tokens"] + usage["output_tokens"],
        "observedClosedTurns": census["observedTurns"], "plannedTurns": census["plannedTurns"],
        "completeConversationComparisonAllowed": False,
        "nativeProcessExitCode": None,
        "candidateParserAndEnvelopePassAssumingExitCodeZero": True,
        "caveat": "Old adapter did not persist process returncode. A native terminal usage receipt is available, but this cannot retroactively prove complete adapter success, alter real Agent replies or outcomes, create missing turns, or restore missing Agent timing.",
        "sources": {str(p.relative_to(HERE)): file_sha(p) for p in (
            call / "events.jsonl", call / "request.json", call / "result.json", census_path)},
        "diagnosticSourceSha256": file_sha(__file__),
        "modelCalls": 0, "formalAcceptance": False}
    write_new(output, value)
    print(json.dumps({"status": value["status"], "recoveredNativeTokens": value["recoveredNativeTokens"],
        "reconciledKnownNativeTotal": value["reconciledKnownNativeTotal"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--attempt", default="core72_v4_A001")
    parser.add_argument("--ordinal", type=int, default=165)
    args = parser.parse_args()
    reconcile(args.output, args.attempt, args.ordinal)
