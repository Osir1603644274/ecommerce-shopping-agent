"""Public cutover acceptance. Only a dedicated test user may create a local test order."""
import json,secrets,sys,time
import httpx,pymysql
from prepare import OUT,ROOT,DB,write,refresh_connections

BASE='http://127.0.0.1:5173'
W='/api/commerce-demo/workspace'
state=json.loads((ROOT/'.runtime/merged-commerce/full-catalog-release.json').read_text())
assert state['database']==DB and state['newProducts']==7635097
refresh_connections()
client=httpx.Client(base_url=BASE,headers={'Origin':BASE,'X-Conversation-Source':'automated_test'},timeout=180)

def call(method,path,body=None):
    response=client.request(method,path,json=body)
    response.raise_for_status()
    return response.json()

assert client.get('/').status_code==200
assert call('GET','/api/commerce-demo/capability')['paymentSimulationEnabled']
private=OUT/'live-session.private.json'
if private.exists():
    saved=json.loads(private.read_text())
else:
    saved=dict(username='full-live-'+secrets.token_hex(5),password=secrets.token_urlsafe(24))
    write(private.name,saved)
if saved.get('cookies'):
    for key,value in saved['cookies'].items():client.cookies.set(key,value,domain='127.0.0.1',path='/')
    me=call('GET','/api/commerce-demo/me')
else:
    body={key:saved[key] for key in ('username','password')}
    response=client.post('/api/commerce-demo/register',json=body)
    if response.status_code==409:response=client.post('/api/commerce-demo/login',json=body)
    response.raise_for_status();me=response.json()
saved['csrf']=me['csrfToken'];saved['cookies']=dict(client.cookies);write(private.name,saved)
client.headers['X-CSRF-Token']=saved['csrf']

mode=sys.argv[1] if len(sys.argv)>1 else 'read'
if mode=='read':
    checks=[]
    for row in json.loads((OUT/'LATE-ACCEPTANCE-INPUTS.json').read_text(encoding='utf8')):
        with httpx.Client(base_url='http://127.0.0.1:8080',timeout=30) as java:
            identity={key:row[key] for key in ('source','sourceItemId','rawSha256')}
            resolved=java.post('/api/products/resolve-sources',json={'identities':[identity]})
            resolved.raise_for_status();assert resolved.json()['data'][0]['productId']==row['expectedProductId']
            view=java.get('/api/products/'+row['expectedProductId']+'/purchase-view')
            view.raise_for_status();offer=view.json()['data']['offer']
            assert offer['canPurchase'] and offer['externalCatalogEligible']
            assert offer['priceMinor']==row['localPriceMinor'] and offer['available']==10
        checks.append(dict(source=row['source'],productId=row['expectedProductId'],priceMinor=offer['priceMinor'],available=offer['available']))
    write('LIVE-READ-ACCEPTANCE.json',dict(status='PASS',tailRecords=checks,publicEntry=BASE))
    print('Public entry and both source-tail identity/offer reads PASS.',flush=True)
elif mode=='search':
    query=sys.argv[2] if len(sys.argv)>2 else '在全量目录中查找闪电购女装217'
    label=sys.argv[3] if len(sys.argv)>3 else 'tail-kuai'
    assert label.replace('-','').isalnum()
    current=call('GET',W)
    call('POST',W+'/conversations',dict(expectedConversationId=current['conversationId']))
    current=call('POST',W+'/run',dict(message=query,requestId='full-live-'+secrets.token_hex(12),mode='continuous'))
    for _ in range(150):
        time.sleep(2);current=call('GET',W+'/control')
        if current['run']['status'] not in {'running','pausing'}:break
    write('LIVE-SEARCH-'+label+'.json',current)
    assert current['run']['status']=='completed' and current['cards']
    print(json.dumps(dict(status=current['run']['status'],cards=[dict(id=c['id'],title=c['title'],purchasable=c.get('purchasable')) for c in current['cards']]),ensure_ascii=False))
elif mode=='trade':
    path=OUT/'LIVE-TRADE.json'
    report=json.loads(path.read_text(encoding='utf8')) if path.exists() else {'steps':{},'testUser':saved['username']}
    assert report['testUser']==saved['username'] and saved['username'].startswith('full-live-')
    steps=report['steps']
    def step(name,value):
        steps[name]=value;write(path.name,report)
    if not steps.get('orderProposal'):
        current=call('GET',W)
        card=next(c for c in current['cards'] if str(c['id'])=='4000000005633437' and c['purchasable'])
        step('product',dict(id=card['id'],title=card['title']))
        call('POST',W+'/selection',dict(productId=card['id'],quantity=1))
        quote=call('POST',W+'/preview',dict(productId=card['id'],quantity=1))
        step('orderProposal',quote['checkout']['proposal'])
    if not steps.get('order'):
        result=call('POST',W+'/confirm',dict(confirmationId=steps['orderProposal']['confirmationId']))
        step('orderOutcome',result['checkout']['outcome'])
        order=result['checkout']['outcome']['result']
        assert order.get('id') and order['status']=='PENDING_PAYMENT'
        step('order',order)
    order=steps['order']
    if not steps.get('sameConfirmation'):
        replay=call('POST',W+'/confirm',dict(confirmationId=steps['orderProposal']['confirmationId']))
        assert replay['checkout']['outcome']['result']['id']==order['id'];step('sameConfirmation',True)
    if not steps.get('paymentProposal'):
        quote=call('POST',W+'/payment-preview',dict(orderId=order['id']))
        step('paymentProposal',quote['checkout']['proposal'])
    if not steps.get('payment'):
        result=call('POST',W+'/confirm',dict(confirmationId=steps['paymentProposal']['confirmationId']))
        step('paymentOutcome',result['checkout']['outcome'])
        step('payment',result['checkout']['outcome']['result'])
    if not steps.get('paid'):
        step('paid',call('POST','/api/commerce-demo/payments/'+steps['payment']['id']+'/simulate-success'))
    step('afterSales',call('GET',W+'/orders/'+order['id']+'/after-sales'))
    secret=json.loads((OUT/'live-connection.private.json').read_text())
    assert secret['database']==DB
    db=pymysql.connect(**secret,cursorclass=pymysql.cursors.DictCursor)
    with db.cursor() as c:
        c.execute('SELECT * FROM customer_order WHERE id=%s',(order['id'],));stored=c.fetchone()
        assert stored['status']=='PAID' and stored['payable_minor']==350900
        c.execute("SELECT * FROM inventory_stock WHERE item_type='PRODUCT' AND item_id=%s",(steps['product']['id'],));stock=c.fetchone()
        assert stock['total_quantity']==10 and stock['available_quantity']==9 and stock['reserved_quantity']==0 and stock['sold_quantity']==1
    db.rollback();db.close();step('sql',dict(order=stored,stock=stock))
    report['status']='PASS';write(path.name,report)
    print('Public source-tail order, same-confirmation replay, local payment and SQL inventory PASS.',flush=True)
else:raise ValueError('explicit read, search or trade mode required')
client.close()
