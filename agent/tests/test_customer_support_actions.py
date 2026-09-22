import json
import pytest
from app.customer_support.planner import SupportPlan, validate_plan
from app.customer_support.runtime import execute_plan
from .test_commerce_workspace import async_test

CASE = {'id': 'case-1', 'orderId': 'order-1', 'itemId': 123, 'quantity': 1, 'type': 'EXCHANGE', 'phase': 'WAITING_CHOICE', 'version': 4}
ORDER = {'id': 'order-1', 'items': [{'itemId': 123, 'titleSnapshot': '原商品'}], '_supportCases': [CASE]}


def test_model_cannot_invent_tracking_or_select_unseen_case():
    plan = {'intent': 'case_action', 'subject': 'current_order', 'case_number': 1, 'case_action': 'return_shipment', 'tracking_quote': 'RETURN-001'}
    valid = validate_plan(json.dumps(plan), message='寄回单号RETURN-001', history=[], order=ORDER, retrieval={})
    assert valid.tracking_quote == 'RETURN-001'
    for changed in [{**plan, 'tracking_quote': 'FAKE-001'}, {**plan, 'case_number': 2}]:
        with pytest.raises(ValueError): validate_plan(json.dumps(changed), message='寄回单号RETURN-001', history=[], order=ORDER, retrieval={})


@async_test
async def test_waiting_stock_is_only_a_user_confirmable_draft():
    calls = []
    async def java(method, path, **kwargs):
        calls.append((method, path)); return CASE
    result = await execute_plan(SupportPlan(intent='case_action', subject='current_order', case_number=1, case_action='wait_stock'),
                                order=ORDER, retrieval={}, java=java, run_id='r1', tool_receipts=[])
    assert calls == [('GET', '/api/after-sales/case-1')]
    assert result['actionDraft']['body'] == {'expectedVersion': 4}
    assert result['actionDraft']['caseId'] == 'case-1'


@async_test
async def test_conversion_only_previews_and_binds_case_and_amount():
    calls = []
    async def java(method, path, **kwargs):
        calls.append((method, path))
        return CASE if method == 'GET' else {'previewId': 'p1', 'caseId': 'case-1', 'amountMinor': 101, 'currency': 'CNY', 'expiresAt': '2099-01-01T00:00:00Z'}
    result = await execute_plan(SupportPlan(intent='case_action', subject='current_order', case_number=1, case_action='conversion_preview'),
                                order=ORDER, retrieval={}, java=java, run_id='r1', tool_receipts=[])
    assert calls == [('GET', '/api/after-sales/case-1'), ('POST', '/api/after-sales/case-1/conversion-preview')]
    assert result['preview']['conversion'] is True and result['preview']['orderId'] == 'order-1'
    assert result['preview']['amountMinor'] == 101


@async_test
async def test_late_cancellation_does_not_create_an_action_draft():
    async def java(*args, **kwargs): return {**CASE, 'phase': 'REPLACEMENT_SHIPPED'}
    result = await execute_plan(SupportPlan(intent='case_action', subject='current_order', case_number=1, case_action='cancel'),
                                order=ORDER, retrieval={}, java=java, run_id='r1', tool_receipts=[])
    assert result['actionDraft'] is None and '不允许' in result['answer']
