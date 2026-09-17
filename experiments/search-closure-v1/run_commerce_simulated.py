"""Actual Java/CE tool experiment with explicitly synthetic prices in an isolated app build."""
import argparse
import asyncio
from contextlib import nullcontext
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from retrieval_runtime import read_json,sha,write_once,fingerprint,model_binding,old_readonly_modules
from authorize_final_test import validate_selection,verify_ref
import run_agent_pairs as agent_runner
from prepare_commerce_simulation import load_simulation_app, verify as verify_simulation_build, BUILD

ROOT=Path('D:/agent-datasets/search-closure-v1')
AUTHORITY_FIELDS=('id','title','brand','categoryL1','categoryL2','categoryL3','snapshotPriceMinor','currency','priceStatus','lifecycleStatus','entityVersion','availableQuantity','inventoryVersion')

def check_request(method,url,body,base):
    parsed=urlsplit(url);expected=urlsplit(base)
    if (parsed.scheme,parsed.hostname,parsed.port)!=(expected.scheme,expected.hostname,expected.port) or parsed.username or parsed.password:
        raise ValueError('Commerce request escaped configured local backend')
    if parsed.hostname not in ('localhost','127.0.0.1','::1'):raise ValueError('Only local component experiment allowed')
    allowed={('GET','/api/products/retrieval'),('GET','/api/products'),('POST','/api/products/resolve')}
    if (method,parsed.path) not in allowed:raise ValueError('Non-read-only commerce endpoint rejected')
    if method=='POST':
        if set(body)!= {'productIds'} or not 1<=len(body['productIds'])<=10 or any(type(i) is not int or i<=0 for i in body['productIds']):
            raise ValueError('Invalid read-only resolution request')

def authority_view(products):
    return [{k:p.get(k) for k in AUTHORITY_FIELDS} for p in sorted(products,key=lambda p:p['id'])]

def main_checks(trace,model_events,expected_model,commerce):
    if not trace.ok:return {'tool_ok':False,'reason':trace.detail}
    d=trace.detail;ids=d['candidatePoolIds'];ranked=d['rankedItemIds'];ce=d['retrievalTrace']['crossEncoder']
    checks={'tool_ok':True,'positive_unique_ids':len(ids)==len(set(ids)) and all(type(i) is int and i>0 for i in ids),
        'ranked_ids_within_pool':set(ranked)<=set(ids),'simulated_prices_disclosed':d['retrievalTrace']['syntheticPricePolicy']=='budget_and_ranking' and all(p.get('priceStatus')=='unverified' and p.get('snapshotPriceMinor') is None and p.get('facts',{}).get('priceStatus')=='synthetic' and p.get('syntheticReferencePrice',{}).get('priceStatus')=='synthetic' for p in d['candidates']),
        'no_ranked_hard_constraint_failure':not any(c.get('priority')=='hard' and c.get('status')=='fail' for p in d['candidates'] for c in p['checks'])}
    if ce['status']=='active':
        checks['one_actual_model_call']=len(model_events)==1 and model_events[0]['status']=='ACTUAL_CE_INFERENCE_COMPLETE'
        if checks['one_actual_model_call']:
            event=model_events[0];logits={int(k):v for k,v in event['logits'].items()};scores,order=commerce.logits_to_rank_scores(logits,ids)
            checks.update(model_binding_matches=ce['modelSha256']==expected_model,
                actual_inference_pool_matches=set(logits)==set(ids),
                transform_matches=ce['orderedProductIds']==order and all(x['rawLogit']==logits[x['productId']] and x['rankScore']==scores[x['productId']] for x in ce['scores']),
                input_hashes_match=ce['inputTextSha256']==event['inputTextSha256'])
    return checks

async def execute(args):
    selection_path=Path(args.selection).resolve()
    if selection_path!=(ROOT/'final-selection/SELECTION.json').resolve():raise ValueError('Actual frozen final selection required')
    verify_ref({'path':str(selection_path),'sha256':args.selection_sha256});choice=read_json(selection_path);validate_selection(choice)
    for ref in choice['inputs']:verify_ref(ref)
    selected=choice['selected'];component=selected if selected['model_path'] else choice['strongest_old']
    bound=model_binding(component['model_path'])
    if fingerprint(bound)!=component['model_binding_sha256']:raise ValueError('Frozen model changed')
    out=ROOT/'commerce-component-simulated-v1'
    if out.exists():raise ValueError('Existing actual experiment: inspect receipts; do not automatically repeat slots')
    modules=load_simulation_app();settings=modules.settings
    from app.domains.ecommerce import tools as shop
    from app.domains.ecommerce import cross_encoder as commerce
    from app import tools as dispatcher
    from app.domains.ecommerce.synthetic_prices import apply_synthetic_prices, synthetic_price_value
    from app.tools import call_tool
    backend=urlsplit(settings.backend_base_url)
    if backend.scheme not in ('http','https') or backend.hostname not in ('localhost','127.0.0.1','::1') or backend.username or backend.password or backend.query or backend.fragment or backend.path not in ('','/'):
        raise ValueError('Only credential-free local backend base URL allowed')
    if settings.product_retrieval_mode!='elasticsearch':raise ValueError('Actual configured Elasticsearch/Java authority route required')
    if settings.product_cross_encoder_enabled:raise ValueError('Default must remain off before experiment')
    overrides={'product_cross_encoder_enabled':False,'product_cross_encoder_model_sha256':fingerprint(bound),
               'used_phone_synthetic_price_policy':'budget_and_ranking','used_phone_synthetic_price_dir':str(ROOT/'commerce-simulated-prices-v1'),'product_title_reranker_enabled':False}
    before={k:getattr(settings,k) for k in overrides}
    code_paths=[Path(__file__),Path(commerce.__file__),Path(shop.__file__),Path(dispatcher.__file__),Path(agent_runner.__file__)]
    code_paths += [Path(shop.__file__).with_name(n) for n in ('models.py','ranking_contract.py')]
    code_paths += [Path(__file__).with_name(n) for n in ('retrieval_runtime.py','authorize_final_test.py')]
    integrity,old=old_readonly_modules();code_paths += [Path(old.__file__),Path(integrity.__file__)]
    binding={'selection_sha256':args.selection_sha256,'component_model':component,'model_binding':bound,'query':'iPhone','search_category':'手机','comparison_category':'phone',
        'component_scope':'Real Java retrieval/resolution and real CE; isolated app with explicitly synthetic prices. No real quote, production, full web controller, TaskState or relevance improvement claim',
        'simulation_build':verify_simulation_build(),'simulation_price_manifest_sha256':sha(ROOT/'commerce-simulated-prices-v1/MANIFEST.json'),
        'winner_uses_ce':selected['model_path'] is not None,'settings_before':before,'code':{str(p):sha(p) for p in code_paths},'backend':settings.backend_base_url,
        'runtime':{n:importlib.metadata.version(n) for n in ('torch','transformers','peft','httpx')},
        'timeout_scope':'Synchronous isolated CE component check; not a production timeout or latency guarantee'}
    write_once(out/'STARTED.json',binding)
    current={'slot':None,'resolved':{},'http':[],'models':[]};encoder=None
    def event(kind,value):
        record={'kind':kind,'slot':current['slot'],**value}
        with (out/'events.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n');stream.flush();os.fsync(stream.fileno())
    async def request_hook(request):
        body=json.loads(request.content) if request.content else None
        check_request(request.method,str(request.url),body,settings.backend_base_url)
        event('HTTP_REQUEST',{'method':request.method,'url':str(request.url),'body':body})
    async def response_hook(response):
        await response.aread();payload=response.json()
        value={'method':response.request.method,'url':str(response.request.url),'status_code':response.status_code,
               'body':payload,'body_sha256':hashlib.sha256(response.content).hexdigest()}
        current['http'].append(value);event('HTTP_RESPONSE',value)
        if response.request.url.path=='/api/products/resolve' and response.is_success:
            for p in payload.get('data',[]):current['resolved'][p['id']]=p
    client=shop._product_http_client();client.event_hooks['request'].append(request_hook);client.event_hooks['response'].append(response_hook)
    async def provider(request):
        nonlocal encoder
        expected=[(i,commerce.commerce_search_text(current['resolved'][i])) for i,_ in request.pairs]
        if expected!=list(request.pairs) or request.model_sha256!=fingerprint(bound):raise ValueError('CE input is not the real resolved product pool')
        for i,_ in request.pairs:
            p=current['resolved'][i]
            if p.get('priceStatus')!='unverified' or p.get('snapshotPriceMinor') is not None or p.get('lifecycleStatus')!='ACTIVE' or type(p.get('availableQuantity')) is not int or p['availableQuantity']<=0:
                raise ValueError('Ineligible authority facts entered CE')
            projected=apply_synthetic_prices([p],directory=settings.used_phone_synthetic_price_dir,policy='budget_and_ranking')[0]
            if synthetic_price_value(projected,allow_budget=True)[0] is None:raise ValueError('Missing bound simulated price')
        pairs=[[request.query,text] for _,text in request.pairs]
        event('MODEL_START',{'model_binding_sha256':fingerprint(bound),'pairs':list(request.pairs),'query':request.query})
        started=time.perf_counter()
        if encoder is None:encoder=old.CrossEncoder(component['model_path'])
        import torch
        torch.cuda.synchronize();logits=encoder.score(pairs,batch_size=16);torch.cuda.synchronize()
        if len(logits)!=len(pairs) or any(not math.isfinite(x) for x in logits):raise ValueError('Invalid actual CE outputs')
        values={i:float(score) for (i,_),score in zip(request.pairs,logits)}
        receipt={'status':'ACTUAL_CE_INFERENCE_COMPLETE','model_binding_sha256':fingerprint(bound),'logits':values,
                 'inputTextSha256':{str(i):hashlib.sha256(t.encode()).hexdigest() for i,t in request.pairs},
                 'elapsed_seconds':time.perf_counter()-started,'max_length':256,'batch_size':16}
        current['models'].append(receipt);event('MODEL_COMPLETE',receipt);return values
    async def forced_failure(request):
        event('CONTROLLED_PROVIDER_FAILURE',{'model_invoked':False,'reason':'Intentional component fallback check'})
        raise RuntimeError('controlled_provider_failure_before_inference')
    records={}
    async def slot(name,requirements,mode):
        current.update(slot=name,resolved={},http=[],models=[])
        settings.product_cross_encoder_enabled=mode!='original'
        write_once(out/'slots'/name/'STARTED.json',{'query':'iPhone','category':'手机','requirements':requirements,'mode':mode})
        scope=commerce.use_commerce_cross_encoder(fingerprint(bound),forced_failure if mode=='forced_failure' else provider) if mode!='original' else nullcontext()
        start=time.perf_counter()
        with scope:trace=await call_tool('search_products',{'query':'iPhone','category':'手机','limit':10,'requirements':requirements})
        result={'trace':trace.model_dump(mode='json',by_alias=True),'elapsed_seconds':time.perf_counter()-start,
                'authority':authority_view(list(current['resolved'].values())),'http_count':len(current['http']),
                'model_events':current['models'],'checks':main_checks(trace,current['models'],fingerprint(bound),commerce)}
        ce=trace.detail.get('retrievalTrace',{}).get('crossEncoder',{}) if trace.ok and isinstance(trace.detail,dict) else {}
        expected={'original':'disabled','ce':'active','forced_failure':'failed'}[mode]
        result['checks']['expected_ce_path']=ce.get('status')==expected and len(current['models'])==(1 if mode=='ce' else 0)
        if mode=='forced_failure':result['checks']['declared_fallback']=ce.get('fallback')=='previous_score_map'
        write_once(out/'slots'/name/'RECEIPT.json',result);records[name]=result;return trace
    try:
        for k,v in overrides.items():setattr(settings,k,v)
        plain=await slot('plain-original',[],'original')
        if not plain.ok or not plain.detail.get('candidates'):raise ValueError('Real original commerce search returned no usable candidates')
        seed=min(plain.detail['candidates'],key=lambda p:p['id'])
        seed_price=synthetic_price_value(seed,allow_budget=True)[0]
        if not seed.get('brand') or type(seed_price) is not int:raise ValueError('Deterministic fixture lacks brand or simulated price')
        cases={'plain':[],'hard-brand':[{'key':'brand','operator':'eq','value':seed['brand'],'unit':'text','priority':'hard','source':'user'}],
               'budget':[{'key':'price_minor','operator':'lte','value':seed_price,'unit':'CNY_MINOR','priority':'hard','source':'user'}]}
        write_once(out/'CASES.json',{'seed_product_id':seed['id'],'case_derivation':'Lowest positive ID from first actual search using the frozen25 synthetic-price bundle; constraints are simulation fixtures, not relevance labels','cases':cases})
        selected_trace=None
        for name,requirements in cases.items():
            original=plain if name=='plain' else await slot(name+'-original',requirements,'original')
            ranked=await slot(name+'-ce',requirements,'ce')
            if name=='plain':selected_trace=ranked
            failed=await slot(name+'-forced-failure',requirements,'forced_failure')
            a=records[name+'-original'];b=records[name+'-forced-failure']
            same=a['authority']==b['authority']
            fallback={'authority_same':same,'model_calls':len(b['model_events']),'forced_failure':True,
                'rank_and_scores_preserved':same and original.ok and failed.ok and original.detail['rankedItemIds']==failed.detail['rankedItemIds'] and [x['scoreBreakdown'] for x in original.detail['candidates']]==[x['scoreBreakdown'] for x in failed.detail['candidates']]}
            write_once(out/'fallback'/f'{name}.json',fallback)
        current.update(slot='comparison',resolved={},http=[],models=[])
        ids=selected_trace.detail['rankedItemIds'][:2] if selected_trace and selected_trace.ok else []
        if len(ids)<2:raise ValueError('Real comparison requires two resolved results')
        write_once(out/'comparison-STARTED.json',{'product_ids':ids})
        comparison=await call_tool('compare_products',{'productIds':ids,'category':'phone','requirements':[]})
        write_once(out/'comparison.json',{'trace':comparison.model_dump(mode='json',by_alias=True),'http_count':len(current['http'])})
        failures=[name for name,x in records.items() if not all(x['checks'].values())]
        fallback_ok=all(read_json(out/'fallback'/f'{name}.json')['rank_and_scores_preserved'] for name in cases)
        comparison_prices_disclosed=comparison.ok and bool(comparison.detail.get('products')) and all(x['facts']['priceStatus']=='synthetic' and x['product']['priceStatus']=='unverified' and x['product']['snapshotPriceMinor'] is None for x in comparison.detail['products'])
        result={'status':'COMMERCE_SIMULATED_PRICE_COMPONENT_EXPERIMENT_COMPLETE','component_checks_pass':not failures and fallback_ok and comparison.ok and comparison_prices_disclosed,
            'price_data_nature':'synthetic','real_verified_price_experiment_completed':False,'comparison_prices_disclosed':comparison_prices_disclosed,
            'failed_slots':failures,'fallback_checks_pass':fallback_ok,'comparison_ok':comparison.ok,
            'actual_ce_calls':sum(len(x['model_events']) for x in records.values()),'search_slots':len(records),
            'full_web_controller_verified':False,'taskstate_mutation_executed':False,'relevance_quality_evaluated':False,'production_activation':False}
        verify_simulation_build()
        if model_binding(component['model_path'])!=bound:raise ValueError('Model changed during execution')
        if any(sha(Path(p))!=h for p,h in binding['code'].items()):raise ValueError('Component code changed during execution')
    except Exception as exc:
        write_once(out/'FAILED.json',{'status':'ACTUAL_EXPERIMENT_INCOMPLETE','error_type':type(exc).__name__,'message':str(exc),'do_not_automatically_repeat_started_slots':True})
        raise
    finally:
        for k,v in before.items():setattr(settings,k,v)
        await shop.close_product_search_clients()
    restored={k:getattr(settings,k) for k in before}
    if restored!=before:raise ValueError('Isolated process settings were not restored')
    write_once(out/'SETTINGS_RESTORED.json',restored)
    result['files']={str(p.relative_to(out)):sha(p) for p in sorted(out.rglob('*')) if p.is_file()}
    write_once(out/'COMPLETE.json',result);return result

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--selection',required=True,type=Path);p.add_argument('--selection-sha256',required=True)
    result=asyncio.run(execute(p.parse_args()));print(json.dumps({k:v for k,v in result.items() if k!='files'}))

if __name__=='__main__':main()
