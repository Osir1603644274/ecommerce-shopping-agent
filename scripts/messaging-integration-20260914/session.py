"""Main-project integration evidence helpers. Never print deployment secrets."""
import datetime,hashlib,json,pathlib,subprocess,sys
ROOT=pathlib.Path('F:/agent')
OUT=ROOT/'docs/messaging-integration-20260914'
OUT.mkdir(parents=True,exist_ok=True)
def run(args,**kwargs):
    return subprocess.run(args,capture_output=True,check=True,**kwargs).stdout
def sql(query):
    raw=run(['docker','exec','local-life-mysql','sh','-c','MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql --default-character-set=utf8mb4 -uroot -D local_life --batch --raw -e "$1"','sh',query],text=True,encoding='utf-8')
    lines=raw.strip().splitlines()
    return [dict(zip(lines[0].split('\t'),x.split('\t'))) for x in lines[1:]] if lines else []
def redis(*args):
    return json.loads(run(['docker','exec','local-life-redis','redis-cli','--json',*args],text=True))
def write(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
def hashes(base):
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in base.rglob('*.py') if '__pycache__' not in p.parts}
if __name__=='__main__' and sys.argv[1]=='prepare':
    backup=ROOT/'.migration-backups'/('mq-main-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    inspect=json.loads(run(['docker','inspect','local-life-backend','local-life-mysql','local-life-redis','local-life-kafka'],text=True))
    (backup/'container-inspect-private.json').write_text(json.dumps(inspect,indent=2),encoding='utf-8')
    for rel in ['compose.merged-commerce.yml','scripts/merged-commerce.ps1','.env']:
        p=ROOT/rel
        if p.is_file():
            dest=backup/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(p.read_bytes())
    dump=run(['docker','exec','local-life-mysql','sh','-c','MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot --single-transaction --skip-lock-tables --no-tablespaces --set-gtid-purged=OFF local_life'])
    (backup/'local_life-before.sql').write_bytes(dump)
    write('backup.json',{'path':str(backup),'mysql_dump_sha256':hashlib.sha256(dump).hexdigest()})
    write('python-before.json',hashes(ROOT/'agent/app')|hashes(ROOT/'agent/tests'))
    write('backend-source-before.json',{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'backend/src').rglob('*') if p.is_file()})
    entries=redis('XRANGE','stream.flash-sale-orders','-','+')
    write('legacy-stream-before.json',entries)
    write('legacy-groups-before.json',redis('XINFO','GROUPS','stream.flash-sale-orders'))
    orders={r['id']:r for r in sql('SELECT id,campaign_id,user_id,amount_minor,status FROM flash_sale_order')}
    mismatches=[];bootstrap=0;matched=0
    for mid,fields in entries:
        f=dict(zip(fields[::2],fields[1::2]))
        if f.get('bootstrap')=='true':bootstrap+=1;continue
        o=orders.get(f.get('orderId'))
        if o and all(o[a]==f[b] for a,b in [('campaign_id','campaignId'),('user_id','userId'),('amount_minor','amountMinor')]):matched+=1
        else:mismatches.append({'message':mid,'fields':f,'order':o})
    write('legacy-reconciliation.json',{'entries':len(entries),'matched_orders':matched,'bootstrap':bootstrap,'mismatches':mismatches})
    for name,query in {
        'requests-before':'SELECT * FROM flash_sale_request',
        'orders-before':'SELECT id,user_id,status,total_amount_minor,updated_at FROM customer_order',
        'fulfillment-before':'SELECT id,status,updated_at FROM fulfillment_task',
        'product-before':'SELECT id,title,entity_version,lifecycle_status FROM product ORDER BY id',
        'activity-before':'SELECT COUNT(*) AS active_transactions FROM information_schema.innodb_trx',
        'flash-orders-before':'SELECT id,campaign_id,user_id,amount_minor,status FROM flash_sale_order ORDER BY id',
    }.items():
        try:write(name+'.json',sql(query))
        except subprocess.CalledProcessError as e:write(name+'-query-error.json',{'error':e.stderr})
    print(json.dumps({'backup':str(backup),'stream_entries':len(entries),'matched':matched,'bootstrap':bootstrap,'mismatches':len(mismatches)}))
