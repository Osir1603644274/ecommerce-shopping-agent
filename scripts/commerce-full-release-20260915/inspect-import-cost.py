"""Read-only statement costs and query-plan shape; no source titles or credentials."""
import json
import pymysql
from prepare import OUT
secret=json.loads((OUT/'connection.private.json').read_text())
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},autocommit=True)
with db.cursor() as c:
    c.execute("SELECT LEFT(DIGEST_TEXT,200),COUNT_STAR,ROUND(SUM_TIMER_WAIT/1000000000000,2),ROUND(AVG_TIMER_WAIT/1000000000,2),SUM_ROWS_EXAMINED FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME='commerce_candidate' ORDER BY SUM_TIMER_WAIT DESC LIMIT 12")
    print(json.dumps(dict(statements=c.fetchall()),default=str))
    c.execute('SHOW INDEX FROM product');print(json.dumps(dict(productIndexes=c.fetchall()),default=str))
    ids=[str(2000000+i*100) for i in range(1000)]
    c.execute('EXPLAIN SELECT id,source_item_id FROM product WHERE source=%s AND source_item_id IN ('+','.join(['%s']*len(ids))+')',('kuaisearch',*ids))
    print(json.dumps(dict(identityLookupPlan=c.fetchall()),default=str))
    c.execute('EXPLAIN SELECT id FROM product WHERE id IN ('+','.join(['%s']*len(ids))+')',tuple(4000000000000000+int(x) for x in ids))
    print(json.dumps(dict(primaryCollisionPlan=c.fetchall()),default=str))
db.close()
