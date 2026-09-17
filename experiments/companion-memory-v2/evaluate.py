"""Post-prediction scorer. Private answers never enter retrieval/prediction."""
import collections,json,sqlite3,statistics,sys
from pathlib import Path
import history as h
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'behavior-search-v1'))
import companion as c
OLD=Path('D:/agent-datasets/behavior-search-v1/companion')

def pairs(data):
    out=set()
    for k,v in (data or {}).items():
        for val in v if isinstance(v,list) else [v]:out.add((c.normalize(k),c.normalize(val)))
    return out

def main():
    truths={r['id']:r['truth'] for r in h.read(OLD/'oracle.private.json')}
    bm={r['id']:r['injected_rank'] for r in h.read(h.OUT/'MEMORY_SCORES.json')['details']}
    selected={r['id']:r['rank'] for r in h.read(h.OUT/'SELECTED_MEMORY_SCORES.json')['details']} if (h.OUT/'SELECTED_MEMORY_SCORES.json').exists() else {}
    db=sqlite3.connect((c.CAT/'retrieval-v1/catalog.sqlite3').as_uri()+'?mode=ro',uri=True)
    counts=collections.defaultdict(collections.Counter);details=[];latencies=collections.defaultdict(list)
    for meta in h.read(h.OUT/'DATASET.json')['public_tasks']:
        qid=meta['id'];t=truths[qid];target=str(t['product_id']);wanted=[]
        for x in t['wanted_features']:
            if ':' in x:k,v=x.split(':',1)
            else:
                matching=[(k,v) for k,v in t['aspects'] if v==x]
                if len(matching)!=1:continue
                k,v=matching[0]
            wanted.append((c.normalize(k),c.normalize(v)))
        old=h.read(OLD/'resolved'/f'{qid}.prediction.json')
        arms={'provided_memory':old}
        for name,folder,retry in [('bm25_memory','pipeline','retry-attempt002'),('selected_memory','semantic-pipeline','semantic-retry-attempt002')]:
            path=h.OUT/folder/f'{qid}.prediction.json'
            if not path.exists():continue
            pred=h.read(path)
            if pred['status']!='complete' and (h.OUT/retry/path.name).exists():pred=h.read(h.OUT/retry/path.name)
            arms[name]=pred
        out={}
        for name,pred in arms.items():
            s=counts[meta['partition']+'/'+name];s['tasks']+=1
            if pred['status']!='complete':s['errors']+=1;continue
            s['complete']+=1;s['reference_hit50']+=target in pred['B'];s['reference_selected']+=target==pred['C'];s['abstain']+=pred['C'] is None
            s['calls']+=len(pred['calls']);s['tokens']+=sum((v.get('usage') or {}).get('total_tokens',0) for v in pred['calls']);latencies[name].append(pred['seconds'])
            extracted={(c.normalize(x.get('attribute','')),c.normalize(x.get('value',''))) for x in pred['extraction'].get('preferences',[]) if isinstance(x,dict)}
            s['wanted_total']+=len(wanted);s['wanted_exact']+=sum(x in extracted for x in wanted)
            s['extracted_pairs_not_in_reference']+=len(extracted-set(wanted))
            match=False
            if pred['C']:
                d=c.products(db,[pred['C']])[0];base=pairs(d['attributes']);match=bool(wanted) and (set(wanted)<=base or any(set(wanted)<=base|pairs(op) for op in d['options']))
            s['one_option_exact_attribute_coverage']+=match
            out[name]={'reference_hit50':target in pred['B'],'selected':pred['C'],'attribute_coverage':match,'extracted':sorted(extracted),'wanted_exact':sum(x in extracted for x in wanted)}
        control=h.read(OLD/'no-memory-control'/f'{qid}.json');s=counts[meta['partition']+'/no_memory'];s['tasks']+=1;s['reference_hit50']+=target in control.get('top50',[])
        details.append({'id':qid,'partition':meta['partition'],'query':meta['query'],'target':target,'wanted':wanted,'bm25_memory_rank':bm[qid],'selected_memory_rank':selected.get(qid),'arms':out})
    result={'counts':{k:dict(v) for k,v in counts.items()},'latency_medians':{k:statistics.median(v) for k,v in latencies.items()},'details':details,'scope':'same 40 previously exposed tasks; constructed background; reference hit and exact attribute diagnostics, not official end-to-end success','extra_extracted_pairs_note':'not in reference is a mismatch diagnostic, not automatically hallucinated; may be alternative keys or background preferences'}
    h.write(h.OUT/'RESULTS.json',result);print(json.dumps(result['counts']),flush=True)
    db.close()

if __name__=='__main__':main()
