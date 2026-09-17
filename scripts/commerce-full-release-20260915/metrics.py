"""Read only, low-cost release capacity observations. Never print connections."""
import json
import pymysql
from prepare import OUT,cmd,write

secret=json.loads((OUT/'connection.private.json').read_text())
assert secret['database']=='commerce_candidate'
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},autocommit=True)
with db.cursor() as c:
    c.execute("SHOW GLOBAL STATUS WHERE Variable_name IN ('Innodb_buffer_pool_reads','Innodb_buffer_pool_read_requests','Innodb_data_fsyncs','Innodb_data_reads','Innodb_data_writes','Innodb_rows_inserted','Threads_running','Com_insert','Com_commit','Innodb_log_waits','Innodb_buffer_pool_wait_free','Innodb_data_pending_fsyncs','Innodb_buffer_pool_pages_dirty','Innodb_buffer_pool_pages_flushed')")
    status=dict(c.fetchall())
    c.execute("SHOW GLOBAL VARIABLES WHERE Variable_name IN ('innodb_buffer_pool_size','innodb_flush_log_at_trx_commit','sync_binlog','max_allowed_packet')")
    variables=dict(c.fetchall())
    c.execute("SELECT source,source_line,status,updated_at FROM external_catalog_import_checkpoint")
    checkpoints=c.fetchall()
    c.execute("SELECT LEFT(DIGEST_TEXT,90),COUNT_STAR,ROUND(SUM_TIMER_WAIT/1000000000000,2),ROUND(AVG_TIMER_WAIT/1000000000,2),SUM_ROWS_AFFECTED FROM performance_schema.events_statements_summary_by_digest WHERE SCHEMA_NAME='commerce_candidate' AND DIGEST_TEXT LIKE 'INSERT INTO%' ORDER BY SUM_TIMER_WAIT DESC LIMIT 6")
    statements=c.fetchall()
    c.execute("SELECT ID,TIME,STATE FROM information_schema.PROCESSLIST WHERE DB='commerce_candidate' AND COMMAND<>'Sleep'")
    active=c.fetchall()
db.close()
print(json.dumps(dict(status=status,variables=variables,checkpoints=checkpoints,statements=statements,active=active),default=str))
