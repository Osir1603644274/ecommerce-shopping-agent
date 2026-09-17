"""Read-only measurements against the actual local MySQL; no connection secrets saved."""
import json, subprocess, time, statistics, hashlib, re
from pathlib import Path
import pymysql

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/product-query-audit-20260914'
OUT.mkdir(parents=True,exist_ok=True)
env=json.loads(subprocess.check_output(['docker','inspect','local-life-mysql'],text=True))[0]['Config']['Env']
secrets=dict(x.split('=',1) for x in env if '=' in x)
db=pymysql.connect(host='127.0.0.1',port=13306,user='root',password=secrets['MYSQL_ROOT_PASSWORD'],database='local_life',charset='utf8mb4',autocommit=True,read_timeout=8)
cur=db.cursor()
cur.execute('SET SESSION MAX_EXECUTION_TIME=2000')
cur.execute('SET SESSION TRANSACTION READ ONLY')
columns=re.search(r'String COLUMNS = """(.*?)"""', (ROOT/'backend/src/main/java/com/example/locallife/product/ProductMapper.java').read_text(),re.S)[1].strip()
def query(sql,args=()):
    cur.execute(sql,args);return cur.fetchall()
ids=[r[0] for r in query("SELECT id FROM product WHERE lifecycle_status='ACTIVE' ORDER BY id LIMIT 100")]
def baseline(selected):
    products=[];offers=[]
    for pid in selected:
        products.extend(query('SELECT '+columns+' FROM product WHERE id=%s',(pid,)))
        offers.extend([(pid,*r) for r in query('SELECT price_minor,currency,price_kind,version FROM product_local_offer WHERE product_id=%s',(pid,))])
    return products,sorted({r[0] for r in offers})
def batch(selected):
    slots=','.join(['%s']*len(selected))
    products=query('SELECT '+columns+' FROM product WHERE id IN ('+slots+')',selected)
    offers=query('SELECT product_id FROM product_local_offer WHERE product_id IN ('+slots+')',selected)
    return sorted(products),sorted({r[0] for r in offers})
results={'scope':'actual MySQL read-only; single connection; warm paired SQL paths, not HTTP/load percentile','counts':query('SELECT lifecycle_status,COUNT(*) FROM product GROUP BY lifecycle_status'),'cases':{}}
for n in (20,100):
    old,new=baseline(ids[:n]),batch(ids[:n]);assert (sorted(old[0]),sorted(old[1]))==new
    times={'before':[],'after':[]}
    for i in range(12):
        for name,fn in ([('before',baseline),('after',batch)] if i%2==0 else [('after',batch),('before',baseline)]):
            start=time.perf_counter();fn(ids[:n]);times[name].append((time.perf_counter()-start)*1000)
    results['cases'][str(n)]={'equal_products_and_offers':True,'before_sql_count':2*n,'after_sql_count':2,'raw_ms':times,'median_ms':{k:statistics.median(v) for k,v in times.items()}}
queries={
 'primary_key':('SELECT '+columns+' FROM product WHERE id=%s',(ids[0],)),
 'category_filter':('SELECT '+columns+" FROM product WHERE lifecycle_status='ACTIVE' AND (category_l1=%s OR category_l2 LIKE CONCAT('%%',%s,'%%') OR category_l3 LIKE CONCAT('%%',%s,'%%')) ORDER BY id LIMIT 20",('手机','手机','手机')),
 'no_match_keyword':('SELECT '+columns+" FROM product WHERE lifecycle_status='ACTIVE' AND (LOWER(title) LIKE CONCAT('%%',LOWER(%s),'%%') OR LOWER(COALESCE(attribute_text,'')) LIKE CONCAT('%%',LOWER(%s),'%%')) ORDER BY id LIMIT 20",('不存在的测试商品xyz','不存在的测试商品xyz')),
 'brand_filter':('SELECT '+columns+" FROM product WHERE lifecycle_status='ACTIVE' AND LOWER(brand)=LOWER(%s) ORDER BY id LIMIT 20",('vivo',))}
results['plans']={}
for name,(sql,args) in queries.items():
    plan=query('EXPLAIN ANALYZE '+sql,args)
    times=[]
    for _ in range(7):
        start=time.perf_counter();rows=query(sql,args);times.append((time.perf_counter()-start)*1000)
    results['plans'][name]={'sql':sql,'parameters':args,'plan':plan,'rows_returned':len(rows),'raw_ms':times,'median_ms':statistics.median(times)}
(OUT/'mysql-measurements.json').write_text(json.dumps(results,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
print(json.dumps({'cases':{k:{a:b for a,b in v.items() if a!='raw_ms'} for k,v in results['cases'].items()},'queries':{k:{'median_ms':v['median_ms'],'rows':v['rows_returned']} for k,v in results['plans'].items()}},ensure_ascii=False,indent=2))
db.close()
