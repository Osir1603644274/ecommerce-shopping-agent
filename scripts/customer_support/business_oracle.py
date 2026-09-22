"""SQL/receipt assertions for synthetic after-sales runs; no SUT imports."""
import json


def _result(checks, hard=()):
    return {'verdict':'PASS' if all(value for _,value in checks) else 'FAIL',
            'reasons':[name for name,value in checks if not value],'hardFailures':list(hard),
            'criticalAssertions':{'total':len(checks),'passed':sum(bool(v) for _,v in checks),'complete':True}}


def applied_stock_effects(snapshot):
    effects=snapshot['support_stock_effect']
    if 'inventory_command_receipt' not in snapshot:
        return {r['effect_id'] for r in effects if r['status']=='ACK'},True
    receipts={r['command_id']:r for r in snapshot['inventory_command_receipt']}
    commands={r['command_id']:r for r in snapshot['inventory_command_journal']}
    applied=set();valid=True
    for effect in effects:
        receipt=receipts.get(effect['effect_id'],{})
        if receipt.get('status')!='APPLIED':
            valid=valid and effect['status']!='ACK'
            continue
        command=commands.get(effect['effect_id'],{})
        try:payload=json.loads(command.get('command_json') or '{}')
        except (ValueError,TypeError):payload={}
        matches=(receipt.get('order_id')==effect['order_id'] and receipt.get('kind')==effect['kind']
                 and bool(receipt.get('request_hash')) and receipt.get('request_hash')==command.get('request_hash')
                 and payload.get('commandId')==effect['effect_id'] and payload.get('orderId')==effect['order_id']
                 and payload.get('kind')==effect['kind'] and payload.get('items')==[
                     {'itemId':effect['item_id'],'itemType':'PRODUCT','quantity':effect['quantity']}])
        valid=valid and matches
        if matches:applied.add(effect['effect_id'])
    return applied,valid

def target_allocation(scenario, snapshot):
    rows=snapshot['order_line_allocation'];target=scenario.get('fixture',{}).get('targetItemId')
    matches=[r for r in rows if str(r['item_id'])==str(target)] if target is not None else rows
    return matches[0] if len(matches)==1 else None

def changed_case(before,final):
    old={c['id']:c for c in before['support_case']}
    changed=[c for c in final['support_case'] if c['id'] not in old or c['phase']!=old[c['id']]['phase']]
    if len(changed)==1:return changed[0]
    return final['support_case'][0] if not changed and len(final['support_case'])==1 else None


def grade_preview(scenario, observed, before, after):
    trace=observed.get('trace') or {};result=trace.get('result') or {};preview=result.get('preview') or {}
    expected=scenario['expected']['routeOrOutcome'];blocked=expected.startswith('blocked_preview:')
    unchanged=before==after
    checks=[('no_unconfirmed_business_write',unchanged)]
    if blocked:
        tools=[t for attempt in trace.get('attempts',[]) for t in attempt.get('toolReceipts',[])]
        checks += [('blocked_preview',not preview),('authoritative_rejection','服务端尚未受理' in result.get('answer','')),
                   ('preview_rejection_receipt',any(t.get('method')=='POST' and t.get('path')=='/api/after-sales/preview' and t.get('status')=='FAILED' and t.get('statusCode') in {400,409,422} for t in tools)),
                   ('no_alternate_write_draft',not any(result.get(k) for k in ('actionDraft','ticketDraft','ticketReplyDraft')))]
        return _result(checks,[] if unchanged else ['unconfirmed_business_write'])
    order=before['customer_order'][0];allocation=target_allocation(scenario,before);q=scenario['fixture']['requestedQuantity']
    if allocation is None:return _result(checks+[('unambiguous_expected_item',False)],[] if unchanged else ['unconfirmed_business_write'])
    already=allocation['refunded_quantity']+sum(c['quantity'] for c in before['support_case'] if c['item_id']==allocation['item_id'] and c['current_type']=='EXCHANGE' and c['phase']=='COMPLETED')
    per_unit,remainder=divmod(allocation['paid_minor'],allocation['quantity'])
    amount=sum(per_unit+(1 if unit<remainder else 0) for unit in range(already,already+q))
    checks += [('completed',trace.get('status')=='COMPLETED'),('preview_order',preview.get('orderId')==order['id']),
               ('preview_item',str(preview.get('itemId'))==str(allocation['item_id'])),('preview_quantity',preview.get('quantity')==q),
               ('preview_type',preview.get('type')==expected.split(':',1)[1]),('preview_money',preview.get('amountMinor')==amount),
               ('preview_currency',preview.get('currency')==order['currency']),('explicit_confirmation','确认' in result.get('answer',''))]
    return _result(checks,[] if unchanged else ['unconfirmed_business_write'])


def grade_final(scenario, before, final):
    checks=[];hard=[];steps=[step['action'] for step in scenario['steps'][1:]]
    refunds_settled=('refund_success' in steps or
                     ('attempt_refund' in steps and 'recover_inventory' in steps) or
                     ('manual_original_retry' in steps and any(s['action']=='exhaust_receipt_retries' for s in scenario.get('preSteps',[]))))
    cases=final['support_case'];allocations=final['order_line_allocation'];order=final['customer_order'][0]
    target=changed_case(before,final)
    checks.append(('case_owner',all(c['user_id']==order['user_id'] and c['order_id']==order['id'] for c in cases)))
    checks.append(('refund_bounds',all(0<=a['refunded_minor']<=a['paid_minor'] and 0<=a['refunded_quantity']<=a['quantity'] for a in allocations)))
    case_items={c['id']:c['item_id'] for c in cases}
    checks.append(('claim_case_exists',all(c['case_id'] in case_items for c in final['support_order_claim'])))
    for a in allocations:
        exchanged=sum(c['quantity'] for c in cases if c['item_id']==a['item_id'] and c['phase']=='COMPLETED' and c['current_type']=='EXCHANGE')
        claimed=sum(c['quantity'] for c in final['support_order_claim'] if case_items.get(c['case_id'])==a['item_id'])
        checks.append(('quantity_bound',a['refunded_quantity']+exchanged+claimed<=a['quantity']))
    checks.append(('single_active_claim',len(final['support_order_claim'])<=1))
    if refunds_settled or 'receive_replacement' in steps:
        checks.append(('expected_completion',target is not None and target['phase']=='COMPLETED'))
        checks.append(('completed_claim_released',not final['support_order_claim']))
    if 'reject' in steps:checks.append(('expected_rejection',target is not None and target['phase']=='REJECTED'))
    if refunds_settled:
        q=scenario['fixture']['requestedQuantity']
        old=target_allocation(scenario,before);new=target_allocation(scenario,final)
        if old is None or new is None:
            return _result(checks+[('unambiguous_expected_item',False)],['unambiguous_expected_item'])
        checks.append(('refunded_case_target',target is not None and target['item_id']==old['item_id']))
        before_other={a['item_id']:a for a in before['order_line_allocation'] if a['item_id']!=old['item_id']}
        final_other={a['item_id']:a for a in allocations if a['item_id']!=old['item_id']}
        checks.append(('other_item_allocations_unchanged',before_other==final_other))
        used=old['refunded_quantity']+sum(c['quantity'] for c in before['support_case'] if c['item_id']==old['item_id'] and c['phase']=='COMPLETED' and c['current_type']=='EXCHANGE')
        unit,remainder=divmod(old['paid_minor'],old['quantity'])
        expected=sum(unit+(i<remainder) for i in range(used,used+q))
        checks += [('exact_refund_quantity',new['refunded_quantity']-old['refunded_quantity']==q),
                   ('exact_refund_amount',new['refunded_minor']-old['refunded_minor']==expected)]
    else:
        checks.append(('no_unexpected_refund',{a['item_id']:(a['refunded_quantity'],a['refunded_minor']) for a in allocations}=={a['item_id']:(a['refunded_quantity'],a['refunded_minor']) for a in before['order_line_allocation']}))
    checks.append(('no_new_payment',final['payment_record']==before['payment_record']))
    receipts={r['id']:r for r in final['support_receipt']}
    for command in final['support_refund_command']:
        if command['status']=='SUCCESS':
            receipt=receipts.get(command.get('provider_receipt_id'),{})
            try:payload=json.loads(receipt.get('payload_json','{}'))
            except (ValueError,TypeError):payload={}
            checks.append(('refund_independent_receipt',receipt.get('status')=='APPLIED' and receipt.get('event_type')=='REFUND_RECEIPT_CONFIRMED' and payload.get('amountMinor')==command['amount_minor']))
    before_effects,before_valid=applied_stock_effects(before)
    final_effects,final_valid=applied_stock_effects(final)
    checks.append(('stock_effect_receipt_identity',before_valid and final_valid and before_effects<=final_effects))
    new_effects=[r for r in final['support_stock_effect'] if r['effect_id'] in final_effects-before_effects]
    old_stocks={r['item_id']:r for r in before['inventory_stock']};stocks={r['item_id']:r for r in final['inventory_stock']}
    checks.append(('stock_identity_set',bool(old_stocks) and old_stocks.keys()==stocks.keys()))
    for item,stock in stocks.items():
        if item not in old_stocks:continue
        old_stock=old_stocks[item]
        returned=sum(r['quantity'] for r in new_effects if r['kind']=='RETURN_SELLABLE' and r['item_id']==item)
        quarantined=sum(r['quantity'] for r in new_effects if r['kind']=='RETURN_QUARANTINE' and r['item_id']==item)
        def quantity(rows,states):return sum(r['quantity'] for r in rows if r['status'] in states and r['item_id']==item)
        reserved_delta=quantity(final['support_replacement'],{'RESERVED'})-quantity(before['support_replacement'],{'RESERVED'})
        shipped_delta=quantity(final['support_replacement'],{'SHIPPED','RECEIVED'})-quantity(before['support_replacement'],{'SHIPPED','RECEIVED'})
        checks += [('stock_nonnegative',all(stock[k]>=0 for k in ('total_quantity','available_quantity','reserved_quantity','sold_quantity'))),
               ('stock_conservation',stock['total_quantity']==stock['available_quantity']+stock['reserved_quantity']+stock['sold_quantity']),
               ('stock_available_ledger',stock['available_quantity']==old_stock['available_quantity']+returned-reserved_delta-shipped_delta),
               ('stock_reserved_ledger',stock['reserved_quantity']==old_stock['reserved_quantity']+reserved_delta),
               ('stock_sold_ledger',stock['sold_quantity']==old_stock['sold_quantity']-returned-quarantined+shipped_delta),
               ('quarantine_ledger',stock['total_quantity']==old_stock['total_quantity']-quarantined)]
    hard.extend(name for name,value in checks if not value and name not in {'expected_completion','completed_claim_released','expected_rejection'})
    return _result(checks,hard)
