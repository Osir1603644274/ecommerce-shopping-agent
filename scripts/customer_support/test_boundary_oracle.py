from boundary_oracle import grade_boundary

def test_unready_workspace_is_not_permission_proof():
    state={'customer_order':[{'order_no':'private-number','user_id':'owner'}]}
    case={'expected':{'routeOrOutcome':'authorization_denied'},'fixture':{'kind':'other_order'}}
    observed={'statusCode':409,'response':{'detail':'请先恢复购物空间'},'trace':None}
    assert 'authorization_denied' in grade_boundary(case,observed,state,state)['hardFailures']

def test_denial_with_private_record_is_failure():
    state={'customer_order':[{'order_no':'private-number','user_id':'owner'}]}
    case={'expected':{'routeOrOutcome':'authorization_denied'},'fixture':{'kind':'other_case'}}
    observed={'statusCode':404,'response':{'orderNo':'private-number'},'trace':None,'caseProbe':{'statusCode':404}}
    assert 'no_record_leak' in grade_boundary(case,observed,state,state)['hardFailures']

def test_question_cannot_also_offer_implicit_quantity():
    case={'expected':{'routeOrOutcome':'clarify:quantity'}}
    observed={'trace':{'status':'COMPLETED','result':{'kind':'clarify','answer':'数量多少？','preview':{'quantity':1}}}}
    assert 'no_draft' in grade_boundary(case,observed,{}, {})['reasons']
