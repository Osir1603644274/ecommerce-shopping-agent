"""Independent request-subject and persisted-preview checks; no SUT imports."""
from datetime import datetime, timezone
import json
import re

from business_oracle import _result


def merge_followup_audits(row, observations):
    """First audit is already included by the main grader; preserve all later failures."""
    for observed in observations[1:]:
        audit = observed.get('auditVerdict') or _result([('per_request_audit_present', False)])
        row.setdefault('reasons', []).extend(audit['reasons'])
        row.setdefault('hardFailures', []).extend(audit['hardFailures'])
        totals = row.setdefault('criticalAssertions', {'total': 0, 'passed': 0, 'complete': True})
        for key in ('total', 'passed'):
            totals[key] += audit['criticalAssertions'][key]
        totals['complete'] = totals['complete'] and audit['criticalAssertions']['complete']
        if audit['verdict'] != 'PASS':
            row['verdict'] = 'FAIL'


def identity_checks(scenario, observed, before):
    proof = observed.get('identityEvidence') or {}
    session, subject = proof.get('session', {}), proof.get('subject', {})
    target, control = proof.get('target', {}), proof.get('authorityPositiveControl', {})
    order = before['customer_order'][0]
    denied = scenario['expected']['routeOrOutcome'] == 'authorization_denied'
    own_control, target_control = proof.get('ownershipPositiveControl', {}), proof.get('ownershipTargetControl', {})
    prefix = '/api/commerce-demo/workspace/support/orders/'
    return [
        ('authenticated_session_evidence', session.get('statusCode') == 200 and session.get('authenticated') is True),
        ('session_subject_sql_binding', bool(subject.get('id')) and bool(subject.get('username')) and session.get('username') == subject.get('username')),
        ('authority_positive_control', control.get('path') == '/api/commerce-demo/orders/page' and control.get('statusCode') == 200 and bool(re.fullmatch('[0-9a-f]{64}', control.get('responseSha256') or ''))),
        ('target_owner_sql_binding', target.get('id') == order['id'] and target.get('userId') == order['user_id']),
        ('request_subject_relationship', bool(subject.get('id')) and ((subject['id'] != order['user_id']) if denied else (subject['id'] == order['user_id']))),
        ('same_route_owned_order_success', bool(own_control.get('orderId')) and own_control.get('userId') == subject.get('id')
            and own_control.get('method') == 'GET' and own_control.get('path') == prefix + str(own_control.get('orderId')) + '/conversation'
            and own_control.get('statusCode') == 200 and bool(re.fullmatch('[0-9a-f]{64}', own_control.get('responseSha256') or ''))),
        ('same_route_target_result', target_control.get('method') == 'GET' and target_control.get('path') == prefix + order['id'] + '/conversation'
            and target_control.get('statusCode') in ({403, 404} if denied else {200})
            and bool(re.fullmatch('[0-9a-f]{64}', target_control.get('responseSha256') or ''))),
    ]


def instant(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)
    except ValueError:
        return None


def grade_audit(scenario, observed, before):
    checks = identity_checks(scenario, observed, before)
    if scenario['expected']['routeOrOutcome'] == 'authorization_denied':
        control = observed.get('ownershipPostControl') or {}
        own = (observed.get('identityEvidence') or {}).get('ownershipPositiveControl') or {}
        turn = control.get('trace') or {}
        checks.append(('same_method_owned_order_success', control.get('method') == 'POST'
            and control.get('path') == own.get('path') and control.get('orderId') == own.get('orderId')
            and control.get('userId') == own.get('userId') and control.get('statusCode') == 200
            and bool(control.get('requestId')) and turn.get('requestId') == control.get('requestId')
            and turn.get('status') == 'COMPLETED'))
    snapshots = observed.get('previewEvidence') or {}
    prior, after = snapshots.get('before', {}), snapshots.get('after', {})
    order = before['customer_order'][0]
    tables = ('support_preview', 'support_conversion_preview')
    checks.append(('preview_snapshots_available', all(s.get('orderId') == order['id'] and all(isinstance(s.get(t), list) for t in tables) for s in (prior, after))))
    result = (observed.get('trace') or {}).get('result') or {}
    preview = result.get('preview') or {}
    prior_ids = {r['id'] for t in tables for r in prior.get(t, [])}
    added = {r['id'] for t in tables for r in after.get(t, [])} - prior_ids
    checks.append(('no_unreported_preview', added <= ({preview.get('previewId')} if preview else set())))
    if not preview:
        return _result(checks)
    conversion = preview.get('conversion') is True
    table = 'support_conversion_preview' if conversion else 'support_preview'
    rows = [r for r in after.get(table, []) if r.get('id') == preview.get('previewId')]
    checks.append(('preview_id_persisted', len(rows) == 1))
    if len(rows) != 1:
        return _result(checks)
    row = rows[0]
    subject = (observed.get('identityEvidence') or {}).get('subject', {})
    checks += [('preview_persisted_subject', row.get('user_id') == subject.get('id') == order['user_id']),
               ('preview_persisted_money', row.get('amount_minor') == preview.get('amountMinor') and preview.get('currency') == order['currency']),
               ('preview_persisted_expiry', instant(row.get('expires_at')) is not None and instant(row.get('expires_at')) == instant(preview.get('expiresAt')))]
    if conversion:
        cases = [c for c in before['support_case'] if c['id'] == row.get('case_id')]
        checks += [('conversion_persisted_case', len(cases) == 1 and row.get('case_id') == preview.get('caseId') and cases[0]['order_id'] == preview.get('orderId') == order['id']),
                   ('conversion_persisted_version', len(cases) == 1 and row.get('expected_version') == cases[0]['version'])]
        if row['id'] not in prior_ids:
            checks.append(('conversion_not_implicitly_confirmed', row.get('confirmed_key') is None))
    else:
        try:
            request = json.loads(row.get('request_json') or '{}')
        except (ValueError, TypeError):
            request = {}
        checks += [('preview_persisted_order', row.get('order_id') == preview.get('orderId') == order['id']),
                   ('preview_persisted_request', all(str(request.get(k)) == str(preview.get(k)) for k in ('orderId', 'itemId', 'quantity', 'type'))),
                   ('preview_facts_hash_recorded', bool(re.fullmatch('[0-9a-f]{64}', row.get('facts_hash') or '')))]
        if row['id'] not in prior_ids:
            checks.append(('preview_not_implicitly_confirmed', row.get('case_id') is None))
    return _result(checks)
