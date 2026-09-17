"""Prepare a fresh V2 workload environment. Starts nothing; never modifies V1 volumes."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--batch-jar', type=Path, required=True)
    parser.add_argument('--control-jar', type=Path, required=True)
    args = parser.parse_args()
    for jar in (args.batch_jar, args.control_jar):
        if not jar.is_file():
            raise SystemExit('Both independently identified JAR files are required')
    runtime = args.directory.resolve()
    subprocess.run([sys.executable, str(Path(__file__).with_name('prepare_runtime.py')),
                    '--directory', str(runtime)], check=True)
    jars = {}
    for name, source in [('batch', args.batch_jar), ('nplusone-control', args.control_jar)]:
        destination = runtime / (name + '.jar')
        shutil.copyfile(source, destination)
        jars[name] = {'file': destination.name,
                      'sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}
    config = yaml.safe_load(Path(__file__).with_name('compose.yml').read_text(encoding='utf-8'))
    config['name'] = 'backend-strengthening-v2'
    app = config['services']['backend']
    app['volumes'] = [{'type': 'bind', 'source': '${MIXED_BACKEND_JAR:?Select the recorded JAR}',
                       'target': '/app/backend.jar', 'read_only': True}]
    app['mem_limit'] = '768m'
    app['command'] = ['java', '-Xmx384m', '-XX:ActiveProcessorCount=1', '-jar', '/app/backend.jar']
    app['environment']['SPRING_DATASOURCE_HIKARI_MAXIMUM_POOL_SIZE'] = '4'
    app['environment']['SPRING_DATASOURCE_HIKARI_MINIMUM_IDLE'] = '4'
    app['environment']['DOMAIN_EVENT_TOPIC'] = 'backend-strengthening-v2.domain-events.v1'
    app['environment']['DOMAIN_EVENT_CONSUMER_GROUP'] = 'backend-strengthening-v2-audit'
    app['environment']['SPRING_APPLICATION_JSON'] = (
        app['environment']['SPRING_APPLICATION_JSON']
        .replace('backend-strengthening-fulfillment', 'backend-strengthening-v2-fulfillment')
        .replace('"kafka-enabled":true', '"kafka-enabled":${BENCH_KAFKA_ENABLED:-true}')
        .replace('"max-attempts":2', '"max-attempts":8'))
    config['services']['mysql']['mem_limit'] = '512m'
    config['services']['kafka']['mem_limit'] = '512m'
    config['services']['kafka']['environment']['KAFKA_HEAP_OPTS'] = '-Xms128m -Xmx256m'
    (runtime / 'compose.validation.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    (runtime / 'jars.json').write_text(json.dumps(jars, indent=2), encoding='utf-8')
    with (runtime / 'compose.env').open('a', encoding='utf-8') as env:
        env.write('MIXED_BACKEND_JAR=' + (runtime / 'batch.jar').as_posix() + '\n')
    print(json.dumps({'prepared': str(runtime), 'project': config['name'], 'jars': jars,
                      'servicesStarted': False, 'productionDefaultsChanged': False}))


if __name__ == '__main__':
    main()
