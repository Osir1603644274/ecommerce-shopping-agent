"""Verify existing full-corpus files and a declared small neural replay sample."""
from __future__ import annotations
import argparse
import gc
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from bootstrap import ROOT, REPO, sha, write_once

OLD = Path('D:/agent-datasets/search-stage1-v1')

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    out=ROOT/'assets'; out.mkdir(parents=True,exist_ok=True)
    if (out/'COMPLETE.json').exists():
        print((out/'COMPLETE.json').read_text(encoding='utf-8')); return
    prior=read(OLD/'data-audit.json')
    specs=[(OLD/'catalog.sqlite',prior['catalog_sha256'])]
    normalization_sha=sha(OLD/'normalization-report.json')
    for source in ['kuaisearch','multicpr']:
        directory=OLD/'indexes'/source
        for kind,filename in [('lexical','lexical.sqlite'),('dense','embeddings.npy')]:
            manifest=directory/f'{kind}.manifest.json'; m=read(manifest)
            binding=m['binding'] if kind=='dense' else m
            if binding['normalization_sha256']!=normalization_sha: raise ValueError('Normalization binding drift')
            if sha(manifest)!=prior['indices'][source][kind]['manifest_sha256']: raise ValueError('Index manifest changed')
            specs.append((directory/filename,prior['indices'][source][kind]['artifact_sha256']))
    model_info=read(OLD/'models/manifest.json')
    for info in model_info.values():
        for item in info['files']: specs.append((Path(info['path'])/item['name'],item['sha256']))
    files=[]
    for path,expected in specs:
        started=time.perf_counter(); actual=sha(path)
        if actual!=expected: raise ValueError(f'Full file SHA mismatch {path}')
        files.append({'path':str(path),'sha256':actual,'bytes':path.stat().st_size})
        print(json.dumps({'verified':str(path),'seconds':round(time.perf_counter()-started,3)}),flush=True)
    write_once(out/'files.json',{'status':'PASS','files':files,'normalization_sha256':normalization_sha})

    import numpy as np
    import torch
    from transformers import AutoModel,AutoTokenizer
    if not torch.cuda.is_available(): raise RuntimeError('CUDA required for comparable sample replay')
    torch.set_num_threads(2)
    info=model_info['dense']
    tokenizer=AutoTokenizer.from_pretrained(info['path'],local_files_only=True)
    model=AutoModel.from_pretrained(info['path'],local_files_only=True,torch_dtype=torch.float16).cuda().eval()
    catalog=sqlite3.connect('file:'+str(OLD/'catalog.sqlite').replace('\\','/')+'?mode=ro',uri=True)
    checks=[]
    # Hash-selected row numbers are fixed before values/labels are examined.
    for source in ['kuaisearch','multicpr']:
        directory=OLD/'indexes'/source; manifest=read(directory/'dense.manifest.json'); b=manifest['binding']
        vectors=np.load(directory/'embeddings.npy',mmap_mode='r')
        if vectors.shape!=(b['documents'],b['dimension']) or str(vectors.dtype)!=b['dtype']:
            raise ValueError('Vector shape/type mismatch')
        offsets=[]
        for i in range(8):
            off=int(hashlib.sha256(f'20260909:replay:{source}:{i}'.encode()).hexdigest(),16)%b['documents']
            while off in offsets: off=(off+1)%b['documents']
            offsets.append(off)
        rows=[]
        for off in offsets:
            row=catalog.execute('SELECT docid,source,text FROM documents WHERE rowid=?',(b['first_rowid']+off,)).fetchone()
            if row is None or row[1]!=source: raise ValueError('Vector row mapping differs')
            rows.append(row)
        with torch.inference_mode():
            inputs=tokenizer([r[2] for r in rows],padding=True,truncation=True,max_length=128,return_tensors='pt').to('cuda')
            encoded=torch.nn.functional.normalize(model(**inputs).last_hidden_state[:,0].float(),p=2,dim=1).cpu().numpy()
        old=np.asarray(vectors[offsets],dtype=np.float32)
        old/=np.linalg.norm(old,axis=1,keepdims=True)
        cos=(old*encoded).sum(axis=1)
        # FP16 batching can differ slightly; compare declared cosine tolerance, not exact bytes.
        for i,row in enumerate(rows):
            checks.append({'source':source,'document_id':row[0],'rowid':b['first_rowid']+offsets[i],
                          'input_text_sha256':hashlib.sha256(row[2].encode()).hexdigest(),
                          'cosine':float(cos[i]),'pass':bool(cos[i]>=0.999),
                          'criterion':'cosine>=0.999, FP16 replay; not bit-exact'})
        del vectors
    if not all(r['pass'] for r in checks):
        write_once(out/'REPLAY_FAILURE.json',{'checks':checks})
        raise ValueError('Neural sample replay outside frozen tolerance')
    catalog.close();del model,tokenizer;gc.collect();torch.cuda.empty_cache()
    write_once(out/'neural-sample-replay.json',{'status':'PASS','checks':checks,
        'sample_per_source':8,'full_neural_replay':False,'criterion_frozen_in_script':sha(Path(__file__))})
    write_once(out/'COMPLETE.json',{'status':'PASS','files_sha256':sha(out/'files.json'),
        'replay_sha256':sha(out/'neural-sample-replay.json'),
        'claim':'Full asset hashes verified plus 16-document neural replay sample; not full-corpus neural recomputation',
        'scripts':{str(Path(__file__)):sha(Path(__file__))}})
    print((out/'COMPLETE.json').read_text(encoding='utf-8'))

if __name__=='__main__': main()
