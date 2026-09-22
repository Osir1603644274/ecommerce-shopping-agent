import json
import pytest
from audit_capture import capture_request_identity, capture_preview_state


class Response:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status
        self.content = json.dumps(body).encode()

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP rejection')


class Browser:
    def __init__(self, positive=200, authenticated=True):
        self.headers = {}
        self.positive = positive
        self.authenticated = authenticated
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if path.endswith('/me'):
            return Response({'authenticated': self.authenticated, 'username': 'actor',
                             'csrfToken': 'PRIVATE-CSRF', 'accessToken': 'PRIVATE-JWT',
                             'unexpectedPassword': 'PRIVATE-PASSWORD'})
        assert self.headers['X-CSRF-Token'] == 'PRIVATE-CSRF'
        if path.endswith('/target/conversation'):
            return Response({'detail': 'rejected'}, 403)
        return Response({'items': []}, self.positive)


def identity_sql(statement, params):
    assert statement.startswith('SELECT ') and '%s' in statement
    if 'user_account' in statement:
        assert params == ('actor',)
        return [{'id': 'actor-id', 'username': 'actor', 'unexpected': 'PRIVATE-SQL'}]
    if 'WHERE user_id=' in statement:
        assert params == ('actor-id',)
        return [{'id': 'own-control', 'user_id': 'actor-id'}]
    assert params == ('target',)
    return [{'id': 'target', 'user_id': 'different-owner'}]


def test_valid_session_positive_control_and_distinct_owner_are_recorded_without_secrets():
    browser = Browser()
    evidence = capture_request_identity(browser, identity_sql, 'target')
    assert evidence['subject']['id'] != evidence['target']['userId']
    assert evidence['authorityPositiveControl']['statusCode'] == 200
    assert browser.paths == ['/api/commerce-demo/me', '/api/commerce-demo/orders/page',
                             '/api/commerce-demo/workspace/support/orders/own-control/conversation',
                             '/api/commerce-demo/workspace/support/orders/target/conversation']
    assert evidence['ownershipPositiveControl']['statusCode'] == 200
    assert evidence['ownershipTargetControl']['statusCode'] == 403
    assert 'PRIVATE-' not in json.dumps(evidence)


def test_authentication_failure_cannot_be_classified_as_ownership_denial():
    with pytest.raises(ValueError, match='authenticated browser identity'):
        capture_request_identity(Browser(authenticated=False), identity_sql, 'target')


def test_authority_rejection_cannot_be_used_as_positive_control():
    with pytest.raises(RuntimeError, match='HTTP rejection'):
        capture_request_identity(Browser(positive=403), identity_sql, 'target')


def test_unknown_sql_subject_is_not_invented():
    with pytest.raises(ValueError, match='independently bound'):
        capture_request_identity(Browser(), lambda *args: [], 'target')


def test_both_preview_tables_are_read_with_bound_order_filter():
    calls = []
    def sql(statement, params):
        calls.append((statement, params))
        assert statement.startswith('SELECT ') and params == ('order-1',)
        return [{'id': 'preview-' + str(len(calls))}]
    result = capture_preview_state(sql, 'order-1')
    assert len(calls) == 2
    assert 'WHERE order_id=%s' in calls[0][0]
    assert 'JOIN support_case' in calls[1][0] and 'WHERE c.order_id=%s' in calls[1][0]
    assert result['support_preview'][0]['id'] != result['support_conversion_preview'][0]['id']
