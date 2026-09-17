"""Create isolated V3 two-instance fault infrastructure; starts no service."""
import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
import yaml


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--jar', type=Path, required=True)
    p.add_argument('--version', choices=['v3','v4'], default='v3')
    a = p.parse_args()
    if not a.jar.is_file():
        raise SystemExit('An existing identified JAR is required')
    root = a.directory.resolve()
    subprocess.run([sys.executable, str(Path(__file__).with_name('prepare_runtime.py')),
                    '--directory', str(root)], check=True)
    shutil.copyfile(a.jar, root / 'backend.jar')
    config = yaml.safe_load(Path(__file__).with_name('compose.yml').read_text(encoding='utf-8'))
    config['name'] = project = 'backend-strengthening-' + a.version
    # A new runtime directory must not silently attach a previous experiment's named volumes.
    volume_scope = hashlib.sha256(str(root).lower().encode('utf-8')).hexdigest()[:12]
    for name in config.get('volumes', {}):
        config['volumes'][name] = {'name': project + '-' + volume_scope + '-' + name}
    original = config['services'].pop('backend')
    for number in (1, 2):
        app = copy.deepcopy(original)
        app['ports'] = [f'127.0.0.1:{38079 + number}:8080']
        app['mem_limit'] = '512m'
        app['command'] = ['java', '-Xmx256m', '-XX:ActiveProcessorCount=1', '-jar', '/app/backend.jar']
        app['volumes'] = [{'type': 'bind', 'source': str(root / 'backend.jar'),
                           'target': '/app/backend.jar', 'read_only': True}]
        env = app['environment']
        env.update(SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE='4',
                   SPRING_DATASOURCE_HIKARI_MINIMUM_IDLE='4',
                   SPRING_DATASOURCE_HIKARI_CONNECTION_TIMEOUT='5000',
                   SPRING_DATA_REDIS_TIMEOUT='300ms', SPRING_DATA_REDIS_CONNECT_TIMEOUT='300ms',
                   RATE_LIMIT_ENABLED='true', AUTH_RATE_LIMIT='10000', WRITE_RATE_LIMIT='10000',
                   DOMAIN_EVENT_TOPIC='backend-strengthening-v3.domain-events.v1',
                   DOMAIN_EVENT_CONSUMER_GROUP='backend-strengthening-v3-audit')
        env['SPRING_APPLICATION_JSON'] = ('{"local-life":{"order":{"expiry-scan-delay":"PT0.5S"},'
            '"fulfillment":{"enabled":true,"worker-enabled":${APP' + str(number) + '_WORKER:-false},'
            '"kafka-enabled":${APP' + str(number) + '_KAFKA:-false},'
            '"warehouse-url":"http://host.docker.internal:19091",'
            '"warehouse-token":"${BENCH_WAREHOUSE_TOKEN}",'
            '"consumer-group":"backend-strengthening-v3-fulfillment",'
            '"lease":"PT10S","workers":1,"queue-size":2,"max-attempts":8,"poll-delay":"PT0.2S"}}}')
        for field in ('DOMAIN_EVENT_TOPIC','DOMAIN_EVENT_CONSUMER_GROUP','SPRING_APPLICATION_JSON'):
            env[field]=env[field].replace('backend-strengthening-v3',project)
        config['services']['app' + str(number)] = app
    config['services']['mysql']['mem_limit'] = '512m'
    config['services']['kafka']['mem_limit'] = '512m'
    config['services']['kafka']['environment']['KAFKA_HEAP_OPTS'] = '-Xms128m -Xmx256m'
    (root / 'compose.validation.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    (root / 'jar.json').write_text(json.dumps({'sha256': hashlib.sha256((root / 'backend.jar').read_bytes()).hexdigest(),
        'source': str(a.jar.resolve()), 'servicesStarted': False}, indent=2), encoding='utf-8')
    print(json.dumps({'directory': str(root), 'project': config['name'], 'servicesStarted': False}))


if __name__ == '__main__':
    main()
