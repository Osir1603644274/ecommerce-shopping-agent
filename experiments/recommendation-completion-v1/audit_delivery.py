"""Independent stdlib metric reconstruction and sealed training-artifact audit."""
import hashlib,json,math,collections
from pathlib import Path

ROOT=Path('D:/agent-datasets/recommendation-completion-v1')
BASE=Path('D:/agent-datasets/recommendation-unified-v1/amazon-luxury-dev-001')
CODE=Path(__file__).resolve().parent
DOC=CODE.parents[1]/'docs/experiments/recommendation-completion-20260916'
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def rows(p):return [json.loads(l) for l in p.read_text(encoding='utf-8').splitlines()]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    checked=0
    for folder,field in [(BASE,'artifacts_sha256'),(ROOT/'neural-dev-001','files'),(ROOT/'ranking-dev-001','files'),
                          *[(ROOT/f'sequence-dev-001/seed-{s}','files') for s in [17,29,43]],(ROOT/'final-001','files')]:
        for name,digest in read(folder/'MANIFEST.json')[field].items():assert sha(folder/name)==digest,(folder,name);checked+=1
    final=ROOT/'final-001';truth={r['request_id']:set(r['target_item_ids']) for r in rows(final/'labels.private.jsonl')}
    req={r['request_id']:r for r in rows(final/'public_histories.jsonl')};assert req.keys()==truth.keys()
    catalog={r['item_id'] for r in rows(BASE/'catalog.jsonl')};warm=set(read(BASE/'fit_artifacts.json')['positive_item_user_counts'])
    metric=collections.defaultdict(list);seen=set()
    for p in rows(final/'predictions.jsonl'):
        rid=p['request_id'];listing=p['item_ids'];key=(rid,p['arm']);assert key not in seen;seen.add(key)
        assert set(listing)<=catalog and len(listing)==len(set(listing)) and len(listing)<=100
        assert not set(listing)&set(req[rid]['seen_all_fit_item_ids'])
        for section,target in [('all',truth[rid]),('warm_item',truth[rid]&warm),('cold_item',truth[rid]-warm)]:
            if not target:continue
            recall=len(set(listing)&target)/len(target)
            ideal=math.fsum(1/math.log2(r+2) for r in range(min(10,len(target))))
            actual=math.fsum(1/math.log2(r+2) for r,item in enumerate(listing[:10]) if item in target)
            metric[p['arm'],section].append((recall,actual/ideal))
    claimed=read(final/'RESULT.json')['metrics']
    for (arm,section),values in metric.items():
        assert len(values)==claimed[arm][section]['users']
        for index,name in [(0,'recall_at_100'),(1,'ndcg_at_10')]:
            actual=math.fsum(v[index] for v in values)/len(values)
            assert abs(actual-claimed[arm][section][name])<1e-12,(arm,section,name)
    assert all(len(metric[arm,'all'])==925 for arm in claimed)
    # Exact training/prediction code versions stored before convenience output-root override.
    core=['common.py','neural_content.py','ranking.py','sequence.py','final_evaluation.py']
    manifest=read(final/'MANIFEST.json')
    for name in core:assert sha(DOC/'training-code-snapshot'/name)==manifest['code'][name],name
    import fitz
    resume=Path('C:/Users/ming/Desktop/袁明珠_简历/推荐算法_20260916/袁明珠_北航28届_搜索推荐算法实习.pdf')
    pdf=fitz.open(resume);assert len(pdf)==1
    text=''.join(p.get_text() for p in pdf)
    assert '0.0301' in text and '925' in text and '8,493' in text and '选填照片' not in text
    page=pdf[0];blocks=page.get_text('blocks');bottom=max(b[3] for b in blocks if str(b[4]).strip())
    report={'status':'SEALED_ARTIFACTS_AND_INDEPENDENT_METRICS_VERIFIED','files_hashed':checked,'users':925,'arms':len(claimed),
            'slices':['all','warm_item','cold_item'],'independent_recalculation':'stdlib math.fsum, no common.score import',
            'core_training_code_exact':core,'resume_pages':1,'resume_last_text_bottom_fraction':bottom/page.rect.height,
            'source_limits':'No new human label audit; source cleaning reviewed in prior frozen bundle; no additional model inference or tuning'}
    (DOC/'AUDIT.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))

if __name__=='__main__':main()
