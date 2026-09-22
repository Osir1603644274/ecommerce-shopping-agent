from ticket_oracle import grade_ticket,grade_ticket_final,grade_ticket_existing_final

def test_ticket_cannot_be_created_during_draft():
    before={'customer_order':[{'id':'o','user_id':'u'}],'support_case':[],'support_ticket':[]}
    after={**before,'support_ticket':[{'id':'t'}]}
    case={'expected':{'routeOrOutcome':'ticket:COMPLAINT'},'steps':[{'message':'服务差'}]}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'确认','ticketDraft':{'orderId':'o','category':'COMPLAINT','summary':'服务差'}}}}
    assert 'unconfirmed_business_write' in grade_ticket(case,observed,before,after)['hardFailures']

def test_ticket_confirmation_cannot_settle_refund():
    before={'customer_order':[{'id':'o','user_id':'u'}],'support_ticket':[],'support_ticket_event':[],'refund':[{'amount':0}]}
    final={**before,'refund':[{'amount':101}],'support_ticket':[{'id':'t','order_id':'o','user_id':'u','case_id':None,'category':'COMPLAINT','summary':'差','status':'OPEN'}],
           'support_ticket_event':[{'ticket_id':'t','actor':'u','action':'CREATED','message':'差'}]}
    observed={'trace':{'result':{'ticketDraft':{'category':'COMPLAINT','summary':'差'}}}}
    assert grade_ticket_final(observed,before,final)['hardFailures']==['business_state_unchanged']

def test_closed_reply_cannot_change_any_state():
    before={'support_ticket':[{'id':'t','status':'CLOSED'}],'support_ticket_event':[],'stock':10}
    after={**before,'stock':9}
    result=grade_ticket_existing_final({'expected':{'routeOrOutcome':'ticket_reply_blocked'}}, {},before,after)
    assert set(result['hardFailures'])=={'financial_state_unchanged','closed_unchanged'}
