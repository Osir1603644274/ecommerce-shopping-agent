"""Read-only diagnosis of frozen recommendation predictions; never select or train models."""
import collections
import hashlib
import json
import math
from pathlib import Path
from statistics import mean, median

SOURCE = Path('D:/agent-datasets/recommendation-completion-v1/final-001')
BASE = Path('D:/agent-datasets/recommendation-unified-v1/amazon-luxury-dev-001')
OUT = Path('F:/agent/docs/experiments/recommendation-ceiling-audit-20260917')

def read(p): return json.loads(p.read_text(encoding='utf-8'))
def rows(p): return [json.loads(s) for s in p.read_text(encoding='utf-8').splitlines() if s]
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def dcg(n): return sum(1 / math.log2(i + 2) for i in range(min(n, 10)))
def ndcg(items, truth):
    return sum((i in truth) / math.log2(k + 2) for k, i in enumerate(items[:10])) / dcg(len(truth))

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    assert not (OUT/'RESULT.json').exists(), 'Preserve completed diagnosis'
    manifest = read(SOURCE/'MANIFEST.json')
    for name, digest in manifest['files'].items():
        assert sha(SOURCE/name) == digest, name
    extra = {str(BASE/n): sha(BASE/n) for n in ['fit_artifacts.json', 'catalog.jsonl']}
    warm = set(read(BASE/'fit_artifacts.json')['positive_item_user_counts'])
    catalog = {p['item_id']: p for p in rows(BASE/'catalog.jsonl')}
    candidates = {r['request_id']: r['item_ids'] for r in rows(SOURCE/'candidates.jsonl')}
    labels = {r['request_id']: set(r['target_item_ids']) for r in rows(SOURCE/'labels.private.jsonl')}
    histories = {r['request_id']: r for r in rows(SOURCE/'public_histories.jsonl')}
    predictions = {(r['request_id'], r['arm']): r['item_ids'] for r in rows(SOURCE/'predictions.jsonl')}
    assert candidates.keys() == labels.keys() == histories.keys()
    parts = collections.defaultdict(list)
    target_rows, per_request = [], []
    for rid, truth in labels.items():
        cand = set(candidates[rid]); assert len(cand) == len(candidates[rid])
        assert cand <= catalog.keys() and not cand.intersection(histories[rid]['seen_all_fit_item_ids'])
        tree = predictions[rid, 'lambdamart_0']; rrf = predictions[rid, 'fixed_union_rrf']
        assert set(tree) <= cand and set(rrf) <= cand
        cf = set(predictions[rid, 'itemcf']); tfidf = set(predictions[rid, 'content_tfidf'])
        assert cf | tfidf <= cand
        routes = {'itemcf100': cf, 'tfidf100': tfidf, 'cf_tfidf_union': cf | tfidf, 'three_route_union': cand}
        for bucket, t in [('all', truth), ('warm', truth & warm), ('cold', truth - warm)]:
            if not t: continue
            covered = len(cand & t)
            parts[bucket].append({'targets':len(t), 'covered':covered, 'tree100':len(set(tree)&t),
                'tree10':len(set(tree[:10])&t), 'rrf100':len(set(rrf)&t),
                'pool_macro_recall':covered/len(t), 'pool_hit':int(covered>0),
                'oracle_ndcg10':dcg(covered)/dcg(len(t)), 'tree_ndcg10':ndcg(tree,t), 'rrf_ndcg10':ndcg(rrf,t),
                'oracle_recall100':min(covered,100)/len(t), 'tree_recall100':len(set(tree)&t)/len(t),
                'route_hits':{n:len(ids&t) for n,ids in routes.items()}})
        for item in sorted(truth):
            stage = ('outside_catalog' if item not in catalog else 'missed_recall' if item not in cand
                     else 'lost_before_top100' if item not in tree else 'rank11_100' if item not in tree[:10] else 'top10')
            target_rows.append({'request_id':rid, 'item_id':item, 'title':catalog.get(item,{}).get('title',''),
                'temperature':'warm' if item in warm else 'cold', 'stage':stage,
                'rank':tree.index(item)+1 if item in tree else None,
                'routes':{n:item in ids for n,ids in routes.items()}})
        p=parts['all'][-1]
        per_request.append({'request_id':rid,'candidate_count':len(cand),**p})
    result={'status':'DESCRIPTIVE_AUDIT_OF_EXISTING_EXPOSED_HOLDOUT_NO_TUNING', 'users':len(labels),
        'candidate_count':{'min':min(map(len,candidates.values())),'median':median(map(len,candidates.values())), 'max':max(map(len,candidates.values()))},
        'buckets':{},'stage_counts':{},'source_hashes':manifest['files'],'additional_sources':extra}
    for name, group in parts.items():
        totals={k:sum(r[k] for r in group) for k in ['targets','covered','tree100','tree10','rrf100']}
        result['buckets'][name]={'users':len(group),**totals,
            **{k:mean(r[k] for r in group) for k in ['pool_macro_recall','pool_hit','oracle_ndcg10','tree_ndcg10','rrf_ndcg10','oracle_recall100','tree_recall100']},
            'micro_pool_recall':totals['covered']/totals['targets'],
            'route_target_hits':{k:sum(r['route_hits'][k] for r in group) for k in group[0]['route_hits']}}
        result['stage_counts'][name]=dict(collections.Counter(r['stage'] for r in target_rows if name=='all' or r['temperature']==name))
    frozen = read(SOURCE/'RESULT.json')['metrics']
    a = result['buckets']['all']
    for arm,key in [('lambdamart_0','tree_ndcg10'),('fixed_union_rrf','rrf_ndcg10')]:
        assert abs(a[key]-frozen[arm]['all']['ndcg_at_10'])<1e-12
    assert abs(a['tree_recall100']-frozen['lambdamart_0']['all']['recall_at_100'])<1e-12
    assert sum(result['stage_counts']['all'].values())==a['targets']
    assert a['tree_ndcg10'] <= a['oracle_ndcg10']
    for name,digest in manifest['files'].items(): assert sha(SOURCE/name)==digest
    for name,digest in extra.items(): assert sha(Path(name))==digest
    result['validation']={'frozen_files_unchanged':True,'ndcg_and_recall_match_original':True,'targets_partition_exact':True}
    (OUT/'RESULT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    for name,data in [('per-request.jsonl',per_request),('target-stages.jsonl',target_rows)]:
        (OUT/name).write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in data),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k not in ['source_hashes','additional_sources']},ensure_ascii=False,indent=2))

if __name__=='__main__': main()
