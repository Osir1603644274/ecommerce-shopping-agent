from action_oracle import grade_action

def state(phase='AWAITING_RETURN'):
    return {'support_case':[{'id':'c','order_id':'o','phase':phase,'version':4,'amount_minor':101,'currency':'CNY'}]}

def test_wrong_case_and_stale_version_are_rejected():
    before=state();observed={'trace':{'status':'COMPLETED','result':{'answer':'请确认','actionDraft':{'caseId':'other','orderId':'o','action':'cancel','body':{'expectedVersion':3}}}}}
    result=grade_action({'expected':{'routeOrOutcome':'action:cancel'}},observed,before,before)
    assert 'draft_identity' in result['reasons'] and 'draft_version' in result['reasons']

def test_cannot_offer_cancellation_after_return_dispatched():
    before=state('RETURN_IN_TRANSIT');observed={'trace':{'status':'COMPLETED','result':{'answer':'不允许，但确认即可','actionDraft':{'action':'cancel'}}}}
    result=grade_action({'expected':{'routeOrOutcome':'action:cancel'}},observed,before,before)
    assert 'forbidden_action_not_offered' in result['reasons']

def test_tracking_must_be_quoted_from_user():
    before=state();observed={'trace':{'status':'COMPLETED','result':{'answer':'请确认','actionDraft':{'caseId':'c','orderId':'o','action':'return_shipment','body':{'expectedVersion':4,'trackingNo':'INVENTED'}}}}}
    case={'expected':{'routeOrOutcome':'action:return_shipment'},'steps':[{'message':'寄回单号RET123'}]}
    assert 'quoted_tracking' in grade_action(case,observed,before,before)['reasons']
