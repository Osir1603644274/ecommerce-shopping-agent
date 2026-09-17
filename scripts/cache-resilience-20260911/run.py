"""Bounded lab runner. Never controls production containers; 5173 health guard."""
from pathlib import Path
import subprocess, json, time, threading, urllib.request, hashlib, sys, os, statistics, zipfile

ROOT=Path(__file__).resolve().parents[2]
SRC=Path(__file__).resolve().parent
OUT=ROOT/'.runtime/cache-resilience-20260911'
LOG=OUT if '--resume' not in sys.argv else OUT/('attempt%03d'%(2+len(list(OUT.glob('attempt*')))))
COMPOSE=['docker','compose','-f',str(SRC/'compose.yaml')]
JAVA=['docker','compose','-f',str(SRC/'compose.yaml'),'exec','-T','runner','java','-Xmx128m','-Xss256k','-XX:ActiveProcessorCount=1','-Dorg.slf4j.simpleLogger.defaultLogLevel=warn','-cp','/lab/classes:/lab/dependency/*','lab.CacheLab']
stop=threading.Event(); unsafe=threading.Event(); observations=[]; steps=[]

def health():
    failures=0
    with (LOG/'health.jsonl').open('a',encoding='utf-8',buffering=1) as f:
        while not stop.is_set():
            t=time.monotonic(); row={'time':time.time()}
            try:
                with urllib.request.urlopen('http://127.0.0.1:5173/',timeout=3) as response: row['status']=response.status;response.read(64)
                failures=0 if row['status']==200 else failures+1
            except Exception as e:row['error']=str(e);failures+=1
            row['latencyMs']=(time.monotonic()-t)*1000;observations.append(row);f.write(json.dumps(row)+'\n')
            if failures>=2:
                unsafe.set();subprocess.run(COMPOSE+['stop'],capture_output=True,timeout=40);return
            stop.wait(5)

def command(args,name,expected=(0,),timeout=180):
    if unsafe.is_set():raise RuntimeError('5173 health guard tripped')
    t=time.monotonic();p=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=timeout)
    (LOG/(name+'.log')).write_text(p.stdout+'\nSTDERR:\n'+p.stderr,encoding='utf-8')
    results=[json.loads(line[7:]) for line in p.stdout.splitlines() if line.startswith('RESULT:')]
    row={'name':name,'returncode':p.returncode,'durationMs':(time.monotonic()-t)*1000,'results':results}
    steps.append(row);(LOG/'steps.json').write_text(json.dumps(steps,indent=2),encoding='utf-8')
    print(json.dumps({'step':name,'code':p.returncode,'result':results[-1:]},ensure_ascii=False),flush=True)
    assert p.returncode in expected,(name,p.stderr[-1500:]);return results

def java(name,*args,**kw):return command(JAVA+list(args),name,**kw)
def redis(*args):
    p=subprocess.run(COMPOSE+['exec','-T','redis','redis-cli','--raw',*args],capture_output=True,text=True,timeout=8);p.check_returncode();return p.stdout.strip()
def wait_ready(run):
    for _ in range(100):
        if redis('GET','ready:'+run)=='yes':return
        time.sleep(.2)
    raise RuntimeError('owner not ready '+run)
def owner(run,mode,hold):
    log=(LOG/(run+'-owner.log')).open('w',encoding='utf-8')
    p=subprocess.Popen(JAVA+['owner',run,mode,str(hold)],stdout=log,stderr=subprocess.STDOUT)
    wait_ready(run);return p,log
def signal(run,sig):
    pid=(OUT/'pids'/f'{run}.pid').read_text().strip();assert pid.isdigit()
    return command(COMPOSE+['exec','-T','runner','kill','-'+sig,pid],run+'-'+sig)
def collect_owner(run,p,log,expected=0):
    rc=p.wait(timeout=25);log.close();assert rc==expected,(run,rc)
    steps.append({'name':run+'-owner','returncode':rc,'effect':redis('GET','effect:'+run)})

def main():
    if LOG==OUT:OUT.mkdir(exist_ok=False)
    else:LOG.mkdir(exist_ok=False)
    baseline=subprocess.check_output(['docker','ps','--format','{{.Names}} {{.Image}} {{.Status}}'],text=True)
    (LOG/'production-before.txt').write_text(baseline,encoding='utf-8')
    worker=threading.Thread(target=health,daemon=True);worker.start()
    try:
        command(['docker','run','--rm','--cpus=0.75','--memory=512m','-e','MAVEN_OPTS=-Xmx256m','-v',str(SRC).replace('\\','/')+':/src:ro','-v',str(OUT).replace('\\','/')+':/out','-v','agent-m2:/root/.m2','-w','/src','maven:3.9-eclipse-temurin-17','mvn','-B','-q','compile','dependency:copy-dependencies','-DoutputDirectory=/out/dependency'],'build',timeout=480)
        command(COMPOSE+['up','-d'],'start',timeout=60)
        for _ in range(60):
            p=subprocess.run(COMPOSE+['exec','-T','mysql','mysql','--protocol=TCP','-h127.0.0.1','-uroot','-pisolated-lab-only','-e','SELECT 1'],capture_output=True)
            if p.returncode==0:break
            time.sleep(1)
        java('init','init')
        java('bloom','bloom',timeout=600)
        java('prime','prime');before=java('before-rollback','status')[-1]
        after=java('atomic-rollback','rollback')[-1]
        assert before['products']==after['products'] and before['tasks']==after['tasks']
        java('durable-update','update');java('crash-before-delete','worker','before-delete',expected=(86,))
        assert redis('GET','inv:1')=='1'
        command(COMPOSE+['stop','redis'],'redis-stop',timeout=30)
        for i in range(2):assert java('redis-down-retry-'+str(i),'worker','normal')[-1]['status']=='RETRY_PENDING'
        command(COMPOSE+['start','redis'],'redis-restart',timeout=30)
        time.sleep(1);assert java('recover-delete','worker','normal')[-1]['status']=='DONE';assert redis('EXISTS','inv:1')=='0'
        java('prime-second','prime');java('second-update','update');java('crash-after-delete','worker','after-delete',expected=(87,))
        assert java('recover-after-delete','worker','normal')[-1]['status']=='DONE'
        java('durable-final-status','status');java('missed-pubsub-ttl','pubsub');java('late-refill-race','race')
        for mode in ['fixed','watchdog','lease']:
            run='long-'+mode;p,log=owner(run,mode,10000);time.sleep(1.5)
            result=java(run+'-probe','probe',run,mode)[-1];assert result['acquired']==(mode!='watchdog')
            collect_owner(run,p,log)
        run='killed-watchdog';p,log=owner(run,'watchdog',20000);signal(run,'KILL');p.wait(timeout=10);log.close();time.sleep(3.5)
        assert java(run+'-recover','probe',run,'watchdog')[-1]['acquired']
        run='paused-watchdog';p,log=owner(run,'watchdog',6500);signal(run,'STOP');time.sleep(4)
        assert java(run+'-new-owner','probe',run,'watchdog')[-1]['acquired'];signal(run,'CONT');collect_owner(run,p,log)
        assert redis('GET','effect:'+run)=='old-owner'
        command(COMPOSE+['stop','redis'],'lock-redis-stop',timeout=30)
        java('redisson-redis-unavailable','probe','unavailable','watchdog',expected=(1,))
        command(COMPOSE+['start','redis'],'lock-redis-restart',timeout=30)
        (LOG/'completed.json').write_text(json.dumps({'status':'COMPLETE','steps':len(steps),'productionSwitched':False}),encoding='utf-8')
    finally:
        subprocess.run(COMPOSE+['stop'],capture_output=True,timeout=60)
        time.sleep(1);stop.set();worker.join(timeout=5)
        (LOG/'steps.json').write_text(json.dumps(steps,indent=2),encoding='utf-8')
        (LOG/'health-summary.json').write_text(json.dumps({'samples':len(observations),'failures':sum(x.get('status')!=200 for x in observations),'guardTripped':unsafe.is_set(),'maxLatencyMs':max((x['latencyMs'] for x in observations),default=None)},indent=2),encoding='utf-8')
        (LOG/'production-after.txt').write_text(subprocess.check_output(['docker','ps','--format','{{.Names}} {{.Image}} {{.Status}}'],text=True),encoding='utf-8')

if __name__=='__main__':main()
