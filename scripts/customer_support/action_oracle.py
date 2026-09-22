"""User-confirmed after-sale changes, checked against pre-action SQL."""
from business_oracle import _result, grade_final

ALLOWED={'cancel':{'AWAITING_REVIEW','AWAITING_RETURN'},'return_shipment':{'AWAITING_RETURN'},'wait_stock':{'WAITING_CHOICE'},'conversion_preview':{'WAITING_CHOICE'}}

def grade_action(scenario,observed,before,after):
    trace=observed.get('trace') or {};result=trace.get('result') or {};action=scenario['expected']['routeOrOutcome'].split(':')[1]
    cases=before['support_case'];checks=[('completed',trace.get('status')=='COMPLETED'),('no_implicit_write',before==after),('one_target',len(cases)==1)]
    if len(cases)!=1:return _result(checks)
    case=cases[0];draft=result.get('actionDraft') or {};preview=result.get('preview') or {};answer=result.get('answer','')
    if case['phase'] not in ALLOWED[action]:
        checks += [('forbidden_action_not_offered',not draft and not preview),('stage_rejection','不允许' in answer)]
    elif action=='conversion_preview':
        checks += [('conversion_identity',preview.get('caseId')==case['id'] and preview.get('orderId')==case['order_id']),
                   ('conversion_money',preview.get('amountMinor')==case['amount_minor'] and preview.get('currency')==case['currency']),
                   ('conversion_marker',preview.get('conversion') is True),('explicit_confirmation','确认' in answer)]
    else:
        checks += [('draft_identity',draft.get('caseId')==case['id'] and draft.get('orderId')==case['order_id']),('draft_action',draft.get('action')==action),
                   ('draft_version',draft.get('body',{}).get('expectedVersion')==case['version']),('explicit_confirmation','确认' in answer)]
        if action=='return_shipment':
            tracking=draft.get('body',{}).get('trackingNo');checks.append(('quoted_tracking',bool(tracking) and tracking in scenario['steps'][0]['message']))
    return _result(checks,[] if before==after else ['unconfirmed_business_write'])

def grade_action_final(scenario,observed,before,final):
    verdict=grade_final(scenario,before,final);action=scenario['expected']['routeOrOutcome'].split(':')[1];checks=[]
    old=before['support_case'][0];case=next((c for c in final['support_case'] if c['id']==old['id']),{})
    if action=='cancel':checks += [('cancelled',case.get('phase')=='CANCELLED'),('claim_released',not final['support_order_claim'])]
    elif action=='return_shipment':
        rows=[r for r in final['support_return'] if r['case_id']==old['id']]
        checks.append(('return_tracking_persisted',len(rows)==1 and rows[0]['tracking_no']==observed['trace']['result']['actionDraft']['body']['trackingNo']))
        if 'refund_success' not in [s['action'] for s in scenario['steps'][1:]]:checks.append(('return_in_transit',case.get('phase')=='RETURN_IN_TRANSIT'))
    elif action=='wait_stock':checks.append(('wait_selected',case.get('phase')=='WAITING_STOCK'))
    elif action=='conversion_preview':checks.append(('converted_type',case.get('current_type')=='RETURN_REFUND'))
    extra=_result(checks);verdict['reasons']+=extra['reasons']
    if extra['verdict']=='FAIL':verdict['verdict']='FAIL'
    for k in ('total','passed'):verdict['criticalAssertions'][k]+=extra['criticalAssertions'][k]
    return verdict
