"""Browser-safe evidence projection. Never expose prompts, credentials or raw tool bodies."""
from urllib.parse import urlsplit


def source_url(url):
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {'http', 'https'} and bool(parsed.netloc) and not parsed.username and not parsed.password
    except (TypeError, ValueError):
        return False


def evidence_view(traces, cards):
    nodes = []
    for trace in traces:
        detail = trace.get('detail')
        detail = detail if isinstance(detail, dict) else {}
        node = dict(kind='tool', label=trace.get('tool', '工具'), outcome='passed' if trace.get('ok') else 'failed',
                    durationMs=trace.get('durationMs'), detail={})
        # Only allowlisted aggregates are exposed, not arbitrary tool/provider text.
        for key in ('count', 'query', 'userQuery', 'productIds', 'code'):
            if key in detail and detail[key] is not None:
                node['detail'][key] = detail[key]
        if trace.get('tool') == 'compare_products':
            node['detail']['purpose'] = '在已检索候选内核验条件并汇集型号证据；工具名不代表已有性能排名。'
            node['detail']['evidencePolicy'] = '当前手机目录启用知识库后，检索计划会追加此证据核验步骤；不是把“续航好”直接解释为两款对比。'
        knowledge = detail.get('knowledge')
        if isinstance(knowledge, dict):
            node['detail']['evidenceCount'] = len(knowledge.get('evidence', []))
            node['detail']['status'] = knowledge.get('status')
        nodes.append(node)
        knowledge = detail.get('knowledge')
        if not isinstance(knowledge, dict):
            continue
        mcp = knowledge.get('transport') == 'MCP_STREAMABLE_HTTP'
        nodes.append(dict(kind='tool', label='知识库 MCP · search_product_evidence',
                          outcome='passed' if mcp and trace.get('ok') else 'unavailable',
                          detail=dict(transport=knowledge.get('transport'), version=knowledge.get('knowledgeVersion'),
                                      evidenceCount=len(knowledge.get('evidence', [])),
                                      query=detail.get('userQuery', detail.get('query')), status=knowledge.get('status'))))
        bindings = {str(b.get('itemId')): b for b in knowledge.get('bindings', []) if b.get('status') == 'CLEAR'}
        for card in cards if trace.get('ok') else []:
            binding = bindings.get(str(card['id']), {})
            rows = []
            for e in knowledge.get('evidence', []) if mcp else []:
                source = e.get('source', {})
                url = source.get('url', '')
                if e.get('modelKey') not in binding.get('modelKeys', []):
                    continue
                if binding.get('region', 'UNKNOWN') != 'UNKNOWN' and source.get('region', 'UNKNOWN') not in {'UNKNOWN', 'TEST_SAMPLE', binding['region']}:
                    continue
                if not source_url(url):
                    continue
                rows.append(dict(id=e['evidenceId'], model=e['modelKey'], text=e['text'],
                                 field=e.get('field'), url=url, region=source.get('region'),
                                 conditions=source.get('testConditions'), software=source.get('softwareVersion'),
                                 section=source.get('section')))
            card['evidence'] = rows
            endurance = '续航' in str(detail.get('userQuery', detail.get('query', '')))
            card['evidenceNotice'] = (
                '本轮未找到该型号的续航实测；电池容量不能代替实测。'
                if endurance and not any(e.get('field') == 'battery_test' for e in rows)
                else '型号资料与实测条件供参考，不能证明这台二手机的实物机况。' if rows
                else '本轮未找到可引用的对应型号资料；未收录不等于不具备。')
    return nodes


def concise_answer(answer, cards):
    """Move only system-rendered evidence boilerplate to the card, not arbitrary prose."""
    if not any('evidence' in c for c in cards):
        return answer
    lines = []
    for line in answer.splitlines():
        if line.strip().startswith(('型号参考', '- 型号参考', '测试条件：', '模拟参考价：', '资料缺口（部分候选）', '商品卡片保留原顺序')):
            continue
        lines.append(line)
    return '\n\n'.join(line for line in lines if line.strip())
