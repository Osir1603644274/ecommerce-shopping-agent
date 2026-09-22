from copy import deepcopy
from ticket_oracle import grade_ticket


def test_dispute_requires_completed_owned_case_and_prevents_repeat_preview():
    rule='已完成换货的原商品数量不再用于自动重复申请。替换商品出现后续争议时，提交关联原订单和售后的工单核实，不能重新用原商品数量自动退款或再次补发。'
    scenario={'steps':[{'message':'换来的货又坏了'}],
              'expected':{'routeOrOutcome':'ticket:AFTERSALE_DISPUTE','replacementDispute':True}}
    before={'customer_order':[{'id':'o','user_id':'u'}],
            'support_case':[{'id':'c','user_id':'u','order_id':'o','current_type':'EXCHANGE','phase':'COMPLETED','item_id':1,'quantity':1,'version':4}]}
    observed={'trace':{'status':'COMPLETED','result':{
        'answer':rule+'请确认后点击提交，尚未登记。',
        'ticketDraft':{'orderId':'o','caseId':'c','category':'AFTERSALE_DISPUTE','summary':'换来的货又坏了'},
        'citations':[{'kind':'dispute_case','fields':{'id':'c','orderId':'o','type':'EXCHANGE','phase':'COMPLETED','itemId':1,'quantity':1,'version':4}},
                     {'kind':'policy','id':'policy:v:replacement-dispute','text':rule}]}}}
    assert grade_ticket(scenario,observed,before,before)['verdict']=='PASS'
    for field,value in [('caseId',None),('caseId','foreign')]:
        changed=deepcopy(observed);changed['trace']['result']['ticketDraft'][field]=value
        assert grade_ticket(scenario,changed,before,before)['verdict']=='FAIL'
    changed=deepcopy(observed);changed['trace']['result']['preview']={'type':'EXCHANGE'}
    assert grade_ticket(scenario,changed,before,before)['verdict']=='FAIL'
    changed=deepcopy(observed);changed['trace']['result']['citations'][0]['fields']['quantity']=2
    assert grade_ticket(scenario,changed,before,before)['verdict']=='FAIL'
