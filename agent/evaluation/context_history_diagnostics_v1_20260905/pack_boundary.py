import argparse
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
from unittest.mock import patch

from agent.app.context_input import experimental_context_input
from agent.app.context_pack import build_context_pack, DEFAULT_TOKEN_BUDGET
from agent.app.settings import settings
from agent.app.task_state import TaskState
from agent.app.domains.ecommerce.shopping_state_authority import select_shopping_state_authority
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha, write_new


async def run(source, output):
    row = json.loads(source.read_text(encoding="utf-8"))
    results = []
    with ExitStack() as stack:
        stack.enter_context(patch.object(settings, "context_history_v1_enabled", True))
        stack.enter_context(experimental_context_input())
        for key in ("preState", "postState"):
            state = TaskState.model_validate(row[key])
            for operation in ("authority", "pack_default", "pack_128k_diagnostic"):
                try:
                    if operation == "authority":
                        result = select_shopping_state_authority(domain_state=state.domain_state,
                            task_id=state.task_id, task_revision=state.revision, goal=state.goal,
                            unknowns=state.unknowns, pending_questions=state.pending_questions,
                            mode=settings.shopping_state_authority)
                    else:
                        result = await build_context_pack(state, allowed_tools=[], history=[],
                            budget_tokens=DEFAULT_TOKEN_BUDGET if operation == "pack_default" else 128000,
                            run_id=row["expectedRunId"])
                    results.append({"state": key, "operation": operation, "status": "PASS"})
                except Exception as exc:
                    results.append({"state": key, "operation": operation, "status": "FAIL",
                        "exceptionType": type(exc).__name__, "error": str(exc)})
    value = {"source": str(source.resolve()), "sourceSha256": file_sha(source), "turn": row["turn"],
        "modelCalls": 0, "businessWrites": 0, "defaultPackBudget": DEFAULT_TOKEN_BUDGET, "results": results,
        "note": "Pure recorded-state boundary probes; larger Pack budget is a diagnostic, not an authorized fix or an experiment result."}
    write_new(output, value)
    print(json.dumps(value, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.source, args.output))
