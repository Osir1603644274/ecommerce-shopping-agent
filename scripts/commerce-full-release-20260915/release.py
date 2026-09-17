"""Validated local release activation and repeatable startup. No destructive rollback."""
import json,secrets,sys,time,subprocess
import pymysql,httpx
from prepare import ROOT,OUT,NAME,DB,cmd,write,refresh_connections

LIVE='commerce-full-live-java-20260915'
IMAGE='agent-backend:commerce-full-20260915-v3'
STATE=ROOT/'.runtime/merged-commerce/full-catalog-release.json'

def owned(name):
    info=json.loads(cmd('inspect',name))[0]
    assert info['Config']['Labels'].get('commerce.release')=='20260915-a1'
    return info

def ensure_backend():
    if (ROOT/'.runtime/merged-commerce/microservices-release.json').exists() or (ROOT/'.runtime/merged-commerce/microservices-maintenance.json').exists():
        raise RuntimeError('Microservices data ownership selected; starting the old monolith is forbidden')
    state=json.loads(STATE.read_text(encoding='utf8'))
    assert state['container']==LIVE and state['candidate']==NAME and state['database']==DB
    candidate=owned(NAME)
    if not candidate['State']['Running']:cmd('start',NAME)
    refresh_connections()
    old=json.loads(cmd('inspect','local-life-backend'))[0]
    assert not old['State']['Running'],'old Java must remain stopped after full release'
    if LIVE in cmd('ps','-a','--format','{{.Names}}').decode().splitlines():
        info=owned(LIVE);assert info['Config']['Image']==IMAGE
        if not info['State']['Running']:cmd('start',LIVE)
        return
    secret=json.loads((OUT/'live-connection.private.json').read_text())
    assert secret['database']==DB and secret['user']=='commerce_live'
    if 'agent_default' not in candidate['NetworkSettings']['Networks']:
        cmd('network','connect','--alias','commerce-release-mysql','agent_default',NAME)
    env=dict(v.split('=',1) for v in old['Config']['Env'] if '=' in v)
    env.update(DB_HOST='commerce-release-mysql',DB_PORT='3306',DB_NAME=DB,DB_USER=secret['user'],DB_PASSWORD=secret['password'],
        LOCAL_LIFE_SEARCH_RECONCILE_CATALOG_VERSION='merged-used-phone-439-20260909-v1',AGENT_BASE_URL='http://host.docker.internal:8000',TZ='UTC')
    args=['run','-d','--pull=never','--label','commerce.release=20260915-a1','--name',LIVE,
        '--network','agent_default','--network-alias','backend','--memory=1536m','-p','127.0.0.1:8080:8080']
    for k,v in env.items():args+=['-e',k+'='+v]
    cmd(*args,IMAGE)

def activate():
    if STATE.exists():raise ValueError('release activation exists; inspect and use ensure-backend')
    subprocess.run([sys.executable,'-X','utf8',str(ROOT/'scripts/commerce-full-release-20260915/freeze-code.py'),'verify'],check=True)
    for file in ('FINAL-INVARIANTS.json','CANDIDATE-BACKUP-RESTORE.json','PRE-SWITCH-AUDIT.json',
                 'ACCEPTANCE-BOUNDARIES.json','ACCEPTANCE-FULFILLMENT.json','CATALOG-READ-PLANS-full.json'):
        assert json.loads((OUT/file).read_text(encoding='utf8'))['status']=='PASS',file
    audit=json.loads((OUT/'PRE-SWITCH-AUDIT.json').read_text(encoding='utf8'))
    assert 0<=time.time()-audit['checkedAtUnix']<=900,'refresh original-row comparison immediately before activation'
    for name in ('local-life-backend','local-life-mysql','local-life-agent'):
        assert not json.loads(cmd('inspect',name))[0]['State']['Running'],'old authority must remain stopped'
    phone=json.loads((OUT/'acceptance-legacy-phone-v2.json').read_text(encoding='utf8'))
    assert phone['cards'] and phone['run']['status']=='completed'
    refresh_connections()
    secret=json.loads((OUT/'connection.private.json').read_text());assert secret['database']==DB
    db=pymysql.connect(**{k:secret[k] for k in ('host','port','user','password','database')},autocommit=True)
    password=secrets.token_hex(24)
    with db.cursor() as c:
        c.execute('CREATE USER %s@%s IDENTIFIED BY %s',('commerce_live','%',password))
        c.execute('GRANT ALL PRIVILEGES ON commerce_candidate.* TO %s@%s',('commerce_live','%'))
    db.close()
    write('live-connection.private.json',dict(host=secret['host'],port=secret['port'],user='commerce_live',password=password,database=DB))
    state=dict(status='VALIDATED_READY',container=LIVE,candidate=NAME,database=DB,image=IMAGE,
        evidenceDirectory=str(OUT),legacyCatalog='merged-used-phone-439-20260909-v1',qualifiedSourceRecords=7636849,
        newProducts=7635097,originalDatabasePreserved=True)
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('Validated release selected; launcher will start it. Public cutover is not yet verified.')

def mark_live():
    state=json.loads(STATE.read_text(encoding='utf8'))
    assert state['status']=='VALIDATED_READY'
    for file in ('LIVE-READ-ACCEPTANCE.json','LIVE-TRADE.json','LIVE-FULFILLMENT.json'):
        assert json.loads((OUT/file).read_text(encoding='utf8'))['status']=='PASS',file
    ui=json.loads((OUT/'ui-results-public.json').read_text(encoding='utf8'))['stats']
    assert ui['expected']==1 and ui['unexpected']==ui['flaky']==ui['skipped']==0
    for label in ('legacy-phone','tail-kuai','tail-multi'):
        result=json.loads((OUT/('LIVE-SEARCH-'+label+'.json')).read_text(encoding='utf8'))
        assert result['run']['status']=='completed' and result['cards'],label
        expected={'tail-kuai':'4000000005633437','tail-multi':'4001000001002822'}.get(label)
        if expected:
            assert any(str(card['id'])==expected and card.get('purchasable') for card in result['cards']),label+' tail identity'
    assert owned(LIVE)['State']['Running'] and owned(NAME)['State']['Running']
    for name in ('local-life-backend','local-life-mysql','local-life-agent'):
        assert not json.loads(cmd('inspect',name))[0]['State']['Running']
    assert httpx.get('http://127.0.0.1:5173/',timeout=10).status_code==200
    state.update(status='LIVE_VERIFIED',verifiedAtUnix=time.time())
    STATE.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    write('PUBLIC-CUTOVER.json',dict(status='PASS',release=state,originalContainersStopped=True))
    print('Public full-catalog cutover verified.')

if __name__=='__main__':
    if sys.argv[1:]==['activate']:activate()
    elif sys.argv[1:]==['ensure-backend']:ensure_backend()
    elif sys.argv[1:]==['mark-live']:mark_live()
    else:raise ValueError('explicit activate, ensure-backend or mark-live required')
