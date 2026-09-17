from pathlib import Path
import subprocess,json,hashlib,zipfile
ROOT=Path(__file__).resolve().parents[2];LAB=ROOT/'.runtime/pool-protection-20260911-v2';DEST=ROOT/'docs/experiments/pool-protection-20260911-v2'
DEST.mkdir(exist_ok=True)
(LAB/'decision.json').write_text(json.dumps({'status':'HOLD','reason':'500 burst returned 315 of 500 as 503; do not deploy fixed four-request admission as default','deployed':False}),encoding='utf-8')
subprocess.run(['docker','compose','-f',str(LAB/'compose.yaml'),'stop'],check=True)
members={}
with zipfile.ZipFile(DEST/'evidence.zip','x',zipfile.ZIP_DEFLATED) as z:
    for p in LAB.rglob('*'):
        if p.is_file() and p.suffix in {'.json','.log','.yaml'}:
            name=p.relative_to(LAB).as_posix();b=p.read_bytes();z.writestr(name,b);members[name]=hashlib.sha256(b).hexdigest()
    for p in Path(__file__).parent.iterdir():
        if p.is_file():
            name='scripts/'+p.name;b=p.read_bytes();z.writestr(name,b);members[name]=hashlib.sha256(b).hexdigest()
    with zipfile.ZipFile(LAB/'release.jar') as release:
        for p in release.namelist():
            if p.startswith('BOOT-INF/classes/observer-source/') and any(n in p for n in ['CatalogRead','CatalogSql','LocalOfferService','ProductOfferController']):
                name='source/'+p.rsplit('/',1)[-1];b=release.read(p);z.writestr(name,b);members[name]=hashlib.sha256(b).hexdigest()
    for name in ['CatalogReadProtectionTests','ProductPurchaseViewControllerTests']:
        p=ROOT/f'backend/target/surefire-reports/TEST-com.example.locallife.product.{name}.xml'
        b=p.read_bytes();z.writestr('tests/'+p.name,b);members['tests/'+p.name]=hashlib.sha256(b).hexdigest()
with zipfile.ZipFile(DEST/'evidence.zip') as z:assert all(hashlib.sha256(z.read(n)).hexdigest()==h for n,h in members.items())
(DEST/'manifest.json').write_text(json.dumps(members,indent=2),encoding='utf-8')
