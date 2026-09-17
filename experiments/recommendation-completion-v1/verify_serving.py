"""Check exported pure-tree serving against every frozen dev prediction and real transitions."""
import os
os.environ['OPENBLAS_NUM_THREADS']='1'
os.environ['OMP_NUM_THREADS']='2'
import asyncio, copy, time
from common import *
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'agent'))
from app.recommendation_catalog import RecommendationCatalog
from app.api.recommendation_workspace import DemoPlan,apply_plan

def main():
    service=RecommendationCatalog(ROOT/'serving-v1')
    expected={r['request_id']:r['item_ids'] for r in rows(ROOT/'ranking-dev-001/predictions.jsonl') if r['arm']=='lambdamart_0'}
    latencies=[];mismatches=[]
    for n,r in enumerate(rows(BASE/'public_histories.jsonl')):
        start=time.perf_counter();out=service.recommend(r['history'],r['seen_all_fit_item_ids'],limit=100)
        latencies.append(time.perf_counter()-start)
        actual=[i['sourceItemId'] for i in out['items']]
        if actual!=expected[r['request_id']]:mismatches.append({'request_id':r['request_id'],'actual':actual,'expected':expected[r['request_id']]})
        if n%50==0:print(f'serving parity {n}/992 last_ms={latencies[-1]*1000:.1f}',flush=True)
    assert not mismatches, f'{len(mismatches)} serving parity mismatches'
    case=service.case('demo-01')
    initial={'caseId':'demo-01','history':copy.deepcopy(case['history']),'seen':case['seen'][:],'excluded':[],'query':'','scope':None,'undo':[]}
    first,_=apply_plan(service,initial,DemoPlan(action='recommend'));first_ids=[i['sourceItemId'] for i in first['scope']['items']]
    assert first_ids and not set(first_ids)&set(initial['seen'])
    exclude,_=apply_plan(service,first,DemoPlan(action='exclude',numbers=[1]));assert first_ids[0] not in [i['sourceItemId'] for i in exclude['scope']['items']]
    undone,_=apply_plan(service,exclude,DemoPlan(action='undo'));assert undone['scope']==first['scope']
    liked,_=apply_plan(service,first,DemoPlan(action='like',numbers=[1]));assert liked['scope']['historySha256']!=first['scope']['historySha256']
    assert first_ids[0] not in [i['sourceItemId'] for i in liked['scope']['items']]
    compare,text=apply_plan(service,first,DemoPlan(action='compare',numbers=[1,2]));assert compare==first and first_ids[0] in text
    searched,_=apply_plan(service,first,DemoPlan(action='search',query='hand cream'));assert searched['scope']['strategy']=='text_search_tfidf' and searched['scope']['items']
    assert all(i['source']==SOURCE and i['commerceAuthority'] is False and i['price'] is None for i in searched['scope']['items'])
    cold=service.recommend([],[],limit=10);assert cold['fallback']=='no_supported_positive_history' and len(cold['items'])==10
    cancelled,_=apply_plan(service,liked,DemoPlan(action='cancel'));assert cancelled['history']==initial['history'] and cancelled['scope'] is None
    for wrong in [0,11,-1]:
        try:apply_plan(service,first,DemoPlan(action='like',numbers=[wrong]));raise AssertionError('bad reference accepted')
        except ValueError:pass
    changed=copy.deepcopy(first);changed['scope']['items'][0]['title']='tampered'
    try:apply_plan(service,changed,DemoPlan(action='compare',numbers=[1,2]));raise AssertionError('tampered scope accepted')
    except ValueError:pass
    write(ROOT/'SERVING_VERIFIED.json',{'status':'FULL_DEV_TOP100_PARITY_AND_TOOL_TRANSITIONS_PASSED','users':len(expected),
        'top100_identical':len(expected),'source':SOURCE,'latency_ms_p50':float(np.median(latencies)*1000),
        'latency_ms_p95':float(np.percentile(latencies,95)*1000),'scope':'warmed CPU tool, excludes API and LLM',
        'transitions':['recommend','exclude','undo','like','compare','same_catalog_search','cold_start','cancel'],
        'rejections':['out_of_range_references','tampered_scope'],'frontend_or_http_verified':False,
        'code':{str(p):sha(p) for p in [Path(service.__class__.__module__.replace('.','/'))] if p.exists()}})
    print(read(ROOT/'SERVING_VERIFIED.json'))

if __name__=='__main__':main()
