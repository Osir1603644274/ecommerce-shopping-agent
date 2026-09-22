from copy import deepcopy
import json
import pytest
from audit_oracle import grade_audit, merge_followup_audits


def fixture():
    case = {'expected': {'routeOrOutcome': 'preview:REFUND_ONLY'}}
    before = {'customer_order': [{'id': 'o', 'user_id': 'u', 'currency': 'CNY'}], 'support_case': []}
    preview = {'previewId': 'p', 'orderId': 'o', 'itemId': 1, 'quantity': 2, 'type': 'REFUND_ONLY',
               'amountMinor': 101, 'currency': 'CNY', 'expiresAt': '2026-09-19T08:00:00Z'}
    stored = {'id': 'p', 'order_id': 'o', 'user_id': 'u', 'request_json': json.dumps(preview),
              'amount_minor': 101, 'expires_at': '2026-09-19 08:00:00', 'facts_hash': 'a' * 64, 'case_id': None}
    observed = {'identityEvidence': {'session': {'statusCode': 200, 'authenticated': True, 'username': 'alice'},
        'subject': {'id': 'u', 'username': 'alice'}, 'target': {'id': 'o', 'userId': 'u'},
        'ownershipPositiveControl': {'method': 'GET', 'path': '/api/commerce-demo/workspace/support/orders/o/conversation',
            'orderId': 'o', 'userId': 'u', 'statusCode': 200, 'responseSha256': 'c' * 64},
        'ownershipTargetControl': {'method': 'GET', 'path': '/api/commerce-demo/workspace/support/orders/o/conversation',
            'statusCode': 200, 'responseSha256': 'd' * 64},
        'authorityPositiveControl': {'path': '/api/commerce-demo/orders/page', 'statusCode': 200, 'responseSha256': 'b' * 64}},
        'previewEvidence': {'before': {'orderId': 'o', 'support_preview': [], 'support_conversion_preview': []},
                            'after': {'orderId': 'o', 'support_preview': [stored], 'support_conversion_preview': []}},
        'trace': {'result': {'preview': preview}}}
    return case, observed, before


def test_bound_persisted_preview_passes():
    assert grade_audit(*fixture())['verdict'] == 'PASS'


@pytest.mark.parametrize('field,value', [('user_id', 'stranger'), ('order_id', 'other'), ('amount_minor', 102),
                                         ('expires_at', '2026-09-19 09:00:00'), ('case_id', 'already-executed')])
def test_tampered_or_implicitly_executed_preview_fails(field, value):
    case, observed, before = fixture()
    observed['previewEvidence']['after']['support_preview'][0][field] = value
    assert grade_audit(case, observed, before)['verdict'] == 'FAIL'


def test_missing_sql_preview_is_not_proven_by_http_id():
    case, observed, before = fixture()
    observed['previewEvidence']['after']['support_preview'] = []
    assert 'preview_id_persisted' in grade_audit(case, observed, before)['reasons']


def test_403_without_positive_authentication_evidence_fails():
    case, observed, before = fixture()
    case['expected']['routeOrOutcome'] = 'authorization_denied'
    observed.update(statusCode=403, trace=None, identityEvidence={})
    observed['previewEvidence']['after']['support_preview'] = []
    assert grade_audit(case, observed, before)['verdict'] == 'FAIL'


def test_valid_stranger_session_and_owned_read_prove_denial_context():
    case, observed, before = fixture()
    case['expected']['routeOrOutcome'] = 'authorization_denied'
    observed['identityEvidence']['subject']['id'] = 'stranger'
    observed['identityEvidence']['ownershipPositiveControl'].update(orderId='own', userId='stranger',
        path='/api/commerce-demo/workspace/support/orders/own/conversation')
    observed['identityEvidence']['ownershipTargetControl']['statusCode'] = 403
    observed.update(statusCode=403, trace=None)
    observed['previewEvidence']['after']['support_preview'] = []
    assert 'same_method_owned_order_success' in grade_audit(case, observed, before)['reasons']
    observed['ownershipPostControl'] = {'method': 'POST', 'path': '/api/commerce-demo/workspace/support/orders/own/conversation',
        'orderId': 'own', 'userId': 'stranger', 'statusCode': 200, 'requestId': 'control',
        'trace': {'requestId': 'control', 'status': 'COMPLETED'}}
    assert grade_audit(case, observed, before)['verdict'] == 'PASS'
    observed['ownershipPostControl']['statusCode'] = 403
    assert 'same_method_owned_order_success' in grade_audit(case, observed, before)['reasons']


def test_conversion_binds_existing_case_version():
    case, observed, before = fixture()
    before['support_case'] = [{'id': 'c', 'order_id': 'o', 'version': 5}]
    row = observed['previewEvidence']['after']['support_preview'].pop()
    row.update(case_id='c', expected_version=5, confirmed_key=None)
    observed['previewEvidence']['after']['support_conversion_preview'] = [row]
    observed['trace']['result']['preview'].update(conversion=True, caseId='c')
    assert grade_audit(case, observed, before)['verdict'] == 'PASS'
    row['expected_version'] = 4
    assert 'conversion_persisted_version' in grade_audit(case, observed, before)['reasons']


def test_later_audit_failure_cannot_be_hidden_by_first_success():
    case, observed, before = fixture()
    first = grade_audit(case, observed, before)
    row = deepcopy(first)
    observed['previewEvidence']['after']['support_preview'][0]['user_id'] = 'stranger'
    second = grade_audit(case, observed, before)
    merge_followup_audits(row, [{'auditVerdict': first}, {'auditVerdict': second}])
    assert row['verdict'] == 'FAIL' and 'preview_persisted_subject' in row['reasons']
    assert row['criticalAssertions']['total'] == first['criticalAssertions']['total'] + second['criticalAssertions']['total']


def test_followup_missing_audit_cannot_silently_pass():
    row = grade_audit(*fixture())
    merge_followup_audits(row, [{'auditVerdict': deepcopy(row)}, {}])
    assert row['verdict'] == 'FAIL' and 'per_request_audit_present' in row['reasons']


def test_generic_authenticated_list_does_not_replace_same_route_control():
    case, observed, before = fixture()
    del observed['identityEvidence']['ownershipPositiveControl']
    assert 'same_route_owned_order_success' in grade_audit(case, observed, before)['reasons']
