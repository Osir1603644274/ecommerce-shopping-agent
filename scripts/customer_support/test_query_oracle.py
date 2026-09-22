from query_oracle import grade_query

def test_payment_success_does_not_answer_order_state():
    before={'customer_order':[{'id':'o','status':'PAID','payable_minor':101,'currency':'CNY'}]}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'支付成功 1.01 CNY','citations':[{'kind':'payment','fields':{'id':'p','status':'SUCCESS','amountMinor':101,'currency':'CNY'}}]}}}
    result=grade_query({'expected':{'routeOrOutcome':'order'}},observed,before,before)
    assert result['verdict']=='FAIL' and 'sql_facts_match' in result['reasons'] and 'answer_status' in result['reasons']

def test_matching_fields_cannot_hide_wrong_rendered_amount():
    before={'customer_order':[{'id':'o','status':'PAID','payable_minor':101,'currency':'CNY'}]}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'已支付 9.99 CNY','citations':[{'kind':'order','fields':{'id':'o','status':'PAID','payableMinor':101,'currency':'CNY'}}]}}}
    assert 'answer_money' in grade_query({'expected':{'routeOrOutcome':'order'}},observed,before,before)['reasons']
