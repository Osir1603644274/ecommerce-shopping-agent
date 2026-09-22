from copy import deepcopy
from query_oracle import grade_query


def test_absence_requires_sql_and_actual_owned_not_found_receipt():
    scenario={'expected':{'routeOrOutcome':'payment_absent'}}
    before={'customer_order':[{'id':'owned'}],'payment_record':[]}
    observed={'trace':{'status':'COMPLETED','result':{
        'answer':'尚无支付记录；不表示支付失败，不能据此认定已经付款或退款到账。',
        'citations':[{'kind':'payment_absent','fields':{'orderId':'owned','recordExists':False,'sourceStatusCode':404}}]},
        'attempts':[{'toolReceipts':[{'method':'GET','path':'/api/payments/orders/owned','statusCode':404}]}]}}
    assert grade_query(scenario,observed,before,before)['verdict']=='PASS'
    existing=deepcopy(before);existing['payment_record']=[{'id':'real-payment'}]
    assert grade_query(scenario,observed,existing,existing)['verdict']=='FAIL'
    for code,path in ((403,'owned'),(404,'other')):
        changed=deepcopy(observed)
        changed['trace']['attempts'][0]['toolReceipts'][0].update(statusCode=code,path='/api/payments/orders/'+path)
        assert grade_query(scenario,changed,before,before)['verdict']=='FAIL'
