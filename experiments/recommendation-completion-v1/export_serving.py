"""Export the selected CPU ranker and public-only demo inputs; no evaluation labels."""
import shutil
from common import *

def main():
    sys.path.insert(0,str(ROOT/'deps'));import lightgbm as lgb
    out=ROOT/'serving-v1'
    if out.exists():raise RuntimeError('Serving bundle already exists')
    out.mkdir(parents=True)
    model=lgb.Booster(model_file=str(ROOT/'ranking-dev-001/model_0.txt'))
    write(out/'trees.json',model.dump_model())
    for name,src in [('catalog.jsonl',BASE/'catalog.jsonl'),('fit_artifacts.json',BASE/'fit_artifacts.json'),
                     ('item_ids.json',ROOT/'embeddings/item_ids.json'),('vectors.npy',ROOT/'embeddings/vectors.npy')]:
        shutil.copyfile(src,out/name)
    # Deterministic case choice depends only on public request ID, never target/hit counts.
    public=sorted(rows(BASE/'public_histories.jsonl'),key=lambda r:hashlib.sha256(r['request_id'].encode()).hexdigest())[:12]
    write(out/'cases.json',[{'caseId':f'demo-{n+1:02d}','history':r['history'],
         'seen':r['seen_all_fit_item_ids'],'cutoff':r['cutoff_timestamp']} for n,r in enumerate(public)])
    write(out/'BUNDLE.json',{'source':SOURCE,'model':'lambdamart_0','model_selection':'fixed-candidate development selection',
        'price':None,'currency':None,'commerceAuthority':False,'event':'historical review >=4',
        'public_case_policy':'12 SHA256 sorted public request IDs; no target-based selection',
        'files':{p.name:sha(p) for p in out.iterdir() if p.is_file()},
        'parent_manifest':sha(ROOT/'ranking-dev-001/MANIFEST.json')})
    print(out)

if __name__=='__main__':main()
