"""Renew only the isolated V2 read fixture's session through real authentication APIs.

Normally rotate the saved refresh token. The explicitly selected bootstrap path
repairs an older fixture that saved only its 15-minute access token: register a
temporary fixture account to obtain a server-encoded random password, assign that
password to the verified synthetic read account, then use the real login API.
Never changes order data, signing keys, TTLs, roles or token versions.
"""
import argparse
import base64
import json
import secrets
import time
import urllib.request
import uuid
from pathlib import Path

import pymysql


def expiry(token):
    part = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))['exp']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    parser.add_argument('--event',type=Path,required=True)
    parser.add_argument('--bootstrap-fixture-credentials',action='store_true')
    args = parser.parse_args()
    if args.event.exists(): raise SystemExit('Event file exists; refusing overwrite')
    runtime = args.runtime.resolve()
    if json.loads((runtime/'compose.validation.json').read_text())['name'] != 'backend-strengthening-v2':
        raise SystemExit('Only isolated V2 is allowed')
    saved = runtime/'read-fixture-private.json'; fixture = json.loads(saved.read_text())
    event = {'startedUnix':time.time(),'oldExpiresUnix':expiry(fixture['token']),'authenticationCalls':[],
             'orderDataChanged':False,'rolesOrTokenVersionChanged':False,'signingKeysOrTtlChanged':False}
    deadline = time.monotonic()+90
    while True:
        try:
            with urllib.request.urlopen('http://127.0.0.1:38080/api/health',timeout=2) as response:
                if response.status==200: break
        except OSError:
            pass
        if time.monotonic()>=deadline: raise TimeoutError('Dedicated backend not ready')
        time.sleep(.5)
    def call(path,body):
        request = urllib.request.Request('http://127.0.0.1:38080'+path,method='POST',
            headers={'Content-Type':'application/json'},data=json.dumps(body).encode())
        with urllib.request.urlopen(request,timeout=15) as response:
            result = json.loads(response.read())['data']
            event['authenticationCalls'].append({'path':path,'status':response.status})
            return result
    try:
        if fixture.get('refreshToken'):
            issued = call('/api/auth/refresh',{'refreshToken':fixture['refreshToken']})
            event['method']='refresh rotation'
        elif args.bootstrap_fixture_credentials:
            secret = dict(line.split('=',1) for line in (runtime/'compose.env').read_text().splitlines() if '=' in line)
            db = pymysql.connect(host='127.0.0.1',port=33316,user='root',password=secret['BENCH_DB_PASSWORD'],
                                 database='backend_strengthening',autocommit=True)
            try:
                with db.cursor() as cursor:
                    cursor.execute('SELECT username,token_version FROM user_account WHERE id=%s',(fixture['user'],))
                    username, version = cursor.fetchone()
                    assert username.startswith('mixed-read-') and version==0
                    cursor.execute("SELECT COUNT(*),SUM(idempotency_key LIKE 'mixed-read-%%') FROM customer_order WHERE user_id=%s",(fixture['user'],))
                    assert cursor.fetchone()==(10000,10000)
                    cursor.execute("SELECT role_name FROM user_role WHERE user_id=%s",(fixture['user'],))
                    assert {row[0] for row in cursor.fetchall()} <= {'USER'}
                    password = secrets.token_urlsafe(24)
                    temporary = call('/api/auth/register',{'username':'mixed-renew-'+uuid.uuid4().hex[:12],'password':password})
                    cursor.execute('SELECT password_hash FROM user_account WHERE id=%s',(temporary['user']['id'],))
                    encoded = cursor.fetchone()[0]
                    cursor.execute('UPDATE user_account SET password_hash=%s WHERE id=%s AND username=%s AND token_version=0',
                                   (encoded,fixture['user'],username))
                    assert cursor.rowcount==1
                issued = call('/api/auth/login',{'username':username,'password':password})
                event['method']='explicit synthetic fixture credential reseed then real login'
            finally:
                db.close()
        else:
            raise ValueError('No refresh token was saved; explicit fixture-only bootstrap required')
        assert issued['user']['id']==fixture['user']
        fixture['token'],fixture['refreshToken']=issued['accessToken'],issued['refreshToken']
        temporary = saved.with_suffix('.next.json'); temporary.write_text(json.dumps(fixture),encoding='utf-8');temporary.replace(saved)
        event['newExpiresUnix']=expiry(fixture['token']);event['status']='PASS'
    except Exception as error:
        event['status']='FAILED';event['error']=type(error).__name__;raise
    finally:
        event['finishedUnix']=time.time()
        args.event.write_text(json.dumps(event,indent=2),encoding='utf-8')
        print(json.dumps(event),flush=True)


if __name__=='__main__':main()
