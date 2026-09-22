"""Read-only isolated runtime probe. Never emits credentials or full container env."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit
import httpx
import pymysql
import redis

FLAGS=('LOCAL_LIFE_SUPPORT_ENABLED','LOCAL_LIFE_SUPPORT_SIMULATOR_ENABLED','LOCAL_LIFE_SUPPORT_RECOVERY_ENABLED',
       'LOCAL_LIFE_FULFILLMENT_ENABLED','LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED','PAYMENT_SIMULATOR_ENABLED')

def run(runtime,output):
    config=json.loads((runtime/'private.json').read_text())
    authority=urlsplit(config['authority'])
    if config['host']!='127.0.0.1' or not config['database'].startswith('support_live_') or authority.scheme!='http' or authority.hostname!='127.0.0.1':
        raise ValueError('explicit isolated loopback runtime required')
    if not config['container'].startswith('support-live-'):raise ValueError('isolated support container required')
    report={'scope':'Read-only runtime availability; not business acceptance','checks':{}}
    def probe(name,fn):
        try:report['checks'][name]={'status':'OBSERVED','evidence':fn()}
        except Exception as exc:report['checks'][name]={'status':'UNAVAILABLE','errorType':type(exc).__name__}
    def docker():
        data=json.loads(subprocess.check_output(['docker','inspect',config['container']],stderr=subprocess.DEVNULL))[0]
        if data['Config']['Labels'].get('support.attempt')!='live-001':raise ValueError('runtime label mismatch')
        env=dict(item.split('=',1) for item in data['Config']['Env'] if '=' in item)
        return {'container':config['container'],'status':data['State']['Status'],'imageId':data['Image'],
                'flags':{k:env.get(k) for k in FLAGS}}
    def database():
        with pymysql.connect(**{k:config[k] for k in ('host','port','user','password','database')},cursorclass=pymysql.cursors.DictCursor) as db,db.cursor() as cursor:
            cursor.execute('SELECT version,success FROM flyway_schema_history ORDER BY installed_rank')
            rows=cursor.fetchall()
            return {'schema':config['database'],'migrationCount':len(rows),'allSuccessful':all(r['success']==1 for r in rows),'lastVersion':rows[-1]['version'] if rows else None}
    def http(origin,path):
        with httpx.Client(timeout=5,trust_env=False,headers={'Origin':origin}) as client:
            response=client.get(origin+path)
            return {'origin':origin,'path':path,'statusCode':response.status_code}
    probe('javaContainer',docker);probe('database',database)
    probe('redis',lambda:{'ping':redis.Redis(host='127.0.0.1',port=16379,socket_timeout=3).ping()})
    probe('javaHealth',lambda:http(config['authority'],'/actuator/health'))
    probe('isolatedBff',lambda:http('http://127.0.0.1:18000','/openapi.json'))
    probe('defaultBff',lambda:http('http://127.0.0.1:8000','/openapi.json'))
    probe('frontend',lambda:http('http://127.0.0.1:5173','/'))
    report['sourceSha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with output.open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--runtime',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args();run(args.runtime,args.output)
