"""Runtime-only candidate for note/recall extraction; does not edit live SUT."""
import argparse
import ast
from contextlib import contextmanager
import json
from pathlib import Path
import re
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.app.task_state import TaskState
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new


def requires_note_semantics(message):
    # A known product vocabulary is insufficient to understand execution notes
    # or historical recall. Route these to the schema-validated extractor;
    # this does not infer values, resolve IDs, or grant action permissions.
    return bool(re.search(r"记录|记下|备注|回顾|回忆|回查|安排|预案|迁移|见面|验机|配件|运输|交接|清单", message))


@contextmanager
def candidate_scope():
    original = llm._requires_context_semantic_change
    with patch.object(llm, "_requires_context_semantic_change", lambda message:
            original(message) or (settings.context_history_v1_enabled and requires_note_semantics(message))):
        yield


@contextmanager
def recorded_v5_semantic_scope():
    source = HERE / "core48_v5_vivo_A001/source_snapshot/agent/app/llm.py"
    name = "_requires_context_semantic_change"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(vars(llm))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    with patch.object(llm, name, namespace[name]):
        yield


def inspect(arm):
    path = HERE / f"core48_v5_vivo_{arm}001/turn-32.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    state = TaskState.model_validate(row["preState"])
    with patch.object(settings, "context_history_v1_enabled", True), recorded_v5_semantic_scope():
        before, old_observation = llm._deterministic_used_phone_task_state_decision(state, row["query"])
        with candidate_scope():
            after, new_observation = llm._deterministic_used_phone_task_state_decision(state, row["query"])
    return {"arm": arm, "source": str(path), "sourceSha256": file_sha(path),
        "query": row["query"], "recordedAnswer": row["answer"],
        "recordedTools": [r["tool"] for r in row["toolTraces"]],
        "recordedModelCalls": [r["ordinal"] for r in row["modelCalls"]],
        "legacySemanticSourceSha256": file_sha(HERE / "core48_v5_vivo_A001/source_snapshot/agent/app/llm.py"),
        "oldObservation": old_observation, "newObservation": new_observation,
        "oldDeterministicPayload": before is not None, "candidateDefersToModel": after is None,
        "factsUnchanged": row["preState"]["facts"] == row["postState"]["facts"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--arms", default="AB")
    args = parser.parse_args()
    if not args.arms or any(a not in "ABC" for a in args.arms):
        raise ValueError("invalid_arms")
    cases = [inspect(a) for a in args.arms]
    value = {"status": "NOTE_EXTRACTION_CANDIDATE_ROUTING_PASS" if all(c["oldDeterministicPayload"] and c["candidateDefersToModel"] for c in cases) else "HOLD",
        "cases": cases, "modelCalls": 0, "businessWrites": 0, "sutSourceEdited": False,
        "liveLlmSha256": file_sha(llm.__file__), "diagnosticSha256": file_sha(__file__),
        "endToEndAcceptance": False, "formalAcceptance": False,
        "note": "Generic '分别' with stale comparedIds took deterministic comparison and skipped note extraction. Candidate routes note/recall semantics to existing validated LLM extraction; actual repaired persistence, planner behavior and complete cost need a future new-version probe."}
    write_new(args.output, value)
    print(json.dumps(value, ensure_ascii=False))
