"""Hand idle MySQL RAM back during disk staging, then restore the prior import budget."""
import json,sys
import pymysql
from prepare import OUT,cmd,write
secret=json.loads((OUT/'connection.private.json').read_text());assert secret['database']=='commerce_candidate'
assert json.loads(cmd('inspect',secret['container']))[0]['Config']['Labels']['commerce.release']=='20260915-a1'
action=sys.argv[1];assert action in ('prepare','import')
target=536870912 if action=='prepare' else 1073741824
record='ORDERED-MEMORY-'+action+'.json';assert not (OUT/record).exists()
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},autocommit=True)
with db.cursor() as c:
    if action=='prepare':
        c.execute("SELECT source_line,status FROM external_catalog_import_checkpoint WHERE source='kuaisearch'")
        assert c.fetchone()==(4237000,'IMPORTING')
    else:assert json.loads((OUT/'ORDERED-TAIL-MANIFEST.json').read_text())['status']=='SEALED'
    sql="SHOW GLOBAL VARIABLES WHERE Variable_name IN ('innodb_buffer_pool_size','innodb_flush_log_at_trx_commit','sync_binlog')"
    c.execute(sql);before=dict(c.fetchall());assert before['innodb_flush_log_at_trx_commit']==before['sync_binlog']=='1'
    c.execute('SET GLOBAL innodb_buffer_pool_size='+str(target));c.execute(sql);after=dict(c.fetchall())
db.close();write(record,dict(before=before,after=after,reason='temporary memory handoff between disk staging and MySQL import',persistent=False))
print(json.dumps(after))
