"""Bind actual unsuccessful read-only commerce execution without inventing success."""
from pathlib import Path
from collections import Counter
from retrieval_runtime import read_json,write_once,sha
from reviews import rows

root=Path('D:/agent-datasets/search-closure-v1');out=root/'commerce-component-v1'
events=rows(out/'events.jsonl');resolved=[p for r in events if r['kind']=='HTTP_RESPONSE' and '/resolve' in r['url'] for p in r['body']['data']]
receipt=read_json(out/'slots/plain-original/RECEIPT.json');listing=read_json(out/'product-list-diagnostic.json')['data']
assert len(resolved)==25 and all(p['snapshotPriceMinor'] is None and p['priceStatus']=='unverified' for p in resolved)
assert receipt['trace']['ok'] is False and not receipt['model_events']
result={'status':'ACTUAL_COMMERCE_ATTEMPT_HOLD_MISSING_VERIFIED_PRICE','tool_error':receipt['trace']['detail']['code'],
    'cause':'All25 resolved products lack snapshotPriceMinor and have unverified price; existing eligibility gate removes all before CE.',
    'retrieval_and_resolve_http_success':all(r['status_code']==200 for r in events if r['kind']=='HTTP_RESPONSE'),
    'resolved_products':len(resolved),'actual_ce_inferences':0,'original_search_completed_successfully':False,
    'list_diagnostic':{'returned':len(listing),'price_status_counts':dict(Counter(p['priceStatus'] for p in listing)),
        'scope':'Single GET /api/products?limit=1000 response; not a complete catalog count'},
    'production_activation':False,'price_data_or_eligibility_rules_modified':False,'repeated_consumed_slots':False,
    'blocked_checks':['live CE positive path','live forced fallback comparison','live compare-products after CE'],
    'next_prerequisite':'Authoritative verified price records with confirmed currency/units; no raw KuaiSearch price reinterpretation or invented fixtures.',
    'evidence':[{'path':str(p),'sha256':sha(p)} for p in [out/'STARTED.json',out/'events.jsonl',out/'FAILED.json',out/'slots/plain-original/RECEIPT.json',out/'product-list-diagnostic.json']]}
write_once(out/'DIAGNOSIS.json',result)
print({'status':result['status'],'resolved':25,'model_calls':0,'sha256':sha(out/'DIAGNOSIS.json')})
