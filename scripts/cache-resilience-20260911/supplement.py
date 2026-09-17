"""Execute preserved deployed ProductCache bytecode; no production service calls except health."""
import run as lab
from pathlib import Path
import threading,time,json,subprocess,hashlib,zipfile

lab.LOG=lab.OUT/'production-cache-probe'
lab.LOG.mkdir(exist_ok=False)
base=lab.ROOT/'.runtime/product-read-experiment-20260910/backend.jar'
release=lab.ROOT/'.runtime/pool-protection-20260911-v3/release.jar'
member='BOOT-INF/classes/com/example/locallife/product/ProductCache.class'
with zipfile.ZipFile(base) as a,zipfile.ZipFile(release) as b:
    assert a.read(member)==b.read(member)
    binding={'classMember':member,'classSha256':hashlib.sha256(a.read(member)).hexdigest(),'baseJarSha256':hashlib.sha256(base.read_bytes()).hexdigest(),'v3JarSha256':hashlib.sha256(release.read_bytes()).hexdigest(),'productCacheByteEqual':True}
    (lab.LOG/'source-binding.json').write_text(json.dumps(binding,indent=2),encoding='utf-8')
cmd=['docker','run','--rm','--network','cache-resilience-20260911_default','--cpus=0.5','--memory=256m','-v',str(lab.OUT).replace('\\','/')+':/lab:ro','-v',str(lab.ROOT/'.runtime/product-read-experiment-20260910/unpacked').replace('\\','/')+':/base:ro','eclipse-temurin:17-jre','java','-Xmx96m','-XX:ActiveProcessorCount=1','-cp','/lab/production-probe:/base/BOOT-INF/classes:/base/BOOT-INF/lib/*','com.example.locallife.product.ProductionCacheProbe']
monitor=threading.Thread(target=lab.health,daemon=True);monitor.start()
try:
    lab.command(lab.COMPOSE+['up','-d','redis','runner'],'start-redis',timeout=60)
    lab.command(cmd+['lock'],'actual-cache-lock',timeout=90)
    lab.command(cmd+['ttl'],'actual-cache-missed-invalidation',timeout=90)
    assert lab.redis('SET','lock:migration-format','manual-owner','PX','60000','NX')=='OK'
    lab.java('redisson-existing-string-lock','probe','migration-format','watchdog',expected=(1,))
    assert 'WRONGTYPE' in (lab.LOG/'redisson-existing-string-lock.log').read_text(encoding='utf-8')
    assert lab.java('redisson-different-key','probe','migration-format-new','watchdog')[-1]['acquired']
    assert lab.redis('GET','lock:migration-format')=='manual-owner'
    lab.redis('DEL','lock:migration-format')
    lab.command(lab.COMPOSE+['stop','redis'],'stop-redis',timeout=30)
    lab.command(cmd+['outage'],'actual-cache-redis-outage',timeout=90)
    (lab.LOG/'completed.json').write_text(json.dumps({'status':'COMPLETE','productionBytecodeProbes':3}),encoding='utf-8')
finally:
    subprocess.run(lab.COMPOSE+['stop'],capture_output=True,timeout=60)
    lab.stop.set();monitor.join(timeout=5)
    (lab.LOG/'health-summary.json').write_text(json.dumps({'samples':len(lab.observations),'failures':sum(x.get('status')!=200 for x in lab.observations),'guardTripped':lab.unsafe.is_set()},indent=2),encoding='utf-8')
