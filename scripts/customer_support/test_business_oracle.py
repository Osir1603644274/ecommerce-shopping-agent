"""Adversarial mutations of recorded local acceptance observations."""
import copy
import json
from pathlib import Path
import pytest
from business_oracle import grade_preview, grade_final

BASE=Path('docs/implementation/customer-support-20260919')


def test_business_rejection_needs_real_preview_receipt_not_generic_error():
    case={'expected':{'routeOrOutcome':'blocked_preview:REFUND_ONLY'}}
    observed={'trace':{'result':{'answer':'服务端尚未受理这项请求：超过申请期限。'},
        'attempts':[{'toolReceipts':[{'method':'POST','path':'/api/after-sales/preview','status':'FAILED','statusCode':409}]}]}}
    assert grade_preview(case,observed,{}, {})['verdict']=='PASS'
    for code,path in ((500,'/api/after-sales/preview'),(403,'/api/after-sales/preview'),(404,'/api/orders/other')):
        changed=copy.deepcopy(observed)
        changed['trace']['attempts'][0]['toolReceipts'][0].update(statusCode=code,path=path)
        assert grade_preview(case,changed,{}, {})['verdict']=='FAIL'

def load_case(category,attempt):
    scenario=next(json.loads(line) for line in (BASE/'evaluation/dataset-draft-001/scenarios.jsonl').read_text(encoding='utf-8').splitlines() if json.loads(line)['id']==f'CS-{category}-01-1')
    folder=BASE/'evidence'/attempt/scenario['id']
    return scenario,{name:json.loads((folder/(name+'.json')).read_text(encoding='utf-8')) for name in ('before','final','http-and-trace','after-chat')}

def test_completed_case_without_exact_refund_fails():
    case,data=load_case('refund_only','eval-dev-refund-001')
    assert grade_final(case,data['before'],data['final'])['verdict']=='PASS'
    altered=copy.deepcopy(data['final']);altered['order_line_allocation'][0]['refunded_minor']=0
    assert 'exact_refund_amount' in grade_final(case,data['before'],altered)['hardFailures']

def test_preview_amount_cannot_be_self_certified():
    case,data=load_case('refund_only','eval-dev-refund-001')
    observed=copy.deepcopy(data['http-and-trace']);observed['trace']['result']['preview']['amountMinor']+=1
    assert 'preview_money' in grade_preview(case,observed,data['before'],data['after-chat'])['reasons']

def test_duplicate_restock_fails_even_when_stock_conserves():
    case,data=load_case('return_refund','eval-dev-return-001')
    altered=copy.deepcopy(data['final']);stock=altered['inventory_stock'][0]
    stock['available_quantity']+=1;stock['sold_quantity']-=1
    verdict=grade_final(case,data['before'],altered)
    assert 'stock_available_ledger' in verdict['hardFailures']
    assert 'stock_conservation' not in verdict['hardFailures']

def test_refund_without_independent_receipt_fails():
    case,data=load_case('refund_only','eval-dev-refund-001')
    altered=copy.deepcopy(data['final']);altered['support_receipt']=[]
    assert 'refund_independent_receipt' in grade_final(case,data['before'],altered)['hardFailures']

def test_second_product_stock_corruption_is_detected():
    before=json.loads((BASE/'evidence/two-items-fixture-001/SQL.json').read_text(encoding='utf-8'))
    scenario={'steps':[{'action':'query'}]}
    assert grade_final(scenario,before,before)['verdict']=='PASS'
    altered=copy.deepcopy(before)
    altered['inventory_stock'][1]['available_quantity']+=1
    altered['inventory_stock'][1]['sold_quantity']-=1
    assert 'stock_available_ledger' in grade_final(scenario,before,altered)['hardFailures']
    assert 'stock_conservation' not in grade_final(scenario,before,altered)['hardFailures']

def test_historical_case_at_end_does_not_replace_current_case():
    case,data=load_case('refund_only','eval-dev-refund-001')
    before=copy.deepcopy(data['before']);final=copy.deepcopy(data['final'])
    history=copy.deepcopy(final['support_case'][0]);history.update(id='historical-rejection',phase='REJECTED')
    before['support_case'].append(history);final['support_case'].append(history)
    assert grade_final(case,before,final)['verdict']=='PASS'
    final['support_case'][0]['phase']='REFUND_PENDING'
    assert 'expected_completion' in grade_final(case,before,final)['reasons']

def test_multi_product_requires_expected_target_not_model_selected_target():
    before=json.loads((BASE/'evidence/two-items-fixture-001/SQL.json').read_text(encoding='utf-8'))
    second=before['order_line_allocation'][1]
    scenario={'fixture':{'requestedQuantity':1},'expected':{'routeOrOutcome':'preview:REFUND_ONLY'}}
    observed={'trace':{'status':'COMPLETED','result':{'answer':'确认','preview':{'orderId':before['customer_order'][0]['id'],'itemId':second['item_id'],'quantity':1,'type':'REFUND_ONLY','amountMinor':213,'currency':'CNY'}}}}
    assert 'unambiguous_expected_item' in grade_preview(scenario,observed,before,before)['reasons']
    scenario['fixture']['targetItemId']=second['item_id']
    assert grade_preview(scenario,observed,before,before)['verdict']=='PASS'
    observed['trace']['result']['preview']['itemId']=before['order_line_allocation'][0]['item_id']
    assert 'preview_item' in grade_preview(scenario,observed,before,before)['reasons']
