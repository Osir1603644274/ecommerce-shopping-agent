"""Read-only request identity and persisted-preview evidence capture."""
from datetime import datetime, timezone
import hashlib


def now():
    return datetime.now(timezone.utc).isoformat()


def capture_request_identity(browser, sql, order_id):
    started = now()
    response = browser.get('/api/commerce-demo/me')
    response.raise_for_status()
    identity = response.json()
    if identity.get('authenticated') is not True or not isinstance(identity.get('username'), str):
        raise ValueError('authenticated browser identity required')
    csrf = identity.get('csrfToken')
    if not isinstance(csrf, str) or not csrf:
        raise ValueError('rotated CSRF token required')
    # /me rotates CSRF; update this same client before the positive control/chat.
    # Never include tokens, cookies, headers or the raw /me response in evidence.
    browser.headers['X-CSRF-Token'] = csrf
    subjects = sql('SELECT id,username FROM user_account WHERE username=%s', (identity['username'],))
    if len(subjects) != 1 or subjects[0]['username'] != identity['username']:
        raise ValueError('session subject cannot be independently bound to SQL')
    target = sql('SELECT id,user_id FROM customer_order WHERE id=%s', (order_id,))
    if len(target) != 1 or target[0]['id'] != order_id:
        raise ValueError('target order unavailable')
    positive = browser.get('/api/commerce-demo/orders/page')
    positive.raise_for_status()
    if positive.status_code != 200:
        raise ValueError('authenticated authority read did not return 200')
    owned = target if target[0]['user_id'] == subjects[0]['id'] else sql(
        'SELECT id,user_id FROM customer_order WHERE user_id=%s ORDER BY created_at DESC,id LIMIT 1', (subjects[0]['id'],))
    if len(owned) != 1 or owned[0]['user_id'] != subjects[0]['id']:
        raise ValueError('an owned order is required for the same-route positive control')
    prefix = '/api/commerce-demo/workspace/support/orders/'
    own_path = prefix + owned[0]['id'] + '/conversation'
    own_response = browser.get(own_path)
    own_response.raise_for_status()
    if own_response.status_code != 200:
        raise ValueError('same-route owned-order control failed')
    target_path = prefix + order_id + '/conversation'
    target_response = own_response if owned[0]['id'] == order_id else browser.get(target_path)
    return {'startedAt': started, 'completedAt': now(),
            'session': {'statusCode': response.status_code, 'authenticated': True,
                        'username': identity['username']},
            'subject': {'id': subjects[0]['id'], 'username': subjects[0]['username']},
            'target': {'id': target[0]['id'], 'userId': target[0]['user_id']},
            'ownershipPositiveControl': {'method': 'GET', 'path': own_path, 'statusCode': own_response.status_code,
                'orderId': owned[0]['id'], 'userId': owned[0]['user_id'],
                'responseSha256': hashlib.sha256(own_response.content).hexdigest()},
            'ownershipTargetControl': {'method': 'GET', 'path': target_path, 'statusCode': target_response.status_code,
                'responseSha256': hashlib.sha256(target_response.content).hexdigest()},
            'authorityPositiveControl': {'path': '/api/commerce-demo/orders/page',
                'statusCode': 200, 'responseSha256': hashlib.sha256(positive.content).hexdigest()}}


def capture_preview_state(sql, order_id):
    # Drafts are separate from money/inventory snapshots: a legitimate preview
    # creates a draft but must not count as an executed refund or stock change.
    regular = sql('SELECT id,order_id,user_id,request_json,facts_hash,amount_minor,expires_at,case_id '
                  'FROM support_preview WHERE order_id=%s ORDER BY id', (order_id,))
    conversion = sql('SELECT p.id,p.case_id,p.user_id,p.expected_version,p.amount_minor,p.expires_at,p.confirmed_key '
                     'FROM support_conversion_preview p JOIN support_case c ON c.id=p.case_id '
                     'WHERE c.order_id=%s ORDER BY p.id', (order_id,))
    return {'observedAt': now(), 'orderId': order_id, 'support_preview': regular,
            'support_conversion_preview': conversion}
