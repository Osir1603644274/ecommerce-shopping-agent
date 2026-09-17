import asyncio
from http.cookiejar import CookieJar

import httpx

from app.api import commerce_demo as auth
from app.api import commerce_workspace as ws
from app import java_http


def test_pooled_java_requests_do_not_share_identity_or_cookies(monkeypatch):
    async def run():
        seen = []
        async def respond(request):
            seen.append((request.headers.get('authorization'), request.headers.get('cookie')))
            return httpx.Response(200, json={'success': True, 'data': {}},
                                  headers={'set-cookie': 'session=private; Path=/'})
        async with httpx.AsyncClient(base_url='http://java', transport=httpx.MockTransport(respond),
                                    cookies=CookieJar(policy=java_http.NoCookies())) as client:
            monkeypatch.setattr(java_http, '_client', client)
            await auth._java('GET', '/first', access_token='alice')
            await auth._java('GET', '/second', access_token='bob')
            await auth._java('GET', '/guest')
            assert seen == [('Bearer alice', None), ('Bearer bob', None), (None, None)]
            assert not list(client.cookies.jar)
    asyncio.run(run())


def test_merged_card_uses_live_offer_not_cached_stock(monkeypatch):
    async def run():
        calls = []
        async def java(method, path):
            calls.append(path)
            return {'product': {'title': 'phone', 'categoryL3': '二手手机', 'priceStatus': 'verified',
                                'snapshotPriceMinor': 100, 'availableQuantity': 50},
                    'offer': {'priceMinor': 200, 'available': 0, 'canPurchase': False, 'currency': 'CNY'}}
        monkeypatch.setattr(auth.settings, 'commerce_workspace_local_offers_enabled', True)
        monkeypatch.setattr(auth, '_java', java)
        card = await ws._card(123)
        assert calls == ['/api/products/123/purchase-view']
        assert card['priceMinor'] == 200 and card['available'] == 0 and not card['purchasable']
    asyncio.run(run())


def test_pool_preserves_auth_deadline_and_does_not_retry(monkeypatch):
    async def run():
        seen=[]
        async def fail(request):
            seen.append(request.extensions['timeout'])
            raise httpx.ReadTimeout('fixture',request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            monkeypatch.setattr(java_http,'_client',client)
            try:
                async with java_http.java_connection(timeout=2.0) as bound:
                    await bound.get('http://java/api/identity/me')
            except httpx.ReadTimeout:pass
            else:raise AssertionError('Timeout must propagate')
            assert len(seen)==1 and seen[0]['read']==2.0 and seen[0]['pool']==2.0
    asyncio.run(run())
