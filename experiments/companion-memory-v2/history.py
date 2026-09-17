"""Construct and retrieve from synthetic histories using public inputs only."""
import argparse, collections, hashlib, json, re, sqlite3, time
from pathlib import Path

OUT=Path('D:/agent-datasets/companion-memory-v2')
PUBLIC=Path('D:/agent-datasets/behavior-search-v1/companion/public.json')
UP=OUT/'upstream/longmemeval_s_cleaned.json'

def sha(text): return hashlib.sha256(text.encode('utf8')).hexdigest()
def read(path): return json.loads(path.read_text(encoding='utf8'))
def write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8')

def array_items(path):
    """Bounded-memory reader for a JSON array of objects."""
    decoder=json.JSONDecoder(); buf=''; started=False; eof=False
    with path.open(encoding='utf8') as f:
        while True:
            if not eof:
                piece=f.read(1024*1024); eof=not piece; buf+=piece
            buf=buf.lstrip()
            if not started:
                assert buf.startswith('[');buf=buf[1:];started=True
            while True:
                buf=buf.lstrip()
                if buf.startswith(','):buf=buf[1:].lstrip()
                if buf.startswith(']'):return
                try: item,end=decoder.raw_decode(buf)
                except json.JSONDecodeError:
                    if eof:raise
                    break
                yield item;buf=buf[end:]

def prepare():
    assert not (OUT/'DATASET.json').exists(), 'sealed dataset exists'
    rows=read(PUBLIC);ids=[]
    for item in array_items(UP): ids.append(str(item['question_id']))
    assert len(ids)==len(set(ids)) and len(ids)>=len(rows)
    chosen=sorted(ids,key=lambda x:sha('memory-v2-background:'+x))[:len(rows)]
    mapping={bid:r for bid,r in zip(chosen,rows)};gold=[];stats=[]
    for item in array_items(UP):
        bid=str(item['question_id'])
        if bid not in mapping:continue
        r=mapping[bid];sessions=[];seen=set()
        for session in item['haystack_sessions']:
            # Explicit field whitelist excludes has_answer and original question/answer metadata.
            text='\n'.join(str(t['role'])+': '+str(t['content']) for t in session)
            h=sha(text)
            if h in seen:continue
            seen.add(h);sessions.append({'text':text})
        memory=re.sub(r'^.*?\[Date:[^\]]+\]\s*','',r['memory'],count=1,flags=re.S)
        assert memory.startswith('user:') and 'most relevant user dialogue' not in memory
        assert sha(memory) not in seen
        pos=int(sha('insert:'+r['id'])[:8],16)%(len(sessions)+1)
        sessions.insert(pos,{'text':memory})
        for i,s in enumerate(sessions):s['id']=sha(r['id']+':'+str(i)+':'+s['text'])[:24]
        task={'id':r['id'],'query':r['query'],'partition':r['partition'],'sessions':sessions}
        write(OUT/'public'/f"{r['id']}.json",task)
        gold.append({'id':r['id'],'injected_session_id':sessions[pos]['id'],'background_id':bid,'insertion_index':pos,'public_memory_sha256':sha(memory)})
        stats.append({'id':r['id'],'sessions':len(sessions),'chars':sum(len(s['text']) for s in sessions),'memory_chars':len(memory)})
    assert len(gold)==40
    write(OUT/'private/injection_map.json',gold)
    write(OUT/'DATASET.json',{'name':'history-injection-v2','scope':'self-constructed static single-product history; not official benchmark or real user logs','public_tasks':[{k:r[k] for k in ['id','query','partition']} for r in rows],'source_public_sha256':hashlib.sha256(PUBLIC.read_bytes()).hexdigest(),'longmemeval':read(OUT/'upstream/DOWNLOAD.json'),'upstream_conversations':len(ids),'construction':'one complete background conversation per task; dedup exact sessions; insert public preference dialogue; opaque IDs; dates omitted because no temporal tasks','stats':stats})
    print('prepared',len(gold),'sessions',sum(x['sessions'] for x in stats),flush=True)

def terms(q):return list(dict.fromkeys(re.findall(r'[^\W_]+',q.lower())))[:20]
def retrieve():
    # No private labels are read during retrieval.
    for task_meta in read(OUT/'DATASET.json')['public_tasks']:
        task=read(OUT/'public'/f"{task_meta['id']}.json");db=sqlite3.connect(':memory:')
        db.execute("create virtual table memories using fts5(session_id UNINDEXED, text, tokenize='porter unicode61')")
        db.executemany('insert into memories values (?,?)',[(s['id'],s['text']) for s in task['sessions']])
        query=' OR '.join('"'+t+'"' for t in terms(task['query']))
        start=time.perf_counter()
        ranked=[{'id':r[0],'bm25':r[1]} for r in db.execute('select session_id,bm25(memories) from memories where memories match ? order by bm25(memories),session_id limit 20',(query,))]
        write(OUT/'retrieval'/f"{task['id']}.json",{'id':task['id'],'ranked':ranked,'seconds':time.perf_counter()-start,'method':'SQLite FTS5 Porter BM25 default','query':task['query']})
        db.close()
    print('retrieval complete',flush=True)

def score():
    truth={r['id']:r for r in read(OUT/'private/injection_map.json')};counts=collections.defaultdict(collections.Counter);details=[]
    for meta in read(OUT/'DATASET.json')['public_tasks']:
        result=read(OUT/'retrieval'/f"{meta['id']}.json");ids=[x['id'] for x in result['ranked']];gold=truth[meta['id']]['injected_session_id'];rank=ids.index(gold)+1 if gold in ids else None
        c=counts[meta['partition']];c['tasks']+=1
        for k in [1,3,5,10,20]:c[f'injected_hit@{k}']+=rank is not None and rank<=k
        details.append(dict(id=meta['id'],query=meta['query'],partition=meta['partition'],injected_rank=rank))
    write(OUT/'MEMORY_SCORES.json',{'counts':{k:dict(v) for k,v in counts.items()},'details':details,'scope':'known injected-session retrieval; background relevance not exhaustively judged'})
    print(json.dumps({k:dict(v) for k,v in counts.items()}),flush=True)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['prepare','retrieve','score']);a=p.parse_args();globals()[a.phase]()
