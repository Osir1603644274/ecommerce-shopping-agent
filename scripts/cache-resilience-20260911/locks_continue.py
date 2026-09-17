"""Barrier-controlled lock experiments. JVM startup time must not end holder lifetime."""
import run as lab
import threading,subprocess,time,json
lab.LOG=lab.OUT/'locks-barrier'
lab.LOG.mkdir(exist_ok=False)
monitor=threading.Thread(target=lab.health,daemon=True);monitor.start()
def release(run):
    (lab.OUT/'pids'/f'{run}.release').write_text('release',encoding='utf-8')
try:
    lab.command(['docker','run','--rm','--cpus=0.75','--memory=512m','-e','MAVEN_OPTS=-Xmx256m','-v',str(lab.SRC).replace('\\','/')+':/src:ro','-v',str(lab.OUT).replace('\\','/')+':/out','-v','agent-m2:/root/.m2','-w','/src','maven:3.9-eclipse-temurin-17','mvn','-B','-q','compile'],'build-barrier',timeout=180)
    lab.command(lab.COMPOSE+['up','-d'],'start',timeout=60)
    for mode in ['fixed','watchdog','lease']:
        run='barrier-'+mode;p,log=lab.owner(run,mode,-1);time.sleep(4)
        result=lab.java(run+'-probe','probe',run,mode)[-1]
        assert p.poll() is None,'Holder must still be running at probe completion'
        assert result['acquired']==(mode!='watchdog'),result
        release(run);lab.collect_owner(run,p,log)
    run='barrier-kill';p,log=lab.owner(run,'watchdog',-1);lab.signal(run,'KILL');p.wait(timeout=10);log.close();time.sleep(4)
    assert lab.java(run+'-recover','probe',run,'watchdog')[-1]['acquired']
    run='barrier-pause';p,log=lab.owner(run,'watchdog',-1);lab.signal(run,'STOP');time.sleep(4)
    assert lab.java(run+'-new-owner','probe',run,'watchdog')[-1]['acquired']
    assert lab.redis('GET','effect:'+run)=='new-owner'
    lab.signal(run,'CONT');release(run);lab.collect_owner(run,p,log)
    assert lab.redis('GET','effect:'+run)=='old-owner','Unfenced resumed holder writes after losing lease'
    lab.command(lab.COMPOSE+['stop','redis'],'redis-stop',timeout=30)
    lab.java('redisson-redis-unavailable','probe','barrier-unavailable','watchdog',expected=(1,))
    (lab.LOG/'completed.json').write_text(json.dumps({'status':'COMPLETE','barrierChecks':6,'holderLifetimeControlled':True}),encoding='utf-8')
finally:
    subprocess.run(lab.COMPOSE+['stop'],capture_output=True,timeout=60)
    lab.stop.set();monitor.join(timeout=5)
    (lab.LOG/'steps.json').write_text(json.dumps(lab.steps,indent=2),encoding='utf-8')
    (lab.LOG/'health-summary.json').write_text(json.dumps({'samples':len(lab.observations),'failures':sum(x.get('status')!=200 for x in lab.observations),'guardTripped':lab.unsafe.is_set()},indent=2),encoding='utf-8')
