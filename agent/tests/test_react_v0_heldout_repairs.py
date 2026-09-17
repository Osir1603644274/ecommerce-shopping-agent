"""Regressions extracted from the ReAct V0 held-out generalization failures."""

import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.control.react_context import build_decision_context_view
from app.llm import (
    _deterministic_used_phone_task_state_decision,
    _explicit_phone_price_ceiling,
    _has_unsupported_used_phone_capability_claim,
    _render_used_phone_evidence_boundary_answer,
    _unsupported_used_phone_capability,
    classify_task_relation,
)
from app.task_state import TaskState


def _state(*, invalidated_scope: bool = False) -> TaskState:
    now = datetime.now(timezone.utc)
    domain_state = {
        "shoppingGuide": {
            "mode": "recommend",
            "category": "phone",
            "requirements": [
                {
                    "key": "os",
                    "operator": "eq",
                    "value": "ios",
                    "unit": "enum",
                    "priority": "hard",
                    "source": "user",
                },
                {
                    "key": "price_minor",
                    "operator": "lte",
                    "value": 220_000,
                    "unit": "CNY_MINOR",
                    "priority": "hard",
                    "source": "user",
                },
            ],
            "candidateIds": [],
            "comparedIds": [],
            "evidenceStatus": "missing",
        },
    }
    if invalidated_scope:
        domain_state["candidateScopeInvalidation"] = {
            "scopeId": "scope-old",
            "status": "invalidated",
            "invalidationReason": "new_full_catalog_search_replaced_scope",
            "replacedByScopeId": "scope-current",
            "invalidatedAt": now.isoformat(),
        }
    return TaskState(
        taskId="task-heldout-repair",
        taskType="ecommerce_guide",
        status="ready",
        revision=10,
        goal="预算2200以内，只看苹果二手手机",
        unknowns=[],
        pendingQuestions=[],
        domainState=domain_state,
        createdAt=now,
        updatedAt=now,
    )


def test_budget_replacement_is_parsed_and_replaces_old_ceiling() -> None:
    assert _explicit_phone_price_ceiling("预算改成1600，系统要求不变") == 160_000

    arguments, observation = _deterministic_used_phone_task_state_decision(
        _state(),
        "预算改成1600，系统要求不变",
    )

    assert arguments is not None
    requirements = {
        row["key"]: row
        for row in arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"]
    }
    assert requirements["price_minor"]["value"] == 160_000
    assert requirements["os"]["value"] == "ios"
    assert observation["route"] == "deterministic_complete"
    assert "price_minor" in observation["mentionedKeys"]


@pytest.mark.parametrize(
    ("message", "expected_minor"),
    [
        ("预算改为1500元", 150_000),
        ("价格调整为1800", 180_000),
        ("价位降到一千六", 160_000),
        ("预算提高至2k", 200_000),
        ("预算上限为1750块", 175_000),
    ],
)
def test_budget_update_paraphrases(message: str, expected_minor: int) -> None:
    assert _explicit_phone_price_ceiling(message) == expected_minor

    arguments, observation = _deterministic_used_phone_task_state_decision(
        _state(),
        f"{message}，系统要求不变",
    )

    assert arguments is not None
    requirements = {
        row["key"]: row
        for row in arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"]
    }
    assert requirements["price_minor"]["value"] == expected_minor
    assert requirements["os"]["value"] == "ios"
    assert observation["route"] == "deterministic_complete"


def test_budget_unchanged_is_covered_during_android_constraint_update() -> None:
    arguments, observation = _deterministic_used_phone_task_state_decision(
        _state(),
        "现在加硬条件：只要安卓，预算不变",
    )

    assert arguments is not None
    requirements = {
        row["key"]: row
        for row in arguments["domainStatePatch"]["shoppingGuide"]["upsertRequirements"]
    }
    assert requirements["price_minor"]["value"] == 220_000
    assert requirements["os"]["value"] == "android"
    assert observation["route"] == "deterministic_complete"
    assert set(observation["mentionedKeys"]) == {"os", "price_minor"}
    assert set(observation["coveredKeys"]) == {"os", "price_minor"}


def test_clear_current_scope_followups_skip_task_manager_model() -> None:
    for message in (
        "继续比较之前那两个",
        "没有实测也别猜，就根据已有属性告诉我怎么选",
        "比较第一个和第三个",
        "这三款运行大型游戏时，哪款帧率最稳、发热最低？",
    ):
        with patch("app.llm.get_client") as get_client:
            decision = asyncio.run(classify_task_relation(message, _state(), []))

        assert decision.relation == "continue_current"
        assert decision.confidence == 1.0
        get_client.assert_not_called()


def test_invalidated_old_pair_reference_clarifies_without_model_fallback() -> None:
    arguments, observation = _deterministic_used_phone_task_state_decision(
        _state(invalidated_scope=True),
        "继续比较之前那两个",
    )

    assert arguments is not None
    assert arguments["status"] == "collecting_information"
    assert "旧筛选范围" in arguments["pendingQuestions"][0]
    assert observation == {
        "schemaVersion": "used-phone-task-state-extraction-decision-v1",
        "route": "deterministic_stale_scope_clarification",
        "reason": "stale_candidate_reference",
        "mentionedKeys": [],
        "coveredKeys": [],
        "uncoveredKeys": [],
    }

    payload = _state(invalidated_scope=True).model_dump(by_alias=True, mode="json")
    payload["status"] = "collecting_information"
    payload["pendingQuestions"] = arguments["pendingQuestions"]
    payload["domainState"]["taskStateExtraction"] = observation
    view = build_decision_context_view(
        TaskState.model_validate(payload),
        user_message="继续比较之前那两个",
        allowed_tool_names=["search_products", "compare_products"],
    )
    assert view.observation_summary.adaptive_trigger == "stale_reference"
    assert [option.option_id for option in view.allowed_action_options] == [
        "clarify.pending.0",
        "clarify.stale.product_ids",
    ]


@pytest.mark.parametrize(
    "message",
    [
        "继续比较最开始那两个",
        "比较最开始的两个",
        "继续比较之前那两个",
        "原来那两个哪个更好",
        "先前那两个再对比一下",
    ],
)
def test_invalidated_scope_reference_paraphrases_fail_closed(message: str) -> None:
    arguments, observation = _deterministic_used_phone_task_state_decision(
        _state(invalidated_scope=True),
        message,
    )

    assert arguments is not None
    assert arguments["status"] == "collecting_information"
    assert observation["reason"] == "stale_candidate_reference"


def test_unsupported_capability_claims_are_detected_but_boundaries_are_allowed() -> None:
    assert _has_unsupported_used_phone_capability_claim(
        "小米14更适合看重手机性能、处理器和新款程度的用户。"
    )
    safe = _render_used_phone_evidence_boundary_answer()
    assert not _has_unsupported_used_phone_capability_claim(safe)
    assert "不含可靠的游戏帧率、散热或拍照质量测试数据" in safe
    assert "不会根据商品标题猜测" in safe


def test_capability_boundary_takes_precedence_over_comparison_intent() -> None:
    state = _state()
    state.domain_state["shoppingGuide"]["comparedIds"] = [1, 2, 3]
    assert _unsupported_used_phone_capability(
        "这三款运行大型游戏时，哪款帧率最稳、发热最低？"
    ) is not None

    arguments, observation = _deterministic_used_phone_task_state_decision(
        state,
        "运行大型游戏时，哪款帧率最稳、发热最低？",
    )

    assert arguments is not None
    assert arguments["status"] == "collecting_information"
    assert observation["route"] == "deterministic_capability_boundary"
    assert observation["reason"] == "unsupported_game_camera_evidence"


@pytest.mark.parametrize(
    "message",
    [
        "这三款运行大型游戏时哪款帧率最稳？",
        "哪款打游戏发热最低？",
        "哪款手机散热更好？",
        "哪款相机实拍画质更好？",
    ],
)
def test_measured_capability_paraphrases_use_evidence_boundary(message: str) -> None:
    arguments, observation = _deterministic_used_phone_task_state_decision(
        _state(),
        message,
    )

    assert arguments is not None
    assert arguments["status"] == "collecting_information"
    assert observation["reason"] == "unsupported_game_camera_evidence"
