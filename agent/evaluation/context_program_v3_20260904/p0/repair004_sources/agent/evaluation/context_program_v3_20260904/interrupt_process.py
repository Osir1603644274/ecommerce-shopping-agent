"""P6 bounded real-process clarification/pause/drift probes (zero model)."""
import json
import os
import subprocess
from unittest.mock import patch
from .common import HERE, file_sha, json_new, now
from .multiturn import owned_redis


def main():
    out = HERE / 'p6/interrupt001'; out.mkdir(parents=True, exist_ok=False)
    with owned_redis(out):
        import redis
        from agent.evaluation.react_v1_durable_checkpoint_v2_20260901_v1 import runner
        port = json.loads((out / 'redis_identity.json').read_text())['port']
        sync = redis.Redis(host='127.0.0.1', port=port, decode_responses=True)
        real_popen = subprocess.Popen
        def hidden(command, *args, **kwargs):
            if '--real-model' in command: raise RuntimeError('model_forbidden')
            kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
            return real_popen(command, *args, **kwargs)
        json_new(out / 'protocol.json', {'at': now(), 'cases': ['clarification', 'operator_pause', 'state_diverged'],
            'sourceSha256': file_sha(runner.__file__), 'modelCalls': 0,
            'scope': 'production ReAct durable graph; real isolated Redis; fresh worker per action; simulated read-only tool'})
        cases = []
        try:
            with patch.object(subprocess, 'Popen', hidden):
                for name, fn in [('clarification', runner._run_clarification), ('operator_pause', runner._run_pause), ('state_diverged', runner._run_state_diverged)]:
                    case = fn(sync, port, 1)
                    json_new(out / f'{name}.json', case); cases.append(case)
                    print('P6 interrupt probe completed: ' + name, flush=True)
        finally: sync.close()
        json_new(out / 'collected.json', {'at': now(), 'cases': [r['family'] for r in cases],
            'unexpectedModelCalls': sum(len(r['modelCalls']) for r in cases),
            'status': 'COLLECTED_PENDING_INDEPENDENT_ASSERTIONS', 'humanAnswer': 'scripted fixture, not human judgment'})


if __name__ == '__main__': main()
