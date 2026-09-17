"""Temporary owned import resource budget; durability is never relaxed."""
import json,shutil
import pymysql
from prepare import OUT,cmd,write
secret=json.loads((OUT/'connection.private.json').read_text())
name=secret['container'];info=json.loads(cmd('inspect',name))[0]
assert info['Config']['Labels']['commerce.release']=='20260915-a1'
assert secret['database']=='commerce_candidate'
assert shutil.disk_usage('E:/').free>15*1024**3
cmd('update','--memory=3g','--cpus=3',name)
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},autocommit=True)
with db.cursor() as c:
    c.execute("SHOW GLOBAL VARIABLES WHERE Variable_name IN ('innodb_buffer_pool_size','innodb_redo_log_capacity','innodb_flush_log_at_trx_commit','sync_binlog')")
    before=dict(c.fetchall())
    assert before['innodb_flush_log_at_trx_commit']=='1' and before['sync_binlog']=='1'
    c.execute('SET GLOBAL innodb_buffer_pool_size=1610612736')
    c.execute('SET GLOBAL innodb_redo_log_capacity=1073741824')
    c.execute("SHOW GLOBAL VARIABLES WHERE Variable_name IN ('innodb_buffer_pool_size','innodb_redo_log_capacity','innodb_flush_log_at_trx_commit','sync_binlog')")
    after=dict(c.fetchall())
db.close()
write('IMPORT-RESOURCE-BUDGET.json',dict(before=before,after=after,containerMemory='3g',cpus=3,
    reason='isolated Agent stopped; reduce random-page IO during bulk import',persistent=False))
print(json.dumps(after))
