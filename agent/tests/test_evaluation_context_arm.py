from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.app.context_pack import build_context_pack
from agent.app.evaluation_context_arm import (
    EvaluationContextBoundaryError,
    apply_history_policy,
    authoritative_context_pack,
    issue_evaluation_context_arm,
    validate_evaluation_capability,
)
from agent.app.schemas import ToolTrace
from agent.app.task_state import TaskFact, TaskState


class FakeCompletions:
    def __init__(self) -> None:
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))],
            usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2),
        )


class FakeClient:
    def __init__(self) -> None:
        self.completions = FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


async def fake_tool(name, arguments, **_kwargs):
    return ToolTrace(tool=name, ok=True, detail={"count": len(arguments)})


def state() -> TaskState:
    now = datetime.now(timezone.utc)
    return TaskState(
        taskId="task-eval", taskType="ecommerce_guide", sessionId="session-eval",
        status="ready", revision=3, goal="推荐耳机",
        facts=[TaskFact(key="category", value="headphones", certainty="confirmed", source="user")],
        constraints=[], unknowns=[], pendingQuestions=[], domainState={},
        createdAt=now, updatedAt=now,
    )


def capability(arm: str, client: FakeClient | None = None):
    return issue_evaluation_context_arm(
        arm=arm, run_id=f"run-{arm.lower()}", task_id="task-eval",
        session_id="session-eval", model="fixture-no-model",
        model_client=client or FakeClient(), tool_transport=fake_tool,
        provider_max_retries=0,
    )


def test_default_history_object_is_unchanged() -> None:
    history = [{"role": "user", "content": "原始历史"}]
    assert apply_history_policy(history, None) is history


def test_treatment_rejects_any_raw_prior_transcript() -> None:
    cap = capability("CONTEXT_TREATMENT")
    with pytest.raises(EvaluationContextBoundaryError, match="raw_history_forbidden"):
        apply_history_policy([{"role": "user", "content": "不得进入"}], cap)
    assert apply_history_policy(None, cap) is None


def test_control_preserves_complete_same_arm_history_copy() -> None:
    history = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "第一答"},
    ]
    copied = apply_history_policy(history, capability("RAW_FULL_CONTROL"))
    assert copied == history and copied is not history


def test_treatment_compiles_server_view_without_history() -> None:
    cap = capability("CONTEXT_TREATMENT")
    current = state()
    pack = asyncio.run(build_context_pack(
        current, allowed_tools=["search_products"], history=None, run_id=cap.identity.run_id
    ))
    projected = asyncio.run(authoritative_context_pack(
        pack, capability=cap, task_revision=current.revision,
        phase="SHOPPING_PLANNER", tool_schemas=[], query="推荐耳机",
    ))
    assert projected.history_summaries == []
    assert cap.context_binding_hash == cap.ledger.context_receipts[-1]["bindingHash"]
    assert len(cap.context_binding_hash or "") == 64


def test_provider_and_tool_receipts_are_hash_only_and_recomputable() -> None:
    raw_client = FakeClient()
    cap = capability("RAW_FULL_CONTROL", raw_client)
    asyncio.run(cap.model_client.chat.completions.create(
        model="fixture-no-model", messages=[{"role": "user", "content": "sensitive"}]
    ))
    asyncio.run(cap.tool_transport("search_products", {"query": "sensitive"}))
    snapshot = cap.ledger.snapshot()
    assert set(snapshot["modelCalls"][0]) == {
        "callId", "callType", "model", "status", "durationMs", "usage",
        "requestSha256", "resultSha256",
    }
    assert set(snapshot["toolCalls"][0]) == {
        "callId", "callType", "toolName", "status", "durationMs",
        "requestSha256", "resultSha256",
    }
    assert "sensitive" not in repr(snapshot)
    assert all(len(item[field]) == 64 for item in snapshot["modelCalls"] + snapshot["toolCalls"] for field in ("requestSha256", "resultSha256"))


def test_identity_and_retry_contract_fail_closed() -> None:
    cap = capability("RAW_FULL_CONTROL")
    with pytest.raises(EvaluationContextBoundaryError, match="identity_mismatch"):
        validate_evaluation_capability(cap, task_id="other", session_id="session-eval")
    with pytest.raises(EvaluationContextBoundaryError, match="retries_must_be_zero"):
        issue_evaluation_context_arm(
            arm="RAW_FULL_CONTROL", run_id="r", task_id="t", session_id="s",
            model="m", model_client=FakeClient(), tool_transport=fake_tool,
            provider_max_retries=1,
        )
