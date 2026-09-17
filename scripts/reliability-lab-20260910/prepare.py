"""Create a new, bounded lab. Uses preserved deployed classes, never live data."""
import json, shutil, hashlib
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[2]
DEST=ROOT/'.runtime/reliability-lab-20260910'
assert not DEST.exists(), 'Do not overwrite a lab attempt'
DEST.mkdir()
(DEST/'classes').mkdir()
base=yaml.safe_load((ROOT/'.runtime/product-read-experiment-20260910/compose.yaml').read_text())
base['name']='reliability-lab-20260910'
backend=base['services'].pop('backend')
backend['volumes']=[str(DEST).replace('\\','/')+':/lab:ro',str(ROOT/'.runtime/product-read-experiment-20260910/unpacked').replace('\\','/')+':/base:ro']
backend['working_dir']='/lab'
backend['command']=['java','-Xmx384m','-XX:ActiveProcessorCount=1','-cp','classes:/base/BOOT-INF/classes:/base/BOOT-INF/lib/*','com.example.locallife.LocalLifeApplication']
backend['environment'].update(SPRING_DATA_REDIS_TIMEOUT='1s',SPRING_DATA_REDIS_CONNECT_TIMEOUT='5s',MESSAGE_STALE_CLAIM_AFTER='PT1S')
for i in (1,2):
    app=json.loads(json.dumps(backend));app['ports']=[f'127.0.0.1:{38481+i}:8080'];base['services'][f'app{i}']=app
base['services']['redis']['ports']=['127.0.0.1:38484:6379']
(DEST/'compose.yaml').write_text(yaml.safe_dump(base,sort_keys=False),encoding='utf-8')
for name in ('CacheLabController.java','MessageFaultController.java','run.py','faults.py','broker.py'):
    shutil.copy2(Path(__file__).with_name(name),DEST/name)
(DEST/'provenance.json').write_text(json.dumps({'baseJarSha256':hashlib.sha256((ROOT/'.runtime/product-read-experiment-20260910/backend.jar').read_bytes()).hexdigest(),
    'sources':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(__file__).parent.glob('*') if p.is_file()},
    'productionCacheChanged':False,'adapters':'L1/L2 experimental adapters; TWO is actual ProductCache; no eviction workload'},indent=2),encoding='utf-8')
print(DEST)
