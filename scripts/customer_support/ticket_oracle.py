"""Ticket drafts and persisted user confirmations, independently judged."""
from business_oracle import _result
from progress_oracle import grade_progress

def grade_ticket(scenario,observed,before,after):
    trace=observed.get('trace') or {};result=trace.get('result') or {};draft=result.get('ticketDraft') or {};order=before['customer_order'][0]
    message=scenario['steps'][0]['message'];summary=draft.get('summary');case_id=draft.get('caseId')
    checks=[('completed',trace.get('status')=='COMPLETED'),('no_implicit_write',before==after),
            ('draft_order',draft.get('orderId')==order['id']),('draft_category',draft.get('category')==scenario['expected']['routeOrOutcome'].split(':')[1]),
            ('quoted_reason',bool(summary) and summary in message),('explicit_confirmation',('确认' in result.get('answer','') or '点击提交' in result.get('answer','')) and '尚未登记' in result.get('answer','')),
            ('owned_case_association',case_id is None or any(c['id']==case_id and c['user_id']==order['user_id'] for c in before['support_case'])),
            ('no_other_draft',not result.get('preview') and not result.get('actionDraft'))]
    if scenario['expected'].get('replacementDispute'):
        rows=[c for c in before['support_case'] if c['id']==case_id and c['current_type']=='EXCHANGE' and c['phase']=='COMPLETED']
        facts=[c.get('fields',{}) for c in result.get('citations',[]) if c.get('kind')=='dispute_case']
        checks.append(('completed_exchange_linked',len(rows)==1))
        if len(rows)==1:
            row=rows[0]
            expected={'id':row['id'],'orderId':row['order_id'],'type':'EXCHANGE','phase':'COMPLETED','itemId':row['item_id'],'quantity':row['quantity'],'version':row['version']}
            checks.append(('completed_case_sql',facts==[expected]))
        rule='已完成换货的原商品数量不再用于自动重复申请。替换商品出现后续争议时，提交关联原订单和售后的工单核实，不能重新用原商品数量自动退款或再次补发。'
        citations=result.get('citations',[])
        checks.append(('repeat_exchange_rule',rule in result.get('answer','') and any(c.get('kind')=='policy' and c.get('text')==rule and c.get('id','').endswith(':replacement-dispute') for c in citations)))
    return _result(checks,[] if before==after else ['unconfirmed_business_write'])

def grade_ticket_final(observed,before,final):
    draft=observed['trace']['result']['ticketDraft'];old={r['id'] for r in before['support_ticket']}
    rows=[r for r in final['support_ticket'] if r['id'] not in old];order=before['customer_order'][0]
    checks=[('exactly_one_ticket',len(rows)==1),('business_state_unchanged',all(before[k]==final[k] for k in before if k not in {'support_ticket','support_ticket_event'}))]
    if len(rows)==1:
        row=rows[0];checks.append(('persisted_draft',row['order_id']==order['id'] and row['user_id']==order['user_id'] and row['case_id']==draft.get('caseId') and row['category']==draft['category'] and row['summary']==draft['summary'] and row['status']=='OPEN'))
        events=[r for r in final['support_ticket_event'] if r['ticket_id']==row['id']]
        checks.append(('user_creation_event',len(events)==1 and events[0]['action']=='CREATED' and events[0]['actor']==order['user_id'] and events[0]['message']==draft['summary']))
    return _result(checks,[k for k,v in checks if not v])


def grade_ticket_existing(scenario,observed,before,after):
    trace=observed.get('trace') or {};result=trace.get('result') or {};kind=scenario['expected']['routeOrOutcome'];order=before['customer_order'][0]
    checks=[('completed',trace.get('status')=='COMPLETED'),('read_only',before==after)]
    rows=before['support_ticket'];facts=[c.get('fields',{}) for c in result.get('citations',[]) if c.get('kind')=='tickets']
    fields=[{'id':t['id'],'orderId':t['order_id'],'caseId':t['case_id'],'category':t['category'],'status':t['status'],'version':t['version']} for t in rows]
    checks.append(('ticket_sql',len(facts)==1 and facts[0].get('orderId')==order['id'] and sorted(facts[0].get('tickets',[]),key=lambda t:t['id'])==sorted(fields,key=lambda t:t['id'])))
    answer=result.get('answer','');draft=result.get('ticketReplyDraft') or {}
    if kind=='ticket_reply':
        t=rows[0];body=draft.get('body') or {};message=body.get('message')
        checks += [('reply_identity',draft.get('orderId')==order['id'] and draft.get('ticketId')==t['id']),('reply_version',body.get('expectedVersion')==t['version']),
                   ('reply_quote',bool(message) and message in scenario['steps'][0]['message']),('reply_confirmation','确认' in answer and '尚未写入' in answer)]
    elif kind=='ticket_reply_blocked':checks += [('closed_no_draft',not draft),('closed_disclosed','已关闭' in answer and '不能追加' in answer)]
    else:
        terms={'OPEN':'待处理','WAITING_CUSTOMER':'等待你补充','RESOLVED':'已处理','CLOSED':'已关闭'}
        checks += [('ticket_status_answer',all(t['id'] in answer and terms[t['status']] in answer for t in rows)),('not_financial_receipt','不代表退款到账' in answer),('no_reply_draft',not draft)]
        progress=grade_progress({'expected':{'routeOrOutcome':'after_sale'}},observed,before,after)
        checks.append(('actual_aftersale_progress',progress['verdict']=='PASS'))
    return _result(checks,[] if before==after else ['unconfirmed_business_write'])


def grade_ticket_existing_final(scenario,observed,before,final):
    kind=scenario['expected']['routeOrOutcome'];checks=[('financial_state_unchanged',all(before[k]==final[k] for k in before if k not in {'support_ticket','support_ticket_event'}))]
    if kind=='ticket_reply_blocked':checks.append(('closed_unchanged',before==final))
    else:
        old=before['support_ticket'][0];new=final['support_ticket'][0];actions=[s['action'] for s in scenario['steps'][1:]]
        expected='OPEN' if 'reply_ticket' in actions else 'RESOLVED' if 'resolve_ticket' in actions else 'CLOSED'
        old_ids={e['id'] for e in before['support_ticket_event']};events=[e for e in final['support_ticket_event'] if e['id'] not in old_ids]
        checks += [('ticket_identity',old['id']==new['id']),('ticket_transition',new['status']==expected and new['version']==old['version']+1),('one_event',len(events)==1)]
        if kind=='ticket_reply' and len(events)==1:
            checks.append(('reply_user_content',events[0]['action']=='REPLY' and events[0]['actor']==old['user_id'] and events[0]['message']==observed['trace']['result']['ticketReplyDraft']['body']['message']))
    return _result(checks,[n for n,v in checks if not v])
