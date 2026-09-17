"""Read-only live acceptance, preserve lab logs, stop only isolated lab."""
import json, subprocess, hashlib, urllib.request
from pathlib import Path
from datetime import datetime, timezone
ROOT=Path(__file__).resolve().parents[2]
LAB=ROOT/'.runtime/reliability-lab-20260910'
def cmd(*args):
    return subprocess.check_output(args,cwd=ROOT,stderr=subprocess.STDOUT).decode('utf-8',errors='replace')
checks={}
for name,url in [('frontend','http://127.0.0.1:5173/'),('purchaseView','http://127.0.0.1:8080/api/products/320633/purchase-view')]:
    with urllib.request.urlopen(url,timeout=20) as response:
        checks[name]={'status':response.status}
        if name=='purchaseView':
            data=json.load(response)['data']
            assert data['product'] and 'offer' in data
            checks[name]['fields']=list(data)
checks['health']=cmd('pwsh','-NoProfile','-File','scripts/merged-commerce.ps1','-Action','health')
checks['image']=cmd('docker','inspect','local-life-backend','--format','{{.Image}}').strip()
assert checks['image']=='sha256:3eb4af95797176e54f6ca22781cde1422d4bedb051205419f572c623e6166f1b'
checks['timeUtc']=datetime.now(timezone.utc).isoformat()
checks['releaseJarSha256']=hashlib.sha256((ROOT/'.runtime/product-read-release-20260910/backend.jar').read_bytes()).hexdigest()
checks['status']='PASS'
(LAB/'live-cutover.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
compose=str(LAB/'broker.compose.yaml')
(LAB/'final-lab-services.log').write_text(cmd('docker','compose','-f',compose,'logs','--no-color','--tail','300'),encoding='utf-8')
(LAB/'lab-stop.log').write_text(cmd('docker','compose','-f',compose,'stop'),encoding='utf-8')
running=cmd('docker','ps','--filter','label=com.docker.compose.project=reliability-lab-20260910','--format','{{.Names}}').strip()
assert not running,running
(LAB/'lab-stop.json').write_text(json.dumps({'status':'PASS','running':[],'containersAndVolumesRetained':True}),encoding='utf-8')
print(json.dumps(checks,ensure_ascii=False))
