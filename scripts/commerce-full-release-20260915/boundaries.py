"""Real Java boundary checks, reversible fixtures in acceptance DB only."""
import json
import secrets
import time
import httpx
import pymysql
from prepare import OUT,write
from import_catalog import lab

secret=json.loads((OUT/'stage-connection.private.json').read_text())
assert secret['database']=='commerce_acceptance'
rows=[json.loads(s) for s in (OUT.parent/'commerce-import-lab-20260914-attempt001/sample.jsonl').read_text(encoding='utf8').splitlines()]
class RateAwareClient(httpx.Client):
    def request(self,*args,**kwargs):
        for attempt in range(3):
            response=super().request(*args,**kwargs)
            if response.status_code!=429:return response
            wait=min(60,max(1,int(response.headers.get('Retry-After','5'))))
            print('Rate limit respected; retry after',wait,'seconds',flush=True);time.sleep(wait)
        response.raise_for_status()
client=RateAwareClient(base_url='http://127.0.0.1:19380',timeout=10)
registration=client.post('/api/auth/register',json={'username':'full-bound-'+secrets.token_hex(5),'password':secrets.token_urlsafe(24)})
registration.raise_for_status();client.headers['Authorization']='Bearer '+registration.json()['data']['accessToken']
db=pymysql.connect(**secret,cursorclass=pymysql.cursors.DictCursor,autocommit=False)
checks=[]
def observe(name,result):
    checks.append(dict(check=name,result=result));write('ACCEPTANCE-BOUNDARIES.json',{'checks':checks})
def resolve(r,**changes):
    identity=dict(source=r['source'],sourceItemId=r['sourceItemId'],rawSha256=r['provenance']['rawSha256']);identity.update(changes)
    return client.post('/api/products/resolve-sources',json={'identities':[identity]})
for r in [rows[20],rows[5020]]:
    response=resolve(r);response.raise_for_status()
    assert response.json()['data'][0]['productId']==r['id']
    assert resolve(r,rawSha256='0'*64).json()['data']==[]
    assert resolve(r,sourceItemId='0'+r['sourceItemId']).status_code==400
    observe('exact-source-binding-'+r['source'],True)
r=rows[23];ident=int(r['id'])
identity=dict(source=r['source'],sourceItemId=r['sourceItemId'],rawSha256=r['provenance']['rawSha256'])
assert client.post('/api/products/resolve-sources',json={'identities':[identity]*21}).status_code==400
observe('oversized-resolver-rejected',True)
original={}
with db.cursor() as c:
    for t,key in [('product','id'),('product_local_offer','product_id'),('inventory_stock','item_id')]:
        c.execute('SELECT * FROM '+t+' WHERE '+key+'=%s',(ident,));original[t]=c.fetchone();assert original[t]
db.rollback()
def preview():
    response=client.post('/api/orders/preview',json={'itemType':'PRODUCT','itemId':str(ident),'quantity':1})
    print('preview status',response.status_code,response.text[:400],flush=True)
    return response
def restore():
    with db.cursor() as c:
        for t,key in [('product','id'),('inventory_stock','item_id')]:
            row=original[t]
            c.execute('UPDATE '+t+' SET '+','.join('`'+k+'`=%s' for k in row)+' WHERE '+key+'=%s',(*row.values(),ident))
        c.execute('SELECT product_id FROM product_local_offer WHERE product_id=%s',(ident,))
        if not c.fetchone():lab.insert_rows(c,'product_local_offer',[original['product_local_offer']])
    db.commit()
try:
    # Prime descriptive caches first: fresh purchase conditions must still block later.
    assert client.get(f'/api/products/{ident}/purchase-view').json()['data']['offer']['canPurchase']
    with db.cursor() as c:c.execute("UPDATE product SET lifecycle_status='ARCHIVED' WHERE id=%s",(ident,))
    db.commit()
    assert resolve(r).json()['data']==[]
    assert not client.get(f'/api/products/{ident}/purchase-view').json()['data']['offer']['canPurchase']
    assert preview().status_code==422
    observe('archive-rejects-even-with-warm-detail-cache',True);restore()
    with db.cursor() as c:c.execute('DELETE FROM product_local_offer WHERE product_id=%s',(ident,))
    db.commit()
    assert not client.get(f'/api/products/{ident}/purchase-view').json()['data']['offer']['canPurchase']
    assert preview().status_code==422
    observe('missing-offer-does-not-invent-price',True);restore()
    with db.cursor() as c:c.execute('UPDATE inventory_stock SET available_quantity=0,sold_quantity=10 WHERE item_type=\'PRODUCT\' AND item_id=%s',(ident,))
    db.commit()
    assert not client.get(f'/api/products/{ident}/purchase-view').json()['data']['offer']['canPurchase']
    assert preview().status_code in (409,422)
    observe('sold-out-rejects-preview',True)
finally:
    restore();db.close();client.close()
write('ACCEPTANCE-BOUNDARIES.json',{'status':'PASS','fixturesRestored':True,'checks':checks})
print('Real authority boundary checks PASS; acceptance fixtures restored.')
