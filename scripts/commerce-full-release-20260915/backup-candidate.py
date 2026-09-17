"""Cold physical snapshot, independent-volume restore and table-count verification."""
import hashlib,json,shutil,time
import pymysql
from prepare import OUT,NAME,DB,cmd,write

report=json.loads((OUT/'FINAL-INVARIANTS.json').read_text(encoding='utf8'));assert report['status']=='PASS'
secret=json.loads((OUT/'connection.private.json').read_text());assert secret['database']==DB
info=json.loads(cmd('inspect',NAME))[0];assert info['Config']['Labels']['commerce.release']=='20260915-a1'
volume=next(m['Name'] for m in info['Mounts'] if m['Destination']=='/var/lib/mysql')
assert volume==NAME+'-data'
backup=OUT/'candidate-mysql.tar.gz'
restore=NAME+'-restore';restore_volume=restore+'-data'
assert not backup.exists(),'existing snapshot must be inspected, never overwritten'
assert restore_volume not in cmd('volume','ls','--format','{{.Name}}').decode().splitlines()
db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},autocommit=True)
with db.cursor() as c:
    c.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s",(DB,))
    tables=[r[0] for r in c.fetchall()];counts={}
    for t in tables:c.execute('SELECT COUNT(*) FROM `'+t+'`');counts[t]=c.fetchone()[0]
db.close()
size=int(cmd('exec',NAME,'du','-sb','/var/lib/mysql').decode().split()[0])
assert shutil.disk_usage('D:/').free>size+10*1024**3
assert shutil.disk_usage('E:/').free>size+10*1024**3
stage=json.loads(cmd('inspect','commerce-full-stage-java-20260915'))[0]
assert stage['Config']['Labels']['commerce.release']=='20260915-a1'
if stage['State']['Running']:cmd('stop','-t','20','commerce-full-stage-java-20260915')
evidence=dict(status='BACKING_UP',sourceVolume=volume,sourceBytes=size,tableCounts=counts,restoreVolume=restore_volume)
write('CANDIDATE-BACKUP-RESTORE.json',evidence)
cmd('stop','-t','90',NAME)
try:
    # The source volume is cleanly stopped and mounted read-only; create a new archive only.
    code="import tarfile; t=tarfile.open('/backup/candidate-mysql.tar.gz','x:gz',compresslevel=1); t.add('/source',arcname='.'); t.close()"
    cmd('run','--rm','--pull=never','--label','commerce.release=20260915-a1','--memory=256m','--cpus=2',
        '--mount',f'type=volume,src={volume},dst=/source,readonly','--mount',f'type=bind,src={OUT},dst=/backup',
        'python:3.12-slim','python','-c',code)
finally:
    cmd('start',NAME)
h=hashlib.sha256()
with backup.open('rb') as f:
    while b:=f.read(4*1024**2):h.update(b)
evidence.update(status='RESTORING',backupSha256=h.hexdigest(),backupBytes=backup.stat().st_size)
write('CANDIDATE-BACKUP-RESTORE.json',evidence)
cmd('volume','create','--label','commerce.release=20260915-a1',restore_volume)
cmd('run','--rm','--pull=never','--label','commerce.release=20260915-a1','--memory=256m',
    '--mount',f'type=volume,src={restore_volume},dst=/restore','--mount',f'type=bind,src={OUT},dst=/backup,readonly',
    'python:3.12-slim','tar','--numeric-owner','-xzpf','/backup/candidate-mysql.tar.gz','-C','/restore')
cmd('run','-d','--pull=never','--name',restore,'--label','commerce.release=20260915-a1','--memory=1g','--cpus=1',
    '-p','127.0.0.1::3306','--mount',f'type=volume,src={restore_volume},dst=/var/lib/mysql','mysql:8.4',
    '--innodb-buffer-pool-size=134217728')
port=int(json.loads(cmd('inspect',restore))[0]['NetworkSettings']['Ports']['3306/tcp'][0]['HostPort'])
try:
    for _ in range(120):
        try:
            restored=pymysql.connect(host='127.0.0.1',port=port,user='root',password=secret['password'],database=DB,
                connect_timeout=3,read_timeout=1800,autocommit=True);break
        except pymysql.MySQLError:time.sleep(1)
    else:raise RuntimeError('restored MySQL failed readiness')
    with restored.cursor() as c:
        c.execute('SET GLOBAL super_read_only=ON')
        for t,expected in counts.items():
            c.execute('SELECT COUNT(*) FROM `'+t+'`');actual=c.fetchone()[0];assert actual==expected,(t,actual,expected)
        c.execute("SELECT COUNT(*) FROM external_catalog_import_checkpoint WHERE status='COMPLETE'");assert c.fetchone()[0]==2
    restored.close()
    evidence.update(status='PASS',allRestoredTableCountsMatch=True,restoreContainer=restore)
    write('CANDIDATE-BACKUP-RESTORE.json',evidence)
    print('Independent-volume MySQL restore PASS.',flush=True)
finally:
    cmd('stop','-t','60',restore)
