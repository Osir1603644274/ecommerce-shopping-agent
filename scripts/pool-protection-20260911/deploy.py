"""Cut over only after acceptance and no pending user operation; keep prior image for rollback."""
import json,os,subprocess,time,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];LAB=ROOT/'.runtime/pool-protection-20260911-v3'
assert json.loads((LAB/'results.json').read_text())['status']=='PASS'
assert json.loads((LAB/'canary-result.json').read_text())['status']=='PASS'
assert json.loads((LAB/'burst-results.json').read_text())['statuses']=={'200':500}
def cmd(*args,**kw):return subprocess.check_output(args,cwd=ROOT,stderr=subprocess.STDOUT,**kw).decode('utf-8',errors='replace')
preflight=cmd(str(ROOT/'.venv/Scripts/python.exe'),'scripts/reliability-lab-20260910/cutover_preflight.py')
env=os.environ.copy()
env['MERGED_OBSERVER_KEY']=(ROOT/'.runtime/merged-commerce/secrets/observer.token').read_text().strip()
env['MERGED_WAREHOUSE_TOKEN']=(ROOT/'.runtime/merged-commerce/secrets/warehouse.token').read_text().strip()
logs=cmd('docker','compose','-f','docker-compose.yml','-f','compose.merged-commerce.yml','up','-d','--no-deps','--no-build','backend',env=env)
(LAB/'deployment.log').write_text(logs,encoding='utf-8')
for _ in range(120):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=2) as r:
            if r.status==200:break
    except (OSError,urllib.error.URLError):pass
    time.sleep(1)
else:raise RuntimeError('Deployment health failed; prior image retained for rollback')
with urllib.request.urlopen('http://127.0.0.1:8080/api/products/320633/purchase-view',timeout=10) as r:
    data=json.load(r)['data'];assert data['product'] and 'offer' in data
expected=cmd('docker','image','inspect','agent-backend:pool-protection-20260911-v3','--format','{{.Id}}').strip()
actual=cmd('docker','inspect','local-life-backend','--format','{{.Image}}').strip();assert actual==expected
health=cmd('pwsh','-NoProfile','-File','scripts/merged-commerce.ps1','-Action','health')
result={'status':'PASS','preflight':json.loads(preflight),'image':actual,'health':json.loads(health),'liveRead':200,'agentRestarted':False}
(LAB/'live-cutover.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
