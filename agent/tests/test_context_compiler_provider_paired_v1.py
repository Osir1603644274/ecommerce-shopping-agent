from __future__ import annotations

from agent.evaluation.context_compiler_provider_paired_v1_20260901_v1 import runner
from agent.evaluation.context_compiler_provider_paired_v1_20260901_v2 import (
    runner as runner_v2,
)
from agent.evaluation.context_compiler_provider_paired_v1_20260901_v3 import (
    runner as runner_v3,
)
from agent.evaluation.context_compiler_provider_paired_v1_20260901_v4 import (
    runner as runner_v4,
)


def _case() -> dict:
    return {
        "caseId": "case-1",
        "scenarioId": "scenario-1",
        "turnId": "T2",
        "query": "刚才那几个继续比较",
        "referenceQuery": True,
        "expectedRecentReference": "先给我三台",
        "contextPayload": {
            "runId": "run-1",
            "taskId": "task-1",
            "baseContextRevision": 2,
            "goal": "刚才那几个继续比较",
            "confirmedFacts": [{"key": "category", "value": "phone"}],
            "hardConstraints": [],
            "softPreferences": [],
            "unknowns": [],
            "pendingQuestions": [],
            "shoppingGuideState": {"category": "phone"},
            "candidateScopeState": {
                "scopeId": "scope-1",
                "taskId": "task-1",
                "sourceRevision": 2,
                "status": "active",
                "rankedItemIds": [101, 102, 103],
                "visibleProductIds": [101, 102, 103],
            },
            "historySummaries": [
                {
                    "role": "user",
                    "summary": "无关旧消息",
                    "atTurn": -1,
                    "kind": "older_summary",
                    "sourceTurns": [1],
                },
                {
                    "role": "user",
                    "summary": "先给我三台",
                    "atTurn": 1,
                    "kind": "recent_verbatim",
                    "sourceTurns": [1],
                },
            ],
            "allowedTools": ["get_product_details"],
            "evidenceRefs": ["dataset:test"],
        },
    }


def test_compile_pair_preserves_protected_fields_and_references() -> None:
    case = _case()
    control = runner.compile_case(case, "CTX1a")
    treatment = runner.compile_case(case, "CTX1b")
    for field in runner.PROTECTED_FIELDS:
        assert control.model_view.get(field) == treatment.model_view.get(field)
    assert treatment.model_view["historySummaries"][-1]["summary"] == "先给我三台"


def test_exact_fidelity_requires_byte_equivalent_values() -> None:
    expected = runner.expected_output(_case())
    assert runner.exact_fidelity(dict(expected), expected)
    changed = dict(expected)
    changed["candidateIds"] = [103, 102, 101]
    assert not runner.exact_fidelity(changed, expected)


def test_v2_prompt_rejects_non_positive_distractor_as_prior_turn() -> None:
    assert "positive integer" in runner_v2.base.SYSTEM_PROMPT
    assert "if no such item exists return null" in runner_v2.base.SYSTEM_PROMPT


def test_v3_adapter_disables_thinking_only_for_tool_requests() -> None:
    class Delegate:
        def __init__(self) -> None:
            self.kwargs = None

        async def create(self, *args, **kwargs):
            self.kwargs = kwargs
            return "ok"

    delegate = Delegate()
    proxy = runner_v3._CompletionsProxy(delegate)
    import asyncio

    assert asyncio.run(proxy.create(tools=[{"type": "function"}])) == "ok"
    assert delegate.kwargs["extra_body"]["thinking"] == {"type": "disabled"}


def _v4_case() -> dict:
    case = _case()
    case["schemaVersion"] = "context-compiler-provider-case-v2"
    case["lineageKind"] = "A2_REAL_UI_SANITIZED"
    payload = case["contextPayload"]
    payload["referenceContextState"] = runner_v4.bind_reference_state({
        "schemaVersion": "context-provider-reference-state-v1",
        "sourceKind": "SIGNED_UI_RECEIPT",
        "taskId": payload["taskId"],
        "contextTaskRevision": payload["baseContextRevision"],
        "referenceTaskRevision": 1,
        "historySourceTurn": None,
        "candidateScopeId": "scope-1",
        "candidateScopeSourceRevision": 2,
        "recentReference": "先给我三台",
        "presentationMode": "compact",
        "presentationIds": [101, 102, 103],
        "focusedProductId": 102,
        "comparedProductIds": [102, 103],
    })
    return case


def test_v4_server_resolved_reference_state_is_protected_and_expected() -> None:
    case = _v4_case()
    state = runner_v4.validate_reference_state(case)
    assert state["focusedProductId"] == 102
    control = runner_v4.compile_case(case, "CTX1a")
    treatment = runner_v4.compile_case(case, "CTX1b")
    assert control.model_view["referenceContextState"] == state
    assert treatment.model_view["referenceContextState"] == state
    expected = runner_v4.expected_output(case)
    assert expected["recentReference"] == "先给我三台"
    assert expected["referenceSource"] == "SIGNED_UI_RECEIPT"
    assert expected["focusedProductId"] == 102
    assert expected["comparedProductIds"] == [102, 103]


def test_v4_reference_state_fails_closed_on_tamper_and_cross_scope() -> None:
    import copy

    tampered = copy.deepcopy(_v4_case())
    tampered["contextPayload"]["referenceContextState"]["bindingHash"] = "0" * 64
    try:
        runner_v4.validate_reference_state(tampered)
    except runner_v4.ReferenceStateError as exc:
        assert str(exc) == "reference_state_binding_invalid"
    else:
        raise AssertionError("tampered binding was accepted")

    wrong_scope = copy.deepcopy(_v4_case())
    state = wrong_scope["contextPayload"]["referenceContextState"]
    state["candidateScopeSourceRevision"] = 99
    wrong_scope["contextPayload"]["referenceContextState"] = (
        runner_v4.bind_reference_state(state)
    )
    try:
        runner_v4.validate_reference_state(wrong_scope)
    except runner_v4.ReferenceStateError as exc:
        assert str(exc) == "reference_state_signed_scope_invalid"
    else:
        raise AssertionError("cross-scope reference was accepted")


def test_v4_prompt_uses_typed_reference_instead_of_history_inference() -> None:
    assert "already resolved and authoritative" in runner_v4.SYSTEM_PROMPT
    assert "never infer a reference from historySummaries" in runner_v4.SYSTEM_PROMPT
