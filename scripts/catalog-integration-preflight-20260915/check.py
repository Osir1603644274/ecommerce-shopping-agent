"""Read-only integration gate probe against the existing isolated 10k MySQL lab.

No application writes, imports, network model calls, source changes, or cache edits.
Runs the exact current _card function with SQL-backed response fixtures, NOT HTTP.
"""
import ast
import asyncio
import collections
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import urllib.request

import pymysql

ROOT=Path(__file__).resolve().parents[2]
PRIOR=Path('D:/agent-datasets/commerce-import-lab-20260914-attempt001')
OUT=Path('D:/agent-datasets/commerce-integration-preflight-20260915-attempt001')
NAME='commerce-import-lab-20260914-a1'
DATABASE='commerce_import_lab'


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def save(name,value):
    (OUT/name).write_text(json.dumps(value,ensure_ascii=False,sort_keys=True,indent=2,default=str)+'\n',encoding='utf8')


def docker(*args):
    p=subprocess.run(['docker',*args],capture_output=True)
    if p.returncode:
        raise RuntimeError('docker '+args[0]+' failed; credentials withheld')
    return p.stdout


def health():
    try:
        with urllib.request.urlopen('http://127.0.0.1:5173/',timeout=3) as r:
            return {'status':r.status}
    except Exception as e:
        return {'error':type(e).__name__}


def snapshot(db):
    result={}
    with db.cursor() as c:
        c.execute('SET TRANSACTION READ ONLY')
        for table,pk in [('product','id'),('product_local_offer','product_id'),('inventory_stock','id'),('import_ledger','product_id')]:
            c.execute('SELECT * FROM '+table+' ORDER BY '+pk)
            result[table]=c.fetchall()
    db.rollback()
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


async def cards(rows):
    """Compile only _card from its actual AST; auth._java is an explicit fixture."""
    path=ROOT/'agent/app/api/commerce_workspace.py'
    tree=ast.parse(path.read_text(encoding='utf8'))
    node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_card')
    fixtures={}
    for r in rows:
        fixtures[int(r['id'])]={'product':dict(title=r['title'],brand=r['brand'],categoryL3=r['category_l3']),
            'offer':dict(priceMinor=r['price_minor'],available=r['available_quantity'],kind=r['price_kind'],
                         currency=r['currency'],canPurchase=eligible(r))}
    calls=[]
    async def java(method,path):
        assert method=='GET' and path.endswith('/purchase-view')
        ident=int(path.split('/')[3]);calls.append(ident)
        return fixtures[ident]
    namespace={'auth':SimpleNamespace(settings=SimpleNamespace(commerce_workspace_local_offers_enabled=True),_java=java)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(path),'exec'),namespace)
    result=[await namespace['_card'](int(r['id'])) for r in rows]
    assert len(calls)==len(rows)
    return result


def eligible(row):
    # Predicate mirrored from ProductOfferController; this is not Java execution.
    return (row['lifecycle_status']=='ACTIVE' and row['price_minor'] is not None and row['price_minor']>0
            and row['available_quantity'] is not None and row['available_quantity']>0)


def main():
    if OUT.exists():
        raise ValueError('evidence exists; cannot overwrite')
    info=json.loads(docker('inspect',NAME))[0]
    if info['Config']['Labels'].get('catalog.lab')!='20260914-attempt001':
        raise ValueError('not the owned isolated lab')
    mounts=info['Mounts']
    assert any(m.get('Name')==NAME+'-data' and m['Destination']=='/var/lib/mysql' for m in mounts)
    OUT.mkdir(parents=True)
    report={'scope':'SQL_READ_ONLY_AND_CURRENT_CARD_FUNCTION_WITH_RESPONSE_FIXTURES_NOT_END_TO_END',
            'frontendBefore':health(),'status':'RUNNING'}
    started=False;db=None
    try:
        manifest=json.loads((PRIOR/'EVIDENCE-MANIFEST.json').read_text(encoding='utf8'))
        for name,item in manifest.items():
            assert sha(PRIOR/name)==item['sha256'],name
        samples=[json.loads(x) for x in (PRIOR/'sample.jsonl').read_text(encoding='utf8').splitlines()]
        env=dict(v.split('=',1) for v in info['Config']['Env'] if '=' in v)
        if not info['State']['Running']:
            docker('start',NAME);started=True
        info=json.loads(docker('inspect',NAME))[0]
        binding=info['NetworkSettings']['Ports']['3306/tcp'][0]
        assert binding['HostIp']=='127.0.0.1' and int(binding['HostPort']) not in (3306,13306)
        for _ in range(30):
            try:
                db=pymysql.connect(host='127.0.0.1',port=int(binding['HostPort']),user='root',
                    password=env['MYSQL_ROOT_PASSWORD'],database=DATABASE,charset='utf8mb4',
                    autocommit=False,connect_timeout=3,read_timeout=30,cursorclass=pymysql.cursors.DictCursor)
                break
            except pymysql.MySQLError:
                time.sleep(1)
        if db is None:
            raise RuntimeError('isolated DB unavailable')
        before=snapshot(db)
        joined=[];started_query=time.monotonic()
        with db.cursor() as c:
            c.execute('SET TRANSACTION READ ONLY')
            for offset in range(0,len(samples),250):
                batch=samples[offset:offset+250]
                # Exact source identity lookup, not title matching or blindly trusting generated IDs.
                c.execute('SELECT p.id,p.source,p.source_item_id,p.title,p.brand,p.category_l3,p.lifecycle_status,'
                    'o.price_minor,o.currency,o.price_kind,s.available_quantity '
                    'FROM product p LEFT JOIN product_local_offer o ON o.product_id=p.id '
                    "LEFT JOIN inventory_stock s ON s.item_type='PRODUCT' AND s.item_id=p.id "
                    'WHERE (p.source,p.source_item_id) IN ('+','.join(['(%s,%s)']*len(batch))+')',
                    tuple(v for r in batch for v in (r['source'],r['sourceItemId'])))
                joined.extend(c.fetchall())
        db.rollback()
        report['sourceLookupSeconds']=round(time.monotonic()-started_query,3)
        by_identity={(r['source'],r['source_item_id']):r for r in joined}
        assert len(by_identity)==len(samples)==10000
        ledger={str(r['product_id']):r for r in before['import_ledger']}
        for r in samples:
            match=by_identity[r['source'],r['sourceItemId']]
            assert str(match['id'])==r['id']
            assert ledger[r['id']]['raw_sha']==r['provenance']['rawSha256']
        current=asyncio.run(cards(joined))
        current_by_id={str(r['id']):r for r in current}
        results=[dict(sourceDocid=r['source']+':'+r['source_item_id'],productId=str(r['id']),
                      eligibleByOfferPredicate=eligible(r),eligibleInCurrentCard=current_by_id[str(r['id'])]['purchasable'],
                      category=r['category_l3'],lifecycle=r['lifecycle_status']) for r in joined]
        counts=collections.Counter()
        for r in results:
            counts['resolved']+=1
            counts['offerEligible']+=bool(r['eligibleByOfferPredicate'])
            counts['currentCardEligible']+=bool(r['eligibleInCurrentCard'])
            counts['blockedByCategory']+=r['eligibleByOfferPredicate'] and not r['eligibleInCurrentCard']
            counts['archived']+=r['lifecycle']=='ARCHIVED'
        assert digest(before)==digest(snapshot(db))
        save('IDENTITY-CARD-RESULTS.json',results)
        report.update(status='PASS_READ_ONLY_PROBE_INTEGRATION_NOT_READY',counts=dict(counts),
                      labCatalogAndLedgerUnchanged=True,
                      restoredLabBusinessSha256=digest({t:before[t] for t in ('product','product_local_offer','inventory_stock')}),
                      examples=results[:3]+[r for r in results if r['eligibleByOfferPredicate'] and not r['eligibleInCurrentCard']][:3])
        prior_report=json.loads((PRIOR/'RESULT.json').read_text(encoding='utf8'))
        assert report['restoredLabBusinessSha256']==prior_report['backup']['restoredBusinessStateSha256']
    except Exception as e:
        report.update(status='FAILED',error=str(e));raise
    finally:
        if db:db.close()
        if started:docker('stop','-t','15',NAME)
        report['frontendAfter']=health()
        files=[Path(__file__).resolve(),ROOT/'agent/app/api/commerce_workspace.py',ROOT/'agent/app/catalog_service.py',
               ROOT/'backend/src/main/java/com/example/locallife/product/ProductOfferController.java']
        report['codeSha256']={str(p.relative_to(ROOT)):sha(p) for p in files}
        save('RESULT.json',report)
        save('EVIDENCE-MANIFEST.json',{p.name:dict(sha256=sha(p),bytes=p.stat().st_size) for p in OUT.iterdir() if p.name!='EVIDENCE-MANIFEST.json'})
        print(json.dumps(report,ensure_ascii=False,default=str))


if __name__=='__main__':
    main()
