"""Read-only SQL equivalence/speed probe; concurrent build timings are exploratory."""
import json
from pathlib import Path
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'search-closure-v1'))
from retrieval_runtime import readonly,query_tokens

out=Path('D:/agent-datasets/catalog-latency-20260913-v1/lexical-probe.json')
rows=[]
for source in ['multicpr','kuaisearch']:
    db=readonly(Path('D:/agent-datasets/search-stage1-v1/indexes')/source/'lexical.sqlite')
    for query in ['抽屉式透明桌面收纳盒','卡骆驰真皮']:
        for channel,table in [('bm25','words'),('character','chars')]:
            terms=query_tokens(query)[channel];expression=' OR '.join('"'+t.replace('"','""')+'"' for t in terms)
            t=time.perf_counter();old=db.execute(f'SELECT rowid,bm25({table}) AS score FROM {table} WHERE {table} MATCH ? ORDER BY score,rowid LIMIT 300',(expression,)).fetchall();oldtime=time.perf_counter()-t
            t=time.perf_counter();limit=600
            while True:
                new=db.execute(f'SELECT rowid,rank FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?',(expression,limit)).fetchall()
                if len(new)<limit or len(new)<300 or new[-1][1]>sorted(new,key=lambda x:(x[1],x[0]))[299][1]:break
                limit*=2
                if limit>19200:raise RuntimeError('large boundary tie in exploratory probe')
            new=sorted(new,key=lambda x:(x[1],x[0]))[:300]
            row={'source':source,'query':query,'channel':channel,'oldSeconds':oldtime,'rankSeconds':time.perf_counter()-t,'same':old==new,'rowsFetched':limit}
            rows.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    db.close()
with out.open('x',encoding='utf-8') as f:json.dump({'scope':'exploratory, concurrent CPU indexing may interfere','rows':rows},f,ensure_ascii=False,indent=2)
