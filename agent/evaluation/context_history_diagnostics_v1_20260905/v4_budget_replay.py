"""Audit recorded v4 turn 31 against live v5 validation, without model/Redis calls."""
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.app.task_state import TaskState
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new
from agent.evaluation.context_history_diagnostics_v1_20260905.budget_after_compare import price


def run(output):
    cases = []
    for arm in "ABC":
        attempt = HERE / f"core72_v4_{arm}001"
        source = attempt / "turn-31.json"
        row = json.loads(source.read_text(encoding="utf-8"))
        updates = [(call, tool) for call in row["modelCalls"]
                   for tool in call.get("answer", {}).get("toolCalls", [])
                   if tool["name"] == "update_task_state"]
        assert len(updates) == 1
        call, tool = updates[0]
        arguments = json.loads(tool["arguments"])
        with patch.object(settings, "context_history_v1_enabled", True):
            payload, _ = llm._build_validated_task_state_payload(
                TaskState.model_validate(row["preState"]), arguments,
                message=row["query"], require_status=True)
        cases.append({"arm": arm, "turn": 31, "query": row["query"],
            "answer": row["answer"], "nativeOrdinal": call["ordinal"],
            "nativeProposedBudgetMinor": price(arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"]),
            "recordedActualBudgetMinor": price(row["postState"]["domainState"]["shoppingGuide"]["requirements"]),
            "v5ValidatedBudgetMinor": price(payload["domainStatePatch"]["shoppingGuide"]["requirements"]),
            "recordedTurnSha256": file_sha(source),
            "recordedNativeResultSha256": file_sha(attempt / f"model_calls/call-{call['ordinal']:03}/result.json")})
    passed = all(c["nativeProposedBudgetMinor"] == c["v5ValidatedBudgetMinor"] == 184000
                 and c["recordedActualBudgetMinor"] == 200000 for c in cases)
    value = {"status": "V5_RECORDED_PAYLOAD_REGRESSION_PASS" if passed else "HOLD",
        "recordedV4Quality": "HOLD_SHARED_HARD_BUDGET_NOT_APPLIED",
        "cases": cases, "modelCalls": 0, "businessWrites": 0,
        "sutSourceEdited": False, "endToEndAcceptance": False,
        "liveLlmSha256": file_sha(Path(llm.__file__)), "diagnosticSha256": file_sha(__file__),
        "note": "All three raw v4 states fail the explicit user budget despite correct model proposals. Live v5 payload validation preserves the proposed budget; this is not a replacement v4 result or a new end-to-end run."}
    write_new(output, value)
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    run(parser.parse_args().output)
