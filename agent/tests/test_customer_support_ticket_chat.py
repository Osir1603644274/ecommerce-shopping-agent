import json
import pytest
from app.customer_support.planner import SupportPlan,validate_plan
from app.customer_support.runtime import execute_plan
from .test_commerce_workspace import async_test

@async_test
async def test_reply_reads_only_owned_order_and_never_posts():
    calls=[]
    async def java(method,path,**kwargs):
        calls.append((method,path));assert method=='GET'
        return [{'id':'foreign','orderId':'other','status':'OPEN','version':0},{'id':'t','orderId':'o','status':'WAITING_CUSTOMER','version':2}]
    result=await execute_plan(SupportPlan(intent='ticket_reply',subject='current_order',reply_quote='包装破损'),order={'id':'o'},retrieval={},java=java,run_id='r',tool_receipts=[])
    assert result['ticketReplyDraft']=={'orderId':'o','ticketId':'t','body':{'expectedVersion':2,'message':'包装破损'}}
    assert len(calls)==1

@async_test
async def test_closed_ticket_never_offers_reply():
    async def java(*args,**kwargs):return [{'id':'t','orderId':'o','status':'CLOSED','version':3}]
    result=await execute_plan(SupportPlan(intent='ticket_reply',subject='current_order',reply_quote='还有疑问'),order={'id':'o'},retrieval={},java=java,run_id='r',tool_receipts=[])
    assert 'ticketReplyDraft' not in result and '不能追加' in result['answer']


@async_test
async def test_closed_ticket_cites_explicit_closed_reply_policy():
    from app.customer_support.knowledge import retrieve_policy
    async def java(*args,**kwargs):return [{'id':'t','orderId':'o','status':'CLOSED','version':3}]
    retrieval=retrieve_policy('已关闭的工单还能追加回复吗')
    result=await execute_plan(SupportPlan(intent='ticket_reply',subject='current_order',reply_quote='还有疑问'),order={'id':'o'},retrieval=retrieval,java=java,run_id='r',tool_receipts=[])
    assert any(c.get('id','').endswith(':tickets') and '已关闭工单不能追加回复' in c.get('text','') for c in result['citations'])


@async_test
async def test_missing_reply_body_checks_closed_state_without_empty_draft():
    plan=validate_plan(json.dumps({'intent':'ticket_reply','subject':'current_order'}),
                       message='我想回复工单',history=[],order={'items':[]},retrieval={})
    for state,required in [('CLOSED','不能追加'),('WAITING_CUSTOMER','请填写')]:
        async def java(method,path,**kwargs):
            assert method=='GET'
            return [{'id':'t','orderId':'o','status':state,'version':3}]
        result=await execute_plan(plan,order={'id':'o'},retrieval={},java=java,run_id='r',tool_receipts=[])
        assert required in result['answer'] and not result.get('ticketReplyDraft')


def test_other_order_reference_produces_order_selection_guidance():
    plan=validate_plan(json.dumps({'intent':'unsupported','subject':'other_or_ambiguous'}),
                       message='关联别人的售后',history=[],order={'items':[]},retrieval={})
    assert (plan.intent,plan.missing)==('clarify','order')


def test_closed_ticket_supplement_is_always_reply_query():
    plan=validate_plan(json.dumps({'intent':'ticket_draft','subject':'current_order','ticket_category':'AFTERSALE_DISPUTE','reason_quote':'我仍然有疑问。'}),
                       message='向已关闭的工单补充：我仍然有疑问。',history=[],order={'items':[]},retrieval={})
    assert plan.intent == 'ticket_reply' and plan.reply_quote == '向已关闭的工单补充：我仍然有疑问。'


def test_other_person_ticket_request_is_always_order_clarification():
    plan=validate_plan(json.dumps({'intent':'unsupported','subject':'current_order'}),
                       message='把我这单的投诉挂到别人的售后记录上。',history=[],order={'items':[]},retrieval={})
    assert (plan.intent, plan.subject, plan.missing) == ('clarify','other_or_ambiguous','order')


@async_test
async def test_replacement_dispute_links_unique_case_and_quotes_policy_without_write():
    from app.customer_support.knowledge import retrieve_policy
    case={'id':'c','orderId':'o','type':'EXCHANGE','phase':'COMPLETED','itemId':1,'quantity':1,'version':4}
    async def java(*args,**kwargs):raise AssertionError('ticket draft must not perform writes')
    plan=SupportPlan(intent='ticket_draft',subject='current_order',ticket_category='AFTERSALE_DISPUTE',reason_quote='换来的货又坏了')
    result=await execute_plan(plan,order={'id':'o','_supportCases':[case]},retrieval=retrieve_policy('换过 补发 又坏 二次'),java=java,run_id='r',tool_receipts=[])
    assert result['ticketDraft']['caseId']=='c'
    assert '不能重新用原商品数量自动退款或再次补发' in result['answer']
    assert {c['kind'] for c in result['citations']}=={'policy','dispute_case'}
    ambiguous=await execute_plan(plan,order={'id':'o','_supportCases':[case,{**case,'id':'other'}]},retrieval={},java=java,run_id='r',tool_receipts=[])
    assert ambiguous['ticketDraft']['caseId'] is None

def test_reply_cannot_quote_assistant_or_old_message():
    with pytest.raises(ValueError):validate_plan(json.dumps({'intent':'ticket_reply','subject':'current_order','reply_quote':'旧内容'}),message='新内容',history=[{'role':'user','content':'旧内容'}],order={'items':[]},retrieval={})

@async_test
async def test_resolved_ticket_reports_still_pending_refund():
    async def java(method,path,**kwargs):
        assert method=='GET'
        if path.startswith('/api/support/tickets'):return [{'id':'t','orderId':'o','status':'RESOLVED','version':2}]
        return [{'id':'c','orderId':'o','type':'REFUND_ONLY','phase':'REFUND_PENDING','quantity':1,'amountMinor':101,'currency':'CNY'}]
    result=await execute_plan(SupportPlan(intent='ticket_status',subject='current_order'),order={'id':'o'},retrieval={},java=java,run_id='r',tool_receipts=[])
    assert '已处理' in result['answer'] and '尚未确认到账' in result['answer'] and '1.01 CNY' in result['answer']
    assert {c['kind'] for c in result['citations']}=={'tickets','after_sale'}
