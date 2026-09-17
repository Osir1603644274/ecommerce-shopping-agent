"""Mechanical routing/cost receipt; root separately reads all four answers."""
import argparse
import json
from pathlib import Path

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import file_sha, now, write_new
from .native_cost_audit import audit


def run(directory, output):
    terminal = json.loads(directory.with_name(directory.name + '_supervisor').joinpath('result.json').read_text(encoding='utf8'))
    if terminal['childExitCode'] != 0:
        raise ValueError('normal_probe_exit_required')
    rows = [json.loads((directory / f'turn-{n:02}.json').read_text(encoding='utf8')) for n in range(1, 5)]
    native = audit(directory)
    checks = {
        'identityClosed': all(r['runId'] == r['expectedRunId'] for r in rows),
        'actualInitialSearch': any(t['tool'] == 'search_products' and t['ok'] for t in rows[0]['toolTraces']),
        'actualExplicitRefresh': any(t['tool'] == 'search_products' and t['ok'] for t in rows[1]['toolTraces']),
        'notesNoProductSearch': all(not any(t['tool'] == 'search_products' for t in r['toolTraces']) for r in rows[2:]),
        'notesActualFinalModel': all(any(x['phase'] == 'final_answer' for x in r['contextPolicyEvidence']) for r in rows[2:]),
        'nativeAccountingClosed': not native['unknownUsageCalls'] and not native['failedOrUnclosedCalls'],
    }
    value = {'at': now(), 'kind': 'ROUTING_PROBE_MECHANICAL_NOT_FULL_QUALITY', 'checks': checks,
        'passed': all(checks.values()), 'native': native,
        'wholeAgentMs': sum(r['durationMs'] for r in rows),
        'turns': [{'turn': r['turn'], 'answer': r['answer'], 'query': r['query'],
                  'toolCalls': r['toolTraces'], 'phases': [x['phase'] for x in r['contextPolicyEvidence']]} for r in rows],
        'sourceHashes': {f'turn-{n:02}.json': file_sha(directory / f'turn-{n:02}.json') for n in range(1, 5)},
        'formalAcceptance': False, 'semanticReadRequired': True}
    write_new(output, value)
    print(json.dumps({'passed': value['passed'], 'checks': checks,
        'nativeTokens': native['completeNativeTokenTotal'], 'wholeAgentMs': value['wholeAgentMs']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('directory', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    run(args.directory, args.output)
