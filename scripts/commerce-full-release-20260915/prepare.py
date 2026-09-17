"""Back up the existing app DB; restore to a NEW owned candidate. No live cutover."""
import gzip
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time
import pymysql

ROOT=Path(__file__).resolve().parents[2]
OUT=Path('D:/agent-datasets/commerce-full-release-20260915-attempt001')
NAME='commerce-full-candidate-20260915'
DB='commerce_candidate'


def cmd(*args):
    p=subprocess.run(['docker',*args],capture_output=True)
    if p.returncode:raise RuntimeError('docker '+args[0]+' failed (arguments withheld)')
    return p.stdout


def write(name,data):
    # Observers must never see half-written release gates or private receipts.
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',dir=OUT,prefix=name+'.',suffix='.tmp',delete=False) as pending:
        pending.write(json.dumps(data,ensure_ascii=False,indent=2,default=str)+'\n')
        pending.flush();os.fsync(pending.fileno());temporary=pending.name
    for attempt in range(10):
        try:
            os.replace(temporary,OUT/name);break
        except PermissionError:
            if attempt==9:raise
            time.sleep(0.05)


def refresh_connections():
    """Docker may reassign an ephemeral host port after a cold restart."""
    info=json.loads(cmd('inspect',NAME))[0]
    assert info['Config']['Labels'].get('commerce.release')=='20260915-a1'
    assert info['State']['Running']
    assert any(m.get('Name')==NAME+'-data' and m['Destination']=='/var/lib/mysql' for m in info['Mounts'])
    bindings=info['NetworkSettings']['Ports']['3306/tcp']
    assert len(bindings)==1 and bindings[0]['HostIp']=='127.0.0.1'
    port=int(bindings[0]['HostPort'])
    for name in ('connection.private.json','live-connection.private.json'):
        path=OUT/name
        if not path.exists():continue
        secret=json.loads(path.read_text(encoding='utf8'))
        assert secret['database']==DB and secret['host']=='127.0.0.1'
        if secret['port']!=port:
            secret['port']=port;write(name,secret)
    return port


def main():
    if OUT.exists():raise ValueError('existing attempt; use resume not overwrite')
    if NAME in cmd('ps','-a','--format','{{.Names}}').decode().splitlines():raise ValueError('container exists')
    if NAME+'-data' in cmd('volume','ls','--format','{{.Name}}').decode().splitlines():raise ValueError('volume exists')
    assert shutil.disk_usage('E:/').free>30*1024**3
    assert shutil.disk_usage('D:/').free>30*1024**3
    OUT.mkdir()
    original=json.loads(cmd('inspect','local-life-mysql'))[0]
    env=dict(v.split('=',1) for v in original['Config']['Env'] if '=' in v)
    started=not original['State']['Running']
    report={'phase':'BACKUP','originalWasRunning':not started,'originalImage':original['Image']}
    try:
        if started:cmd('start','local-life-mysql')
        for _ in range(60):
            try:
                db=pymysql.connect(host='127.0.0.1',port=13306,user=env['MYSQL_USER'],password=env['MYSQL_PASSWORD'],
                    database=env['MYSQL_DATABASE'],connect_timeout=3,read_timeout=30,cursorclass=pymysql.cursors.DictCursor)
                break
            except pymysql.MySQLError:time.sleep(1)
        else:raise RuntimeError('source DB unavailable')
        with db.cursor() as c:
            c.execute('SET TRANSACTION READ ONLY')
            c.execute('SHOW TABLES');tables=[next(iter(r.values())) for r in c.fetchall()]
            counts={}
            for t in tables:
                c.execute('SELECT COUNT(*) AS n FROM `'+t+'`');counts[t]=c.fetchone()['n']
            baseline={}
            for t in ('product','product_local_offer','inventory_stock','catalog_state'):
                c.execute('SELECT * FROM `'+t+'`');baseline[t]=c.fetchall()
        db.rollback();db.close()
        write('original-counts.json',counts);write('original-catalog.json',baseline)
        p=subprocess.Popen(['docker','exec','-e','MYSQL_PWD='+env['MYSQL_ROOT_PASSWORD'],'local-life-mysql',
            'mysqldump','-uroot','--single-transaction','--routines','--events','--no-tablespaces','--set-gtid-purged=OFF',env['MYSQL_DATABASE']],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        h=hashlib.sha256();raw_bytes=0
        with gzip.open(OUT/'original.sql.gz','wb') as f:
            while b:=p.stdout.read(1024*1024):f.write(b);h.update(b);raw_bytes+=len(b)
        if p.wait()!=0:raise RuntimeError('source backup failed')
        report.update(backupRawBytes=raw_bytes,backupRawSha256=h.hexdigest(),backupGzipBytes=(OUT/'original.sql.gz').stat().st_size)
    finally:
        if started:cmd('stop','-t','15','local-life-mysql')
        write('PREPARE.json',report)
    password=secrets.token_hex(24)
    image=json.loads(cmd('image','inspect','mysql:8.4'))[0]['Id']
    cmd('run','-d','--pull=never','--name',NAME,'--label','commerce.release=20260915-a1',
        '--cpus=2','--memory=2g','--pids-limit=512','-p','127.0.0.1::3306',
        '-e','MYSQL_ROOT_PASSWORD='+password,'-e','MYSQL_ROOT_HOST=%','-e','MYSQL_DATABASE='+DB,
        '-v',NAME+'-data:/var/lib/mysql',image,'--innodb-buffer-pool-size=536870912',
        '--innodb-redo-log-capacity=268435456','--max-connections=80')
    info=json.loads(cmd('inspect',NAME))[0];port=int(info['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort'])
    assert port not in (3306,13306)
    # Local connection material only; never copy this file into public evidence.
    write('connection.private.json',dict(host='127.0.0.1',port=port,user='root',password=password,database=DB,container=NAME))
    for _ in range(90):
        try:
            candidate=pymysql.connect(host='127.0.0.1',port=port,user='root',password=password,database=DB,
                connect_timeout=3,read_timeout=30,cursorclass=pymysql.cursors.DictCursor);break
        except pymysql.MySQLError:time.sleep(1)
    else:raise RuntimeError('candidate DB unavailable')
    p=subprocess.Popen(['docker','exec','-i','-e','MYSQL_PWD='+password,NAME,'mysql','-uroot',DB],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    with gzip.open(OUT/'original.sql.gz','rb') as f:
        while b:=f.read(1024*1024):p.stdin.write(b)
    p.stdin.close()
    if p.wait()!=0:raise RuntimeError('candidate restore failed')
    with candidate.cursor() as c:
        c.execute('SET TRANSACTION READ ONLY')
        for t,n in counts.items():
            c.execute('SELECT COUNT(*) AS n FROM `'+t+'`');assert c.fetchone()['n']==n,t
        for t,expected in baseline.items():
            c.execute('SELECT * FROM `'+t+'`')
            stable=lambda rows:sorted(json.dumps(r,sort_keys=True,default=str) for r in rows)
            assert stable(c.fetchall())==stable(expected),t
    candidate.rollback();candidate.close()
    report.update(phase='BACKUP_RESTORE_VERIFIED',candidateContainer=NAME,candidatePort=port,
                  originalTableCount=len(counts),allTableCountsMatch=True,oldCatalogExactMatch=True,
                  freeBytes={d:shutil.disk_usage(d+':/').free for d in 'DEF'})
    write('PREPARE.json',report)
    print(json.dumps(report),flush=True)


if __name__=='__main__':main()
