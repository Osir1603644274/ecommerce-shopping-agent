"""Zero-model recorded payload replay; candidate patch lives only in this process.

Does not edit the SUT or touch its running Redis. This is not an experiment arm.
"""
import argparse
import ast
from contextlib import contextmanager
import json
import re
from unittest.mock import patch

from agent.app import llm
from agent.app.settings import settings
from agent.app.task_state import TaskState
from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE, file_sha, write_new


def budget_needs_semantic_interpretation(message):
    text = re.sub(r"\s+", "", message).casefold()
    if not re.search(r"预算|(?:手机)?硬?上限|价位", text):
        return False
    return bool(re.search(
        r"改成|改为|调整|降到|降至|提高|恢复|替换|增加|减少|预留|留出|"
        r"合计|总计|分之|[×÷]|\d[+*/-]\d", text))


@contextmanager
def candidate_semantic_scope():
    original = llm._requires_context_semantic_change
    def candidate(message):
        return original(message) or (
            settings.context_history_v1_enabled
            and budget_needs_semantic_interpretation(message)
        )
    with patch.object(llm, "_requires_context_semantic_change", candidate):
        yield


def load_case(arm):
    attempt = HERE / f"core24_v3_{arm}001"
    row_path = attempt / "turn-07.json"
    native_path = attempt / "model_calls/call-012/result.json"
    row = json.loads(row_path.read_text(encoding="utf-8"))
    native = json.loads(native_path.read_text(encoding="utf-8"))
    calls = native["answer"]["toolCalls"]
    assert len(calls) == 1 and calls[0]["name"] == "update_task_state"
    arguments = json.loads(calls[0]["arguments"])
    return row, arguments, {str(p.relative_to(HERE)): file_sha(p) for p in (row_path, native_path)}


def price(requirements):
    return next(item["value"] for item in requirements if item["key"] == "price_minor")


@contextmanager
def recorded_legacy_semantic_scope():
    source = HERE / "core24_v3_A001/source_snapshot/agent/app/llm.py"
    name = "_requires_context_semantic_change"
    module = ast.parse(source.read_text(encoding="utf-8"))
    node = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = dict(vars(llm))
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    with patch.object(llm, name, namespace[name]):
        yield


def replay(arm):
    row, arguments, hashes = load_case(arm)
    state = TaskState.model_validate(row["preState"])
    with patch.object(settings, "context_history_v1_enabled", True), recorded_legacy_semantic_scope():
        before, _ = llm._build_validated_task_state_payload(
            state, arguments, message=row["query"], require_status=True)
        with candidate_semantic_scope():
            after, _ = llm._build_validated_task_state_payload(
                state, arguments, message=row["query"], require_status=True)
    return {"arm": arm, "sourceHashes": hashes, "query": row["query"],
        "legacySemanticSourceSha256": file_sha(HERE / "core24_v3_A001/source_snapshot/agent/app/llm.py"),
        "recordedAnswer": row["answer"],
        "recordedPostPriceMinor": price(row["postState"]["domainState"]["shoppingGuide"]["requirements"]),
        "originalPayload": before, "candidatePayload": after,
        "originalPriceMinor": price(before["domainStatePatch"]["shoppingGuide"]["requirements"]),
        "candidatePriceMinor": price(after["domainStatePatch"]["shoppingGuide"]["requirements"])}


def run(output):
    results = [replay(arm) for arm in "ABC"]
    passed = all(r["originalPriceMinor"] == 160000 and r["candidatePriceMinor"] == 140000 for r in results)
    value = {"status": "RECORDED_BUDGET_CANDIDATE_PASS" if passed else "HOLD",
        "modelCalls": 0, "businessWrites": 0, "sutSourceEdited": False,
        "formalExperimentalAcceptance": False,
        "note": "Runtime-only routing candidate. Pure validated payload replay, not persisted authority or end-to-end acceptance.",
        "diagnosticSourceSha256": file_sha(__file__), "results": results}
    write_new(output, value)
    print(json.dumps({"status": value["status"], "prices": [
        [r["arm"], r["originalPriceMinor"], r["candidatePriceMinor"]] for r in results]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    run(parser.parse_args().output)
