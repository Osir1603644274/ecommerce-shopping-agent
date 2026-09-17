"""Archive targeted evidence without credentials or user dialogue; retain lab resources."""
from pathlib import Path
import json,subprocess,hashlib,zipfile,xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[2];LAB=ROOT/'.runtime/pool-protection-20260911-v3';DEST=ROOT/'docs/experiments/pool-protection-20260911-v3'
DEST.mkdir(exist_ok=True)
def cmd(*args):return subprocess.check_output(args,cwd=ROOT,stderr=subprocess.STDOUT).decode('utf-8',errors='replace')
assert json.loads((LAB/'results.json').read_text())['status']=='PASS'
assert json.loads((LAB/'canary-result.json').read_text())['status']=='PASS'
assert json.loads((LAB/'live-cutover.json').read_text())['status']=='PASS'
for name in ['baseline','fixed']:
    (LAB/f'{name}.log').write_text(cmd('docker','logs','--tail','250',f'pool-protection-20260911-v3-{name}-1'),encoding='utf-8')
(LAB/'stop.log').write_text(cmd('docker','compose','-f',str(LAB/'compose.yaml'),'stop'),encoding='utf-8')
cmd('docker','compose','-f',str(ROOT/'.runtime/reliability-lab-20260910/compose.yaml'),'stop','mysql','redis')
assert not cmd('docker','ps','--filter','label=com.docker.compose.project=pool-protection-20260911-v3','--format','{{.Names}}').strip()
tests=[];reportfiles=[]
for p in (ROOT/'backend/target/surefire-reports').glob('TEST-*.xml'):
    if not any(p.name.endswith(n+'.xml') for n in ['CatalogReadProtectionTests','ProductPurchaseViewControllerTests']):continue
    node=ET.parse(p).getroot();tests.append({'name':node.get('name'),**{k:int(node.get(k,0)) for k in ['tests','failures','errors','skipped']}});reportfiles.append(p)
assert any('CatalogReadProtectionTests' in t['name'] for t in tests)
assert sum(t['failures']+t['errors'] for t in tests)==0
(LAB/'tests.json').write_text(json.dumps(tests,indent=2),encoding='utf-8')
manifest={};archive=DEST/'evidence.zip'
assert not archive.exists()
with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
    files=[('runtime/'+p.relative_to(LAB).as_posix(),p) for p in LAB.rglob('*') if p.is_file() and p.suffix in {'.json','.yaml','.log'}]
    files += [('scripts/'+p.name,p) for p in Path(__file__).parent.iterdir() if p.is_file()]
    files += [('tests/'+p.name,p) for p in reportfiles]
    with zipfile.ZipFile(LAB/'release.jar') as release:
        for name in ['CatalogReadBudget','CatalogReadAdmissionFilter','CatalogSqlTimeoutInterceptor','LocalOfferService','ProductOfferController']:
            b=release.read(f'BOOT-INF/classes/observer-source/com/example/locallife/product/{name}.java')
            n='source/'+name+'.java';z.writestr(n,b);manifest[n]=hashlib.sha256(b).hexdigest()
    for name,p in files:
        b=p.read_bytes();z.writestr(name,b);manifest[name]=hashlib.sha256(b).hexdigest()
with zipfile.ZipFile(archive) as z:assert all(hashlib.sha256(z.read(n)).hexdigest()==h for n,h in manifest.items())
(DEST/'manifest.json').write_text(json.dumps({'archiveSha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'members':manifest},indent=2),encoding='utf-8')
print(json.dumps({'tests':sum(t['tests'] for t in tests),'failures':0,'errors':0,'members':len(manifest),'experimentStopped':True}))
