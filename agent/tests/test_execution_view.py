import json
from types import SimpleNamespace
from app.execution_view import safe_value, execution_nodes, source_reference
from app.agent_trace import TraceBuilder
from app import backend_observer as obs

def test_business_projection_excludes_nested_secrets():
    value = safe_value({'query': 'battery', 'password': 'private', 'requirements': {'brand': 'vivo', 'accessToken': 'private'},
                        'items': [{'itemId': 123, 'quantity': 2, 'authorization': 'private'}]})
    assert value['items'][0] == {'itemId': 123, 'quantity': 2}
    assert 'private' not in json.dumps(value)

def test_source_is_allowlisted_and_contains_exact_numbered_lines():
    import hashlib
    from pathlib import Path
    from app import execution_view
    assert source_reference('../../settings') is None
    source = source_reference('compare_products')
    assert source['function'] == 'compare_products_tool' and len(source['sha256']) == 64
    assert source['snippet'].startswith(str(source['line']) + ': async def compare_products_tool(')
    assert source['sha256'] == hashlib.sha256((Path(execution_view.__file__).parent / 'domains/ecommerce/tools.py').read_bytes()).hexdigest()

def test_unavailable_receipts_do_not_discard_phase_results(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app import task_state
    from app.api.commerce_controls import lifecycle_nodes
    monkeypatch.setattr(task_state, 'get_session_task_state', AsyncMock(side_effect=RuntimeError('private backend detail')))
    nodes = asyncio.run(lifecycle_nodes({}, {'engine': 'owner', 'nodes': [{'label': 'planner', 'outcome': 'passed'}]}))
    assert nodes[0]['outcome'] == 'passed' and nodes[1]['outcome'] == 'unavailable'
    assert 'private' not in json.dumps(nodes)

def test_execution_projection_excludes_previous_turn_and_keeps_real_receipts():
    old = {'taskId': 't', 'planId': 'p', 'stepId': 'old', 'toolName': 'compare_products'}
    new = {**old, 'stepId': 'new', 'resolvedArguments': {'productIds': [123], 'password': 'private'},
           'startedAt': '2026-09-10T01:00:00Z', 'finishedAt': '2026-09-10T01:00:01Z', 'durationMs': 1000,
           'outcome': 'tool_succeeded', 'toolTrace': {'detail': {'count': 1, 'prompt': 'private'}}}
    task = SimpleNamespace(domain_state={'stepExecutionResults': [old, new]})
    nodes = execution_nodes(task, {'initialStepKeys': ['t|p|old'], 'requestId': 'request-1'})
    assert len(nodes) == 1 and nodes[0]['startedAt'] == new['startedAt']
    assert nodes[0]['input'] == {'productIds': [123]} and nodes[0]['output'] == {'count': 1}
    assert nodes[0]['cause']['requestId'] == 'request-1'
    assert execution_nodes(task, {'requestId': 'legacy'}) == []
    assert 'private' not in json.dumps(nodes)

def test_nested_phase_times_and_unclosed_phase_are_recorded():
    builder = TraceBuilder('r')
    builder.start_phase('outer'); builder.start_phase('inner'); builder.end_phase('passed')
    phases = builder.finish().phases
    assert all(p.started_at and p.finished_at and p.started_at <= p.finished_at for p in phases)
    assert phases[1].outcome == 'unclosed'

def test_java_timeout_is_an_observation_not_proof_of_no_execution():
    collection = obs.Collection(); token = obs._current.set(collection)
    try:
        timing = obs.begin_call({'quantity': 2, 'password': 'private'})
        obs.observe_response(None, 'POST', '/api/orders', timing)
        call = collection.calls[0]
        assert call['status'] == 0 and call['traceId'] is None
        assert call['startedAt'] <= call['finishedAt'] and call['durationMs'] >= 0
        assert call['input']['body'] == {'quantity': 2}
    finally: obs._current.reset(token)
def test_browser_trace_preserves_long_identifiers_and_amounts():
    from app.backend_observer import browser_trace
    assert browser_trace({'productId': 6055970412849301893, 'quantity': 2, 'amountMinor': 500,
                          'productIds': [11, 6055970412849301893]}) == {
        'productId': '6055970412849301893', 'quantity': 2, 'amountMinor': 500,
        'productIds': ['11', '6055970412849301893']}
