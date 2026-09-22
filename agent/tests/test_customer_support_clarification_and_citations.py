import asyncio
import hashlib
import json
import pytest
from copy import deepcopy
from app.customer_support import planner, runtime


@pytest.mark.parametrize('intent', ['payment', 'after_sale'])
def test_payment_and_refund_question_reads_both_authoritative_records(intent):
    calls=[]
    async def java(method,path,**kwargs):
        calls.append((method,path)); assert method=='GET'
        if '/payments/' in path:
            return {'id':'pay','orderId':'owned','status':'SUCCESS','amountMinor':303,'currency':'CNY','version':1}
        return [{'id':'case','orderId':'owned','phase':'REFUND_PENDING','type':'REFUND_ONLY',
                 'quantity':1,'amountMinor':101,'currency':'CNY'}]
    result=asyncio.run(runtime.execute_plan(planner.SupportPlan(intent=intent,subject='current_order'),
        order={'id':'owned'},retrieval={},java=java,run_id='test',tool_receipts=[],question='原支付成功说明退款到账了吗？'))
    assert len(calls)==2
    assert '3.03 CNY' in result['answer'] and '1.01 CNY' in result['answer']
    assert '不代表售后退款已经到账' in result['answer'] and '尚未确认到账' in result['answer']
    assert {c['kind'] for c in result['citations']}=={'payment','after_sale'}


def test_pending_receipt_is_reported_without_applying_and_foreign_receipt_is_rejected():
    stored = {'id': 'receipt', 'caseId': 'case', 'eventType': 'REPLACEMENT_DISPATCH_CONFIRMED', 'status': 'PENDING'}
    calls = []
    async def java(method, path, **kwargs):
        calls.append((method, path))
        assert method == 'GET'
        if path.endswith('/receipts'):
            return [deepcopy(stored)]
        return [{'id': 'case', 'orderId': 'owned', 'type': 'EXCHANGE', 'phase': 'REPLACEMENT_READY', 'quantity': 1}]
    parsed = planner.SupportPlan(intent='after_sale', subject='current_order')
    def run():
        return asyncio.run(runtime.execute_plan(parsed, order={'id': 'owned'}, retrieval={}, java=java,
            run_id='test', tool_receipts=[], question='补发回执没同步，查进度'))
    result = run()
    assert '已记录补发出库回执，待同步至售后状态' in result['answer']
    assert result['citations'][0]['fields']['receipts'][0]['status'] == 'PENDING'
    assert not result['preview'] and not result['actionDraft']
    stored['caseId'] = 'other'
    with pytest.raises(ValueError, match='receipt identity'):
        run()


def plan(tracking=''):
    return dict(intent='case_action', subject='current_order', case_number=1,
                case_action='return_shipment', tracking_quote=tracking)


def validate(arguments, message):
    return planner.validate_plan(json.dumps(arguments), message=message, history=[],
                                 order={'id': 'owned', '_supportCases': [{'id': 'case'}]}, retrieval={})


def test_missing_tracking_becomes_explicit_question_without_tools_or_draft():
    parsed = validate(plan(), '退货寄出了，帮我登记单号。')
    assert parsed.intent == 'clarify' and parsed.missing == 'tracking'
    async def forbidden(*args, **kwargs):
        raise AssertionError('clarification must not call business tools')
    result = asyncio.run(runtime.execute_plan(parsed, order={'id': 'owned'}, retrieval={},
                         java=forbidden, run_id='test', tool_receipts=[]))
    assert '运单号' in result['answer'] and result['actionDraft'] is None


def test_user_supplied_tracking_keeps_action():
    parsed = validate(plan('SF123456'), '寄回单号SF123456')
    assert parsed.intent == 'case_action' and parsed.tracking_quote == 'SF123456'


@pytest.mark.parametrize('message', ['帮我登记单号', '别人的单号不能用于本单'])
def test_invented_tracking_remains_rejected(message):
    with pytest.raises(ValueError, match='tracking number must be quoted'):
        validate(plan('FAKE123'), message)


def test_policy_number_indexes_the_policy_after_dispute_fact_is_added():
    text = '替换商品后续争议应关联原售后核实。'
    citation = {'id': 'policy:v1:replacement-dispute', 'text': text,
                'contentSha256': hashlib.sha256(text.encode()).hexdigest(), 'kind': 'policy'}
    target = {'id': 'case', 'orderId': 'owned', 'type': 'EXCHANGE', 'phase': 'COMPLETED',
              'itemId': 1, 'quantity': 1, 'version': 3}
    parsed = planner.SupportPlan(intent='ticket_draft', subject='current_order',
                                  ticket_category='AFTERSALE_DISPUTE', reason_quote='换货后仍有问题')
    async def forbidden(*args, **kwargs):
        raise AssertionError('a ticket draft must not execute business operations')
    result = asyncio.run(runtime.execute_plan(parsed, order={'id': 'owned', '_supportCases': [target]},
                         retrieval={'status': 'FOUND', 'citations': [citation]},
                         java=forbidden, run_id='test', tool_receipts=[]))
    assert text + ' [1]' in result['answer']
    assert result['citations'][0]['id'] == citation['id']
    assert result['citations'][1]['kind'] == 'dispute_case'
    assert result['ticketDraft']['caseId'] == 'case'


