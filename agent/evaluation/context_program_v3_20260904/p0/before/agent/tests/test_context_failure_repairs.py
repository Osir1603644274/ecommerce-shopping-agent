"""Offline regressions for the Context P4 failure boundaries (no provider calls)."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import llm
from app.domains.ecommerce.shopping_state_authority import (
    ShoppingStateAuthorityError, bind_authoritative_write,
)
from app.evaluation_context_arm import issue_evaluation_context_arm
from tests.test_shopping_state_update import _state


def _client(responses=()):
    create = AsyncMock(side_effect=list(responses))
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _response(arguments, *, finish_reason="tool_calls", reasoning=None):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(content=None, reasoning_content=reasoning, tool_calls=[
            SimpleNamespace(id="extract-1", function=SimpleNamespace(
                name="update_task_state", arguments=arguments,
            )),
        ]),
    )])


@pytest.mark.parametrize("failure", ["exception", "timeout", "missing_update"])
def test_pre_harness_failure_preserves_evaluation_identity(monkeypatch, failure):
    state = _state().model_copy(update={"session_id": "repair-session"})
    cap = issue_evaluation_context_arm(
        arm="RAW_FULL_CONTROL", run_id="run-repair-bound", task_id=state.task_id,
        session_id=state.session_id, model="fixture", model_client=_client(),
        tool_transport=AsyncMock(), provider_max_retries=0,
    )
    extractor = AsyncMock(return_value=state)
    if failure != "missing_update":
        extractor.side_effect = TimeoutError() if failure == "timeout" else ValueError("bad payload")
    monkeypatch.setattr(llm, "_update_task_state_for_unified_harness", extractor)
    monkeypatch.setattr(llm, "_persist_trace_safely", AsyncMock())
    graph = AsyncMock(side_effect=AssertionError("must not enter durable graph"))
    monkeypatch.setattr(llm, "_run_explicit_harness_agent", graph)
    monkeypatch.setattr(llm.settings, "agent_control_runtime", "react_v1")
    monkeypatch.setattr(llm.settings, "agent_react_live_enabled", True)
    monkeypatch.setattr(llm.settings, "agent_graph_v2_durable_enabled", True)
    result = asyncio.run(llm._run_unified_harness_agent(
        "继续", history=None, task_state=state, on_answer_delta=None,
        on_task_state=None, session_id=state.session_id, evaluation_context_arm=cap,
    ))
    assert result[3] == cap.identity.run_id
    assert result[4].run_id == cap.identity.run_id
    assert result[1] == []
    graph.assert_not_awaited()


def test_server_use_case_overlay_preserves_new_model_requirements(monkeypatch):
    state = _state()
    arguments = {
        "status": "ready", "pendingQuestions": [],
        "domainStatePatch": {"shoppingGuide": {"upsertRequirements": [{
            "key": "price_minor", "operator": "lte", "value": 160000,
            "unit": "CNY_MINOR", "priority": "hard", "source": "user",
        }]}},
    }
    captured = []

    async def persist(current, payload, **kwargs):
        captured.append(deepcopy(payload))
        domain = {**current.domain_state, **payload["domainStatePatch"]}
        domain = {key: value for key, value in domain.items() if value is not None}
        bound = bind_authoritative_write(
            domain, task_id=current.task_id, task_revision=current.revision + 1,
            goal=payload.get("goal", current.goal), unknowns=[], pending_questions=[],
        )
        return current.model_copy(update={"revision": current.revision + 1, "domain_state": bound})

    monkeypatch.setattr(llm, "_persist_task_patch", persist)
    updated = asyncio.run(llm._apply_task_state_update(
        state, arguments, message="预算改为 1600 元", on_task_state=None,
        require_status=True,
        server_domain_patch_builder=lambda proposed: llm._server_owned_use_case_guide_patch(
            proposed, "daily_stability", "预算改为 1600 元",
        ),
    ))
    guide = updated.domain_state["shoppingGuide"]
    snapshot = updated.domain_state["shoppingTaskStateV2"]
    assert next(r["value"] for r in guide["requirements"] if r["key"] == "price_minor") == 160000
    assert snapshot["shoppingGuide"] == guide
    assert "daily_stability" in guide["useCases"]
    assert guide["candidateIds"] == []
    assert updated.domain_state["candidateScope"]["status"] == "invalidated"
    assert len(captured) == 1


def test_extraction_checks_truncation_even_if_json_prefix_is_valid(monkeypatch):
    # A syntactically complete prefix is not proof that the intended patch finished.
    client = _client([_response('{"status":"ready"}', finish_reason="length")])
    monkeypatch.setattr(llm.settings, "deepseek_model", "deepseek-v4-flash")
    call, response = asyncio.run(llm._submit_task_state_extraction(client, []))
    with pytest.raises(llm.TaskStatePayloadValidationError, match="truncated"):
        llm._parse_task_state_arguments(call, response=response)


def test_extraction_uses_phase_budget_and_explicit_non_thinking(monkeypatch):
    client = _client([_response('{"status":"ready"}')])
    monkeypatch.setattr(llm.settings, "deepseek_model", "deepseek-v4-flash")
    asyncio.run(llm._submit_task_state_extraction(client, []))
    request = client.chat.completions.create.await_args.kwargs
    assert request["max_tokens"] >= 4096
    assert request["extra_body"]["thinking"] == {"type": "disabled"}
    assert "tool_choice" not in request


@pytest.mark.parametrize("raw", [
    '{"status":"ready","status":"completed"}',
    '{"status":"ready","upsertFacts":[{"key":"x","value":NaN,"source":"user"}]}',
    '{"status":"ready","upsertFacts":[{"key":"x","value":1e999,"source":"user"}]}',
])
def test_extraction_rejects_ambiguous_or_non_json_numbers(raw):
    call = _response(raw).choices[0].message.tool_calls[0]
    with pytest.raises(llm.TaskStatePayloadValidationError):
        llm._parse_task_state_arguments(call)


def test_nested_arguments_remains_rejected_with_actionable_error():
    with pytest.raises(llm.TaskStatePayloadValidationError) as error:
        llm._validated_model_task_patch({"arguments": {"status": "ready"}})
    assert error.value.code == "unexpected_task_state_arguments_wrapper"
    assert error.value.field_path == "arguments.arguments"


def _strict_wire(native, payload):
    if native.get("type") == "object":
        return {
            key: _strict_wire(child, payload[key]) if key in payload else None
            for key, child in native["properties"].items()
        }
    if native.get("type") == "array":
        return [_strict_wire(native["items"], child) for child in payload]
    return payload


@pytest.mark.parametrize("native_payload", [
    {"status": "ready"},
    {"status": "ready", "pendingQuestions": []},
    {"status": "ready", "domainStatePatch": {"shoppingGuide": {"requirements": []}}},
    {"status": "ready", "upsertFacts": [{"key": "nullable", "value": None, "source": "user"}]},
    {"status": "ready", "domainStatePatch": {"shoppingGuide": {"upsertRequirements": [{
        "key": "brand", "operator": "in", "value": ["apple", "honor"],
        "unit": "text", "priority": "soft", "source": "user",
    }]}}},
])
def test_strict_wire_preserves_omission_empty_arrays_and_required_null(native_payload):
    native = deepcopy(llm.TASK_STATE_TOOL_SCHEMA)
    wire = _strict_wire(native["function"]["parameters"], native_payload)
    call = _response(json.dumps(wire)).choices[0].message.tool_calls[0]
    assert llm._parse_task_state_arguments(call, strict=True) == native_payload
    assert llm.TASK_STATE_TOOL_SCHEMA == native


@pytest.mark.parametrize("mutation", ["missing_status", "null_status", "extra_key", "nested_document"])
def test_strict_wire_fails_closed_before_normalization(mutation):
    wire = _strict_wire(llm.TASK_STATE_TOOL_SCHEMA["function"]["parameters"], {"status": "ready"})
    if mutation == "missing_status":
        del wire["status"]
    elif mutation == "null_status":
        wire["status"] = None
    elif mutation == "extra_key":
        wire["arguments"] = {"status": "ready"}
    else:
        wire["upsertFacts"] = [{"key": "x", "source": "user", "certainty": None, "value": {"x": 1}}]
    with pytest.raises(llm.TaskStatePayloadValidationError):
        llm._parse_task_state_arguments(
            _response(json.dumps(wire)).choices[0].message.tool_calls[0], strict=True,
        )


def test_strict_extraction_requires_explicit_beta_endpoint(monkeypatch):
    client = _client()
    monkeypatch.setattr(llm.settings, "task_state_extraction_strict_enabled", True)
    monkeypatch.setattr(llm.settings, "deepseek_base_url", "https://api.deepseek.com")
    with pytest.raises(ValueError, match="requires_official_deepseek_beta"):
        asyncio.run(llm._submit_task_state_extraction(client, []))
    client.chat.completions.create.assert_not_awaited()


def test_strict_extraction_uses_strict_tool_without_mutating_native_schema(monkeypatch):
    original = deepcopy(llm.TASK_STATE_TOOL_SCHEMA)
    client = _client([_response('{}')])
    monkeypatch.setattr(llm.settings, "task_state_extraction_strict_enabled", True)
    monkeypatch.setattr(llm.settings, "deepseek_base_url", "https://api.deepseek.com/beta")
    monkeypatch.setattr(llm.settings, "deepseek_model", "deepseek-v4-flash")
    asyncio.run(llm._submit_task_state_extraction(client, []))
    function = client.chat.completions.create.await_args.kwargs["tools"][0]["function"]
    assert function["strict"] is True
    assert set(function["parameters"]["required"]) == set(function["parameters"]["properties"])
    assert llm.TASK_STATE_TOOL_SCHEMA == original


def test_pre_harness_stop_rejects_cross_task_capability(monkeypatch):
    state = _state().model_copy(update={"session_id": "session-a"})
    cap = issue_evaluation_context_arm(
        arm="RAW_FULL_CONTROL", run_id="run-other", task_id="different-task",
        session_id="session-a", model="fixture", model_client=_client(),
        tool_transport=AsyncMock(), provider_max_retries=0,
    )
    persist = AsyncMock()
    monkeypatch.setattr(llm, "_persist_trace_safely", persist)
    with pytest.raises(RuntimeError, match="identity_mismatch"):
        asyncio.run(llm._unified_pre_harness_safe_stop(
            "继续", state=state, answer="停止", failure_code="invalid",
            on_answer_delta=None, evaluation_context_arm=cap,
        ))
    persist.assert_not_awaited()


def test_unified_write_rejection_never_falls_back_to_partial_domain_write(monkeypatch):
    persist = AsyncMock(side_effect=ValueError("authority rejected write"))
    monkeypatch.setattr(llm, "_persist_task_patch", persist)
    with pytest.raises(ValueError, match="authority rejected write"):
        asyncio.run(llm._apply_task_state_update(
            _state(), {"status": "ready"}, message="继续", on_task_state=None,
            require_status=True,
        ))
    assert persist.await_count == 1


def test_server_overlay_is_rebuilt_from_latest_proposed_state_on_occ(monkeypatch):
    state = _state()
    latest = state.model_copy(deep=True, update={"revision": state.revision + 1})
    latest.domain_state["shoppingGuide"]["requirements"].append({
        "key": "storage_gb", "operator": "gte", "value": 128,
        "unit": "GB", "priority": "hard", "source": "user",
    })
    arguments = {"status": "ready", "domainStatePatch": {"shoppingGuide": {
        "upsertRequirements": [{"key": "price_minor", "operator": "lte", "value": 160000,
            "unit": "CNY_MINOR", "priority": "hard", "source": "user"}],
    }}}

    async def persist(current, payload, **kwargs):
        rebuilt = kwargs["retry_payload_builder"](latest)
        guide = rebuilt["domainStatePatch"]["shoppingGuide"]
        values = {item["key"]: item["value"] for item in guide["requirements"]}
        assert values == {"os": "ios", "price_minor": 160000, "storage_gb": 128}
        assert guide["useCases"] == ["daily"]
        return latest

    monkeypatch.setattr(llm, "_persist_task_patch", persist)
    asyncio.run(llm._apply_task_state_update(
        state, arguments, message="预算 1600", on_task_state=None, require_status=True,
        server_domain_patch_builder=lambda proposed: llm._server_owned_use_case_guide_patch(
            proposed, "daily", "日常使用",
        ),
    ))


def test_server_only_requirement_change_rebuilds_lifecycle_without_weakening_authority(monkeypatch):
    state = _state()
    guide = deepcopy(state.domain_state["shoppingGuide"])
    guide["requirements"][1]["value"] = 160000
    guide.update(candidateIds=[], comparedIds=[], evidenceStatus="missing")

    def bind(current, payload):
        domain = {**current.domain_state, **payload["domainStatePatch"]}
        domain = {key: value for key, value in domain.items() if value is not None}
        return bind_authoritative_write(
            domain, task_id=current.task_id, task_revision=current.revision + 1,
            goal=current.goal, unknowns=[], pending_questions=[],
        )

    # Reconstruct the previous implementation's late overlay. Its snapshot
    # still says 2200 while its guide says 1600: authority must reject it.
    old_payload, _ = llm._build_validated_task_state_payload(
        state, {"status": "ready"}, message="继续", require_status=True,
    )
    old_payload["domainStatePatch"]["shoppingGuide"] = guide
    with pytest.raises(ShoppingStateAuthorityError) as rejected:
        bind(state, old_payload)
    assert "requirements must equal" in str(rejected.value.__cause__)

    async def persist(current, payload, **kwargs):
        domain = bind(current, payload)
        assert domain["shoppingTaskStateV2"]["requirements"][1]["value"] == 160000
        assert domain["shoppingTaskStateV2"]["shoppingGuide"] == domain["shoppingGuide"]
        assert domain["candidateScope"]["status"] == "invalidated"
        return current.model_copy(update={"domain_state": domain})

    monkeypatch.setattr(llm, "_persist_task_patch", persist)
    asyncio.run(llm._apply_task_state_update(
        state, {"status": "ready"}, message="继续", on_task_state=None,
        require_status=True, server_domain_patch={"shoppingGuide": guide},
    ))
