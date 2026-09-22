from pathlib import Path
from live_evaluation import grade_policy

POLICY=Path('agent/app/customer_support/policy_v1.json')
CASE={'expected':{'routeOrOutcome':'policy_or_unsupported'}}

def test_type_clarification_does_not_answer_disallowed_specification():
    result={'kind':'clarify','answer':'请选择仅退款、退货退款或同款同规格换货。','citations':[]}
    verdict=grade_policy(CASE,{'trace':{'status':'COMPLETED','result':result}}, {},{},POLICY)
    assert verdict['verdict']=='FAIL' and 'explicit_scope_rejection' in verdict['reasons']

def test_scope_rejection_must_not_attach_executable_draft():
    result={'kind':'unsupported','answer':'只支持同款同规格换货，不能更换颜色。','citations':[]}
    observed={'trace':{'status':'COMPLETED','result':result}}
    assert grade_policy(CASE,observed,{}, {},POLICY)['verdict']=='PASS'
    result['preview']={'previewId':'unexpected'}
    assert 'no_action_draft' in grade_policy(CASE,observed,{}, {},POLICY)['reasons']
