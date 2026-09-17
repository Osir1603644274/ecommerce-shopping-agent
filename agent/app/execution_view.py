"""Allowlisted owner-facing execution data; not prompts or full tool payloads."""
import ast
import hashlib
from pathlib import Path
from .web_query_intake import redact_explicit_secrets

FIELDS = frozenset(('query', 'userQuery', 'category', 'limit', 'productIds', 'productId', 'itemId',
    'failureCode', 'failedBatchCount', 'failureReasons', 'httpStatus', 'retryable', 'attemptCount',
    'quantity', 'orderId', 'campaignId', 'status', 'count', 'code', 'decision', 'tool', 'toolOk',
    'plannedTools', 'taskRevision', 'brand', 'brands', 'budgetMin', 'budgetMax', 'minPrice', 'maxPrice',
    'currency', 'payableMinor', 'amountMinor', 'thresholdMinor', 'discountMinor', 'requirements',
    'hardConstraints', 'softPreferences', 'maxPriceMinor', 'minPriceMinor', 'purpose', 'intent',
    'transport', 'knowledgeVersion', 'bindingStatus', 'evidenceCount', 'items', 'size', 'userCouponId', 'itemType'))
TOOLS = {'search_products': 'search_products_tool', 'compare_products': 'compare_products_tool',
         'get_product_details': 'get_product_details_tool', 'rerank_products_in_scope': 'rerank_products_in_scope_tool'}

def safe_value(value, depth=0):
    if depth > 4:
        return '[深度已截断]'
    if isinstance(value, dict):
        return {k: safe_value(v, depth + 1) for k, v in value.items() if k in FIELDS and v is not None}
    if isinstance(value, (list, tuple)):
        return [safe_value(v, depth + 1) for v in value[:50]]
    if isinstance(value, str):
        return redact_explicit_secrets(value[:2000])[0]
    if isinstance(value, int) and abs(value) > 9007199254740991:
        return str(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return '[未公开此类型]'

def source_reference(tool):
    """Only fixed implementation files; never browser-controlled file paths."""
    bindings = {'react_decision': ('graph/nodes/react_policy.py', 'react_policy_node'),
                'pre_harness': ('llm.py', '_unified_pre_harness_safe_stop')}
    relative, name = bindings.get(tool, ('domains/ecommerce/tools.py', TOOLS.get(tool)))
    if not name:
        return None
    try:
        path = Path(__file__).parent / relative
        raw = path.read_bytes()
        text = raw.decode('utf-8-sig')
        node = next(n for n in ast.parse(text).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
        end = min(node.end_lineno, node.lineno + 35)
        return {'file': 'agent/app/' + relative, 'function': name, 'line': node.lineno,
            'endLine': end, 'sha256': hashlib.sha256(raw).hexdigest(),
            'snippet': '\n'.join(f'{i + 1}: {line}' for i, line in enumerate(text.splitlines()) if node.lineno <= i + 1 <= end),
            'scope': '记录生成时的实现源码参考；不是逐行执行覆盖率'}
    except (OSError, ValueError, StopIteration, SyntaxError):
        return None

def execution_key(row):
    return '|'.join(str(row.get(k, '')) for k in ('taskId', 'planId', 'stepId'))

def execution_nodes(task, run):
    """Project new immutable Executor receipts, never guessed parameters."""
    if not task or 'initialStepKeys' not in run:
        return []
    previous = set(run['initialStepKeys'])
    nodes = []
    for row in task.domain_state.get('stepExecutionResults', []):
        if not isinstance(row, dict) or execution_key(row) in previous:
            continue
        tool = row.get('toolName', '')
        if tool not in TOOLS:
            continue
        result = row.get('toolTrace') or {}
        detail = result.get('detail') or {}
        public_output = safe_value(detail)
        knowledge = detail.get('knowledge')
        if isinstance(knowledge, dict):
            public_output['knowledge'] = safe_value({**knowledge, 'evidenceCount': len(knowledge.get('evidence', []))})
        nodes.append({'id': execution_key(row), 'parentId': row.get('planId'), 'kind': 'tool', 'label': tool,
            'outcome': row.get('outcome'), 'startedAt': row.get('startedAt'), 'finishedAt': row.get('finishedAt'),
            'durationMs': row.get('durationMs'), 'input': safe_value(row.get('resolvedArguments') or {}),
            'output': public_output, 'source': source_reference(tool), 'error': row.get('errorType'),
            'cause': {'requestId': run['requestId'], 'planId': row.get('planId'), 'stepId': row.get('stepId'),
                      'description': 'Executor 执行已接受计划中的此步骤；参数取自实际执行回执。'},
            'detail': {'purpose': '输入输出仅展示业务白名单；未展示字段不代表未传入。'}})
    return nodes
