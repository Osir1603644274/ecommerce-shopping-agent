"""Cross-check actual browser HTTP/SSE and runtime evidence, not answer quality."""
import json
from .common import HERE, file_sha, json_new, now, rows, sha
out=HERE/'p6/browser001';http=rows(out/'http.jsonl');turns=rows(out/'turns.jsonl');traces=rows(out/'traces.jsonl')
protocol=json.loads((out/'protocol.json').read_text(encoding='utf-8'))
requests=[json.loads(r['request']) for r in http]
events=[[json.loads(x[6:]) for x in r['response'].splitlines() if x.startswith('data: ')] for r in http]
completed=[[x['data'] for x in ev if x.get('type')=='complete'] for ev in events]
assert len(http)==4 and all(len(x)==1 for x in completed)
data=[x[0] for x in completed];states=[x['taskState'] for x in data]
guides=[s['domainState']['shoppingGuide'] for s in states]
second=data[0]['guideResult']['products'][1]['product']['id'];third=data[0]['guideResult']['products'][2]['product']['id']
starts=[r for r in rows(HERE/'provider_ledger.jsonl') if r['event']=='START' and r.get('binding',{}).get('attempt')=='browser001']
checks={
    'fourRegisteredMessages':[r['message'] for r in requests]==protocol['scriptedMessages'],
    'allHTTP200':all(r['status']==200 for r in http),
    'noSSEError':all(not any(e.get('type')=='error' for e in ev) for ev in events),
    'oneTaskOneSession':len({s['taskId'] for s in states})==len({s['sessionId'] for s in states})==1,
    'preservedHardRequirements':all(g['requirements']==guides[0]['requirements'] for g in guides),
    'focusedIdStringNotRounded':all(r['referenceContext']['focusedProductId']==second and isinstance(second,str) for r in requests[1:]),
    'ambiguousNoToolAndNoStateWrite':not data[1]['tool_trace'] and states[1]==states[0] and 'runId' not in data[1],
    'comparisonExactlyFocusedAndThird':{str(x) for x in guides[2]['comparedIds']}=={second,third},
    'comparisonToolExecuted':any(t['tool']=='compare_products' and t['ok'] for t in data[2]['tool_trace']),
    'sameScopeAcrossTurns':len({d['referenceContext']['scopeId'] for d in data})==1,
    'reuseNoBusinessTool':not data[3]['tool_trace'],
    'reuseNoModelCall':not any(r['binding'].get('runId')==data[3]['runId'] for r in starts),
    'runTraceBinding':len(turns)==len(traces)==3 and {t['runId'] for t in turns}=={d['runId'] for d in data if 'runId' in d},
    'traceHashes':all(sha(t['trace'])==t['sha256'] for t in traces),
    'bodyHashes':all(sha(r['request'])==r['requestSha256'] and sha(r['response'])==r['responseSha256'] for r in http),
    'cardsDiscloseSyntheticPrices':all('AI' in p['product']['priceDisclosure'] for d in data for p in d['guideResult']['products']),
    'agentStatusOK':all(d['traceSummary']['agentStatus']=='ok' and d['traceSummary']['runId']==d['runId'] for d in data if 'runId' in d),
}
result={'at':now(),'status':'PASS_BOUNDED_BROWSER_ENTRY' if all(checks.values()) else 'FAIL',
    'checks':checks,'httpRequests':len(http),'actualAgentRuns':len(turns),'providerCalls':len(starts),
    'sourceHashes':{n:file_sha(out/n) for n in ('protocol.json','http.jsonl','turns.jsonl','traces.jsonl','UI_OBSERVATIONS.md')},
    'boundary':'real browser plus HTTP/SSE; frozen tools and buffered answer; not production backend, provider token streaming, payment, or human quality approval',
    'humanAnswerQuality':'NOT_MEASURED_WITH_REVIEW_NOTES'}
json_new(out/'verification.json',result);print(result)
