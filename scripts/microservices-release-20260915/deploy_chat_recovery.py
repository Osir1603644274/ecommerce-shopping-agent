"""Guarded application-only rollout; preserve stopped originals for rollback.

No schema/data migration. Only four explicitly owned catalog/trade containers.
Run after freezing the public Agent; recovery: --rollback.
"""
import json
import subprocess
import sys
import time
from pathlib import Path
import httpx
from live_release import ROOT, PRIVATE, STATE, NAMES, PORTS, owned, save

OUT = Path('D:/agent-experiments/chat-recovery-20260916/deployment')
TAG = 'chat-recovery-20260916-v1'
IMAGE = 'agent-backend:' + TAG
ROLES = ('catalog-a', 'catalog-b', 'trade-a', 'trade-b')
JOURNAL = OUT / 'journal.json'
MAINTENANCE = ROOT / '.runtime/merged-commerce/microservices-maintenance.json'

def docker(*args):
    result = subprocess.run(['docker', *args], capture_output=True, timeout=240)
    if result.returncode:
        raise RuntimeError('Docker operation failed: ' + args[0] + '; arguments withheld')
    return result.stdout.decode('utf8')

def write(path, value):
    pending = path.with_suffix(path.suffix + '.tmp')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    pending.replace(path)

def inspect(name):
    names = docker('ps', '-a', '--format', '{{.Names}}').splitlines()
    return json.loads(docker('inspect', name))[0] if name in names else None

def wait(role):
    url = 'http://127.0.0.1:' + str(PORTS[role]) + '/actuator/health'
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=3, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError('Health check timed out: ' + role)

def rollback(journal):
    for role in reversed(ROLES):
        original = journal['original'][role]
        backup = inspect(original['backupName'])
        if backup is None:
            continue
        assert backup['Id'] == original['id'], 'backup identity drift'
        current = inspect(NAMES[role])
        if current:
            assert current['Config']['Labels'].get('chat-recovery.release') == TAG
            assert current['Image'] == journal['newImage']
            # This new application container has no mounts; business data is elsewhere.
            assert not current['Mounts']
            docker('stop', '--time', '20', NAMES[role])
            docker('rm', NAMES[role])
        docker('rename', original['backupName'], NAMES[role])
        docker('start', NAMES[role])
        wait(role)
    write(PRIVATE, json.loads((OUT / 'topology.before.private.json').read_text()))
    save(json.loads((OUT / 'release.before.json').read_text()))
    journal['status'] = 'ROLLED_BACK'
    write(JOURNAL, journal)
    if MAINTENANCE.exists():
        assert json.loads(MAINTENANCE.read_text()).get('owner') == TAG
        MAINTENANCE.unlink()

def deploy():
    assert not JOURNAL.exists(), 'Existing rollout journal: inspect or explicitly rollback first'
    assert not MAINTENANCE.exists(), 'Another maintenance operation owns the deployment'
    OUT.mkdir(parents=True, exist_ok=True)
    config = json.loads(PRIVATE.read_text())
    state = json.loads(STATE.read_text())
    assert config['images'] == state['images']
    new_image = json.loads(docker('image', 'inspect', IMAGE))[0]['Id']
    journal = {'status': 'PREPARED', 'newImage': new_image, 'original': {}}
    for role in ROLES:
        info = owned(NAMES[role])
        assert info['Image'] == state['images']['backend'] and not info['Mounts']
        assert set(info['NetworkSettings']['Networks']) == {'agent_default'}
        assert info['HostConfig']['PortBindings']['8080/tcp'] == [{'HostIp': '127.0.0.1', 'HostPort': str(PORTS[role])}]
        backup = NAMES[role] + '-before-' + TAG
        assert inspect(backup) is None
        journal['original'][role] = {'id': info['Id'], 'backupName': backup, 'image': info['Image']}
        (OUT / (role + '.env.private')).write_text('\n'.join(info['Config']['Env']) + '\n', encoding='utf8')
    write(OUT / 'topology.before.private.json', config)
    write(OUT / 'release.before.json', state)
    write(JOURNAL, journal)
    write(MAINTENANCE, {'owner': TAG, 'purpose': 'application rollout only; no data migration'})
    try:
        for role in ROLES:
            print('Replacing owned application:', role, flush=True)
            assert owned(NAMES[role])['Id'] == journal['original'][role]['id']
            docker('stop', '--time', '30', NAMES[role])
            docker('rename', NAMES[role], journal['original'][role]['backupName'])
            docker('run', '-d', '--pull=never', '--name', NAMES[role],
                   '--label', 'microservices.attempt=20260915-a1', '--label', 'chat-recovery.release=' + TAG,
                   '--network', 'agent_default', '--memory', '512m', '--cpus', '1', '--pids-limit', '256',
                   '--env-file', str(OUT / (role + '.env.private')), '-p',
                   '127.0.0.1:' + str(PORTS[role]) + ':8080', new_image)
            wait(role)
        config['images']['backend'] = new_image
        state['images']['backend'] = new_image
        state['applicationRevision'] = TAG
        write(PRIVATE, config)
        save(state)
        journal['status'] = 'APPLICATIONS_HEALTHY_PENDING_UI_ACCEPTANCE'
        write(JOURNAL, journal)
        MAINTENANCE.unlink()
        print('All four applications healthy; originals retained. UI acceptance still required.', flush=True)
    except BaseException:
        rollback(journal)
        raise

if __name__ == '__main__':
    if sys.argv[1:] == ['--deploy']:
        deploy()
    elif sys.argv[1:] == ['--rollback']:
        rollback(json.loads(JOURNAL.read_text()))
    else:
        raise SystemExit('Explicit --deploy or --rollback required')
