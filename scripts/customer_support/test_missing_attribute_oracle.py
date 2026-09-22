from copy import deepcopy
from product_oracle import grade_product


def test_missing_waterproof_evidence_is_not_invented_or_silently_ignored():
    case={'expected':{'routeOrOutcome':'product:missing_attributes'}}
    before={'order_item':[{'item_id':1}],'product':[{'id':1,'attribute_text':'颜色：黑色。存储容量：256GB。'}],'customer_order':[{}]}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'当前商品资料没有检索到支持这个问题的证据，不能凭常识猜测配置、赠品或兼容性。可提交信息核实工单。','citations':[]},
        'attempts':[{'toolReceipts':[{'method':'GET','path':'/api/products/1','status':'SUCCEEDED'}]}]}}
    assert grade_product(case,observed,before,before)['verdict']=='PASS'
    for evidence in ('防水等级IP68','Waterproof IP67','water resistant'):
        known=deepcopy(before);known['product'][0]['attribute_text']=evidence
        assert grade_product(case,observed,known,known)['verdict']=='FAIL'
    claimed=deepcopy(observed);claimed['trace']['result']['answer']+='支持IP68。'
    assert grade_product(case,claimed,before,before)['verdict']=='FAIL'
    unread=deepcopy(observed);unread['trace']['attempts']=[]
    assert grade_product(case,unread,before,before)['verdict']=='FAIL'
