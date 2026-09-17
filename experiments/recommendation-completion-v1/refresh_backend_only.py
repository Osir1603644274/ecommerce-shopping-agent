"""Refresh only the registered local Agent process, preserving its exact environment.

Never starts Java/Docker/search, never prints inherited secrets, never kills unowned processes.
"""
import datetime, json, os, subprocess, time
from pathlib import Path
import psutil, requests

ROOT=Path(__file__).resolve().parents[2]
REGISTRY=ROOT/'.runtime/merged-commerce/processes.json'

def main():
    original=REGISTRY.read_bytes();state=json.loads(original);record=state['agent'];process=psutil.Process(record['pid'])
    creation=datetime.datetime.fromisoformat(record['createdAtUtc'].replace('Z','+00:00')).timestamp()
    assert abs(process.create_time()-creation)<.02 and record['marker'] in ' '.join(process.cmdline())
    environment=process.environ()  # Do not emit environment values, credentials or command-line config.
    assert environment.get('REDIS_URL')=='redis://[::1]:6379/0'
    environment['OPENBLAS_NUM_THREADS']='1';environment['OMP_NUM_THREADS']='2'
    # Explicitly retain the selected source path and model bundle; all other settings are inherited.
    environment['RECOMMENDATION_BUNDLE_DIR']='D:/agent-datasets/recommendation-completion-v1/serving-v1'
    logdir=ROOT/'.runtime/recommendation-completion';logdir.mkdir(exist_ok=True)
    command=[str(ROOT/'.venv/Scripts/python.exe'),'-m','uvicorn','agent.app.main:app','--host','127.0.0.1','--port','8000']
    # No descendants are expected for this remote-search BFF. Refuse ambiguous ownership.
    assert not process.children(recursive=True),'Agent has children; inspect instead of broad termination'
    process.terminate();process.wait(timeout=10)
    with (logdir/'agent.out.log').open('ab') as out,(logdir/'agent.err.log').open('ab') as err:
        child=subprocess.Popen(command,cwd=ROOT,env=environment,stdout=out,stderr=err,creationflags=subprocess.CREATE_NO_WINDOW)
    deadline=time.monotonic()+100;owner=None
    while time.monotonic()<deadline:
        try:
            response=requests.get('http://127.0.0.1:8000/health',timeout=2)
            if response.status_code==200:
                for p in [psutil.Process(child.pid),*psutil.Process(child.pid).children(recursive=True)]:
                    if p.name().lower()=='python.exe' and p.pid!=child.pid:owner=p
                owner=owner or psutil.Process(child.pid);break
        except (requests.RequestException,psutil.Error):pass
        if child.poll() is not None:raise RuntimeError('Agent restart failed; inspect dedicated logs')
        time.sleep(1)
    assert owner is not None,'Agent readiness timed out; process left available for diagnosis'
    assert REGISTRY.read_bytes()==original,'Registry concurrently changed; do not overwrite'
    state['agent']={'marker':record['marker'],'pid':owner.pid,
        'createdAtUtc':datetime.datetime.fromtimestamp(owner.create_time(),datetime.timezone.utc).isoformat().replace('+00:00','Z')}
    backup=logdir/'processes.before.json'
    if not backup.exists():backup.write_bytes(original)
    temp=REGISTRY.with_suffix('.recommendation.partial');temp.write_text(json.dumps(state,indent=2),encoding='utf-8');os.replace(temp,REGISTRY)
    (logdir/'RESTART.json').write_text(json.dumps({'status':'HEALTHY','old_pid':record['pid'],'new_pid':owner.pid,
        'other_services_restarted':False,'environment':'inherited without disclosure','port':8000},indent=2))
    print('Registered Agent refreshed; other services untouched; port 8000 healthy.')

if __name__=='__main__':main()
