"""Wait for verification load to finish, run cache then fault campaigns, save logs."""
import json, subprocess, sys, time, urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]/'.runtime/reliability-lab-20260910'
for _ in range(600):
    active=subprocess.run(['docker','ps','-q','--filter','name=^/product-read-verification-20260910$'],capture_output=True,text=True,check=True).stdout.strip()
    if not active:break
    time.sleep(3)
else:raise RuntimeError('Verification still running; no concurrent benchmark')
for port in (38482,38483):
    for _ in range(180):
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=2) as r:
                if r.status==200:break
        except Exception:time.sleep(1)
    else:raise RuntimeError('Lab not healthy')
for script in ('run.py','faults.py','broker.py'):
    log=ROOT/(script+'.log')
    with log.open('x',encoding='utf-8') as f:
        result=subprocess.run([sys.executable,str(ROOT/script)],stdout=f,stderr=subprocess.STDOUT)
    print(json.dumps({'script':script,'exitCode':result.returncode,'log':str(log)}),flush=True)
    if result.returncode:break
