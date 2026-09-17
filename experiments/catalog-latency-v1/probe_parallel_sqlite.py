"""Isolate parallel scheduling from the already checked mmap storage change."""
import json
from pathlib import Path
import statistics
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from agent.app.catalog_fast_retrieval import lexical_query,old
from agent.app.catalog_fast_retrieval_v3 import LexicalReader,configure_read

import argparse
parser=argparse.ArgumentParser();parser.add_argument('--out',default='sqlite-eval002');args=parser.parse_args()
ROOT=Path('D:/agent-datasets/catalog-latency-20260913-v1');out=ROOT/args.out;out.mkdir(exist_ok=False)
queries=old.read_json(ROOT/'dev-eval003/CONTRACT.json')['queries']
baseline=old.read_json(ROOT/'dev-eval003/LEXICAL.json');rows=[]
dbs={'catalog':old.readonly(old.OLD_ROOT/'catalog.sqlite')}
dbs.update({s:old.readonly(old.OLD_ROOT/'indexes'/s/'lexical.sqlite') for s in old.SOURCES})
for db in dbs.values():configure_read(db)
reader=LexicalReader(old.OLD_ROOT)
old.write_once(out/'CONTRACT.json',{'queries':queries,'baselineSha256':old.sha(ROOT/'dev-eval003/LEXICAL.json'),
    'change':'mapped serial versus mapped two-channel parallel; same SQL/labels/model; alternating order',
    'codeSha256':old.sha(Path(__file__).resolve().parents[2]/'agent/app/catalog_fast_retrieval_v3.py')})
try:
    for i,q in enumerate(queries):
        for mode in (['serial','parallel'] if i%2==0 else ['parallel','serial']):
            start=time.perf_counter()
            if mode=='serial':result,timing=lexical_query(dbs[q['source']],dbs['catalog'],q['source'],q['query'])
            else:result,timing=reader.collect(reader.submit(q['source'],q['query']))
            row={'query_id':q['query_id'],'mode':mode,'seconds':time.perf_counter()-start,
                'exactMatch':result==baseline[q['query_id']]['channels'],'timings':timing}
            rows.append(row);old.write_once(out/(q['query_id']+'-'+mode+'.json'),row)
            if not row['exactMatch']:raise ValueError('lexical_results_changed')
        print(json.dumps({'query':i+1,'seconds':{r['mode']:round(r['seconds'],3) for r in rows[-2:]}}),flush=True)
    old.write_once(out/'RESULTS.json',{'status':'PASS','queries':40,'allExactMatch':True,
        'timings':{m:{'mean':statistics.mean(r['seconds'] for r in rows if r['mode']==m),
            'median':statistics.median(r['seconds'] for r in rows if r['mode']==m)} for m in ['serial','parallel']}})
finally:
    reader.close()
    for db in dbs.values():db.close()
