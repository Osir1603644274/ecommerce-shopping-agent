import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

from app.llm import classify_task_relation
from app.runtime import resolve_domain
from app.task_state import TaskState


def _ecommerce_task(category: str = "phone") -> TaskState:
    now = datetime.now(UTC)
    return TaskState(
        taskId="task-ecommerce",
        taskType="ecommerce_guide",
        sessionId="session-1",
        status="ready",
        revision=1,
        goal="推荐一台手机",
        domainState={"shoppingGuide": {"category": category}},
        createdAt=now,
        updatedAt=now,
    )


def test_explicit_domain_hint_has_highest_priority():
    decision = resolve_domain(
        "推荐一台手机",
        domain_hint="local_life",
        active_task_type="ecommerce_guide",
    )
    assert decision.domain_id == "local_life"
    assert decision.reason == "explicit_hint"


def test_active_ecommerce_task_keeps_keyword_free_followup_in_domain():
    decision = resolve_domain(
        "预算再低一点，重量也轻一点",
        active_task_type="ecommerce_guide",
    )
    assert decision.domain_id == "ecommerce"
    assert decision.reason == "active_task"


def test_clear_cross_domain_message_switches_from_active_task():
    decision = resolve_domain(
        "附近有什么咖啡店？",
        active_task_type="ecommerce_guide",
    )
    assert decision.domain_id == "local_life"
    assert decision.reason == "detected_switch"


def test_message_matching_both_domains_requires_clarification():
    decision = resolve_domain("附近哪里买降噪耳机？")
    assert decision.ambiguous is True
    assert decision.clarification_question


def test_find_used_phone_routes_to_ecommerce_without_explicit_hint():
    decision = resolve_domain("想找 iOS 二手机。")

    assert decision.domain_id == "ecommerce"
    assert decision.reason == "detected_message"
    assert decision.ambiguous is False


def test_switching_product_category_starts_an_isolated_task():
    decision = asyncio.run(
        classify_task_relation(
            "换成一台 5000 元以内的笔记本",
            _ecommerce_task("phone"),
            [],
        )
    )
    assert decision.relation == "start_new"
    assert decision.confidence == 1.0


def test_same_product_category_continues_without_task_manager_model_call():
    with patch("app.llm.get_client") as get_client:
        decision = asyncio.run(
            classify_task_relation(
                "推荐一台 iOS、优先原装屏的二手手机",
                _ecommerce_task("phone"),
                [],
            )
        )

    assert decision.relation == "continue_current"
    assert decision.confidence == 1.0
    get_client.assert_not_called()


def test_explicit_constraint_patch_continues_without_category_or_model_call():
    with patch("app.llm.get_client") as get_client:
        decision = asyncio.run(
            classify_task_relation(
                "预算放宽到 3000，其他要求不变",
                _ecommerce_task("phone"),
                [],
            )
        )

    assert decision.relation == "continue_current"
    assert decision.confidence == 1.0
    get_client.assert_not_called()


def test_unambiguous_cancel_uses_deterministic_task_relation_path():
    with patch("app.llm.get_client") as get_client:
        decision = asyncio.run(
            classify_task_relation("不买了", _ecommerce_task("phone"), [])
        )

    assert decision.relation == "cancel_current"
    assert decision.confidence == 1.0
    get_client.assert_not_called()
