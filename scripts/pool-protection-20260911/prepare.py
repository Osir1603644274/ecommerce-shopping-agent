"""Package a minimal patch over the deployed release; create no production test endpoint."""
from pathlib import Path
import hashlib,json,zipfile,subprocess,yaml
ROOT=Path(__file__).resolve().parents[2]
DEST=ROOT/'.runtime/pool-protection-20260911-v3'
DEST.mkdir(exist_ok=False)
base=ROOT/'.runtime/product-read-release-20260910/backend.jar'
assert hashlib.sha256(base.read_bytes()).hexdigest()=='eeb1d2950f136ef9f6f7e2f1ecc6a068c9309fc25912efc7d36a75a93a1edbde'
classes=ROOT/'backend/target/classes/com/example/locallife/product'
names=['CatalogReadBudget','CatalogReadAdmissionFilter','CatalogSqlTimeoutInterceptor','LocalOfferService','ProductOfferController']
members={}
for name in names:
    for p in classes.glob(name+'*.class'):
        members['BOOT-INF/classes/com/example/locallife/product/'+p.name]=p.read_bytes()
    p=ROOT/f'backend/src/main/java/com/example/locallife/product/{name}.java'
    members['BOOT-INF/classes/observer-source/com/example/locallife/product/'+p.name]=p.read_bytes()
assert all('BOOT-INF/classes/com/example/locallife/product/'+n+'.class' in members for n in names)
with zipfile.ZipFile(base) as source, zipfile.ZipFile(DEST/'release.jar','x') as out:
    for info in source.infolist():out.writestr(info,members.pop(info.filename,source.read(info.filename)))
    for name,data in members.items():out.writestr(name,data)
with zipfile.ZipFile(base) as source,zipfile.ZipFile(DEST/'release.jar') as target:
    changed={n:hashlib.sha256(target.read(n)).hexdigest() for n in target.namelist() if n not in source.namelist() or target.read(n)!=source.read(n)}
    assert all(any('/'+n+'.' in p or '/'+n+'$' in p for n in names) for p in changed)
    assert not any('PoolLab' in n for n in target.namelist())
manifest={'baseSha256':hashlib.sha256(base.read_bytes()).hexdigest(),'releaseSha256':hashlib.sha256((DEST/'release.jar').read_bytes()).hexdigest(),'changedMembers':changed,'otherMembersByteEqual':True}
(DEST/'release-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
(DEST/'lab-classes').mkdir()
subprocess.run(['docker','run','--rm','-v',f'{ROOT.as_posix()}:/repo','-w','/repo','eclipse-temurin:17-jdk','javac','-parameters','-cp','.runtime/product-read-experiment-20260910/unpacked/BOOT-INF/lib/*','-d','.runtime/pool-protection-20260911-v3/lab-classes','scripts/pool-protection-20260911/PoolLabController.java'],check=True)
with zipfile.ZipFile(DEST/'release.jar') as source,zipfile.ZipFile(DEST/'lab.jar','x') as out:
    for info in source.infolist():out.writestr(info,source.read(info.filename))
    for p in (DEST/'lab-classes').rglob('*.class'):out.writestr('BOOT-INF/classes/'+p.relative_to(DEST/'lab-classes').as_posix(),p.read_bytes())
original=yaml.safe_load((ROOT/'.runtime/reliability-lab-20260910/compose.yaml').read_text())['services']['app1']
compose={'name':'pool-protection-20260911-v3','services':{},'networks':{'default':{'external':True,'name':'reliability-lab-20260910_default'}}}
for i,name in enumerate(['baseline','fixed']):
    app=json.loads(json.dumps(original));app['volumes']=[DEST.as_posix()+':/lab:ro'];app['command']=['java','-Xmx384m','-XX:ActiveProcessorCount=1','-jar','/lab/lab.jar']
    app['ports']=[f'127.0.0.1:{38512+i}:8080']
    app['environment']['LOCAL_LIFE_CATALOG_READ_ENABLED']='false' if name=='baseline' else 'true'
    app['environment']['LOCAL_LIFE_CATALOG_READ_MAX_CONCURRENT']='0'
    app['environment']['LOCAL_LIFE_CATALOG_READ_SQL_TIMEOUT_SECONDS']='2'
    compose['services'][name]=app
(DEST/'compose.yaml').write_text(yaml.safe_dump(compose,sort_keys=False),encoding='utf-8')
print(DEST)
