"""Fresh frozen-source physical backup and independent-volume restore proof.

Only explicit --frozen runs; requires both live writers stopped. Never restores
over the source. A failed attempt keeps its archive and owned restore volume.
"""
import hashlib
import json
import shutil
import socket
import sys
import time
import pymysql
from topology_lab import ROOT,OUT,root_connection,docker,write

def main():
    assert sys.argv[1:]==['--frozen'],'explicit frozen-source operation required'
    root=root_connection();name=root['container'];schema=root['database']
    live=json.loads(docker('inspect','commerce-full-live-java-20260915'))[0]
    assert live['Config']['Labels'].get('commerce.release')=='20260915-a1' and not live['State']['Running']
    with socket.socket() as probe:assert probe.connect_ex(('127.0.0.1',8000))!=0,'Agent writer must be frozen'
    # No second JVM may still write this database during a physical snapshot.
    for name_running in docker('ps','--format','{{.Names}}').splitlines():
        info=json.loads(docker('inspect',name_running))[0]
        env=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
        assert env.get('DB_NAME')!=schema,'active database writer: '+name_running
    name=root['container'];info=json.loads(docker('inspect',name))[0]
    volume=next(m['Name'] for m in info['Mounts'] if m['Destination']=='/var/lib/mysql')
    assert volume=='commerce-full-candidate-20260915-data'
    backup=OUT/'frozen-live-mysql.tar.gz';restore='micro-live-restore-20260915';restore_volume=restore+'-data'
    assert not backup.exists() and restore not in docker('ps','-a','--format','{{.Names}}').splitlines(),'never overwrite an attempt'
    assert restore_volume not in docker('volume','ls','--format','{{.Name}}').splitlines()
    size=int(docker('exec',name,'du','-sb','/var/lib/mysql').split()[0])
    assert shutil.disk_usage('D:/').free>size+5*1024**3
    assert shutil.disk_usage('E:/').free>size+3*1024**3,'restore needs actual Docker-disk headroom'
    counts={};small_hashes={}
    with pymysql.connect(**{k:root[k] for k in ('host','port','user','password')},database=schema,autocommit=True,read_timeout=1800) as db,db.cursor() as c:
        c.execute('SHOW TABLES');tables=[r[0] for r in c.fetchall()]
        for table in tables:
            c.execute('SELECT COUNT(*) FROM `'+table+'`');counts[table]=c.fetchone()[0]
            if counts[table]<=100000:
                c.execute('SELECT * FROM `'+table+'`')
                rows=sorted(json.dumps(row,default=str,ensure_ascii=False) for row in c.fetchall())
                small_hashes[table]=hashlib.sha256('\n'.join(rows).encode()).hexdigest()
    report={'status':'BACKING_UP','sourceSchema':schema,'sourceContainer':name,'sourceVolume':volume,'sourceBytes':size,
            'tableCounts':counts,'smallTableHashes':small_hashes,'restoreVolume':restore_volume,'frozenAtUnix':time.time()}
    write('FROZEN-LIVE-BACKUP.json',report)
    docker('stop','-t','90',name)
    try:
        code="import tarfile; t=tarfile.open('/backup/frozen-live-mysql.tar.gz','x:gz',compresslevel=1); t.add('/source',arcname='.'); t.close()"
        docker('run','--rm','--pull=never','--label','microservices.attempt=20260915-a1','--memory','256m','--cpus','2',
               '--mount','type=volume,src='+volume+',dst=/source,readonly','--mount','type=bind,src='+str(OUT)+',dst=/backup',
               'python:3.12-slim','python','-c',code)
    finally:docker('start',name)
    checksum=hashlib.sha256()
    with backup.open('rb') as f:
        while block:=f.read(4*1024**2):checksum.update(block)
    report.update(status='RESTORING',backupSha256=checksum.hexdigest(),backupBytes=backup.stat().st_size)
    write('FROZEN-LIVE-BACKUP.json',report)
    docker('volume','create','--label','microservices.attempt=20260915-a1',restore_volume)
    docker('run','--rm','--pull=never','--label','microservices.attempt=20260915-a1','--memory','256m',
           '--mount','type=volume,src='+restore_volume+',dst=/restore','--mount','type=bind,src='+str(OUT)+',dst=/backup,readonly',
           'python:3.12-slim','tar','--numeric-owner','-xzpf','/backup/frozen-live-mysql.tar.gz','-C','/restore')
    docker('run','-d','--pull=never','--name',restore,'--label','microservices.attempt=20260915-a1','--memory','512m','--cpus','1',
           '-p','127.0.0.1::3306','--mount','type=volume,src='+restore_volume+',dst=/var/lib/mysql','mysql:8.4',
           '--innodb-buffer-pool-size=134217728','--skip-log-bin')
    port=int(json.loads(docker('inspect',restore))[0]['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort'])
    try:
        for _ in range(120):
            try:
                db=pymysql.connect(host='127.0.0.1',port=port,user=root['user'],password=root['password'],database=schema,
                                   connect_timeout=3,read_timeout=1800,autocommit=True);break
            except pymysql.MySQLError:time.sleep(1)
        else:raise RuntimeError('restore did not start')
        with db,db.cursor() as c:
            c.execute('SET GLOBAL super_read_only=ON')
            for table,count in counts.items():
                c.execute('SELECT COUNT(*) FROM `'+table+'`');assert c.fetchone()[0]==count,table
                if table in small_hashes:
                    c.execute('SELECT * FROM `'+table+'`')
                    rows=sorted(json.dumps(row,default=str,ensure_ascii=False) for row in c.fetchall())
                    assert hashlib.sha256('\n'.join(rows).encode()).hexdigest()==small_hashes[table],table
        report.update(status='PASS',restoreContainer=restore,restoredAllCountsMatch=True,restoredSmallTableContentsMatch=True)
        write('FROZEN-LIVE-BACKUP.json',report);print('Fresh backup and independent-volume restore PASS.',flush=True)
    finally:docker('stop','-t','60',restore)

if __name__=='__main__':main()
