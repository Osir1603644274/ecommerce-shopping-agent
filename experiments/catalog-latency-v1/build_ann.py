"""Derived, resumable IVF-PQ index; never modifies audited source vectors."""
import os
os.environ.setdefault('OMP_NUM_THREADS','8')
os.environ.setdefault('OPENBLAS_NUM_THREADS','4')
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

OUT=Path('D:/agent-datasets/catalog-latency-20260913-v1')
ROOT=Path('D:/agent-datasets/search-stage1-v1')
ANN_FOLDER='ann-v1'
PQ_M=64
sys.path.insert(0,str(OUT/'deps'))
import faiss
import numpy as np
import psutil


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
    return h.hexdigest()


def write(path,value):
    path=Path(path)
    with path.open('x',encoding='utf-8') as f:json.dump(value,f,ensure_ascii=False,indent=2)


def build(source):
    out=OUT/ANN_FOLDER/source;out.mkdir(parents=True,exist_ok=True)
    original=ROOT/'indexes'/source/'embeddings.npy'
    manifest=json.loads(original.with_name('dense.manifest.json').read_text('utf-8'))
    n=manifest['binding']['documents'];nlist=1024 if source=='kuaisearch' else 256
    config={'version':f'ivfpq{PQ_M}-v1','source':source,'faissVersion':faiss.__version__,
        'nlist':nlist,'pqM':PQ_M,'pqBits':8,'metric':'normalized_inner_product',
        'seed':20260913,'trainingSamples':min(n,65536),'coarseIterations':12,'pqIterations':15,
        'sourceVectors':str(original),'sourceSha256':manifest['vectors_sha256'],
        'sourceManifestSha256':sha(original.with_name('dense.manifest.json')),
        'documents':n,'firstRowid':manifest['binding']['first_rowid'],'dimension':512}
    if (out/'COMPLETE.json').exists():
        complete=json.loads((out/'COMPLETE.json').read_text('utf-8'))
        assert complete['config']==config and sha(out/complete['file'])==complete['sha256']
        print(source+' already complete and verified',flush=True);return
    if (out/'CONFIG.json').exists():assert json.loads((out/'CONFIG.json').read_text('utf-8'))==config
    else:write(out/'CONFIG.json',config)
    began=time.perf_counter()
    assert sha(original)==config['sourceSha256'],'audited source changed'
    vectors=np.load(original,mmap_mode='r');assert vectors.shape==(n,512) and vectors.dtype==np.float16
    faiss.omp_set_num_threads(8)
    checkpoints=sorted(out.glob('checkpoint-*.faiss'))
    if checkpoints:
        index=faiss.read_index(str(checkpoints[-1]));done=index.ntotal
        assert done<=n and index.d==512 and index.is_trained
    else:
        rng=np.random.default_rng(config['seed']);ids=np.sort(rng.choice(n,size=config['trainingSamples'],replace=False))
        sample=np.array(vectors[ids],dtype=np.float32)
        assert np.isfinite(sample).all();faiss.normalize_L2(sample)
        quantizer=faiss.IndexFlatIP(512)
        index=faiss.IndexIVFPQ(quantizer,512,nlist,PQ_M,8,faiss.METRIC_INNER_PRODUCT)
        index.cp.niter=config['coarseIterations'];index.cp.seed=config['seed']
        index.pq.cp.niter=config['pqIterations'];index.pq.cp.seed=config['seed']
        index.use_precomputed_table=-1
        print(json.dumps({'source':source,'stage':'train','samples':len(sample)}),flush=True)
        t=time.perf_counter();index.train(sample)
        faiss.write_index(index,str(out/'checkpoint-00000000.faiss'))
        write(out/'TRAIN.json',{'seconds':time.perf_counter()-t,'sampleIdsSha256':hashlib.sha256(ids.tobytes()).hexdigest()})
        del sample,ids;done=0
    last_checkpoint=done;last_print=time.perf_counter()
    for start in range(done,n,8192):
        batch=np.array(vectors[start:start+8192],dtype=np.float32)
        assert np.isfinite(batch).all();faiss.normalize_L2(batch)
        index.add(batch);done=start+len(batch)
        if done-last_checkpoint>=524288 or done==n:
            name=f'checkpoint-{done:08d}.faiss'
            faiss.write_index(index,str(out/name))
            write(out/(name+'.json'),{'ntotal':index.ntotal,'sha256':sha(out/name),'elapsedSeconds':time.perf_counter()-began})
            last_checkpoint=done
        if time.perf_counter()-last_print>15 or done==n:
            print(json.dumps({'source':source,'stage':'add','done':done,'total':n,
                'seconds':round(time.perf_counter()-began,2),'rssMB':round(psutil.Process().memory_info().rss/2**20)}),flush=True)
            last_print=time.perf_counter()
    assert index.ntotal==n
    file=f'checkpoint-{n:08d}.faiss'
    write(out/'COMPLETE.json',{'status':'COMPLETE','config':config,'file':file,'sha256':sha(out/file),
        'bytes':(out/file).stat().st_size,'buildCodeSha256':sha(__file__), 'seconds':time.perf_counter()-began})
    print(source+' index complete',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',choices=['kuaisearch','multicpr','both'],default='both');a=p.parse_args()
    for source in (['multicpr','kuaisearch'] if a.source=='both' else [a.source]):build(source)
