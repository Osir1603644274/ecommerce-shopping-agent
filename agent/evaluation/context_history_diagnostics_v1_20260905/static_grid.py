"""Read-only development working-window grid over recorded, closed requests.

No new model call, no simulated C summaries, no comparative benefit claim.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from agent.app.context_history import HistoryArchive, HistoryArchiveError
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import canonical, file_sha, sha, write_new
from agent.evaluation.context_history_strategies_v1_20260905.history_strategies import HistoryPolicy, HistoryStrategies, tokens, message


def captured_archive(directory):
    identity = json.loads((directory / "identity.json").read_text(encoding="utf-8"))
    raw = (directory / "messages.jsonl").read_bytes()
    previous, records = sha(identity), []
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # Never read an incomplete future append.
        row = json.loads(line)
        payload = {key: value for key, value in row.items() if key != "recordHash"}
        if row["identity"] != identity or row["ordinal"] != len(records) + 1 or row["previousHash"] != previous or sha(payload) != row["recordHash"]:
            raise ValueError("archive_chain_invalid")
        previous = row["recordHash"]
        records.append(row)
    return records, hashlib.sha256(raw).hexdigest()


def prefix_archive(records, last_id):
    if last_id is None:
        archive = object.__new__(HistoryArchive)
        archive._records, archive._index = [], {}
        return archive
    found = next((i for i, row in enumerate(records) if row["messageId"] == last_id), None)
    if found is None:
        raise ValueError("request_archive_endpoint_missing")
    archive = object.__new__(HistoryArchive)
    archive._records = deepcopy(records[:found + 1])
    archive._index = {row["messageId"]: row for row in archive._records}
    return archive


def c_target_candidate(budget, target, fixed_tokens, recent_tokens, lower_bound):
    # Mirror _llm_history's pre-model arithmetic, not an approximate bound on
    # the serialized final wire. JSON framing/token boundaries differ.
    remaining = int(budget * target) - fixed_tokens - recent_tokens
    return {"workingBudget": budget, "targetFraction": target,
        "protectedLowerBoundExceedsTarget": lower_bound > budget * target,
        "summaryTargetTokens": remaining,
        "lessThan128SummaryHeadroom": remaining < 128,
        "scope": "IF_TRIGGERED_ON_THIS_RECORDED_STATE_NOT_A_COUNTERFACTUAL_TRAJECTORY"}


def inspect(directory, through):
    started = json.loads((directory / "started.json").read_text(encoding="utf-8"))
    last = json.loads((directory / f"turn-{through:02}.json").read_text(encoding="utf-8"))
    ordinals = [call["ordinal"] for call in last["modelCalls"]]
    if not ordinals:
        raise ValueError("choose_closed_endpoint_with_native_calls")
    max_ordinal = max(ordinals)
    call_turns = {}
    for turn in range(1, through + 1):
        turn_row = json.loads((directory / f"turn-{turn:02}.json").read_text(encoding="utf-8"))
        call_turns.update({call["ordinal"]: turn for call in turn_row["modelCalls"]})
    records, archive_hash = captured_archive(directory / "archive")
    receipts = {}
    for line in (directory / "context_inputs.jsonl").read_bytes().splitlines(keepends=True):
        if line.endswith(b"\n"):
            value = json.loads(line)
            receipts[value["renderedRequestHash"]] = value
    measured = []
    for ordinal in range(1, max_ordinal + 1):
        path = directory / f"model_calls/call-{ordinal:03}/request.json"
        request = json.loads(path.read_text(encoding="utf-8"))
        receipt = receipts.get(sha(request))
        if receipt is None:
            continue  # Summary and lookup follow-ups are not initial requests.
        boundaries = []
        for index, entry in enumerate(request["messages"]):
            try:
                value = json.loads(entry.get("content") or "")
            except ValueError:
                continue
            if isinstance(value, dict) and isinstance(value.get("history"), dict):
                boundaries.append((index, value))
        if len(boundaries) != 1:
            raise ValueError("history_boundary_ambiguous")
        index, payload = boundaries[0]
        history_messages = payload["history"]["messages"]
        endpoint = history_messages[-1]["messageId"] if history_messages else None
        if endpoint is None and call_turns[ordinal] != 1:
            raise ValueError("unexpected_empty_history_after_first_turn")
        archive = prefix_archive(records, endpoint)
        if archive._records and archive._records[-1]["turn"] > call_turns[ordinal]:
            raise ValueError("future_source_leak")
        fixed = {key: value for key, value in payload.items() if key != "history"}
        wire = deepcopy(request)
        wire["messages"][index]["content"] = canonical(fixed)
        fixed_tokens = tokens(wire)
        recent = [message(row) for row in archive.records()[-4:]]
        protected = {"messages": recent, "summary": {"items": []}}
        wire["messages"][index]["content"] = canonical({**fixed, "history": protected})
        lower_bound = tokens(wire)
        row = {"nativeOrdinal": ordinal, "turn": call_turns[ordinal], "phase": receipt["phase"],
            "requestSha256": file_sha(path), "actualRequestTokens": tokens(request), "fixedTokens": fixed_tokens,
            "protectedRecentLowerBound": lower_bound, "sourceEndpoint": endpoint}
        if started["arm"] == "B_PACK_VIEW":
            candidates = []
            for budget in (12000, 16000, 24000):
                policy = HistoryPolicy(input_budget=started["historyPolicy"]["input_budget"], working_budget=budget,
                    fill_history_budget=True)
                strategy = HistoryStrategies(archive, policy)
                try:
                    selected = strategy.pack_history(fixed["currentUserMessage"], fixed_tokens=fixed_tokens)
                    wire["messages"][index]["content"] = canonical({**fixed, "history": selected})
                    exact_tokens = tokens(wire)
                    candidates.append({"budget": budget, "status": "RENDERED", "exactRequestTokens": exact_tokens,
                        "nominalBudgetExceeded": exact_tokens > budget, "selectedMessages": len(selected["messages"]),
                        "hardCapExceeded": exact_tokens > policy.input_budget})
                except HistoryArchiveError as exc:
                    candidates.append({"budget": budget, "status": "INFEASIBLE", "reason": str(exc)})
            row["BWorkingCandidates"] = candidates
        else:
            row["recentMessageTokens"] = tokens(recent)
            row["CProtectedTargetCandidates"] = [c_target_candidate(budget, target, fixed_tokens, tokens(recent), lower_bound)
                for budget in (24000, 32000, 48000) for target in (.35, .45, .55)]
        measured.append(row)
    return {"status": "STATIC_DEVELOPMENT_GRID_ONLY", "arm": started["arm"], "parent": str(directory.resolve()),
        "diagnosticSourceSha256": file_sha(__file__),
        "throughTurn": through, "lastNativeOrdinal": max_ordinal, "startedSha256": file_sha(directory / "started.json"),
        "archiveSnapshotSha256": archive_hash, "endpointTurnSha256": file_sha(directory / f"turn-{through:02}.json"),
        "requests": measured, "modelCalls": 0, "selectedConfiguration": None, "formalAcceptance": False,
        "note": "Actual own-arm recorded snapshots only, not matched A/B states or evolving C simulation. C headroom mirrors the runtime fixed+recent arithmetic conditional on a trigger; the exact serialized empty-summary wire bound is a separate diagnostic. Nine static window/target cells do not authorize nine actual C configurations or predict triggers/summary quality. Feasibility alone proves no quality, savings, or latency benefit. B working budget is currently nominal additive accounting; exact wire size is reported separately."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--through", type=int, required=True)
    args = parser.parse_args()
    value = inspect(args.directory, args.through)
    write_new(args.output, value)
    print(json.dumps({"status": value["status"], "arm": value["arm"], "requests": len(value["requests"])}))
