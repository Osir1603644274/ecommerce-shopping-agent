"""P6 new-only cross-process recovery evidence, no model or production tools."""
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

from .common import HERE, ROOT, file_sha, json_new, now


def main():
    # These workers use fixed simulated read-only tool results while executing
    # real durable graph/inbox code and a dedicated Redis process.
    from agent.evaluation import graph_v2_process_recovery_1x as graph
    from agent.evaluation import checkpoint_recovery_inbox_v2 as inbox
    out = HERE / 'p6/process001'; out.mkdir(parents=True, exist_ok=False)
    real_popen = subprocess.Popen
    created = []
    def hidden_popen(command, *args, **kwargs):
        command = list(command)
        if 'redis-server' in str(command[0]).lower() and '--bind' not in command:
            command += ['--bind', '127.0.0.1']
        kwargs['creationflags'] = kwargs.get('creationflags', 0) | subprocess.CREATE_NO_WINDOW
        proc = real_popen(command, *args, **kwargs)
        created.append({'pid': proc.pid, 'executable': str(command[0])})
        return proc
    json_new(out / 'protocol.json', {'at': now(), 'pid': os.getpid(), 'modelCalls': 0,
        'boundary': 'real subprocess crash + production durable graph/inbox + real isolated Redis; simulated tool outputs',
        'runnerSha256': file_sha(__file__), 'dependencies': {
            'graph': file_sha(graph.__file__), 'inbox': file_sha(inbox.__file__)},
        'notProven': ['external business exactly-once', 'real model continuation', 'browser approval UX']})
    with patch.object(subprocess, 'Popen', hidden_popen):
        graph_result = graph.run_1x(out / 'graph')
        inbox_result = inbox.run_checkpoint_recovery_inbox_v2(out / 'inbox')
    json_new(out / 'result.json', {'at': now(), 'graph': graph_result, 'inbox': inbox_result,
        'processes': created, 'providerCalls': 0, 'productionServicesTouched': False})
    print('P6 graph=' + str(graph_result) + ' inbox=' + str(inbox_result.get('score')), flush=True)


if __name__ == '__main__': main()
