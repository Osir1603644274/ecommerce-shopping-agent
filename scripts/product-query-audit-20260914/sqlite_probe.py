"""Bounded read-only probes of the large retrieval stores; no index mutation."""
import sqlite3,time,json,statistics
from pathlib import Path
root=Path('D:/agent-datasets/search-stage1-v1')
out=Path(__file__).resolve().parents[2]/'docs/product-query-audit-20260914/sqlite-probe.json'
results={'scope':'read-only SQL probes, no model inference; warm reads, 5s cap per execution','sources':{}}
for source in ('kuaisearch','multicpr'):
    path=root/'indexes'/source/'lexical.sqlite'
    db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    db.execute('PRAGMA query_only=ON');db.execute('PRAGMA mmap_size=2147418112');db.execute('PRAGMA cache_size=-16384')
    entry={'path':str(path),'size_gib':path.stat().st_size/2**30,'queries':{}}
    for expression in ('"手机"','"收纳"','"不存在xyz商品"'):
        sql='SELECT rowid,rank FROM words WHERE words MATCH ? ORDER BY rank LIMIT 600'
        plan=db.execute('EXPLAIN QUERY PLAN '+sql,(expression,)).fetchall();times=[];error=None;rows=[]
        for _ in range(3):
            start=time.perf_counter();db.set_progress_handler(lambda: int(time.perf_counter()-start>5),10000)
            try:rows=db.execute(sql,(expression,)).fetchall();times.append((time.perf_counter()-start)*1000)
            except sqlite3.OperationalError as exc:error=str(exc);break
        entry['queries'][expression]={'plan':plan,'rows':len(rows),'raw_ms':times,'median_ms':statistics.median(times) if times else None,'error':error}
    db.close();results['sources'][source]=entry
out.write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(results,ensure_ascii=False,indent=2))
