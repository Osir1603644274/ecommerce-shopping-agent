"""Indexed online variant of the frozen full-corpus runtime.

Weights, tokenization, CE and original embeddings are unchanged. IVF-PQ only
proposes dense candidates; original vectors supply the final cosine scores.
"""
from collections import OrderedDict
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import numpy as np

ROOT=Path(__file__).resolve().parents[2]
DEFAULT=Path('D:/agent-datasets/catalog-latency-20260913-v1')
_name='_catalog_frozen_retrieval_runtime'
if _name not in sys.modules:
    _spec=importlib.util.spec_from_file_location(_name,ROOT/'experiments/search-closure-v1/retrieval_runtime.py')
    _module=importlib.util.module_from_spec(_spec);sys.modules[_name]=_module;_spec.loader.exec_module(_module)
old=sys.modules[_name]


def lexical_topk(db,table,expression,depth):
    if not expression:return [],0
    # ORDER BY rank activates FTS5's top-k path. Include every score tie at
    # the boundary before applying the original rowid tie-break ourselves.
    limit=depth*2
    while True:
        rows=db.execute(f'SELECT rowid,rank FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?',
                        (expression,limit)).fetchall()
        ordered=sorted(rows,key=lambda r:(r[1],r[0]))
        if len(rows)<limit or len(rows)<depth or rows[-1][1]>ordered[depth-1][1]:
            return ordered[:depth],limit
        limit*=2
        if limit>19200:
            return db.execute(f'SELECT rowid,bm25({table}) AS score FROM {table} WHERE {table} MATCH ? ORDER BY score,rowid LIMIT ?',
                              (expression,depth)).fetchall(),-1


def resolve_rows(catalog,source,rowids):
    result={}
    ids=[int(x) for x in rowids]
    for start in range(0,len(ids),500):
        chunk=ids[start:start+500]
        if not chunk:continue
        for rid,did,native in catalog.execute('SELECT rowid,docid,source FROM documents WHERE rowid IN ('+','.join('?' for _ in chunk)+')',chunk):
            if native!=source:raise ValueError('fast_row_source_mismatch')
            result[rid]=did
    if set(result)!=set(ids):raise ValueError('fast_row_missing')
    return result


def lexical_query(db,catalog,source,query,depth=300):
    started=time.perf_counter();tokens=old.query_tokens(query)
    timings={'tokenization_seconds':time.perf_counter()-started};channels={}
    for channel,table in [('bm25','words'),('character','chars')]:
        began=time.perf_counter();expr=' OR '.join('"'+t.replace('"','""')+'"' for t in tokens[channel])
        found,fetched=lexical_topk(db,table,expr,depth);matched=len(found);used={r[0] for r in found}
        if len(found)<depth:
            for rid, in catalog.execute('SELECT rowid FROM documents WHERE source=? ORDER BY rowid LIMIT ?',(source,depth*2)):
                if rid not in used:found.append((rid,0.0));used.add(rid)
                if len(found)==depth:break
        mapping=resolve_rows(catalog,source,[r[0] for r in found])
        channels[channel]=[{'document_id':mapping[rid],'rank':i,'score':-float(score)} for i,(rid,score) in enumerate(found,1)]
        timings[channel]={'search_and_resolve_seconds':time.perf_counter()-began,'matched':matched,
            'zero_score_padding':len(found)-matched,'tokens':tokens[channel],'topk_fetch_limit':fetched}
    return channels,timings


class FastRuntime(old.RetrievalRuntime):
    def __init__(self,*,directory=DEFAULT,parameters=None,progress=None):
        super().__init__(progress=progress)
        self.directory=Path(directory);sys.path.insert(0,str(self.directory/'deps'))
        import faiss
        self.faiss=faiss;faiss.omp_set_num_threads(4)
        self.parameters=parameters or old.read_json(self.directory/'selected.json')['parameters']
        self.ann={};self.ann_receipts={};self.vectors={};self.query_cache=OrderedDict()
        for source in old.SOURCES:
            path=self.directory/'ann-v1'/source/'COMPLETE.json';receipt=old.read_json(path)
            indexfile=path.parent/receipt['file'];original=self.dense_manifests[source]
            if (receipt['status']!='COMPLETE' or receipt['config']['sourceSha256']!=original['vectors_sha256']
                or receipt['config']['documents']!=original['binding']['documents'] or old.sha(indexfile)!=receipt['sha256']):
                raise ValueError('ann_source_binding_mismatch')
            # Compressed codes fit this 16GB host; original vectors stay mapped.
            index=faiss.read_index(str(indexfile))
            if index.ntotal!=original['binding']['documents'] or index.d!=512:raise ValueError('ann_count_dimension')
            self.ann[source]=index;self.ann_receipts[source]={'sha256':old.sha(path),'indexSha256':receipt['sha256']}
            self.vectors[source]=np.load(self.root/'indexes'/source/'embeddings.npy',mmap_mode='r')
            self.indexes[source].execute('PRAGMA cache_size=-16384')
        self.catalog.execute('PRAGMA cache_size=-16384')

    def _query_vectors(self,queries):
        import torch
        from transformers import AutoModel,AutoTokenizer
        torch.set_num_threads(2)
        began=time.perf_counter();cache_state='model_warm' if self._dense_model is not None else 'model_cold'
        if self._dense_model is None:
            info=self.models['dense'];self._tokenizer=AutoTokenizer.from_pretrained(info['path'],local_files_only=True)
            self._dense_model=AutoModel.from_pretrained(info['path'],local_files_only=True,torch_dtype=torch.float16).eval().cuda()
        load=time.perf_counter()-began;began=time.perf_counter()
        missing=list(dict.fromkeys(q['query'] for q in queries if q['query'] not in self.query_cache))
        with torch.inference_mode():
            for start in range(0,len(missing),32):
                texts=missing[start:start+32]
                tokens=self._tokenizer([old.QUERY_INSTRUCTION+q for q in texts],padding=True,truncation=True,max_length=128,return_tensors='pt').to('cuda')
                values=torch.nn.functional.normalize(self._dense_model(**tokens).last_hidden_state[:,0].float(),p=2,dim=1)
                for q,v in zip(texts,values):self.query_cache[q]=v.detach().clone()
            result=torch.stack([self.query_cache[q['query']] for q in queries])
        while len(self.query_cache)>128:self.query_cache.popitem(last=False)
        torch.cuda.synchronize()
        return result,{'model_cache_state':cache_state,'dense_model_load_seconds':load,
                       'query_encode_seconds':time.perf_counter()-began,'encoded_queries':len(missing)}

    def dense_query(self,source,qvector,parameters=None):
        import torch
        spec=(parameters or self.parameters)[source];index=self.ann[source]
        if not 1<=spec['nprobe']<=index.nlist or not 300<=spec['candidates']<=index.ntotal:raise ValueError('invalid_ann_parameters')
        began=time.perf_counter();params=self.faiss.SearchParametersIVF(nprobe=spec['nprobe'])
        scores,ids=index.search(qvector.detach().float().cpu().numpy().reshape(1,-1),spec['candidates'],params=params)
        ids=np.unique(ids[0][ids[0]>=0]);annseconds=time.perf_counter()-began
        if len(ids)<300:raise ValueError('ann_insufficient_candidates')
        began=time.perf_counter();raw=np.array(self.vectors[source][ids],dtype=np.float32)
        with torch.inference_mode():
            block=torch.nn.functional.normalize(torch.from_numpy(raw).cuda(),p=2,dim=1)
            exact=(qvector.reshape(1,-1)@block.T).float().cpu().numpy()[0]
        first=self.dense_manifests[source]['binding']['first_rowid']
        best,rowids=old.stable_topk(exact,ids+first,300)
        mapping=resolve_rows(self.catalog,source,rowids)
        values=[{'document_id':mapping[int(rid)],'rank':i,'score':float(score)} for i,(score,rid) in enumerate(zip(best,rowids),1)]
        return values,{'ann_seconds':annseconds,'original_vector_refine_seconds':time.perf_counter()-began,
            'proposed_count':len(ids),'nprobe':spec['nprobe'],'candidate_limit':spec['candidates']}

    def retrieve_batch(self,source,queries,*,include_dense=True):
        if source not in old.SOURCES or not queries:raise ValueError('unsupported_source_or_empty_query')
        if len({q['query_id'] for q in queries})!=len(queries):raise ValueError('duplicate_query')
        began=time.perf_counter();channels={};lex={};dense={}
        vectors,timing=self._query_vectors(queries) if include_dense else (None,{})
        for i,q in enumerate(queries):
            channels[q['query_id']],lex[q['query_id']]=lexical_query(self.indexes[source],self.catalog,source,q['query'])
            if include_dense:channels[q['query_id']]['dense'],dense[q['query_id']]=self.dense_query(source,vectors[i])
            else:channels[q['query_id']]['dense']=[]
        self._source_calls[source]+=1
        return {'source':source,'queries':queries,'channels':channels,'timings':dict(timing,
            wall_seconds=time.perf_counter()-began,lexical=lex,dense=dense,
            source_call_state='first_call_process' if self._source_calls[source]==1 else 'repeated_call_process',
            query_count=len(queries),scope='single_query_wall' if len(queries)==1 else 'batch_wall_not_online_p95')}

    def score_pairs(self,model_path,pairs):
        path=str(Path(model_path).resolve());began=time.perf_counter()
        state='model_warm' if self._ce_path==path else 'model_cold'
        if self._ce_path!=path:
            self._ce_binding=old.model_binding(path);_,previous=old.old_readonly_modules()
            self._ce=previous.CrossEncoder(path);self._ce_path=path
        load=time.perf_counter()-began;began=time.perf_counter()
        scores=self._ce.score(pairs,batch_size=16)
        if len(scores)!=len(pairs) or not np.isfinite(scores).all():raise ValueError('invalid_ce_output')
        return scores,{'model_cache_state':state,'model_load_seconds':load,'score_seconds':time.perf_counter()-began,
            'pair_count':len(pairs),'input_sha256':old.fingerprint(pairs),'model_binding':self._ce_binding,'max_length':256,'batch_size':16}

    def warm(self,model_path):
        began=time.perf_counter();old.query_tokens('索引预热')
        self._query_vectors([{'query':'索引预热','query_id':'startup-warmup'}])
        self.score_pairs(model_path,[['索引预热','预热仅用于加载模型，不计入检索验收']])
        return {'seconds':time.perf_counter()-began,'kind':'startup_model_warmup_not_evaluation'}

    def strategy_manifest(self,*,profile='w211',model_path=None):
        result=super().strategy_manifest(profile=profile,model_path=model_path)
        result.update(version='catalog-indexed-online-v1',ann=self.ann_receipts,annParameters=self.parameters,
                      fastCodeSha256=old.sha(__file__),lexicalPolicy='fts_rank_complete_boundary_ties_v1',
                      modelPolicy='resident_cuda_same_fp16_weights_v1')
        return result

    def close(self):
        self.ann={};self.vectors={};self.query_cache=OrderedDict()
        super().close()
