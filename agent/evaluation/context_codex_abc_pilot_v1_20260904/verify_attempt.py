"""Read-only audit of an executed attempt; verification record is append-only."""
import json
import sys
from . import pilot as p


def main(attempt):
    p.verify_freeze()
    out = p.HERE / attempt
    runtime = p.read(out / "runtime.json")
    assert p.file_sha(out / "pilot_source.py") == runtime["runnerSha256"]
    schedule = p.read(out / "schedule.json")
    ledger = [json.loads(line) for line in (out / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(schedule) == 18 and len(ledger) == 36
    ids, results = [], []
    for row in schedule:
        call = out / f'{row["ordinal"]:02d}-{row["fixture"]}-{row["arm"]}'
        req = p.read(call / "request.json")
        result = p.read(call / "result.json")
        fixture = p.read(p.HERE / "inputs" / (row["fixture"] + ".json"))
        start, end = [e for e in ledger if e["ordinal"] == row["ordinal"]]
        assert start["event"] == "START" and end["event"] == "END"
        assert start["requestHash"] == p.file_sha(call / "request.json")
        assert end["resultHash"] == p.file_sha(call / "result.json")
        assert req["promptSha256"] == p.sha(fixture["prompts"][row["arm"]])
        assert (call / "prompt.txt").read_text(encoding="utf-8") == fixture["prompts"][row["arm"]]
        assert req["commonPackHash"] == p.sha(fixture["pack"])
        assert req["rawSourceHash"] == p.sha(fixture["raw"])
        assert req["contextHash"] == p.sha(fixture["contexts"][row["arm"]])
        expected_args = [p.CODEX, *p.cli_options(runtime["overrides"]), "exec", "--ephemeral", "--skip-git-repo-check", "--json", "--color", "never"]
        if fixture["schema"]:
            expected_args += ["--output-schema", str(p.HERE / "inputs" / (row["fixture"] + ".schema.json"))]
        assert req["args"] == expected_args + ["-"]
        assert result["status"] == "COMPLETED" and result["terminalCount"] == 1
        assert not result["unexpectedEvents"] and not result["failureEvents"] and not result["malformedEventLines"]
        events = [json.loads(s) for s in (call / "events.jsonl").read_text(encoding="utf-8").splitlines() if s.strip()]
        terminal = [e for e in events if e.get("type") == "turn.completed"]
        assert len(terminal) == 1 and terminal[0]["usage"] == result["usage"]
        for k in ("input_tokens", "output_tokens", "cached_input_tokens"):
            assert isinstance(result["usage"][k], int) and result["usage"][k] >= 0
        assert len(result["threadIds"]) == 1
        ids += result["threadIds"]
        results.append(result)
    assert len(ids) == len(set(ids)) == 18
    state_count = sum(r["validation"].get("originalBusinessValidatorAccepted", False) and r["validation"].get("expectedBudget", False) for r in results)
    assert state_count == 9
    record = {"at": p.now(), "status": "AUDIT_PASS", "scheduled": 18, "ledgerEvents": 36,
        "uniqueThreads": 18, "validUsageReports": 18, "observedToolActions": 0,
        "observedTransportErrorEvents": 0, "stateAcceptedAndCorrect": state_count,
        "sourceAndInputHashesUnchanged": True, "allRequestResultAndLedgerHashesVerified": True,
        "providerRequestCount": None, "formalEfficiencyQualityGate": "NOT_CLAIMED"}
    f6 = p.read(p.HERE / "inputs/pilot-06.json")
    b6 = next(r for r in results if r["fixture"] == "pilot-06" and r["arm"] == "B_PACK")
    c6 = next(r for r in results if r["fixture"] == "pilot-06" and r["arm"] == "C_PACK_COMPILER")
    assert f6["prompts"]["B_PACK"] == f6["prompts"]["C_PACK_COMPILER"]
    record["sameApplicationInputDifferentUsage"] = {
        "fixture": "pilot-06", "bInput": b6["usage"]["input_tokens"], "cInput": c6["usage"]["input_tokens"]}
    record["applicationOnlyTokenMeasurementGate"] = "HOLD"
    p.write_new(out / "verification.json", record)
    print(p.canonical(record))


if __name__ == "__main__":
    main(sys.argv[1])
