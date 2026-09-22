import copy
from progress_oracle import grade_progress

def test_shipped_state_requires_independent_receipt():
    state={'customer_order':[{'id':'o'}],'fulfillment_task':[{'status':'SHIPPED','tracking_no':'T123'}],'support_order_receipt':[]}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'尚未签收 T123 没有承诺送达时间','citations':[{'kind':'logistics','fields':{'orderId':'o','status':'SHIPPED','trackingNo':'T123'}}]}}}
    result=grade_progress({'expected':{'routeOrOutcome':'logistics:original_order'}},observed,state,state)
    assert 'independent_logistics_receipt' in result['reasons']

def test_original_tracking_cannot_substitute_replacement():
    case={'id':'c','current_type':'EXCHANGE','phase':'REPLACEMENT_SHIPPED','item_id':1,'quantity':1,'amount_minor':101,'currency':'CNY','version':3}
    state={'customer_order':[{'id':'o'}],'support_case':[case],'support_receipt':[{'case_id':'c','status':'APPLIED','event_type':'REPLACEMENT_DISPATCH_CONFIRMED','payload_json':'{"trackingNo":"REPLACE123"}'}]}
    fields={'id':'c','type':'EXCHANGE','phase':'REPLACEMENT_SHIPPED','itemId':1,'quantity':1,'amountMinor':101,'currency':'CNY','version':3}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'尚未签收，申请数量 1 件 ORIGINAL123','citations':[{'kind':'after_sale','fields':{'orderId':'o','cases':[fields]}},{'kind':'replacement_logistics','fields':{'caseId':'c','trackingNo':'ORIGINAL123'}}]}}}
    result=grade_progress({'expected':{'routeOrOutcome':'logistics:replacement'}},observed,state,state)
    assert result['verdict']=='FAIL' and 'replacement_tracking' in result['reasons']
