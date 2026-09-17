"""Same v2 retrieval; independent read-only channels overlap on the CPU."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing,nullcontext
from pathlib import Path
import sqlite3
import threading
import time
from .catalog_fast_retrieval import lexical_topk,resolve_rows,old,DEFAULT
from .catalog_fast_retrieval_v2 import FastRuntime as IndexRuntime

MMAP_BYTES=2147418112


def configure_read(db):
    db.execute('PRAGMA cache_size=-16384')
    size=db.execute(f'PRAGMA mmap_size={MMAP_BYTES}').fetchone()[0]
    if size!=MMAP_BYTES:raise ValueError('sqlite_mmap_not_supported')


def read_channel(root,source,channel,tokens,depth=300,db_pair=None):
    began=time.perf_counter();table={'bm25':'words','character':'chars'}[channel]
    with (nullcontext(db_pair[0]) if db_pair else closing(old.readonly(Path(root)/'indexes'/source/'lexical.sqlite'))) as db, (nullcontext(db_pair[1]) if db_pair else closing(old.readonly(Path(root)/'catalog.sqlite'))) as catalog:
        configure_read(db);configure_read(catalog)
        expr=' OR '.join('"'+t.replace('"','""')+'"' for t in tokens)
        found,fetched=lexical_topk(db,table,expr,depth);matched=len(found);used={r[0] for r in found}
        if len(found)<depth:
            for rid, in catalog.execute('SELECT rowid FROM documents WHERE source=? ORDER BY rowid LIMIT ?',(source,depth*2)):
                if rid not in used:found.append((rid,0.0));used.add(rid)
                if len(found)==depth:break
        mapping=resolve_rows(catalog,source,[r[0] for r in found])
        values=[{'document_id':mapping[rid],'rank':i,'score':-float(score)} for i,(rid,score) in enumerate(found,1)]
        return values,{'search_and_resolve_seconds':time.perf_counter()-began,'matched':matched,
            'zero_score_padding':len(found)-matched,'tokens':tokens,'topk_fetch_limit':fetched}


class LexicalReader:
    def __init__(self,root):
        self.root=root;self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='catalog-read')
        self.local=threading.local();self.connections=[];self.registry_lock=threading.Lock()

    def _read(self,source,channel,tokens):
        if not hasattr(self.local,'dbs'):self.local.dbs={}
        for name,path in [('catalog',Path(self.root)/'catalog.sqlite'),(source,Path(self.root)/'indexes'/source/'lexical.sqlite')]:
            if name not in self.local.dbs:
                # Each connection is used by only its owning reader thread.
                # Cross-thread close is allowed after executor shutdown.
                db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,check_same_thread=False)
                db.execute('PRAGMA query_only=ON');configure_read(db)
                self.local.dbs[name]=db
                with self.registry_lock:self.connections.append(db)
        return read_channel(self.root,source,channel,tokens,db_pair=(self.local.dbs[source],self.local.dbs['catalog']))

    def submit(self,source,query):
        began=time.perf_counter();tokens=old.query_tokens(query)
        timing={'tokenization_seconds':time.perf_counter()-began}
        futures={c:self.pool.submit(self._read,source,c,tokens[c]) for c in ['bm25','character']}
        return futures,timing

    def collect(self,pending):
        futures,timing=pending;channels={}
        for c,future in futures.items():channels[c],timing[c]=future.result()
        return channels,timing

    def close(self):
        self.pool.shutdown(wait=True,cancel_futures=True)
        for db in self.connections:db.close()
        self.connections=[]


class FastRuntime(IndexRuntime):
    def __init__(self,*,directory=DEFAULT,parameters=None,progress=None):
        parameters=parameters or old.read_json(Path(directory)/'selected-v3.json')['parameters']
        super().__init__(directory=directory,parameters=parameters,progress=progress)
        configure_read(self.catalog)
        self.reader=LexicalReader(self.root)

    def retrieve_batch(self,source,queries,*,include_dense=True):
        if source not in old.SOURCES or not queries:raise ValueError('unsupported_source_or_empty_query')
        if len({q['query_id'] for q in queries})!=len(queries):raise ValueError('duplicate_query')
        began=time.perf_counter();channels={};lex={};dense={}
        vectors,timing=self._query_vectors(queries) if include_dense else (None,{})
        for i,q in enumerate(queries):
            qid=q['query_id'];pending=self.reader.submit(source,q['query'])
            hits=[]
            if include_dense:hits,dense[qid]=self.dense_query(source,vectors[i])
            channels[qid],lex[qid]=self.reader.collect(pending)
            channels[qid]['dense']=hits
        self._source_calls[source]+=1
        return {'source':source,'queries':queries,'channels':channels,'timings':dict(timing,
            wall_seconds=time.perf_counter()-began,lexical=lex,dense=dense,
            source_call_state='first_call_process' if self._source_calls[source]==1 else 'repeated_call_process',
            query_count=len(queries),scope='single_query_wall' if len(queries)==1 else 'batch_wall_not_online_p95',
            scheduling='two_sql_readers_overlap_dense')}

    def strategy_manifest(self,*,profile='w211',model_path=None):
        result=super().strategy_manifest(profile=profile,model_path=model_path)
        result.update(version='catalog-indexed-online-v3',fastCodeSha256=old.sha(__file__),
            indexRuntimeCodeSha256=old.sha(Path(__file__).with_name('catalog_fast_retrieval_v2.py')),
            storagePolicy={'mmapBytes':MMAP_BYTES,'lexicalThreads':2,'denseOverlapsLexical':True})
        return result

    def close(self):
        if hasattr(self,'reader'):self.reader.close()
        super().close()
