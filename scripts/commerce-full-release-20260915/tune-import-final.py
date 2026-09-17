"""One bounded buffer increase after all owned acceptance services are stopped."""
import json
import pymysql
from prepare import OUT,cmd,write

target=OUT/'IMPORT-RESOURCE-BUDGET-SECOND.json'
assert not target.exists(),'keep the original resource-change evidence'
secret=json.loads((OUT/'connection.private.json').read_text())
assert secret['database']=='commerce_candidate'
info=json.loads(cmd('inspect',secret['container']))[0]
assert info['Config']['Labels']['commerce.release']=='20260915-a1'
for service in ('java','warehouse','kafka','rabbit','redis'):
    assert not json.loads(cmd('inspect','commerce-full-stage-'+service+'-20260915'))[0]['State']['Running']
mem=dict(line.split(':',1) for line in cmd('exec',secret['container'],'cat','/proc/meminfo').decode().splitlines())
assert int(mem['MemAvailable'].split()[0])>3*1024**2
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},autocommit=True)
with db.cursor() as c:
    sql="SHOW GLOBAL VARIABLES WHERE Variable_name IN ('innodb_buffer_pool_size','innodb_redo_log_capacity','innodb_flush_log_at_trx_commit','sync_binlog')"
    c.execute(sql);before=dict(c.fetchall())
    assert before['innodb_buffer_pool_size']=='1610612736'
    assert before['innodb_flush_log_at_trx_commit']==before['sync_binlog']=='1'
    c.execute('SET GLOBAL innodb_buffer_pool_size=2147483648')
    c.execute(sql);after=dict(c.fetchall())
db.close()
write(target.name,dict(before=before,after=after,containerMemoryUnchanged=info['HostConfig']['Memory'],
    reason='all isolated test services stopped; add 512 MiB to buffer pool within the existing 3 GiB container limit',persistent=False))
print(json.dumps(after))
