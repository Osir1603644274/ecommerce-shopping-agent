from product_oracle import grade_product

def test_catalog_price_cannot_replace_original_purchase_price():
    state={'customer_order':[{'id':'o','currency':'CNY'}],'order_item':[{'item_id':1,'unit_price_minor':101}],
           'product':[{'id':1,'snapshot_price_minor':999}]}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'原订单9.99 CNY','citations':[{'kind':'order_item_price','fields':{'orderId':'o','itemId':1,'unitPriceMinor':999,'currency':'CNY'}}]}}}
    result=grade_product({'expected':{'routeOrOutcome':'product:original_price'}},observed,state,state)
    assert result['verdict']=='FAIL' and 'price_source' in result['reasons'] and 'price_answer' in result['reasons']

def test_real_but_irrelevant_quote_is_not_an_answer():
    state={'customer_order':[{'id':'o','currency':'CNY'}],'order_item':[{'item_id':1}],
           'product':[{'id':1,'attribute_text':'颜色：黑色。充电接口：USB-C。'}]}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'颜色：黑色，不替代原订单规格','citations':[{'kind':'product','itemId':'1','text':'颜色：黑色'}]}}}
    case={'expected':{'routeOrOutcome':'product:attributes'},'steps':[{'message':'充电接口是什么？'}]}
    assert 'question_relevance' in grade_product(case,observed,state,state)['reasons']
