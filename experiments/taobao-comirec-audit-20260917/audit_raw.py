"""Bounded-memory CSV/ZIP audit. Never extracts archive paths or trains a model."""
import argparse
import csv
import hashlib
import io
import json
import sqlite3
import time
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

START, END = 1511539200, 1512316800  # 2017-11-25 to 2017-12-04, UTC+8, end exclusive


@contextmanager
def rows(path):
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if Path(n).name.lower() == 'userbehavior.csv']
            if len(names) != 1:
                raise ValueError(f'Expected one UserBehavior.csv member, got {names}')
            with z.open(names[0]) as raw, io.TextIOWrapper(raw, encoding='utf-8-sig') as f:
                yield csv.reader(f)
    else:
        with path.open(encoding='utf-8-sig', newline='') as f:
            yield csv.reader(f)


def audit(path, out, max_rows=100000):
    out.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(out / 'audit.sqlite')
    db.execute('PRAGMA cache_size=-32768')
    db.execute('PRAGMA temp_store=FILE')
    db.execute('CREATE TABLE events(u INTEGER,i INTEGER,c INTEGER,b TEXT,t INTEGER,PRIMARY KEY(u,i,c,b,t)) WITHOUT ROWID')
    counts = Counter(); types = Counter(); sample=[]; batch=[]; t0=time.perf_counter()
    exhausted = True
    with rows(path) as reader:
        for n, row in enumerate(reader, 1):
            if max_rows and n > max_rows:
                exhausted=False;break
            counts['rows'] += 1
            try:
                if len(row) != 5: raise ValueError('field_count')
                u,i,c,b,t=int(row[0]),int(row[1]),int(row[2]),row[3],int(row[4])
                if min(u,i,c) < 0 or b not in {'pv','buy','cart','fav'}: raise ValueError('value')
            except ValueError:
                counts['invalid'] += 1;continue
            types[b]+=1;counts['valid']+=1
            if not START <= t < END: counts['outside_declared_time']+=1
            if len(sample)<5: sample.append(row)
            batch.append((u,i,c,b,t))
            if len(batch)>=10000:
                db.executemany('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',batch);db.commit();batch=[]
    db.executemany('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?)',batch);db.commit()
    unique=db.execute('SELECT count(*) FROM events').fetchone()[0]
    stats={'unique_events':unique,'exact_duplicate_events':counts['valid']-unique}
    for col,name in [('u','users'),('i','items'),('c','categories')]:
        stats[name]=db.execute(f'SELECT count(DISTINCT {col}) FROM events').fetchone()[0]
    stats['timestamp_min_max']=db.execute('SELECT min(t),max(t) FROM events').fetchone()
    stats['user_pv_count_distribution']=[list(r) for r in db.execute("SELECT n,count(*) FROM (SELECT u,count(*) n FROM events WHERE b='pv' GROUP BY u) GROUP BY n ORDER BY n")]
    db.close()
    result={'input':str(path),'scope':'full_file' if exhausted else 'prefix_only_not_representative',
            'row_limit':max_rows,'counts':dict(counts),'behaviors':dict(types),**stats,
            'raw_examples':sample,'seconds':time.perf_counter()-t0,
            'warning':'Prefix audit cannot establish whole-dataset sparsity, leakage, cold-start or model quality.'}
    (out/'RESULT.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--input',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--max-rows',type=int,default=100000)
    a=p.parse_args()
    if a.max_rows < 0: p.error('--max-rows must be >= 0')
    print(json.dumps(audit(a.input,a.out,a.max_rows),ensure_ascii=False,indent=2))
