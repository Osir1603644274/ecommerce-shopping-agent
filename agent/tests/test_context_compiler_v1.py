from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from app.context_compiler_v1 import (
    ContextBudgetExceeded,
    ContextIntegrityError,
    ContextItemScopeV1,
    ContextItemV1,
    ContextReceiptPersistenceError,
    ContextReceiptStore,
    RunContextV1,
    compile_context_v1,
    compile_context_pack_shadow_v1,
    context_items_from_pack,
    sha256_json,
)


ROOT = Path(__file__).resolve().parents[2]
HASH_A = "a" * 64
HASH_B = "b" * 64


class _Pack:
    def __init__(self, payload: dict):
        self.payload = payload
        self.run_id = payload.get("runId", "run-1")
        self.candidate_scope_state = payload.get("candidateScope")

    def model_dump(self, **_kwargs):
        return dict(self.payload)


def _run(**updates) -> RunContextV1:
    raw = {
        "runId": "run-1",
        "parentRunId": None,
        "handoffId": None,
        "tenantId": "tenant-local",
        "ownerId": "owner-1",
        "sessionId": "session-1",
        "recipientType": "SELF",
        "recipientId": "owner-1",
        "taskId": "task-1",
        "taskRevision": 4,
        "agentRole": "SHOPPING_AGENT",
        "phase": "SHOPPING_PLANNER",
        "modelCallOrdinal": 0,
        "candidateScopeId": "scope-1",
        "candidateScopeSourceRevision": 4,
        "candidateScopeHash": HASH_A,
        "deadlineAt": datetime.now(timezone.utc) + timedelta(minutes=5),
        "compilerVersion": "context-compiler-v1",
        "policyVersion": "context-policy-v1",
        "capabilityGrantId": "grant-1",
        "capabilityGrantHash": HASH_B,
        "sensitivity": "SERVER_ONLY",
    }
    raw.update(updates)
    return RunContextV1.model_validate(raw)


def _payload() -> dict:
    return {
        "runId": "legacy-run-id",
        "taskId": "task-1",
        "baseContextRevision": 4,
        "goal": "两千元内拍照手机",
        "confirmedFacts": [{"key": "recipient", "value": "self"}],
        "hardConstraints": [{"key": "price", "value": 200000}],
        "softPreferences": ["拍照"],
        "historySummaries": [
            {
                "role": "user",
                "summary": "之前问过游戏散热",
                "kind": "older_summary",
                "sourceTurns": [1],
            },
            {
                "role": "user",
                "summary": "我长期在意拍照",
                "kind": "older_summary",
                "sourceTurns": [2],
            },
            {
                "role": "user",
                "summary": "这次预算两千元",
                "kind": "recent_verbatim",
                "sourceTurns": [3],
            },
        ],
        "evidenceRefs": ["product:1:title"],
    }


def _compile(run: RunContextV1, items, **kwargs):
    return compile_context_v1(
        run,
        items,
        budget_tokens=10_000,
        tool_schema_hash=HASH_A,
        model_config_hash=HASH_B,
        **kwargs,
    )


def test_research_child_run_requires_exact_phase_and_bindings() -> None:
    with pytest.raises(ValueError, match="EVIDENCE_RESEARCH phase"):
        _run(
            agentRole="EVIDENCE_RESEARCH_AGENT",
            phase="SHOPPING_PLANNER",
            parentRunId="parent-1",
            handoffId="handoff-1",
        )

    with pytest.raises(ValueError, match="missing required bindings"):
        _run(
            agentRole="EVIDENCE_RESEARCH_AGENT",
            phase="EVIDENCE_RESEARCH",
            parentRunId=None,
            handoffId="handoff-1",
        )

    with pytest.raises(ValueError, match="shopping run cannot"):
        _run(phase="EVIDENCE_RESEARCH")


def test_ctx1a_losslessly_reconstructs_current_context_pack_model_view() -> None:
    run = _run()
    source = _payload()
    items = context_items_from_pack(_Pack(source), run)
    compiled = _compile(run, items, history_policy="preserve")

    expected = {key: value for key, value in source.items() if key != "runId"}
    assert compiled.model_view == expected
    assert compiled.receipt.semantic_hash == sha256_json(expected)
    assert compiled.receipt.actual_tokens is None
    assert compiled.receipt.token_status == "ESTIMATED"

    schema = json.loads(
        (
            ROOT
            / "schemas"
            / "context-multiagent-v1"
            / "context-receipt.schema.json"
        ).read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(
        compiled.receipt.model_dump(by_alias=True, mode="json")
    )


def test_shadow_adapter_is_lossless_and_does_not_require_persistence() -> None:
    source = _payload()
    compiled = asyncio.run(
        compile_context_pack_shadow_v1(
            _Pack(source),
            tenant_id="tenant-local",
            owner_id="owner-1",
            session_id="session-1",
            task_id="task-1",
            task_revision=4,
            phase="SHOPPING_PLANNER",
            model_call_ordinal=0,
            tool_schemas=[],
            model_config={"provider": "deepseek", "model": "deepseek-chat"},
            deadline_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            budget_tokens=10_000,
        )
    )

    assert compiled.model_view == {
        key: value for key, value in source.items() if key != "runId"
    }
    assert compiled.receipt.persistence_status == "FAILED"


def test_semantic_hash_is_stable_but_binding_hash_changes_with_owner() -> None:
    first_run = _run()
    second_run = _run(ownerId="owner-2", runId="run-2")
    first = _compile(first_run, context_items_from_pack(_Pack(_payload()), first_run))
    second = _compile(second_run, context_items_from_pack(_Pack(_payload()), second_run))

    assert first.receipt.semantic_hash == second.receipt.semantic_hash
    assert first.receipt.binding_hash != second.receipt.binding_hash


def test_wrong_owner_is_rejected_before_model_view() -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)
    wrong_scope = items[0].scope.model_copy(update={"owner_id": "other-owner"})
    items[0] = items[0].model_copy(update={"scope": wrong_scope})

    compiled = _compile(run, items)

    assert any(
        item.item_id == items[0].item_id and item.reason == "wrong_owner"
        for item in compiled.receipt.rejected_items
    )
    assert items[0].payload["field"] not in compiled.model_view


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("tenant_id", "other-tenant", "wrong_tenant"),
        ("session_id", "other-session", "wrong_session"),
        ("task_id", "other-task", "wrong_task"),
        ("task_revision", 99, "wrong_revision"),
        ("candidate_scope_id", "other-scope", "wrong_scope"),
    ],
)
def test_all_scope_identity_mismatches_are_rejected(
    field: str, value, expected_reason: str
) -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)
    changed_scope = items[0].scope.model_copy(update={field: value})
    items[0] = items[0].model_copy(update={"scope": changed_scope})

    compiled = _compile(run, items)

    assert any(
        rejected.item_id == items[0].item_id
        and rejected.reason == expected_reason
        for rejected in compiled.receipt.rejected_items
    )


@pytest.mark.parametrize(
    ("update", "expected_reason"),
    [
        ({"recipient_allowlist": ("EVIDENCE_RESEARCH_AGENT",)}, "wrong_recipient"),
        ({"phase_allowlist": ("SHOPPING_EXECUTOR",)}, "wrong_phase"),
    ],
)
def test_recipient_and_phase_mismatch_are_rejected(update, expected_reason) -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)
    items[0] = items[0].model_copy(update=update)

    compiled = _compile(run, items)

    assert any(
        rejected.item_id == items[0].item_id
        and rejected.reason == expected_reason
        for rejected in compiled.receipt.rejected_items
    )


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ("expired", "expired"),
        ("future", "authority_lost"),
        ("server_only", "server_only"),
    ],
)
def test_temporal_and_server_only_items_are_rejected(
    mutation: str, expected_reason: str
) -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)
    if mutation == "expired":
        update = {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)}
    elif mutation == "future":
        update = {"valid_from": datetime.now(timezone.utc) + timedelta(hours=1)}
    else:
        update = {"sensitivity": "SERVER_ONLY"}
    items[0] = items[0].model_copy(update=update)

    compiled = _compile(run, items)

    assert any(
        rejected.item_id == items[0].item_id
        and rejected.reason == expected_reason
        for rejected in compiled.receipt.rejected_items
    )


def test_superseded_and_duplicate_items_are_rejected_deterministically() -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)
    items[1] = items[1].model_copy(
        update={"supersedes_item_ids": (items[0].item_id,)}
    )
    items[2] = items[2].model_copy(update={"semantic_key": items[1].semantic_key})

    compiled = _compile(run, items)

    reasons = {item.item_id: item.reason for item in compiled.receipt.rejected_items}
    assert reasons[items[0].item_id] == "superseded"
    assert reasons[items[2].item_id] == "duplicate"


def test_query_focused_history_keeps_relevant_old_and_all_recent() -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)

    compiled = _compile(
        run,
        items,
        history_policy="query_focused",
        query="这次更在意拍照",
    )

    summaries = compiled.model_view["historySummaries"]
    assert [item["summary"] for item in summaries] == [
        "我长期在意拍照",
        "这次预算两千元",
    ]
    assert any(item.reason == "irrelevant" for item in compiled.receipt.rejected_items)


def test_query_focused_history_preserves_all_history_for_reference_query() -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)

    compiled = _compile(
        run,
        items,
        history_policy="query_focused",
        query="还是比较最开始那两个",
    )

    assert compiled.model_view["historySummaries"] == _payload()["historySummaries"]
    assert not any(
        item.reason == "irrelevant" for item in compiled.receipt.rejected_items
    )


def test_tampered_content_hash_fails_closed() -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)
    items[0] = items[0].model_copy(update={"content_hash": "0" * 64})

    with pytest.raises(ContextIntegrityError, match="content hash mismatch"):
        _compile(run, items)


def test_protected_context_cannot_be_budget_evicted() -> None:
    run = _run()
    scope = ContextItemScopeV1(
        tenantId=run.tenant_id,
        ownerId=run.owner_id,
        sessionId=run.session_id,
        taskId=run.task_id,
        taskRevision=run.task_revision,
        candidateScopeId=run.candidate_scope_id,
        candidateScopeHash=run.candidate_scope_hash,
    )
    payload = {"field": "hardConstraints", "value": ["must keep"]}
    item = ContextItemV1(
        itemId="protected-1",
        itemType="HARD_CONSTRAINT",
        semanticKey="hard:1",
        authorityDomain="TASK_STATE",
        sourceKind="SHOPPING_TASK_STATE_V2",
        sourceRefs=("task:1",),
        sourceRevision=4,
        scope=scope,
        recipientAllowlist=("SHOPPING_AGENT",),
        phaseAllowlist=("SHOPPING_PLANNER",),
        sensitivity="MODEL_VISIBLE",
        validFrom=datetime.now(timezone.utc),
        payload=payload,
        contentHash=sha256_json(payload),
        estimatedTokens=100,
    )

    with pytest.raises(ContextBudgetExceeded, match="protected context"):
        compile_context_v1(
            run,
            [item],
            budget_tokens=50,
            tool_schema_hash=HASH_A,
            model_config_hash=HASH_B,
        )


def test_low_priority_context_is_evicted_before_protected_context() -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)
    background = next(item for item in items if item.item_type == "BACKGROUND")
    items[items.index(background)] = background.model_copy(
        update={"estimated_tokens": 10_000}
    )
    protected_types = {
        "TASK_FACT",
        "HARD_CONSTRAINT",
        "FRESH_EVIDENCE",
        "WORKING_SET",
        "REFERENCE_CONTEXT",
        "RESEARCH_REPORT",
    }
    protected_tokens = sum(
        item.estimated_tokens for item in items if item.item_type in protected_types
    )

    compiled = compile_context_v1(
        run,
        items,
        budget_tokens=protected_tokens + 100,
        tool_schema_hash=HASH_A,
        model_config_hash=HASH_B,
    )

    reasons = {item.item_id: item.reason for item in compiled.receipt.rejected_items}
    assert reasons[background.item_id] == "budget_evicted"
    selected_ids = {item.item_id for item in compiled.receipt.selected_items}
    assert all(
        item.item_id in selected_ids
        for item in items
        if item.item_type in protected_types
    )


def test_server_only_item_is_never_model_visible() -> None:
    run = _run()
    items = context_items_from_pack(_Pack(_payload()), run)
    items[0] = items[0].model_copy(update={"sensitivity": "SERVER_ONLY"})
    compiled = _compile(run, items)

    assert any(item.reason == "server_only" for item in compiled.receipt.rejected_items)


def test_context_receipt_primary_failure_uses_spool(tmp_path: Path) -> None:
    run = _run()
    receipt = _compile(run, context_items_from_pack(_Pack(_payload()), run)).receipt
    spool = tmp_path / "context-receipts.jsonl"
    store = ContextReceiptStore(spool_path=spool)
    store._save_primary = AsyncMock(side_effect=RuntimeError("redis down"))

    persisted = asyncio.run(store.save(receipt, evaluation_mode=True))

    assert persisted.persistence_status == "SPOOL"
    row = json.loads(spool.read_text(encoding="utf-8").splitlines()[0])
    assert row["persistenceStatus"] == "SPOOL"
    assert "两千元内拍照手机" not in json.dumps(row, ensure_ascii=False)


def test_context_receipt_evaluation_fails_closed_without_durable_store(
    tmp_path: Path,
) -> None:
    run = _run()
    receipt = _compile(run, context_items_from_pack(_Pack(_payload()), run)).receipt
    store = ContextReceiptStore(spool_path=tmp_path / "context-receipts.jsonl")
    store._save_primary = AsyncMock(side_effect=RuntimeError("redis down"))
    store._append_spool = AsyncMock(side_effect=OSError("disk down"))

    with pytest.raises(ContextReceiptPersistenceError):
        asyncio.run(store.save(receipt, evaluation_mode=True))
