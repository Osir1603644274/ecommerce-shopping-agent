import json,time
import httpx
from topology_lab import ROOT,OUT,write

latest=max(OUT.glob('micro-live-session-*.private.json'),key=lambda p:p.stat().st_mtime)
session=json.loads(latest.read_text());base='http://127.0.0.1:5173'
client=httpx.Client(base_url=base,timeout=15,trust_env=False,headers={'Origin':base,'X-Conversation-Source':'automated_test'})
for key,value in session['cookies'].items():client.cookies.set(key,value,domain='127.0.0.1',path='/')
assert client.get('/').status_code==200
try:httpx.get('http://127.0.0.1:18110/health',timeout=1,trust_env=False)
except (httpx.ConnectError,httpx.ConnectTimeout) as error:connection_error=type(error).__name__
else:raise AssertionError('Search must actually be unavailable')
started=time.monotonic();me=client.get('/api/commerce-demo/me');me.raise_for_status()
client.headers['X-CSRF-Token']=me.json()['csrfToken']
orders=client.get('/api/commerce-demo/orders/page');orders.raise_for_status()
agent=httpx.get('http://127.0.0.1:8000/health',trust_env=False);agent.raise_for_status()
write('SEARCH-OUTAGE-ACCEPTANCE.json',{'status':'PASS','searchConnectionError':connection_error,'authenticatedIdentityHttp':me.status_code,
    'orderReadHttp':orders.status_code,'identityAndOrderReadMs':round((time.monotonic()-started)*1000,2),'agentAlive':True,
    'scope':'actual independent search process stopped; no order write issued'})
print('Search process unavailable; authenticated identity and order reads still PASS.')
