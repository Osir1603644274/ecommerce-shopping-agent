"""Two real compactions over actual recorded dialogue; mechanism, not A/B/C benefit."""
import argparse
import asyncio
import json
from pathlib import Path

from agent.app.context_history import HistoryArchive
from .artifacts import HERE, canonical, file_sha, write_new
from .history_strategies import HistoryPolicy, HistoryStrategies, CodexSummarizer, tokens
from .subscription import SubscriptionClient


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    source = HERE / "long_dev2_A001/archive/messages.jsonl"
    rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines()]
    identity = rows[0]["identity"]
    archive = HistoryArchive(output / "archive", session_id=identity["sessionId"], task_id=identity["taskId"], create=True)
    policy = HistoryPolicy(input_budget=6000, trigger_fraction=0.6, target_fraction=0.35, recent_messages=2)
    strategy = HistoryStrategies(archive, policy)
    client = SubscriptionClient(output / "summary_calls", max_calls=4)
    write_new(output / "source.json", {"source": str(source), "sha256": file_sha(source),
        "policy": vars(policy), "cuts": [24, 48], "purpose": "REPEATED_COMPACTION_MECHANISM_ONLY",
        "note": "6k history-only test budget is not a selected production threshold or paired full-Agent window. All source messages came from an actual prior run."})
    async def forbidden(*args):
        raise AssertionError("summary_before_trigger")
    assert await strategy.llm_history(forbidden) == strategy.full()
    for cut in (24, 48):
        for row in rows:
            if row["ordinal"] <= len(archive.records()) or row["turn"] > cut:
                continue
            copied = archive.append(row["role"], row["content"], turn=row["turn"], scope_id=row["scopeId"], display=row["display"])
            assert copied["messageId"] == row["messageId"]
        before = tokens(strategy.full())
        selected = await strategy.llm_history(CodexSummarizer(client))
        write_new(output / f"cut-{cut:02}.json", {"fullHistoryTokens": before, "selectedHistoryTokens": tokens(selected),
            "selectedContext": selected, "receipts": strategy.receipts.copy(), "sourceQuoteValidation": True})
        print(canonical({"cut": cut, "fullHistoryTokens": before, "selectedHistoryTokens": tokens(selected)}), flush=True)
    commits = [row for row in strategy.receipts if row["kind"] == "C_LLM_SUMMARY"]
    success = len(commits) == 2 and set(commits[0]["sourceIds"]).issubset(commits[1]["sourceIds"])
    write_new(output / "result.json", {"status": "REPEATED_REAL_SUMMARY_PASS" if success else "REPEATED_REAL_SUMMARY_HOLD",
        "commits": len(commits), "modelCalls": len(client.calls), "fullAgentIntegration": False,
        "independentSemanticReview": False, "formalExperimentalAcceptance": False})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    asyncio.run(run(parser.parse_args().output))
