"""Reuse the recorded HTTP harness without changing the sealed diagnosis."""
import asyncio,hashlib,json,shutil,argparse
from pathlib import Path
import run_multi

BASE=Path('D:/agent-datasets/search-agent-repair-20260913-v1')
p=argparse.ArgumentParser();p.add_argument('--attempt',default='live003');p.add_argument('--inputs',default='final-live-inputs.json');args=p.parse_args()
ROOT=BASE/args.attempt
ROOT.mkdir(exist_ok=False)
shutil.copyfile(BASE/args.inputs,ROOT/'multi-inputs.json')
REPO=Path(__file__).resolve().parents[2]
names=['agent/app/catalog_conversation.py','agent/app/catalog_requirements.py','agent/app/catalog_service.py','agent/app/api/catalog_workspace.py']
binding={n:hashlib.sha256((REPO/n).read_bytes()).hexdigest() for n in names}
def verify():
    assert all(hashlib.sha256((REPO/n).read_bytes()).hexdigest()==h for n,h in binding.items())
    assert hashlib.sha256((ROOT/'multi-inputs.json').read_bytes()).hexdigest()==inputs_sha
inputs_sha=hashlib.sha256((ROOT/'multi-inputs.json').read_bytes()).hexdigest()
run_multi.ROOT=ROOT
run_multi.verify=verify
with (ROOT/'LIVE-CONTRACT.json').open('x',encoding='utf8') as f:
    json.dump({'code':binding,'inputSha':inputs_sha,'expectedTurns':sum(len(d['turns']) for d in json.loads((ROOT/'multi-inputs.json').read_text('utf8'))),
       'note':'old harness complete flag compares to24; use expectedTurns here; all turns retained'},f,indent=2)
asyncio.run(run_multi.main())
