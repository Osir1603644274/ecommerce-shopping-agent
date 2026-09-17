"""Continue this one release's validation/backup work; never activate the public entry."""
import json,subprocess,sys,time
from prepare import OUT,ROOT,write

script=ROOT/'scripts/commerce-full-release-20260915/backup-candidate.py'
started=time.time()
while True:
    path=OUT/'FINAL-INVARIANTS.json'
    if path.exists():
        report=json.loads(path.read_text(encoding='utf8'))
        if report['status']=='PASS':break
    # This worker only observes files. The root agent still checks the live import.
    if time.time()-started>6*3600:raise TimeoutError('validation did not finish within release window')
    time.sleep(30)
print('Full SQL verification passed; starting cold backup and independent restore.',flush=True)
subprocess.run([sys.executable,'-X','utf8',str(script.with_name('verify-read-plans.py'))],check=True)
with (OUT/'backup-candidate.log').open('x',encoding='utf8') as log:
    result=subprocess.run([sys.executable,'-X','utf8',str(script)],stdout=log,stderr=subprocess.STDOUT)
if result.returncode:raise RuntimeError('backup/restore failed; preserve the partial evidence and inspect')
assert json.loads((OUT/'CANDIDATE-BACKUP-RESTORE.json').read_text())['status']=='PASS'
print('Backup and independent restore PASS. Original-row refresh and public activation remain pending.',flush=True)
