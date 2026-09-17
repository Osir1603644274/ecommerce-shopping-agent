"""Real Java/BFF/Redis check with one isolated local test account.

Only this account's BFF access deadline is advanced; no JWTs are fabricated,
no user credentials are read, and no order/payment writes are performed.
"""
import asyncio
import hashlib
import json
import secrets
import time
from pathlib import Path

import httpx
import redis.asyncio as redis


async def main():
    report = {'kind': 'isolated_live_session_refresh', 'checks': {}}
    check = report['checks']
    db = redis.Redis(host='::1', port=6379, decode_responses=True)
    username = 'refresh-check-' + secrets.token_hex(6)
    async with httpx.AsyncClient(base_url='http://127.0.0.1:5173', timeout=20,
            headers={'Origin': 'http://127.0.0.1:5173', 'X-Conversation-Source': 'automated_test'}) as c:
        response = await c.post('/api/commerce-demo/register',
                                json={'username': username, 'password': secrets.token_urlsafe(24)})
        assert response.status_code == 200, ('register', response.status_code)
        c.headers['X-CSRF-Token'] = response.json()['csrfToken']
        sid = c.cookies.get('commerce_demo_session')
        assert sid
        key = 'commerce:demo:session:' + hashlib.sha256(sid.encode()).hexdigest()
        original = json.loads(await db.get(key))
        check['redis_lifetime_exceeds_access_lifetime'] = await db.ttl(key) > 900
        before = await c.get('/api/commerce-demo/workspace')
        assert before.status_code == 200
        conversation = before.json()['conversationId']

        async def expire_access():
            raw = await db.get(key)
            state = json.loads(raw)
            assert state['username'] == username
            state['accessExpiresAtEpoch'] = int(time.time()) - 1
            ttl = await db.ttl(key)
            assert await db.eval("if redis.call('GET',KEYS[1])~=ARGV[1] then return 0 end "
                "redis.call('SET',KEYS[1],ARGV[2],'EX',ARGV[3]); return 1", 1,
                key, raw, json.dumps(state, separators=(',', ':')), ttl) == 1

        await expire_access()
        responses = await asyncio.gather(*(c.get('/api/commerce-demo/orders/page') for _ in range(8)))
        report['concurrent_http_statuses'] = [r.status_code for r in responses]
        assert all(r.status_code == 200 for r in responses)
        renewed = json.loads(await db.get(key))
        check['both_java_tokens_rotated'] = all(renewed[k] != original[k] for k in ('accessToken', 'refreshToken'))
        check['same_session_and_csrf'] = c.cookies.get('commerce_demo_session') == sid and renewed['csrfDigest'] == original['csrfDigest']
        check['absolute_login_deadline_unchanged'] = renewed['sessionExpiresAtEpoch'] == original['sessionExpiresAtEpoch']
        after = await c.get('/api/commerce-demo/workspace')
        check['same_workspace_after_refresh'] = after.status_code == 200 and after.json()['conversationId'] == conversation
        check['no_credentials_in_public_response'] = all(t not in r.text for t in (renewed['accessToken'], renewed['refreshToken']) for r in responses)
        # Exercise a second rotation, proving the first replacement refresh token
        # was retained, not the already-consumed original token.
        await expire_access()
        response = await c.get('/api/commerce-demo/orders/page')
        second = json.loads(await db.get(key))
        check['second_rotation_uses_replacement'] = response.status_code == 200 and second['refreshToken'] != renewed['refreshToken']
        response = await c.post('/api/commerce-demo/logout')
        check['logout_clears_session'] = response.status_code == 200 and not await db.exists(key)
        # Retained browser identity points to an authenticated workspace; the
        # expired/missing login must prompt login instead of returning 409 forever.
        response = await c.get('/api/commerce-demo/workspace/conversations')
        check['missing_login_signals_401_not_409'] = response.status_code == 401
        assert all(check.values()), check
    await db.aclose()
    report['status'] = 'PASS'
    out = Path('D:/agent-datasets/session-refresh-20260914-v1/live-results.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
