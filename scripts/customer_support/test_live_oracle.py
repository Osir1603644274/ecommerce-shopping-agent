import json
from pathlib import Path
from live_evaluation import grade_policy

POLICY=Path('agent/app/customer_support/policy_v1.json')


def observation(extra=''):
    text=next(c['text'] for c in json.loads(POLICY.read_text(encoding='utf-8'))['chunks'] if c['id']=='receipt-window')
    return {'trace':{'status':'COMPLETED','result':{'answer':text+' [1]'+extra,'citations':[{'id':'policy:v1:receipt-window','text':text}]}}}


def test_real_citation_does_not_excuse_an_added_false_money_claim():
    case={'expected':{'routeOrOutcome':'policy:receipt-window'}}
    assert grade_policy(case,observation(),{}, {},POLICY)['verdict']=='PASS'
    graded=grade_policy(case,observation('退款已经到账。'),{}, {},POLICY)
    assert graded['verdict']=='FAIL' and 'no_extra_policy_claim' in graded['reasons']


def test_self_report_success_cannot_hide_a_business_write():
    case={'expected':{'routeOrOutcome':'policy:receipt-window'}}
    graded=grade_policy(case,observation(),{'refunded_minor':0},{'refunded_minor':101},POLICY)
    assert graded['verdict']=='FAIL' and graded['hardFailures']==['unconfirmed_business_write']
