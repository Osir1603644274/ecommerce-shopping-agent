"""Run the registered counterbalanced phases, preserving successful and failed attempts."""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def capture(runtime, output):
    names = ['backend-strengthening-v2-' + service + '-1' for service in ['backend','mysql','kafka']]
    for name in names:
        if name.endswith('-backend-1'):
            result = subprocess.run(['docker','logs','--timestamps',name],capture_output=True)
            (output/'backend-container.log').write_bytes(result.stdout+result.stderr)
        result = subprocess.run(['docker','inspect','--format',
            '{"state":{{json .State}},"memoryLimit":{{.HostConfig.Memory}},"nanoCpus":{{.HostConfig.NanoCpus}},"imageId":{{json .Image}}}',name],capture_output=True)
        (output/(name+'-state.json')).write_bytes(result.stdout)
    result = subprocess.run(['docker','stats','--no-stream','--format','{{json .}}',*names],capture_output=True)
    (output/'resources-after.jsonl').write_bytes(result.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime',type=Path,required=True)
    args = parser.parse_args()
    runtime = args.runtime.resolve()
    if json.loads((runtime/'compose.validation.json').read_text())['name'] != 'backend-strengthening-v2':
        raise SystemExit('Only isolated V2 is allowed')
    phases = [(1,'nplusone-control'),(1,'batch'),(2,'batch'),(2,'nplusone-control'),
              (3,'batch'),(3,'nplusone-control'),(4,'nplusone-control'),(4,'batch')]
    script = Path(__file__).with_name('mixed_workload.py')
    expected_sha = hashlib.sha256(script.read_bytes()).hexdigest()
    jars = json.loads((runtime/'jars.json').read_text())
    sequence = [(f'pair{pair}-'+('batch' if arm=='batch' else 'nplusone')+'-attempt001',arm,0) for pair,arm in phases]
    sequence.append(('row-lock-attempt001','batch',3))
    for name,arm,lock in sequence:
        output = runtime/name
        if output.exists():
            saved = json.loads((output/'result.json').read_text())
            if (saved['status'] != 'BOUNDED_MIXED_WORKLOAD_ACCEPT' or saved['scriptSha256'] != expected_sha
                    or saved['jarSha256'] != jars[arm]['sha256']):
                raise SystemExit('Existing attempt cannot be reused or overwritten: '+name)
            print(json.dumps({'phase':name,'action':'reuse exact successful attempt'}),flush=True)
        else:
            print(json.dumps({'phase':name,'action':'start'}),flush=True)
            with (runtime/(name+'.log')).open('xb') as log:
                result = subprocess.run([sys.executable,str(script),'--runtime',str(runtime),'--output',str(output),
                    '--arm',arm,'--duration','30','--lock-seconds',str(lock)],stdout=log,stderr=subprocess.STDOUT)
            if result.returncode:
                capture(runtime,output)
                raise SystemExit('Phase failed; original retained: '+name)
        if not (output/'backend-container.log').exists():
            capture(runtime,output)
        saved = json.loads((output/'result.json').read_text())
        print(json.dumps({'phase':name,'status':saved['status'],'requests':saved['http']['requests'],
                         'errors':saved['http']['errors'],'orders':saved['business']['orders']}),flush=True)


if __name__ == '__main__':
    main()
