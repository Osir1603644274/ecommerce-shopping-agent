from pathlib import Path
import json,subprocess,time,urllib.request,urllib.error,yaml
ROOT=Path(__file__).resolve().parents[2];DEST=ROOT/'.runtime/pool-protection-20260911-v3'
config=yaml.safe_load((DEST/'compose.yaml').read_text());app=config['services']['fixed']
app['image']='agent-backend:pool-protection-20260911-v3';app.pop('command');app.pop('volumes');app.pop('working_dir')
app['ports']=['127.0.0.1:38514:8080'];config['services']={'canary':app}
path=DEST/'canary.yaml';path.write_text(yaml.safe_dump(config,sort_keys=False),encoding='utf-8')
subprocess.run(['docker','compose','-f',str(path),'up','-d'],check=True)
for _ in range(120):
    try:
        with urllib.request.urlopen('http://127.0.0.1:38514/api/health',timeout=2) as r:
            if r.status==200:break
    except (OSError,urllib.error.URLError):pass
    time.sleep(1)
else:raise RuntimeError('Canary did not become healthy')
with urllib.request.urlopen('http://127.0.0.1:38514/api/products/920000/purchase-view',timeout=10) as r:
    data=json.load(r)['data'];assert data['product']['id'] and data['offer']['priceMinor']==60000
try:
    urllib.request.urlopen('http://127.0.0.1:38514/api/products/pool-lab/slow',timeout=5)
    raise AssertionError('Lab endpoint exposed')
except urllib.error.HTTPError as e:assert e.code==404,e.code
(DEST/'canary-result.json').write_text(json.dumps({'status':'PASS','readOffer':True,'labEndpoint':404}),encoding='utf-8')
subprocess.run(['docker','compose','-f',str(path),'stop','canary'],check=True)
