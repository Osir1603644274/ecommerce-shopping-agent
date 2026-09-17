"""Create a new isolated validation directory and local-only credentials; start nothing."""
import argparse
import json
import secrets
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--directory', type=Path, required=True)
args = parser.parse_args()
target = args.directory.resolve()
target.mkdir(parents=True, exist_ok=False)
values = {key: secrets.token_urlsafe(36) for key in (
    'BENCH_DB_PASSWORD', 'BENCH_JWT_SECRET', 'BENCH_CALLBACK_SECRET', 'BENCH_WAREHOUSE_TOKEN')}
(target/'compose.env').write_text(''.join(key+'='+value+'\n' for key,value in values.items()), encoding='utf-8')
(target/'warehouse-token.txt').write_text(values['BENCH_WAREHOUSE_TOKEN'], encoding='utf-8')
(target/'fault.json').write_text('{"mode":"normal"}', encoding='utf-8')
(target/'mysql-admin.json').write_text(json.dumps({'host':'127.0.0.1','port':33316,'user':'root',
    'password':values['BENCH_DB_PASSWORD'],'database':'backend_bench_new_attempt'}, indent=2), encoding='utf-8')
print(json.dumps({'created':str(target),'servicesStarted':False,'credentialsPrinted':False}))
