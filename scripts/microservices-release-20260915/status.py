"""Small read-only progress snapshot; never print database credentials or SQL values."""
import json
import pymysql
from topology_lab import root_connection
from live_release import SCHEMAS,STATE

root=root_connection()
with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},autocommit=True,cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as c:
    c.execute('SELECT ID,DB,COMMAND,TIME,STATE FROM information_schema.processlist WHERE COMMAND<>\'Sleep\'')
    processes=c.fetchall()
    c.execute('SELECT table_schema,COUNT(*) AS tables FROM information_schema.tables WHERE table_schema IN (%s,%s,%s) GROUP BY table_schema',tuple(SCHEMAS.values()))
    tables=c.fetchall()
print(json.dumps({'release':json.loads(STATE.read_text(encoding='utf8'))['status'] if STATE.exists() else 'NOT_SELECTED','activeDatabaseOperations':processes,'schemas':tables}))
