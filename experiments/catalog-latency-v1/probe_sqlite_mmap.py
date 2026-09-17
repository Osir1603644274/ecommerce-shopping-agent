"""Compare SQLite storage access only; identical lexical SQL and row ordering."""
import json
from pathlib import Path
import statistics
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from agent.app.catalog_fast_retrieval import lexical_query,old

ROOT=Path('D:/agent-datasets/catalog-latency-20260913-v1')
out=ROOT/'sqlite-eval001';out.mkdir(exist_ok=False)
queries=json.loads((ROOT/'dev-eval003/CONTRACT.json').read_text('utf-8'))['queries']
baseline=json.loads((ROOT/'dev-eval003/LEXICAL.json').read_text('utf-8'))
rows=[]
connections={}
actual={}
for mode,size in [('default',0),('mmap',2147418112)]:
    dbs={'catalog':old.readonly(old.OLD_ROOT/'catalog.sqlite')}
    dbs.update({s:old.readonly(old.OLD_ROOT/'indexes'/s/'lexical.sqlite') for s in old.SOURCES})
    actual[mode]={}
    for name,db in dbs.items():
        db.execute('PRAGMA cache_size=-16384')
        actual[mode][name]=db.execute(f'PRAGMA mmap_size={size}').fetchone()[0]
    connections[mode]=dbs
old.write_once(out/'CONTRACT.json',{'queries':queries,'actualMmapBytes':actual,
    'baselineSha256':old.sha(ROOT/'dev-eval003/LEXICAL.json'),
    'change':'SQLite read storage only, identical tokens/SQL/ties/row IDs; alternate evaluation order'})
try:
    for i,q in enumerate(queries):
        for mode in (['default','mmap'] if i%2==0 else ['mmap','default']):
            dbs=connections[mode];start=time.perf_counter()
            result,timing=lexical_query(dbs[q['source']],dbs['catalog'],q['source'],q['query'])
            row={'query_id':q['query_id'],'source':q['source'],'mode':mode,'seconds':time.perf_counter()-start,
                 'exactMatch':result==baseline[q['query_id']]['channels'],'timings':timing}
            rows.append(row);old.write_once(out/(q['query_id']+'-'+mode+'.json'),row)
            if not row['exactMatch']:raise ValueError('lexical_results_changed')
        print(json.dumps({'query':i+1,'seconds':{r['mode']:round(r['seconds'],3) for r in rows[-2:]}}),flush=True)
    old.write_once(out/'RESULTS.json',{'status':'PASS','queries':40,'allExactMatch':True,
        'actualMmapBytes':actual,'timings':{m:{'mean':statistics.mean(r['seconds'] for r in rows if r['mode']==m),
            'median':statistics.median(r['seconds'] for r in rows if r['mode']==m)} for m in connections}})
finally:
    for dbs in connections.values():
        for db in dbs.values():db.close()
