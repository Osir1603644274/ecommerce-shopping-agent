"""Run the current local demo stack; preserve prior databases and foreign processes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import uuid

import httpx
import psutil
import pymysql


def serve(runtime, reuse_database_from=None):
    root = Path(__file__).resolve().parents[2]
    base = root / 'docs/implementation/customer-support-20260919/runtime'
    for port in (18080, 18000, 5173):
        with socket.socket() as probe:
            if os.name == 'nt':
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', port))
    runtime.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    for line in (base / 'live-001/service.env').read_text().splitlines():
        if '=' in line:
            key, value = line.split('=', 1)
            env[key] = value
    secrets = dict(line.split('=', 1) for line in (base / 'mysql-remote-002/test.env').read_text().splitlines() if '=' in line)
    source = json.loads((base / 'inventory-network-001/private.json').read_text())
    config = {k: source[k] for k in ('host', 'port', 'user', 'password')}
    if config['host'] != '127.0.0.1':
        raise ValueError('loopback database required')
    if reuse_database_from is not None:
        config = json.loads((reuse_database_from / 'private.json').read_text())
        if (config.get('ownedNativeRuntime') is not True or config.get('host') != '127.0.0.1'
                or not config.get('database', '').startswith('support_demo_')
                or config.get('authority') != 'http://127.0.0.1:18080'):
            raise ValueError('only an owned loopback demo database may be reused')
        database = config['database']
    else:
        database = 'support_demo_' + uuid.uuid4().hex[:12]
        config.update(database=database, authority='http://127.0.0.1:18080', ownedNativeRuntime=True)
        config['paymentCallbackSecret'] = env.get('PAYMENT_CALLBACK_SECRET')
        with pymysql.connect(host=config['host'], port=config['port'], user='root', password=secrets['MYSQL_ROOT_PASSWORD'], autocommit=True) as db, db.cursor() as cursor:
            cursor.execute('CREATE DATABASE ' + database)
            cursor.execute('GRANT ALL PRIVILEGES ON ' + database + '.* TO %s@%s', (config['user'], '%'))
    (runtime / 'private.json').write_text(json.dumps(config), encoding='utf-8')
    env.update(SERVER_PORT='18080', SERVER_ADDRESS='127.0.0.1',
        SPRING_DATASOURCE_URL=f"jdbc:mysql://127.0.0.1:{config['port']}/{database}?allowPublicKeyRetrieval=true&useSSL=false&serverTimezone=UTC",
        DB_USER=config['user'], DB_PASSWORD=config['password'], REDIS_HOST='127.0.0.1', REDIS_PORT='16379',
        LOCAL_LIFE_SUPPORT_ENABLED='true', LOCAL_LIFE_SUPPORT_SIMULATOR_ENABLED='true',
        LOCAL_LIFE_SUPPORT_RECOVERY_ENABLED='false', LOCAL_LIFE_FULFILLMENT_ENABLED='true',
        LOCAL_LIFE_FULFILLMENT_WORKER_ENABLED='false', PAYMENT_SIMULATOR_ENABLED='true',
        LOCAL_LIFE_INVENTORY_REMOTE_ENABLED='false', LOCAL_LIFE_INVENTORY_RECOVERY_ENABLED='false')
    jar = root / 'backend/target/local-life-backend-0.1.0-SNAPSHOT.jar'
    manifest = {'scope': 'Local demo availability, not acceptance', 'database': database,
        'jarSha256': hashlib.sha256(jar.read_bytes()).hexdigest(), 'services': {}}
    processes, logs = [], []
    def start(name, command, cwd, process_env, port, path):
        log = (runtime / (name + '.log')).open('w', encoding='utf-8')
        logs.append(log)
        process = subprocess.Popen(command, cwd=cwd, env=process_env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        processes.append(process)
        deadline = time.monotonic() + 100
        with httpx.Client(timeout=2, trust_env=False) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(name + ' exited; inspect private runtime log')
                try:
                    owned = {process.pid, *[p.pid for p in psutil.Process(process.pid).children(recursive=True)]}
                    listeners = {c.pid for c in psutil.net_connections(kind='tcp') if c.status == 'LISTEN' and c.laddr.port == port}
                    origin = f'http://127.0.0.1:{port}'
                    if listeners and listeners <= owned and client.get(origin + path, headers={'Origin': origin}).status_code == 200:
                        manifest['services'][name] = {'pid': process.pid, 'port': port, 'ready': True}
                        print(name + ' ready on ' + str(port), flush=True)
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(.5)
        raise TimeoutError(name + ' readiness')
    try:
        start('java', ['C:/Users/ming/.jdks/jdk-21/jdk-21.0.10/bin/java.exe', '-Duser.timezone=UTC', '-jar', str(jar)], root, env, 18080, '/actuator/health')
        # Existing foreground scripts set the BFF feature flags and frontend proxy.
        shell = 'powershell.exe'
        start('bff', [shell, '-NoProfile', '-File', str(root / 'scripts/customer_support/run_live_bff.ps1')], root, os.environ.copy(), 18000, '/api/commerce-demo/workspace')
        start('frontend', [shell, '-NoProfile', '-File', str(root / 'scripts/customer_support/run_support_frontend.ps1')], root, os.environ.copy(), 5173, '/')
        (runtime / 'PUBLIC_STATE.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        print('DEMO_READY: http://127.0.0.1:5173; Ctrl+C stops only owned services.', flush=True)
        while all(p.poll() is None for p in processes):
            time.sleep(1)
        raise RuntimeError('an owned service exited')
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                children = psutil.Process(process.pid).children(recursive=True)
                for child in reversed(children):
                    try:
                        child.kill()
                    except psutil.NoSuchProcess:
                        pass
                process.kill()
                process.wait(timeout=15)
        for log in logs:
            log.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--reuse-database-from', type=Path)
    args = parser.parse_args()
    serve(args.runtime.resolve(), args.reuse_database_from.resolve() if args.reuse_database_from else None)
