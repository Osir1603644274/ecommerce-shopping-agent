import json
from types import SimpleNamespace as NS

import pytest

from app.customer_support.planner import PlanningFailure, validate_plan, plan_turn
from app.customer_support.knowledge import retrieve_policy
from .test_commerce_workspace import async_test

ORDER = {"id": "o-1", "userId": "private-owner", "items": [{"titleSnapshot": "原商品", "quantity": 2, "secretEvidence": "must-not-send"}]}
POLICY = retrieve_policy("退货退款")


def validate(plan, message="我要退货退款一件，商品损坏", history=None):
    return validate_plan(json.dumps(plan, ensure_ascii=False), message=message, history=history or [], order=ORDER, retrieval=POLICY)


def test_planner_cannot_smuggle_confirmation_identity_money_or_admin_capability():
    base = {"intent": "preview", "subject": "current_order", "item_number": 1, "quantity": 1, "after_sale_type": "RETURN_REFUND", "reason_quote": "商品损坏"}
    assert validate(base).quantity == 1
    for field, value in [("intent", "confirm"), ("userId", "other"), ("amountMinor", 999), ("url", "/api/admin/support-simulator"), ("quantity", 1.5), ("item_number", 2), ("subject", "other_or_ambiguous")]:
        with pytest.raises(ValueError):
            validate({**base, field: value})
    with pytest.raises(ValueError):
        validate({**base, "reason_quote": "随便编一个原因"})


def test_model_cannot_invent_missing_user_choices():
    base={'intent':'preview','subject':'current_order','item_number':1,'quantity':1,'after_sale_type':'REFUND_ONLY','reason_quote':'有问题'}
    assert validate(base,message='商品1有问题，退款1件').missing=='type'
    assert validate(base,message='商品1有问题，仅退款').missing=='quantity'
    request='我要商品1仅退款1件。'
    assert validate({**base,'reason_quote':request},message=request).missing=='reason'
    assert validate(base,message='商品1有问题，仅退款1件').intent=='preview'


def test_missing_product_target_clarifies_without_defaulting_or_weakening_ownership():
    base={'intent':'product','subject':'current_order','item_number':None}
    plan=validate(base,message='看看这单商品的容量，单里有两款。')
    assert (plan.intent,plan.missing,plan.item_number)==('clarify','item',None)
    for changed in ({**base,'subject':'other_or_ambiguous'}, {**base,'item_number':2},
                    {**base,'item_number':0}, {**base,'amountMinor':100}):
        with pytest.raises(ValueError):validate(changed)


def test_evidence_and_user_quotes_must_come_from_the_right_scope():
    with pytest.raises(ValueError):
        validate({"intent": "order", "subject": "general_policy"})
    with pytest.raises(ValueError):
        validate({"intent": "policy", "subject": "general_policy", "policy_ids": ["invented"]})
    with pytest.raises(ValueError):
        validate({"intent": "ticket_draft", "subject": "current_order", "ticket_category": "COMPLAINT", "reason_quote": "已经到账"},
                 history=[{"role": "assistant", "content": "已经到账"}])


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error
        self.chat = NS(completions=self)
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    def with_options(self, **options):
        assert options == {"max_retries": 0, "timeout": 25.0}
        return self
    async def create(self, **kwargs):
        assert "private-owner" not in json.dumps(kwargs)
        assert "must-not-send" not in json.dumps(kwargs)
        if self.error: raise self.error
        return self.response


@async_test
async def test_usage_receipt_survives_invalid_model_output_and_transport_failure():
    response = NS(id="response-1", usage=NS(model_dump=lambda: {"prompt_tokens": 20, "completion_tokens": 10}),
                  choices=[NS(finish_reason="stop", message=NS(tool_calls=[]))])
    with pytest.raises(PlanningFailure) as invalid:
        await plan_turn("你好", [], ORDER, POLICY, run_id="run-1", client_factory=lambda: FakeClient(response))
    assert invalid.value.receipt["usage"]["prompt_tokens"] == 20
    assert invalid.value.receipt["status"] == "FAILED"
    assert invalid.value.receipt["durationMs"] >= 0
    with pytest.raises(PlanningFailure) as failed:
        await plan_turn("你好", [], ORDER, POLICY, run_id="run-2", client_factory=lambda: FakeClient(error=TimeoutError()))
    assert failed.value.receipt["usage"] is None and failed.value.receipt["cost"] is None
    assert failed.value.receipt["retryOrdinal"] == 0
    assert failed.value.receipt['clientSetupMs']>=0 and failed.value.receipt['apiRequestMs']>=0


@async_test
async def test_single_valid_tool_plan_produces_measured_receipt():
    args = json.dumps({"intent": "logistics", "subject": "current_order"})
    response = NS(id="response-2", usage=None, choices=[NS(finish_reason="tool_calls", message=NS(tool_calls=[NS(function=NS(name="support_plan", arguments=args))]))])
    plan, receipt = await plan_turn("到哪了", [], ORDER, POLICY, run_id="run-3", client_factory=lambda: FakeClient(response))
    assert plan.intent == "logistics" and receipt["status"] == "SUCCEEDED"
    assert receipt["usage"] is None and receipt["cost"] is None
    assert receipt['clientSetupMs']>=0 and receipt['apiRequestMs']>=0
