from evaluation.shopping_task_state_context_ab_v2_live_scorer import (
    _action,
    _normalized_scope,
    _percentile,
    _semantic_state,
)


def test_normalized_scope_ignores_run_identity_but_preserves_safety_content() -> None:
    left = {
        "taskState": {"domainState": {"candidateScope": {
            "scopeId": "a", "taskId": "ta", "sourceRevision": 1,
            "sourcePlanId": "pa", "sourceStepId": "sa", "createdAt": "x",
            "category": "phone", "rankedItemIds": [3, 2, 1], "status": "active",
        }}}
    }
    right = {
        "taskState": {"domainState": {"candidateScope": {
            "scopeId": "b", "taskId": "tb", "sourceRevision": 9,
            "sourcePlanId": "pb", "sourceStepId": "sb", "createdAt": "y",
            "category": "phone", "rankedItemIds": [3, 2, 1], "status": "active",
        }}}
    }
    assert _normalized_scope(left) == _normalized_scope(right)
    right["taskState"]["domainState"]["candidateScope"]["rankedItemIds"] = [2, 3, 1]
    assert _normalized_scope(left) != _normalized_scope(right)


def test_semantic_state_and_action_are_identity_free() -> None:
    row = {
        "selectedAction": {"kind": "CALL_TOOL", "toolName": "search_products"},
        "taskState": {
            "taskId": "ignored",
            "goal": "g",
            "status": "ready",
            "unknowns": [],
            "pendingQuestions": [],
            "domainState": {"shoppingGuide": {
                "mode": "recommend", "category": "phone", "requirements": [],
                "comparedIds": [], "evidenceStatus": "missing",
            }},
        },
    }
    assert _action(row) == ("CALL_TOOL", "search_products")
    assert _semantic_state(row)["goal"] == "g"
    assert "taskId" not in _semantic_state(row)


def test_nearest_rank_percentiles_match_runner_contract() -> None:
    values = [1.0, 2.0, 3.0, 4.0]
    assert _percentile(values, 0.50) == 2.0
    assert _percentile(values, 0.95) == 4.0
