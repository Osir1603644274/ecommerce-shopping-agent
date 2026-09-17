"""Real subscription lookup probe; not a full Agent or quality experiment."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path

from agent.app.context_history import HistoryArchive
from agent.app.context_input import experimental_context_input, phase_context
from agent.app.task_state import TaskState
from .artifacts import HERE, canonical, file_sha, write_new
from .context_client import ContextClient
from .history_strategies import HistoryPolicy, HistoryStrategies
from .subscription import SubscriptionClient


async def run(output):
    output.mkdir(parents=True, exist_ok=False)
    archive = HistoryArchive(output / "archive", session_id="lookup-probe", task_id="lookup-task", create=True)
    sources = sorted((HERE / "long_dev_A002").glob("turn-*.json"))[:9]
    for path in sources:
        row = json.loads(path.read_text(encoding="utf-8"))
        archive.append("user", row["query"], turn=row["turn"])
        archive.append("assistant", row["answer"], turn=row["turn"])
    write_new(output / "source_manifest.json", {str(path): file_sha(path) for path in sources})
    instant = datetime.now(timezone.utc)
    state = TaskState(taskId="lookup-task", taskType="ecommerce_guide", sessionId="lookup-probe",
        status="ready", revision=1, goal="历史原文回查机制验真", createdAt=instant, updatedAt=instant)
    async def loader(task_id):
        return state
    base = SubscriptionClient(output / "model_calls", max_calls=3)
    client = ContextClient(base, arm="B_PACK_VIEW", history=HistoryStrategies(archive, HistoryPolicy(input_budget=5000)),
        task_id=state.task_id, session_id=state.session_id, query="请先回查第4轮用户原文，再告诉我当时的预算上限。",
        output=output / "inputs.jsonl", state_loader=loader)
    with experimental_context_input():
        payload = phase_context("final_answer", {"taskId": state.task_id,
            "instruction": "这是只读历史查询，请实际调用context_history_lookup读取第4轮，之后引用消息ID并回答。"})
    response = await client.create(model="gpt-5.6-sol", messages=[{"role": "user", "content": canonical(payload)}])
    answer = response.choices[0].message.content
    receipts = [json.loads(line) for line in (output / "history_lookups.jsonl").read_text(encoding="utf-8").splitlines()] if (output / "history_lookups.jsonl").exists() else []
    success = bool(receipts) and "6500" in answer and all(not row["result"]["actionAuthorized"] for row in receipts)
    write_new(output / "result.json", {"status": "LOOKUP_MECHANISM_PASS" if success else "LOOKUP_MECHANISM_HOLD",
        "answer": answer, "modelCalls": len(base.calls), "lookups": len(receipts), "formalAcceptance": False,
        "note": "Actual source dialogue copied for a read-only mechanism probe; no Agent execution or gold TaskState injection claim."})
    print(canonical({"success": success, "answer": answer}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    asyncio.run(run(parser.parse_args().output))
