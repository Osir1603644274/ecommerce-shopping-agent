"""Full locator verification and real historical ranking replay; no inference."""
import asyncio
import collections as C
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'agent'))
from app.catalog_data import EvidenceStore,digest,use_catalog_metadata
from app.catalog_evidence import CatalogBinding,use_catalog_evidence_provider,search_catalog_evidence_tool
from app.settings import settings

OUT=Path('D:/agent-datasets/integration-repair-20260913-v1')
def load(p):return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x]
def write(p,v):p.write_text(json.dumps(v,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def phone_views():
    files={
      'catalog':ROOT/'datasets/current/used-phone/catalog.jsonl',
      'prices':ROOT/'datasets/current/used-phone/prices.jsonl',
      'knowledge':ROOT/'datasets/knowledge/phone-v2/listings.jsonl',
      'revision':Path('D:/agent-datasets/used-phone-price-revision-v1/catalog.revised.jsonl')}
    rows={k:{str(r['itemId']):r for r in load(p)} for k,p in files.items()}
    assert all(set(x)==set(rows['catalog']) for x in rows.values())
    links={r['itemId']:r for r in load(OUT/'metadata/phone-links.jsonl')}
    output=[]
    for pid,catalog in rows['catalog'].items():
        price=rows['prices'][pid];rev=rows['revision'][pid]['historicalPriceEvidence'];knowledge=rows['knowledge'][pid]
        assert price['dataNature']=='synthetic' and not rev['eligibleForBudgetFiltering']
        assert hashlib.sha256(catalog['title'].encode()).hexdigest()==knowledge['titleSha256']
        attributes={k:{'value':a['value'] if a['status']=='known' else None,'state':a['status'],
                        'basis':'controlled_source_interpretation_not_inspection','evidence':a['evidenceRefs']} for k,a in catalog['attributes'].items()}
        output.append({'itemId':pid,'title':catalog['title'],'identityLink':links[pid],
            'attributes':attributes,'modelBinding':{'state':knowledge['status'],'modelKeys':knowledge['modelKeys'],
                'physicalIdentityVerified':False,'canApplySingleModelFacts':knowledge['status']=='CLEAR' and len(knowledge['modelKeys'])==1},
            'historicalPrice':rev,'simulatedPrice':{'amountMinor':price['referencePriceMinor'],'currency':'CNY',
                'state':'synthetic','realBudgetEligible':False,'requiresExplicitSimulationPolicy':True},
            'verifiedPrice':None,'inventory':None,'commerceAuthority':False})
    (OUT/'phone-evidence.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in output),encoding='utf-8')
    write(OUT/'PHONE-VALIDATION.json',{'rows':len(output),'original_files_sha256':{str(p):digest(p) for p in files.values()},
        'verified_prices_created':0,'synthetic_prices_preserved':len(output),'native_id_links':sum(x['identityLink']['nativeDocid'] is not None for x in output),
        'model_states':dict(C.Counter(x['modelBinding']['state'] for x in output)),'original_files_changed':False})


async def main():
    store=EvidenceStore(OUT/'metadata',expected_manifest_sha256=digest(OUT/'metadata/MANIFEST.json'))
    started=time.perf_counter();counts=C.Counter();offsets=C.Counter();samples=0
    for did,source,line,offset,length in store.db.execute('SELECT docid,source,source_line,byte_offset,byte_length FROM records ORDER BY rowid'):
        counts[source]+=1
        assert line==counts[source] and offset==offsets[source] and length>0
        offsets[source]+=length
        if line==1 or line%10007==0:
            assert store.record(did)['docid']==did;samples+=1
    for source,offset in offsets.items():assert Path(store.manifest['sources'][source]['path']).stat().st_size==offset
    assert dict(counts)=={'kuaisearch':6634118,'multicpr':1002822}
    print('full locator continuity verified',flush=True)
    catalog=sqlite3.connect('file:D:/agent-datasets/search-stage1-v1/catalog.sqlite?mode=ro',uri=True)
    binding=CatalogBinding(dataRoot='D:/agent-datasets/search-stage1-v1',runId='metadata-replay-20260913',manifestSha256=store.manifest_sha256)
    config={'catalog_evidence_enabled':True,'catalog_evidence_data_root':binding.data_root,
        'catalog_evidence_run_id':binding.run_id,'catalog_evidence_manifest_sha256':binding.manifest_sha256,'catalog_evidence_source':'kuaisearch'}
    before={k:getattr(settings,k) for k in config};report=C.Counter();files={};receipts=[];query_groups={}
    try:
        for k,val in config.items():setattr(settings,k,val)
        for split in ['dev','historical145']:
            p=Path('D:/agent-datasets/search-closure-v1/ce-lambdamart-fusion-v1')/split/'rankings.jsonl'
            files[str(p)]=digest(p)
            for r in load(p):
                if r['alpha'] not in [1.,.75]:continue
                settings.catalog_evidence_source=r['source']
                hits=[]
                for rank,(did,score) in enumerate(zip(r['ranking'][:10],r['scores'][:10]),1):
                    source,text=catalog.execute('SELECT source,text FROM documents WHERE docid=?',(did,)).fetchone()
                    hits.append({'docid':did,'source':source,'text':text,'rank':rank,'score':score,
                        'provenance':{'replaySource':str(p),'rankingSha256':files[str(p)],'modelCalled':False,'scoreType':'saved_weighted_rrf_rank_score'},'unknown':[]})
                async def provider(request):return {'binding':binding.model_dump(by_alias=True),'source':request.source,'query':request.query,'hits':hits}
                with use_catalog_metadata(store),use_catalog_evidence_provider(binding,provider):
                    trace=await search_catalog_evidence_tool(r['query'],r['source'],10)
                assert trace.ok,trace.detail
                out=trace.detail
                assert [(x['docid'],x['rank'],x['score'],x['text']) for x in out['hits']]==[(x['docid'],x['rank'],x['score'],x['text']) for x in hits]
                members=[x for g in out['presentationGroups'] for x in g['members']]
                assert sorted(x['docid'] for x in members)==sorted(x['docid'] for x in hits)
                assert len(set(g['title'] for g in out['presentationGroups']))==len(out['presentationGroups'])
                key=f"{split}/{r['method']}";q=query_groups.setdefault(key,{'queries':0,'raw_hits':0,'presentation_groups':0})
                q['queries']+=1;q['raw_hits']+=len(hits);q['presentation_groups']+=len(out['presentationGroups'])
                report['ranking_replays']+=1;report['hit_checks']+=len(hits)
                receipts.append({'split':split,'method':r['method'],'query':r['query'],'queryId':r['query_id'],
                    'realHistoricalRanking':True,'newModelCalls':0,'detail':out})
    finally:
        for k,val in before.items():setattr(settings,k,val)
        store.close();catalog.close()
    assert report['ranking_replays']==356 and report['hit_checks']==3560
    assert all(digest(Path(p))==sha for p,sha in files.items())
    (OUT/'real-ranking-replays.jsonl').write_text(''.join(json.dumps(x,ensure_ascii=False)+'\n' for x in receipts),encoding='utf-8')
    report.update({'source_rows':sum(counts.values()),'record_hash_samples':samples})
    write(OUT/'REPLAY-VALIDATION.json',{'status':'PASS_STRUCTURAL_AND_PRESENTATION_REPLAY','counts':dict(report),
        'by_split_method':query_groups,'input_rankings_sha256':files,'rank_text_score_changes':0,
        'identity_loss':0,'new_model_calls':0,'query_or_qrel_changes':False,'settings_restored':all(getattr(settings,k)==val for k,val in before.items()),
        'elapsed_seconds':time.perf_counter()-started,'scope':'old rankings through actual repaired tool adapter; not fresh retrieval or answer quality evaluation'})
    phone_views();print(json.dumps(dict(report)),flush=True)


if __name__=='__main__':
    sys.stdout.reconfigure(encoding='utf-8');asyncio.run(main())
