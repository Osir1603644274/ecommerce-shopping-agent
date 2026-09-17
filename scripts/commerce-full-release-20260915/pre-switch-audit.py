"""Compare all old business rows with current original DB before choosing a clone."""
import json,time
import pymysql
from prepare import OUT,cmd,write,refresh_connections

baseline=json.loads((OUT/'original-counts.json').read_text())
refresh_connections()
secret=json.loads((OUT/'connection.private.json').read_text());assert secret['database']=='commerce_candidate'
original=json.loads(cmd('inspect','local-life-mysql'))[0]
env=dict(v.split('=',1) for v in original['Config']['Env'] if '=' in v)
for name in ('local-life-backend','local-life-agent'):
    assert not json.loads(cmd('inspect',name))[0]['State']['Running'],'old writers must be stopped'
assert not original['State']['Running'],'original DB changed from stopped baseline; investigate before audit'
candidate=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},
    charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,autocommit=True)
cmd('start','local-life-mysql')
source=None;flags=None;checks={}
def stable(rows):return sorted(json.dumps(r,sort_keys=True,default=str,ensure_ascii=False) for r in rows)
try:
    for _ in range(90):
        try:
            source=pymysql.connect(host='127.0.0.1',port=13306,user='root',password=env['MYSQL_ROOT_PASSWORD'],
                database=env['MYSQL_DATABASE'],charset='utf8mb4',connect_timeout=3,
                cursorclass=pymysql.cursors.DictCursor,autocommit=True);break
        except pymysql.MySQLError:time.sleep(1)
    assert source is not None
    with source.cursor() as c:
        c.execute('SELECT @@global.read_only ro,@@global.super_read_only sro');flags=c.fetchone()
        c.execute('SET GLOBAL super_read_only=ON')
    for table,n in baseline.items():
        with source.cursor() as c:c.execute('SELECT * FROM `'+table+'`');rows=c.fetchall()
        assert len(rows)==n,table+' changed count since backup'
        with candidate.cursor() as c:
            if table in ('product','inventory_stock','product_local_offer'):
                key='product_id' if table=='product_local_offer' else 'id';ids=[r[key] for r in rows]
                c.execute('SELECT * FROM `'+table+'` WHERE `'+key+'` IN ('+','.join(['%s']*len(ids))+')',ids)
            else:c.execute('SELECT * FROM `'+table+'`')
            assert stable(c.fetchall())==stable(rows),table+' changed content since backup'
        checks[table]=len(rows)
    write('PRE-SWITCH-AUDIT.json',dict(status='PASS',checkedAtUnix=time.time(),comparedAllOriginalBusinessRows=True,tableCounts=checks))
    print('Every original business row matches candidate baseline; no intervening writes lost.',flush=True)
finally:
    if source:
        if flags:
            with source.cursor() as c:
                if not flags['sro']:c.execute('SET GLOBAL super_read_only=OFF')
                if not flags['ro']:c.execute('SET GLOBAL read_only=OFF')
        source.close()
    candidate.close();cmd('stop','-t','30','local-life-mysql')
