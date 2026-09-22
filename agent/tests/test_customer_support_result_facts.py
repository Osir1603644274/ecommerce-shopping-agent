import json
from app.customer_support.planner import SupportPlan
from app.customer_support.runtime import execute_plan
from .test_commerce_workspace import async_test


@async_test
async def test_completed_refund_reports_amount_but_pending_never_claims_paid():
    for phase in ('COMPLETED', 'REFUND_PENDING', 'REJECTED'):
        async def java(*args, **kwargs):
            return [{'id':'c1','orderId':'o1','phase':phase,'type':'REFUND_ONLY','quantity':1,'amountMinor':101,'currency':'CNY'}]
        result = await execute_plan(SupportPlan(intent='after_sale',subject='current_order'),order={'id':'o1'},retrieval={},java=java,run_id='r',tool_receipts=[])
        assert '1.01 CNY' in result['answer']
        assert ('已退款 1.01' in result['answer']) == (phase == 'COMPLETED')


@async_test
async def test_replacement_uses_applied_event_and_not_original_shipment():
    async def java(method,path,**kwargs):
        if path.endswith('/events'):
            return [{'id':'e1','type':'REPLACEMENT_RECEIPT_CONFIRMED','evidence':json.dumps({'payload':{'itemId':123,'quantity':1,'trackingNo':'REPLACEMENT-42'}})}]
        assert '/fulfillment' not in path
        return [{'id':'c1','orderId':'o1','phase':'COMPLETED','type':'EXCHANGE','quantity':1,'itemId':123}]
    result = await execute_plan(SupportPlan(intent='logistics',subject='current_order',logistics_scope='replacement'),order={'id':'o1'},retrieval={},java=java,run_id='r',tool_receipts=[])
    assert '补发已签收' in result['answer'] and 'REPLACEMENT-42' in result['answer']
    assert '已退款' not in result['answer']
    assert result['citations'][0]['fields']['eventId']=='e1'
