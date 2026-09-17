"""Read-only ownership and public-boundary checks before Agent unfreeze."""
import json
import pymysql
import httpx
from topology_lab import OUT,root_connection,write
from live_release import PRIVATE,SCHEMAS,PORTS

config=json.loads(PRIVATE.read_text(encoding='utf8'));root=root_connection();denials=[]
for role,other,table in [('trade','catalog','product'),('trade','inventory','inventory_stock'),('catalog','trade','customer_order'),('inventory','trade','customer_order')]:
    account=config['accounts'][role]
    with pymysql.connect(host=root['host'],port=root['port'],user=account['user'],password=account['password'],database=SCHEMAS[role],autocommit=True) as db,db.cursor() as c:
        try:c.execute('SELECT * FROM '+SCHEMAS[other]+'.'+table+' LIMIT 1')
        except pymysql.err.OperationalError as error:
            assert error.args[0]==1142;denials.append({'reader':role,'forbiddenOwner':other,'mysqlCode':1142})
        else:raise AssertionError('cross-service SQL was allowed')
instances=set();client=httpx.Client(base_url='http://127.0.0.1:8080',timeout=10,trust_env=False)
for _ in range(8):
    response=client.get('/api/products/4000000005633437/purchase-view');response.raise_for_status()
    data=response.json()['data'];assert data['offer']['canPurchase'] and data['offer']['priceMinor']==350900
    instances.add(response.headers.get('X-Service-Instance'))
assert instances=={'catalog-a','catalog-b'},instances
internal={}
for role in ('catalog-a','catalog-b'):
    base='http://127.0.0.1:'+str(PORTS[role])
    assert httpx.get(base+'/internal/catalog/manifest',trust_env=False).status_code==403
    result=httpx.get(base+'/internal/catalog/manifest',headers={'X-Internal-Service-Token':config['internalToken']},trust_env=False)
    result.raise_for_status();assert result.json()['data']['productCount']==439;internal[role]='authenticated-manifest-439'
for path in ['/internal/catalog/manifest','/internal/inventory/stocks/PRODUCT/1','/actuator/env']:
    assert client.get(path).status_code in (401,403,404)
assert httpx.get('http://127.0.0.1:5173/',trust_env=False).status_code==200
write('LIVE-OWNERSHIP-ACCEPTANCE.json',{'status':'PASS','databaseDenials':denials,'gatewayCatalogInstances':sorted(instances),'internalCatalog':internal,
    'originalTailProductPriceMinor':350900,'publicInternalPathsDenied':True})
print('Live schema permissions, two-instance catalog routing, internal authentication and original tail product PASS.')
