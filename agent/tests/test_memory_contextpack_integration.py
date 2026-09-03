"""B3b seam tests: optional memory never mutates the canonical ContextPack."""

from __future__ import annotations

import asyncio
import httpx
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

import app.memory.context_projection as memory_context_projection
from app.context_pack import (
    TaskConstraint,
    build_context_pack,
    context_pack_hash,
    context_pack_system_message,
)
from app.context_view import ContextProjector
from app.memory.context_projection import LongTermMemoryContext, derive_long_term_memory_context
from app.memory.projection_client import (
    MemoryAccessCredential,
    MemoryProjectionV2Entry,
)
from app.memory import v3_runtime
from tests.memory_v2_fetch_support import fetched_v2_result
from app.memory.governance import (
    MemoryApplicationContext,
    memory_snapshot_from_projection_v2,
    projection_result_from_effective,
    resolve_effective_preferences,
)
from app.planner import build_planner_context_from_view, build_planner_messages
from app.settings import settings
from app.task_state import TaskState


def _state() -> TaskState:
    return TaskState(
        taskId="memory-context-task",
        revision=1,
        status="ready",
        goal="find a product",
        taskType="ecommerce_guide",
        createdAt=datetime.now(timezone.utc),
        updatedAt=datetime.now(timezone.utc),
    )


def _pack():
    return asyncio.run(build_context_pack(_state(), allowed_tools=["search_products"], run_id="run-memory-context"))


def _phone_pack(monkeypatch):
    monkeypatch.setattr(settings, "shopping_state_authority", "legacy")
    state = _state().model_copy(update={"domain_state": {"shoppingGuide": {
        "mode": "recommend",
        "category": "phone",
        "requirements": [],
        "candidateIds": [],
        "comparedIds": [],
        "evidenceStatus": "missing",
    }}})
    return asyncio.run(build_context_pack(
        state, allowed_tools=["search_products"], run_id="run-memory-context-v3"
    ))


def _memory(*values: tuple[str, str] | tuple[str, str, str]):
    entries = []
    now = datetime(2026,8,29,tzinfo=timezone.utc)
    for index, item in enumerate(values):
        category, semantic_key, value = (
            item if len(item) == 3 else ("shopping_preference", item[0], item[1])
        )
        entries.append(MemoryProjectionV2Entry(
            f"memory-entry-{index}",category,"phone","self",semantic_key,value,
            "explicit_user","long_term_preference",1,"ACTIVE",
            now-timedelta(days=5),now-timedelta(days=1),now+timedelta(days=20),None,True,
        ))
    result=fetched_v2_result(tuple(entries))
    snapshot=memory_snapshot_from_projection_v2(result,authenticated_owner_user_id="user-1")
    effective=resolve_effective_preferences(snapshot,MemoryApplicationContext(
        "user-1","phone","self",now,memory_enabled=True
    ))
    return derive_long_term_memory_context(projection_result_from_effective(effective))


def _planner(projector: ContextProjector):
    return projector.planner_view(["search_products"], "ready", user_message="find a product")


def _replanner(projector: ContextProjector):
    return projector.replanner_view(
        failed_plan_summary={}, failure_reason="insufficient_evidence", replan_attempt=1,
    )


def _final(projector: ContextProjector):
    return projector.final_answer_view(validated_results=[])


def _v3_binding(monkeypatch):
    body = {
        "success": True,
        "data": {
            "schemaVersion": 3,
            "revision": 7,
            "ownerBinding": "a" * 64,
            "truncated": False,
            "entries": [{
                "entryId": "entry-1",
                "categoryId": "phone",
                "recipientScope": "self",
                "preferenceKind": "avoid",
                "attributeKey": "brand",
                "normalizedValue": "apple",
                "catalogRevision": "shopping-companion-9a8a2a1c13f",
                "version": 1,
                "status": "ACTIVE",
                "createdAt": "2026-08-01T00:00:00Z",
                "updatedAt": "2026-08-20T00:00:00Z",
                "expiresAt": "2027-02-16T00:00:00Z",
                "supersedes": None,
                "chainVerified": True,
            }],
        },
        "message": "ok",
        "timestamp": "2026-08-30T00:00:00Z",
    }
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=body, request=request)
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        v3_runtime.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(transport=transport),
    )
    projection = asyncio.run(v3_runtime.MemoryProjectionV3Client(
        enabled=True,
        backend_url="http://memory-authority",
    ).fetch(MemoryAccessCredential("opaque-token")))
    return v3_runtime.build_memory_run_binding(
        projection,
        category_id="phone",
        catalog_revision="shopping-companion-9a8a2a1c13f",
    )


def test_disabled_missing_invalid_and_enabled_seam_preserve_contextpack_bytes():
    pack = _pack()
    before = pack.model_dump_json(by_alias=True)
    before_hash = context_pack_hash(pack)
    before_message = context_pack_system_message(pack)
    memory = _memory(("avoid_brand", "brand-x"))

    for projector in (
        ContextProjector(pack),
        ContextProjector(pack, long_term_memory_context=memory),
        ContextProjector(pack, long_term_memory_context=LongTermMemoryContext(), long_term_memory_enabled=True),
        ContextProjector(pack, long_term_memory_context=memory, long_term_memory_enabled=True),
    ):
        view = _planner(projector)
        assert pack.model_dump_json(by_alias=True) == before
        assert context_pack_hash(pack) == before_hash
        assert context_pack_system_message(pack) == before_message
        if projector is not None and view.long_term_memory == []:
            assert "longTermMemory" not in view.model_dump(by_alias=True)


def test_visible_phases_receive_no_id_channel_but_executor_and_validator_do_not():
    memory = _memory(("avoid_brand", "brand-x"), ("prefer_attribute", "compact"))
    projector = ContextProjector(pack := _pack(), long_term_memory_context=memory, long_term_memory_enabled=True)

    for view in (_planner(projector), _replanner(projector), _final(projector)):
        assert view.long_term_memory == [
            {"category": "shopping_preference", "semanticKey": "avoid_brand", "value": "brand-x"},
            {"category": "shopping_preference", "semanticKey": "prefer_attribute", "value": "compact"},
        ]
        dumped = view.model_dump(by_alias=True)
        assert "memoryRevision" not in dumped and "provenance" not in dumped
        assert "memory-entry-0" not in str(dumped)
        assert "brand-x" not in repr(view)

    executor = projector.executor_view(
        plan_id="plan-1", step_id="step-1", step_description="search", tool_name="search_products"
    )
    validator = projector.validator_view(executed_steps=[])
    assert "longTermMemory" not in executor.model_dump(by_alias=True)
    assert "longTermMemory" not in validator.model_dump(by_alias=True)
    assert "brand-x" not in repr(executor)
    assert "brand-x" not in context_pack_system_message(pack)["content"]


def test_planner_bridge_serializes_only_the_visible_channel_when_enabled():
    memory = _memory(("avoid_brand", "brand-x"))
    enabled = _planner(ContextProjector(
        _pack(), long_term_memory_context=memory, long_term_memory_enabled=True
    ))
    context = build_planner_context_from_view(enabled)
    payload = build_planner_messages(context)[1]["content"]
    assert '"longTermMemory": [{"category": "shopping_preference", "semanticKey": "avoid_brand", "value": "brand-x"}]' in payload
    assert "memory-entry-0" not in payload and "memoryRevision" not in payload

    disabled = _planner(ContextProjector(_pack()))
    disabled_payload = build_planner_messages(build_planner_context_from_view(disabled))[1]["content"]
    assert "longTermMemory" not in disabled_payload


def test_current_task_semantic_key_overrides_memory_value_without_mutating_context():
    pack = _pack().model_copy(update={"soft_preferences": [
        {"semanticKey": "avoid_brand", "value": "current", "source": "user"},
    ]})
    memory = _memory(("avoid_brand", "brand-x"), ("prefer_attribute", "compact"))
    view = _planner(ContextProjector(pack, long_term_memory_context=memory, long_term_memory_enabled=True))
    assert view.long_term_memory == [
        {"category": "shopping_preference", "semanticKey": "prefer_attribute", "value": "compact"},
    ]
    assert memory.payload_for_phase("planner") is not None


def test_forged_or_tampered_context_fails_closed_without_hiding_contextpack_errors():
    pack = _pack()
    forged = LongTermMemoryContext()
    assert _planner(ContextProjector(pack, long_term_memory_context=forged, long_term_memory_enabled=True)).long_term_memory == []

    issued = _memory(("avoid_brand", "brand-x"))
    record = memory_context_projection._ISSUED_CONTEXTS[id(issued)]
    memory_context_projection._ISSUED_CONTEXTS[id(issued)] = memory_context_projection._IssuedContext(
        record.reference, record.sealed, b"tampered"
    )
    assert _planner(ContextProjector(pack, long_term_memory_context=issued, long_term_memory_enabled=True)).long_term_memory == []


def test_parallel_projectors_do_not_share_optional_memory_channel():
    pack = _pack()
    left = _memory(("avoid_brand", "brand-a"))
    right = _memory(("avoid_brand", "brand-b"))

    def project(memory):
        return _planner(ContextProjector(pack, long_term_memory_context=memory, long_term_memory_enabled=True)).long_term_memory

    with ThreadPoolExecutor(max_workers=2) as pool:
        left_value, right_value = list(pool.map(project, (left, right)))
    assert left_value[0]["value"] == "brand-a"
    assert right_value[0]["value"] == "brand-b"


def test_channel_rechecks_b3a_entry_and_total_budget():
    # The B3a issuer rejects over-budget values before this seam can publish;
    # this test fixes the boundary expectations without duplicating its policy.
    memory = _memory(("avoid_brand", "brand-x"))
    channel = _planner(ContextProjector(_pack(), long_term_memory_context=memory, long_term_memory_enabled=True)).long_term_memory
    assert len(channel) <= 8
    assert len(str(channel).encode("utf-8")) <= 4096
    assert all(len(str(item).encode("utf-8")) <= 512 for item in channel)


def test_v3_binding_is_visible_only_to_model_phases_and_current_task_wins(monkeypatch):
    binding = _v3_binding(monkeypatch)
    phone_pack = _phone_pack(monkeypatch)
    projector = ContextProjector(
        phone_pack, long_term_memory_context=binding, long_term_memory_enabled=True,
    )
    expected = [{
        "categoryId": "phone",
        "preferenceKind": "avoid",
        "attributeKey": "brand",
        "normalizedValue": "apple",
    }]
    assert _planner(projector).long_term_memory == expected
    assert _replanner(projector).long_term_memory == expected
    assert _final(projector).long_term_memory == expected
    assert "longTermMemory" not in projector.executor_view(
        plan_id="plan-1", step_id="step-1", step_description="search",
        tool_name="search_products",
    ).model_dump(by_alias=True)
    assert "longTermMemory" not in projector.validator_view(
        executed_steps=[]
    ).model_dump(by_alias=True)
    assert "entry-1" not in repr(_planner(projector))
    assert "ownerBinding" not in str(_planner(projector).model_dump(by_alias=True))

    current_pack = phone_pack.model_copy(update={"hard_constraints": [
        TaskConstraint(key="brand", operator="eq", value="huawei", source="user")
    ]})
    overridden = ContextProjector(
        current_pack,
        long_term_memory_context=binding,
        long_term_memory_enabled=True,
    )
    assert _planner(overridden).long_term_memory == []

    wrong_category_pack = phone_pack.model_copy(update={"shopping_guide_state": {
        "mode": "recommend",
        "category": "laptop",
        "requirements": [],
        "candidateIds": [],
        "comparedIds": [],
        "evidenceStatus": "missing",
    }})
    category_mismatch = ContextProjector(
        wrong_category_pack,
        long_term_memory_context=binding,
        long_term_memory_enabled=True,
    )
    assert _planner(category_mismatch).long_term_memory == []


def test_retained_v13_binding_disables_durable_checkpoint_for_that_run(monkeypatch):
    from app.llm import _durable_enabled_for_run

    monkeypatch.setattr(settings, "agent_graph_v2_durable_enabled", True)
    binding = _v3_binding(monkeypatch)
    assert _durable_enabled_for_run(binding) is False
    assert _durable_enabled_for_run(
        v3_runtime.empty_memory_run_binding(
            "phone", "shopping-companion-9a8a2a1c13f"
        )
    ) is True
