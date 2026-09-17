"""Source-linked, time-cutoff history snapshots. No model training."""
import argparse,collections,hashlib,json,sqlite3,time
from pathlib import Path

OLD=Path('D:/agent-datasets/behavior-search-v1')
SRC=Path('D:/agent-datasets/kuaisearch-lite-09807c773ce67360ed8df30842e372182fcf7ad9/recall_lite.train.jsonl')
OUT=Path('D:/agent-datasets/behavior-history-v2')
def read(p):return json.loads(p.read_text(encoding='utf8'))
def write(p,obj):p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8')
def digest(p):
    h=hashlib.sha256()
    with p.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()
def connect(readonly=True):
    p=OUT/'requests.sqlite3'
    db=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) if readonly else sqlite3.connect(p)
    db.execute('pragma cache_size=-8192');return db

def index():
    OUT.mkdir(parents=True,exist_ok=True)
    assert not (OUT/'requests.sqlite3').exists(), 'existing index must be preserved'
    cfg=read(OLD/'config.json');start=time.time();db=connect(False)
    db.execute('create table requests(sid integer primary key,uid integer,t integer,split text,exposures integer,clicks integer,items text,byte_offset integer,byte_length integer,line_sha256 text)')
    db.execute('create table targets(sid integer primary key,partition text,ordinal integer,first_pair integer,last_pair integer,old_has_history integer)')
    h=hashlib.sha256();batch=[];counts=collections.Counter();offset=0
    with SRC.open('rb') as f:
        for n,line in enumerate(f,1):
            h.update(line);r=json.loads(line)
            assert len(set(r['clicked_item_ids']))==len(r['clicked_item_ids'])
            assert set(r['clicked_item_ids'])<=set(r['impressed_item_ids'])
            batch.append((r['session_id'],r['user_id'],r['time_index'],r['split'],len(r['impressed_item_ids']),len(r['clicked_item_ids']),json.dumps(r['clicked_item_ids']),offset,len(line),hashlib.sha256(line).hexdigest()))
            offset+=len(line);counts[r['split']]+=1
            if len(batch)>=5000:db.executemany('insert into requests values(?,?,?,?,?,?,?,?,?,?)',batch);db.commit();batch=[]
            if n%100000==0:print('indexed',n,flush=True)
    if batch:db.executemany('insert into requests values(?,?,?,?,?,?,?,?,?,?)',batch)
    assert h.hexdigest()==cfg['source_hashes']
    db.execute('create index by_user_time on requests(uid,t,sid)')
    sources={}
    for sp in ['train','dev','test']:
        groups=read(OLD/f'{sp}_groups.json');sources[sp]={name:digest(OLD/name) for name in [f'{sp}.jsonl',f'{sp}_groups.json',f'{sp}_y.npy']}
        with (OLD/f'{sp}.jsonl').open(encoding='utf8') as f:
            for i,line in enumerate(f):
                row=json.loads(line);g=groups[i];assert row['session_id']==g['sid'] and row['user_id']==g['uid']
                raw=db.execute('select byte_offset,byte_length from requests where sid=?',(g['sid'],)).fetchone()
                with SRC.open('rb') as source:source.seek(raw[0]);assert json.loads(source.read(raw[1]))==row
                db.execute('insert into targets values(?,?,?,?,?,?)',(g['sid'],sp,i,g['start'],g['end'],int(g['has_history'])))
        assert i+1==len(groups)
    db.commit();db.close()
    write(OUT/'INDEX.json',{'source':str(SRC),'source_sha256':h.hexdigest(),'request_counts':dict(counts),'old_sources':sources,'old_config_sha256':digest(OLD/'config.json'),'history_end':cfg['history_end'],'train_end':cfg['train_end'],'seconds':time.time()-start})
    print('index complete',round(time.time()-start,1),flush=True)

def cutoff(sp,t,cfg):return min(t-1,cfg['train_end']) if sp=='train' else cfg['train_end']
def statistics(rows):
    return {'requests':len(rows),'exposures':sum(x[2] for x in rows),'clicks':sum(x[3] for x in rows),'clicked_requests':sum(x[3]>0 for x in rows),'last_time':max((x[1] for x in rows),default=None)}

def build():
    cfg=read(OUT/'INDEX.json');db=connect();started=time.time()
    for sp in ['train','dev','test']:
        path=OUT/f'{sp}_snapshots.jsonl';assert not path.exists()
        targets=db.execute('select r.sid,r.uid,r.t,t.ordinal from targets t join requests r using(sid) where t.partition=? order by t.ordinal',(sp,)).fetchall()
        with path.open('w',encoding='utf8') as f:
            for sid,uid,t,ordinal in targets:
                end=cutoff(sp,t,cfg)
                hist=db.execute("select sid,t,exposures,clicks,items from requests where uid=? and split='train' and t<=? order by t,sid",(uid,end)).fetchall()
                old=[r for r in hist if r[1]<=cfg['history_end']]
                recent_end=hist[-20][1] if len(hist)>=20 else (hist[0][1] if hist else None)
                recent=[r for r in hist if recent_end is not None and r[1]>=recent_end]
                # Group requests sharing a time; never invent within-time click ordering.
                groups=[]
                for r in hist:
                    if not groups or groups[-1]['time_index']!=r[1]:groups.append({'time_index':r[1],'requests':[]})
                    groups[-1]['requests'].append({'sid':r[0],'exposures':r[2],'clicked_item_ids':json.loads(r[4])})
                unique={i for r in hist for i in json.loads(r[4])}
                snapshot={'sid':sid,'uid':uid,'partition':sp,'ordinal':ordinal,'request_time_index':t,'history_cutoff_inclusive':end,'old':statistics(old),'new':{**statistics(hist),'unique_clicked_items':len(unique)},'recent20_including_boundary_ties':{**statistics(recent),'start_time_inclusive':recent_end},'history_time_groups':groups}
                f.write(json.dumps(snapshot,separators=(',',':'))+'\n')
        print('snapshots',sp,len(targets),flush=True)
    db.close();write(OUT/'BUILT.json',{'seconds':time.time()-started,'snapshot_hashes':{sp:digest(OUT/f'{sp}_snapshots.jsonl') for sp in ['train','dev','test']}})

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['index','build']);a=p.parse_args();globals()[a.phase]()
