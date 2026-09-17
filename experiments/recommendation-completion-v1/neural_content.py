"""Fixed MiniLM content representation, then development scoring in a separate stage."""
import argparse, time
import numpy as np
from common import *

OUT = ROOT/'neural-dev-001'

def encode():
    import torch
    from transformers import AutoTokenizer, AutoModel
    verify_base()
    dest = ROOT/'embeddings'; dest.mkdir(parents=True, exist_ok=True)
    if (dest/'ENCODED.json').exists():
        manifest = read(dest/'ENCODED.json')
        assert sha(dest/'vectors.npy') == manifest['vectors_sha256']
        assert sha(BASE/'catalog.jsonl') == manifest['catalog_sha256']
        return
    download = read(ROOT/'minilm/DOWNLOAD.json')
    for name, info in download['files'].items(): assert sha(ROOT/'minilm'/name)==info['sha256']
    catalog = sorted(rows(BASE/'catalog.jsonl'),key=lambda p:p['item_id'])
    texts = [' '.join(str(p.get(k) or '') for k in ('title','brand','category')) for p in catalog]
    torch.set_num_threads(2)
    tokenizer = AutoTokenizer.from_pretrained(ROOT/'minilm', local_files_only=True, trust_remote_code=False)
    model = AutoModel.from_pretrained(ROOT/'minilm', local_files_only=True, trust_remote_code=False).cuda().eval()
    start = time.perf_counter(); vectors=[]
    with torch.inference_mode():
        for offset in range(0,len(texts),32):
            encoded = tokenizer(texts[offset:offset+32],padding=True,truncation=True,max_length=256,return_tensors='pt').to('cuda')
            output = model(**encoded).last_hidden_state
            mask = encoded['attention_mask'].unsqueeze(-1)
            mean = (output*mask).sum(1)/mask.sum(1).clamp(min=1)
            vectors.append(torch.nn.functional.normalize(mean,dim=1).cpu().numpy())
            if offset%1024==0: print(f'encoded {offset}/{len(texts)}',flush=True)
    matrix = np.concatenate(vectors).astype('float32')
    assert matrix.shape == (len(catalog),384) and np.isfinite(matrix).all()
    np.save(dest/'vectors.npy',matrix)
    write(dest/'item_ids.json',[p['item_id'] for p in catalog])
    write(dest/'ENCODED.json',{'model':download['model'],'revision':download['revision'],
        'model_download_sha256':sha(ROOT/'minilm/DOWNLOAD.json'),'catalog_sha256':sha(BASE/'catalog.jsonl'),
        'vectors_sha256':sha(dest/'vectors.npy'),'item_ids_sha256':sha(dest/'item_ids.json'),
        'seconds':time.perf_counter()-start,'device':'cuda','dtype':'float32','batch_size':32,
        'max_length':256,'pooling':'attention mask mean, L2','fields':['title','brand','category'],
        'shape':list(matrix.shape),'code_sha256':sha(__file__)})

def predict():
    verify_base(); OUT.mkdir(parents=True,exist_ok=True)
    if (OUT/'PREDICTED.json').exists(): raise RuntimeError('Preserve completed predictions')
    ids = read(ROOT/'embeddings/item_ids.json'); index={item:n for n,item in enumerate(ids)}
    encoded = read(ROOT/'embeddings/ENCODED.json')
    assert sha(ROOT/'embeddings/vectors.npy')==encoded['vectors_sha256']
    vectors = np.load(ROOT/'embeddings/vectors.npy')
    fit = read(BASE/'fit_artifacts.json'); public=rows(BASE/'public_histories.jsonl')
    cf = {p['request_id']:p['item_ids'] for p in rows(BASE/'predictions.jsonl') if p['arm']=='itemcf'}
    write(OUT/'PROTOCOL.json',{'change':'TFIDF -> fixed MiniLM representation only; additional equal CF/content RRF arm',
        'parent_manifest_sha256':sha(BASE/'MANIFEST.json'),'embeddings_sha256':sha(ROOT/'embeddings/ENCODED.json'),
        'history':'unique fit positive item mean, L2; no recency weighting','exclude':'all fit reviewed items',
        'catalog':len(ids),'final_depth':100,'rrf_weights':[0.5,0.5],'rrf_k':60,'evaluation_split':'dev',
        'no_current_query':True,'no_popular_backfill':True,'label_access_at_prediction':False})
    results=[]; times=[]
    for r in public:
        seen=old.check_public(r,fit); start=time.perf_counter()
        history = sorted({index[h['item_id']] for h in r['history'] if h['item_id'] in index})
        if history:
            mean = vectors[history].mean(0); mean/=max(np.linalg.norm(mean),1e-12)
            scores = vectors@mean
            listing=rank(scores,ids,seen,positive=True)
        else: listing=[]
        times.append(time.perf_counter()-start)
        for arm,items in [('content_minilm',listing),('cf_minilm_rrf',rrf([cf[r['request_id']],listing]))]:
            results.append({'request_id':r['request_id'],'arm':arm,'source':SOURCE,'item_ids':items})
    write_rows(OUT/'predictions.jsonl',results)
    write(OUT/'PREDICTED.json',{'sha256':sha(OUT/'predictions.jsonl'),'code_sha256':sha(__file__),
        'users':len(public),'latency_ms_p50':float(np.median(times)*1000),'latency_ms_p95':float(np.percentile(times,95)*1000),
        'scope':'CPU full catalog dot product and deterministic sort; excludes offline encoding'})

def evaluate():
    assert sha(OUT/'predictions.jsonl')==read(OUT/'PREDICTED.json')['sha256']
    predictions=rows(BASE/'predictions.jsonl')+rows(OUT/'predictions.jsonl')
    for p in read(BASE.parent/'amazon-luxury-fusion-dev-001/predictions.json'):
        predictions.append({**p,'source':SOURCE})
    details,result=score(predictions,rows(BASE/'public_histories.jsonl'),rows(BASE/'labels.private.jsonl'),read(BASE/'fit_artifacts.json'),rows(BASE/'catalog.jsonl'))
    result['status']='DEVELOPMENT_ONLY'; result['final_evaluated']=False
    write_rows(OUT/'per_user.jsonl',details);write(OUT/'RESULT.json',result);seal(OUT)
    print(json.dumps({a:v['all'] for a,v in result['metrics'].items()},indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('stage',choices=['encode','predict','evaluate']);a=p.parse_args()
    globals()[a.stage]()
