"""Read raw observations, summarize without dropping failed attempts, archive reproducibly."""
from pathlib import Path
import json,hashlib,statistics,zipfile,subprocess,urllib.request
ROOT=Path(__file__).resolve().parents[2]
SRC=Path(__file__).resolve().parent
OUT=ROOT/'.runtime/cache-resilience-20260911'
DOC=ROOT/'docs/experiments/cache-resilience-20260911'
assert (OUT/'locks-barrier/completed.json').exists()
assert (OUT/'production-cache-probe/completed.json').exists()
steps=json.loads((OUT/'attempt003/steps.json').read_text())
assert any(x['name']=='late-refill-race' and x['returncode']==0 for x in steps)
binding=json.loads((OUT/'production-cache-probe/source-binding.json').read_text())
live_sha=subprocess.check_output(['docker','exec','local-life-backend','sha256sum','/app/app.jar'],text=True).split()[0]
assert live_sha==binding['v3JarSha256'],'Live ProductCache provenance changed; do not claim equivalence'
final_health={'deployedJarSha256':live_sha,'checks':{}}
for name,url in {'frontend':'http://127.0.0.1:5173/','agent':'http://127.0.0.1:8000/health','javaReadiness':'http://127.0.0.1:8080/actuator/health/readiness','javaLiveness':'http://127.0.0.1:8080/actuator/health/liveness'}.items():
    with urllib.request.urlopen(url,timeout=5) as response:
        final_health['checks'][name]={'status':response.status}
        if name.startswith('java'):final_health['checks'][name]['body']=json.loads(response.read())
assert all(x['status']==200 for x in final_health['checks'].values())
(OUT/'final-health.json').write_text(json.dumps(final_health,indent=2),encoding='utf-8')
samples=json.loads((OUT/'bloom-samples.json').read_text())
summary=[]
for scenario in ['unique_missing','repeated_missing','normal_95','mixed_50']:
    for mode in ['null','bloom','combined']:
        group=[x for x in samples if x['scenario']==scenario and x['mode']==mode]
        assert len(group)==3
        summary.append({'scenario':scenario,'mode':mode,'requests':sum(x['requests'] for x in group),'sqlPerRound':[x['sql'] for x in group],'p95MsPerRound':[x['p95Ms'] for x in group],'medianP95Ms':statistics.median(x['p95Ms'] for x in group),'redisKeysPerRound':[x['redisKeys'] for x in group],'errors':sum(x['errors'] for x in group),'wrongAnswers':sum(x['wrongAnswers'] for x in group)})
health=[]
for p in OUT.rglob('health.jsonl'):
    health.extend(json.loads(line) for line in p.read_text().splitlines() if line.strip())
result={'bloom':summary,'allRequests':sum(x['requests'] for x in samples),'health':{'samples':len(health),'failures':sum(x.get('status')!=200 for x in health),'maxLatencyMs':max(x['latencyMs'] for x in health)},'claimBoundary':'isolated component prototypes; deployed ProductCache bytecode supplement is separate; no production switch'}
(DOC/'summary.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
members={}
for p in OUT.rglob('*'):
    if p.is_file() and p.suffix in ('.json','.jsonl','.log','.txt') and not any(x in p.parts for x in ('dependency','maven-status')):
        members['observations/'+p.relative_to(OUT).as_posix()]=p.read_bytes()
for p in SRC.glob('*'):
    if p.is_file():members['sources/'+p.name]=p.read_bytes()
for rel in ['product/ProductCache.java','product/ProductService.java','product/ProductAdminService.java','shop/ShopCache.java','flashsale/FlashSaleRedisGateway.java','search/CacheInvalidationPublisher.java','search/ProductChangedProjection.java','integration/ProductSearchEventService.java']:
    p=ROOT/'backend/src/main/java/com/example/locallife'/rel;members['audited-worktree-source/'+rel]=p.read_bytes()
deps={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (OUT/'dependency').glob('*.jar')}
members['dependency-sha256.json']=json.dumps(deps,indent=2).encode()
classes={p.relative_to(OUT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.rglob('*.class')}
members['class-sha256.json']=json.dumps(classes,indent=2).encode()
members['summary.json']=(DOC/'summary.json').read_bytes()
target=DOC/'evidence.zip'
assert not target.exists(),'Never overwrite published evidence'
with zipfile.ZipFile(target,'x',compression=zipfile.ZIP_DEFLATED) as z:
    for name,data in sorted(members.items()):z.writestr(name,data)
manifest={'zipSha256':hashlib.sha256(target.read_bytes()).hexdigest(),'members':{name:hashlib.sha256(data).hexdigest() for name,data in sorted(members.items())}}
(DOC/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
with zipfile.ZipFile(target) as z:
    assert set(z.namelist())==set(manifest['members'])
    assert all(hashlib.sha256(z.read(name)).hexdigest()==digest for name,digest in manifest['members'].items())
print(json.dumps(result,indent=2))
