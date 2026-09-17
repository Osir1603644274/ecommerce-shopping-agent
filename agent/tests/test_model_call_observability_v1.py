from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from app.llm import (
    _observe_llm_call,
    begin_agent_llm_call_span,
    end_agent_llm_call_span,
)
from app.model_call_observability import (
    ModelCallReceiptPersistenceError,
    ModelCallReceiptStore,
    build_model_call_receipt,
)


ROOT = Path(__file__).resolve().parents[2]


def _response(*, prompt: int, completion: int, cached: int | None = None):
    details = (
        SimpleNamespace(cached_tokens=cached) if cached is not None else None
    )
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=details,
        )
    )


def _receipt():
    return build_model_call_receipt(
        run_id="run-1",
        call_purpose="shopping_policy_decision",
        agent_role="SHOPPING_AGENT",
        provider="deepseek",
        model="deepseek-chat",
        duration_ms=12.5,
        retry_ordinal=0,
        failed=False,
        response=_response(prompt=100, completion=20, cached=40),
    )


def test_span_emits_one_schema_valid_receipt_per_real_call() -> None:
    begin_agent_llm_call_span(run_id="run-observed")
    _observe_llm_call(
        "react_decision",
        9.5,
        response=_response(prompt=120, completion=30, cached=50),
        model_call_id="rmc-exact-1",
        context_binding_hash="a" * 64,
    )
    snapshot = end_agent_llm_call_span()

    assert snapshot["modelCalls"] == {"react_decision": 1}
    assert len(snapshot["modelCallReceipts"]) == 1
    receipt = snapshot["modelCallReceipts"][0]
    assert receipt["modelCallId"] == "rmc-exact-1"
    assert receipt["runId"] == "run-observed"
    assert receipt["callPurpose"] == "shopping_policy_decision"
    assert receipt["inputTokens"] == 120
    assert receipt["outputTokens"] == 30
    assert receipt["cachedInputTokens"] == 50
    assert receipt["tokenStatus"] == "OBSERVED"

    schema = json.loads(
        (
            ROOT
            / "schemas"
            / "context-multiagent-v1"
            / "model-call-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(receipt)


def test_missing_provider_usage_is_not_instrumented_never_zero() -> None:
    begin_agent_llm_call_span(run_id="run-uninstrumented")
    _observe_llm_call("final_answer", 4.0, response=SimpleNamespace())
    receipt = end_agent_llm_call_span()["modelCallReceipts"][0]

    assert receipt["tokenStatus"] == "NOT_INSTRUMENTED"
    assert receipt["inputTokens"] is None
    assert receipt["outputTokens"] is None
    assert receipt["cachedInputTokens"] is None


def test_retry_ordinal_and_failure_are_per_purpose() -> None:
    begin_agent_llm_call_span(run_id="run-retry")
    _observe_llm_call("final_answer", 2.0, failed=True)
    _observe_llm_call("final_answer", 3.0, response=_response(prompt=1, completion=1))
    receipts = end_agent_llm_call_span()["modelCallReceipts"]

    assert [item["retryOrdinal"] for item in receipts] == [0, 1]
    assert [item["status"] for item in receipts] == ["FAILED", "SUCCEEDED"]
    assert receipts[0]["errorCode"] == "provider_call_failed"


def test_primary_failure_uses_redacted_append_only_spool(tmp_path: Path) -> None:
    spool = tmp_path / "receipts.jsonl"
    store = ModelCallReceiptStore(spool_path=spool)
    store._save_primary = AsyncMock(side_effect=RuntimeError("redis down"))

    status = asyncio.run(store.save_many([_receipt()], evaluation_mode=True))

    assert status == "SPOOL"
    rows = [json.loads(line) for line in spool.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["callPurpose"] == "shopping_policy_decision"
    forbidden = json.dumps(rows[0]).lower()
    for secret_field in ("messages", "prompt", "authorization", "toolarguments"):
        assert secret_field not in forbidden


def test_evaluation_fails_closed_if_primary_and_spool_both_fail(
    tmp_path: Path,
) -> None:
    store = ModelCallReceiptStore(spool_path=tmp_path / "receipts.jsonl")
    store._save_primary = AsyncMock(side_effect=RuntimeError("redis down"))
    store._append_spool = AsyncMock(side_effect=OSError("disk down"))

    with pytest.raises(ModelCallReceiptPersistenceError):
        asyncio.run(store.save_many([_receipt()], evaluation_mode=True))


def test_normal_service_marks_failed_if_all_persistence_is_unavailable(
    tmp_path: Path,
) -> None:
    store = ModelCallReceiptStore(spool_path=tmp_path / "receipts.jsonl")
    store._save_primary = AsyncMock(side_effect=RuntimeError("redis down"))
    store._append_spool = AsyncMock(side_effect=OSError("disk down"))

    assert asyncio.run(store.save_many([_receipt()], evaluation_mode=False)) == "FAILED"
