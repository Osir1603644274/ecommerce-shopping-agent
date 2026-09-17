"""P3 fixed-input paired fidelity/cost: 40 cases, 3 interleaved blocks."""
import argparse
import asyncio
from copy import deepcopy
import json
import random
from collections import defaultdict

from openai import AsyncOpenAI
from agent.app.settings import settings
from agent.evaluation.context_compiler_provider_paired_v1_20260901_v4 import runner as v4
from .common import HERE, ROOT, RecordedClient, append, canonical, freeze, check_freeze, file_sha, json_new, rows, now


def selected_cases():
    source = ROOT / 'agent/evaluation/context_compiler_provider_paired_v1_20260901_v4/scenarios.jsonl'
    grouped = defaultdict(list)
    for case in rows(source): grouped[case['scenarioId']].append(case)
    for group in grouped.values(): group.sort(key=lambda c: c['caseId'])
    selected = []
    while len(selected) < 40:
        for group in sorted(grouped):
            if grouped[group]: selected.append(grouped[group].pop(0))
            if len(selected) == 40: break
    return source, selected


def summarize(traces):
    result = {}
    for arm in v4.ARMS:
        selected = [r for r in traces if r['arm'] == arm]
        valid = [r for r in selected if r.get('usage')]
        result[arm] = {'calls': len(selected), 'exact': sum(r['exact'] for r in selected),
            'usageObserved': len(valid),
            'promptTokens': sum(r['usage']['prompt_tokens'] for r in valid),
            'completionTokens': sum(r['usage']['completion_tokens'] for r in valid),
            'totalTokens': sum(r['usage']['total_tokens'] for r in valid)}
    a, b = (result[arm] for arm in v4.ARMS)
    ratio = b['totalTokens']/a['totalTokens'] if a['totalTokens'] else None
    return {'byArm': result, 'totalTokenRatio': ratio,
        'allExact': len(traces) == 240 and all(r['exact'] for r in traces),
        'tenPercentSaving': ratio is not None and ratio <= .9,
        'status': 'MEASURED_FIXED_INPUT_NOT_END_TO_END_AGENT_QUALITY',
        'inference': 'development corpus reused; descriptive, not independent human generalization'}


async def run(attempt):
    p2 = json.loads((HERE / 'p2/attempt002/result.json').read_text(encoding='utf-8'))
    if p2['status'] != 'PASS': raise RuntimeError('P2_gate_not_passed')
    out = HERE / 'p3' / attempt
    out.mkdir(parents=True, exist_ok=False)
    source, cases = selected_cases()
    json_new(out / 'cases_input.json', cases)
    compiled = {}
    for case in cases:
        for arm in v4.ARMS:
            compiled[(case['caseId'], arm)] = v4.compile_case(case, arm).model_view
    json_new(out / 'protocol.json', {'at': now(), 'baseCalls': 240, 'blocks': 3,
        'selection': '40 sorted round-robin by original scenarioId; no outcome-based selection',
        'sourceSha256': file_sha(source), 'selectedSha256': file_sha(out / 'cases_input.json'),
        'inputFamilies': len({c['scenarioId'] for c in cases}),
        'strict': p2['selectedStrict'], 'maxTokens': 4096, 'temperature': 0, 'sdkRetries': 0,
        'eligibility': 'all exact typed-field fidelity; report cost and latency separately',
        'productionDefaultsChanged': False})
    freeze(out / 'source_freeze.json')
    trace = []
    tool = deepcopy(v4.FIDELITY_TOOL)
    if p2['selectedStrict']: tool['function']['strict'] = True
    async with AsyncOpenAI(api_key=settings.deepseek_api_key, base_url='https://api.deepseek.com/beta',
                           timeout=45, max_retries=0) as provider:
        client = RecordedClient(provider, phase='P3', output=out)
        try:
            for block in (1, 2, 3):
                for i, case in enumerate(cases):
                    order = v4.ARMS if (i+block)%2 else tuple(reversed(v4.ARMS))
                    for arm in order:
                        client.binding = {'attempt': attempt, 'caseId': case['caseId'], 'scenarioId': case['scenarioId'], 'block': block, 'arm': arm}
                        row = {**client.binding, 'exact': False}
                        start = asyncio.get_running_loop().time()
                        try:
                            response = await client.create(model=settings.deepseek_model,
                                messages=[{'role': 'system', 'content': v4.SYSTEM_PROMPT},
                                    {'role': 'user', 'content': canonical({'caseId': case['caseId'], 'context': compiled[(case['caseId'], arm)]})}],
                                tools=[tool], max_tokens=4096)
                            row['usage'] = response.usage.model_dump() if response.usage else None
                            calls = response.choices[0].message.tool_calls or []
                            if len(calls) != 1 or response.choices[0].finish_reason == 'length': raise ValueError('incomplete_tool_response')
                            actual = v4._parse_tool_arguments(response)
                            row['actual'], row['expected'] = actual, v4.expected_output(case)
                            row['exact'] = actual == row['expected']
                        except Exception as exc:
                            row['errorType'] = type(exc).__name__
                            if client.halted: raise
                        row['durationMs'] = (asyncio.get_running_loop().time()-start)*1000
                        trace.append(row); append(out / 'traces.jsonl', row)
                        print(f'P3 {len(trace)}/240 exact={row["exact"]}', flush=True)
        except Exception as exc:
            json_new(out / 'stop.json', {'at': now(), 'errorType': type(exc).__name__, 'completed': len(trace)})
    check_freeze(out / 'source_freeze.json')
    result = summarize(trace)
    json_new(out / 'result.json', result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--attempt', required=True)
    args = parser.parse_args()
    if not __import__('re').fullmatch(r'attempt\d{3}', args.attempt): raise ValueError('invalid_attempt')
    asyncio.run(run(args.attempt))
