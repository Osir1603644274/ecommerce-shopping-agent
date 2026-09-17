"""DecisionContextView boundary tests for minimal ReAct V0."""

from datetime import datetime, timezone

import pytest

from app.control.react_actions import ActionOutcome
from app.control.react_context import (
    DECISION_VIEW_TOKEN_BUDGET,
    build_decision_context_view,
    decision_view_token_count,
)
from app.task_state import TaskState


_PRICE_REQ = {
    "key": "price_minor",
    "operator": "lte",
    "value": 300_000,
    "unit": "CNY_MINOR",
    "priority": "hard",
    "source": "user",
}


def _state(*, task_id: str = "task-react-v0", validated: bool = False) -> TaskState:
    now = datetime.now(timezone.utc)
    domain_state = {
        "shoppingGuide": {
            "mode": "recommend",
            "category": "phone",
            "useCases": ["续航"],
            "requirements": [_PRICE_REQ],
            "brandAvoidances": [],
            "candidateIds": [11, 12, 13],
            "comparedIds": [],
            "evidenceStatus": "partial",
        },
        "candidateScope": {
            "scopeId": "scope-react-v0-r17",
            "taskId": task_id,
            "sourceRevision": 17,
            "sourcePlanId": "plan-search",
            "sourceStepId": "step-search",
            "category": "phone",
            "candidatePoolIds": list(range(1, 51)),
            "rankedItemIds": list(range(1, 21)),
            "visibleProductIds": [1, 2, 3],
            "requirementsSnapshot": [_PRICE_REQ],
            "brandAvoidancesSnapshot": [],
            "evidenceRefs": ["product:1:title"],
            "createdAt": now.isoformat(),
            "status": "active",
            "invalidationReason": None,
        },
        "scopeRerankRequest": {
            "scopeId": "scope-react-v0-r17",
            "rankingIntent": "gaming_title_claim",
            "createdAt": now.isoformat(),
        },
    }
    if validated:
        domain_state["validationResult"] = {
            "outcome": "passed",
            "basedOnRevision": 17,
        }
    return TaskState(
        taskId=task_id,
        taskType="ecommerce_guide",
        status="ready",
        revision=18,
        goal="三千以内、续航好的二手手机",
        unknowns=[],
        pendingQuestions=[],
        domainState=domain_state,
        createdAt=now,
        updatedAt=now,
    )


def test_decision_view_is_compact_hash_bound_and_excludes_raw_pool() -> None:
    view = build_decision_context_view(
        _state(),
        user_message="这三台哪台更适合我？",
        allowed_tool_names=[
            "search_products",
            "compare_products",
            "rerank_products_in_scope",
            "create_order",
        ],
    )

    dumped = view.model_dump(by_alias=True, mode="json")
    assert dumped["taskRevision"] == 18
    assert dumped["candidateScope"] == {
        "scopeId": "scope-react-v0-r17",
        "status": "active",
        "candidatePoolCount": 50,
        "rankedItemCount": 20,
        "visibleProductIds": [1, 2, 3],
    }
    assert [tool["name"] for tool in dumped["allowedTools"]] == [
        "search_products",
        "rerank_products_in_scope",
    ]
    assert "candidatePoolIds" not in str(dumped)
    assert "create_order" not in str(dumped)
    assert len(view.decision_view_hash) == 64
    assert decision_view_token_count(view) <= DECISION_VIEW_TOKEN_BUDGET


def test_decision_view_hash_changes_with_revision_bound_input() -> None:
    state = _state()
    first = build_decision_context_view(
        state,
        user_message="哪台适合我？",
        allowed_tool_names=["search_products"],
    )
    second = build_decision_context_view(
        state.model_copy(update={"revision": 19}),
        user_message="哪台适合我？",
        allowed_tool_names=["search_products"],
    )
    assert first.decision_view_hash != second.decision_view_hash


def test_zero_result_publishes_only_server_owned_adaptive_terminal_options() -> None:
    payload = _state().model_dump(by_alias=True, mode="json")
    payload["domainState"].pop("candidateScope", None)
    payload["domainState"].pop("scopeRerankRequest", None)
    payload["domainState"]["validationResult"] = {
        "outcome": "insufficient_evidence",
        "errorCode": "product_candidates_missing",
        "basedOnRevision": 17,
        "stepResults": [{
            "evidenceSummary": {
                "requiresProductCandidates": {
                    "candidatePoolCount": 50,
                    "rankedItemCount": 0,
                    "hasCompleteMatch": False,
                },
            },
        }],
    }
    state = TaskState.model_validate(payload)
    outcome = ActionOutcome(
        actionId="action-search",
        status="REJECTED",
        observationRef=None,
        validatorOutcome="REJECTED",
        stateRevisionAfter=18,
        retryable=False,
        errorCode="product_candidates_missing",
    )

    view = build_decision_context_view(
        state,
        user_message="预算300以内，其他硬条件不变",
        allowed_tool_names=["search_products"],
        last_outcome=outcome,
    )

    assert view.observation_summary.adaptive_trigger == "zero_result"
    assert view.observation_summary.ranked_item_count == 0
    assert view.answer_context_ref == "zero-result-task:task-react-v0:r18"
    assert [item.option_id for item in view.allowed_action_options] == [
        "answer.zero_result",
        "clarify.pending.0",
    ]
    assert all(item.kind != "CALL_TOOL" for item in view.allowed_action_options)


def test_answer_reference_requires_current_passed_validation() -> None:
    unvalidated = build_decision_context_view(
        _state(),
        user_message="回答吧",
        allowed_tool_names=[],
    )
    validated = build_decision_context_view(
        _state(validated=True),
        user_message="回答吧",
        allowed_tool_names=[],
    )
    assert unvalidated.answer_context_ref == "validated-scope:scope-react-v0-r17"
    assert validated.answer_context_ref == "validated-task:task-react-v0:r18"


def test_text_claim_discovery_deterministically_runs_new_search() -> None:
    from app.control.react_decision import deterministic_next_action

    state = _state()
    state.domain_state["taskStateExtraction"] = {
        "reason": "camera_title_claim",
    }
    view = build_decision_context_view(
        state,
        user_message="主要用来拍照，不怎么打游戏",
        allowed_tool_names=["search_products", "rerank_products_in_scope"],
    )
    action = deterministic_next_action(view)
    assert view.server_signals["broadCatalogDiscovery"] is True
    assert action is not None
    assert action.kind == "CALL_TOOL"
    assert action.tool_name == "search_products"


def test_repeated_exact_constraint_answers_from_current_scope() -> None:
    from app.control.react_decision import deterministic_next_action

    state = _state()
    state.domain_state["taskStateExtraction"] = {
        "reason": "complete_controlled_coverage",
    }
    view = build_decision_context_view(
        state,
        user_message="我预算是3000呀",
        allowed_tool_names=["search_products", "rerank_products_in_scope"],
    )
    action = deterministic_next_action(view)
    assert view.server_signals["scopeAnswerRequested"] is True
    assert action is not None
    assert action.kind == "ANSWER"


def test_changed_requirements_invalidate_scope_answer_reference() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["shoppingGuide"]["requirements"][0]["value"] = 250_000
    changed = TaskState.model_validate(payload)
    view = build_decision_context_view(
        changed,
        user_message="预算改成2500",
        allowed_tool_names=["search_products"],
    )
    assert view.answer_context_ref is None
    assert view.candidate_scope is None
    assert [tool.name for tool in view.allowed_tools] == ["search_products"]
    assert view.server_signals["staleCandidateScope"] is True
    assert view.server_signals["validatedEvidenceAvailable"] is False


def test_unbound_negative_target_is_projected_as_required_clarification() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "unbound_negative_target",
    }
    ambiguous = TaskState.model_validate(payload)

    view = build_decision_context_view(
        ambiguous,
        user_message="不要那个牌子",
        allowed_tool_names=["search_products"],
    )

    assert view.server_signals["unboundNegativeTarget"] is True
    assert view.pending_questions == ["你说的“那个牌子”具体指哪个品牌？"]
    assert {"kind": "unbound_negative_target", "key": "brand"} in view.unknowns


def test_stale_scope_reference_cannot_publish_compare_tool() -> None:
    state = _state()
    payload = state.model_dump(by_alias=True, mode="json")
    payload["domainState"]["shoppingGuide"]["requirements"][0]["value"] = 250_000
    payload["domainState"]["shoppingGuide"]["comparedIds"] = [1, 2]
    stale = TaskState.model_validate(payload)

    view = build_decision_context_view(
        stale,
        user_message="还是比较最开始那两个",
        allowed_tool_names=["search_products", "compare_products"],
    )

    assert view.candidate_scope is None
    assert view.server_signals["staleScopeReference"] is True
    assert [tool.name for tool in view.allowed_tools] == ["search_products"]
    assert view.pending_questions == [
        "这两个来自旧筛选范围，可能不满足当前硬条件。"
        "请基于当前候选重新指定序号，或明确提供要比较的商品 ID。"
    ]
    assert [item.option_id for item in view.allowed_action_options] == [
        "clarify.pending.0",
        "clarify.stale.product_ids",
    ]


def test_old_compared_ids_outside_new_current_scope_trigger_clarification() -> None:
    payload = _state().model_dump(by_alias=True, mode="json")
    payload["domainState"]["shoppingGuide"]["comparedIds"] = [101, 102]
    state = TaskState.model_validate(payload)

    view = build_decision_context_view(
        state,
        user_message="还是比较最开始那两个",
        allowed_tool_names=["search_products", "compare_products"],
    )

    assert view.observation_summary.adaptive_trigger == "stale_reference"
    assert view.observation_summary.stale_candidate_scope is True
    assert [item.option_id for item in view.allowed_action_options] == [
        "clarify.pending.0",
        "clarify.stale.product_ids",
    ]


def test_unsupported_evidence_publishes_two_safe_boundary_options() -> None:
    from app.control.react_decision import deterministic_next_action

    payload = _state().model_dump(by_alias=True, mode="json")
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "unsupported_game_camera_evidence",
    }
    payload["pendingQuestions"] = ["缺少性能实测数据"]
    state = TaskState.model_validate(payload)

    view = build_decision_context_view(
        state,
        user_message="这三款谁帧率最高、散热最好？",
        allowed_tool_names=["search_products", "compare_products"],
    )

    assert view.observation_summary.adaptive_trigger == "unsupported_evidence"
    assert view.observation_summary.evidence_gap_keys == ["gaming_fps", "thermal"]
    assert [item.option_id for item in view.allowed_action_options] == [
        "answer.validated_context",
        "clarify.unsupported_evidence",
    ]
    assert view.allowed_action_options[0].answer_context_ref == (
        f"evidence-boundary:gaming:{state.task_id}:r{state.revision}"
    )
    action = deterministic_next_action(view)
    assert action is not None
    assert action.kind == "ANSWER"
    assert action.reason_code == "answer_evidence_boundary"


def test_unsupported_camera_evidence_publishes_camera_specific_boundary() -> None:
    payload = _state().model_dump(by_alias=True, mode="json")
    payload["domainState"]["taskStateExtraction"] = {
        "reason": "unsupported_game_camera_evidence",
    }
    state = TaskState.model_validate(payload)
    view = build_decision_context_view(
        state,
        user_message="当前候选中哪个拍照实际表现更好，尤其是夜景？",
        allowed_tool_names=["search_products", "compare_products"],
    )
    assert view.observation_summary.evidence_gap_keys == [
        "camera_performance", "night_photography"
    ]
    assert view.allowed_action_options[0].answer_context_ref == (
        f"evidence-boundary:camera:{state.task_id}:r{state.revision}"
    )
    assert view.answer_context_ref == view.allowed_action_options[0].answer_context_ref


def test_cross_task_candidate_scope_fails_closed() -> None:
    state = _state()
    copied = state.model_dump(by_alias=True, mode="json")
    copied["domainState"]["candidateScope"]["taskId"] = "task-other"
    mismatched = TaskState.model_validate(copied)

    with pytest.raises(ValueError, match="does not belong"):
        build_decision_context_view(
            mismatched,
            user_message="继续",
            allowed_tool_names=["search_products"],
        )


def test_tool_arguments_are_server_references_not_values() -> None:
    view = build_decision_context_view(
        _state(),
        user_message="继续",
        allowed_tool_names=["search_products"],
    )
    refs = view.allowed_tools[0].argument_refs
    assert refs == {
        "query": "taskState.goal",
        "category": "shoppingGuide.category",
        "requirements": "shoppingGuide.compiledRequirements",
    }
    assert "三千" not in str(refs)
