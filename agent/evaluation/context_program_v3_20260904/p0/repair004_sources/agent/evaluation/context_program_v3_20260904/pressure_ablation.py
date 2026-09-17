"""P5 deterministic pressure matrix, including deliberate protected overflows."""
from collections import Counter
from agent.evaluation.context_program_v1_20260904 import budget_ablation as lib
from .common import HERE, append, json_new, file_sha, now


def main():
    out = HERE / 'p5/pressure001'; out.mkdir(parents=True, exist_ok=False)
    inputs = []
    for history_count in (0, 8, 40, 160):
        for protected_count in (1, 8, 40, 160):
            cid = f'h{history_count}-p{protected_count}'
            payload = {'runId': cid, 'taskId': cid, 'baseContextRevision': 5,
                'goal': '保留预算和系统硬条件，综合长期使用体验',
                'confirmedFacts': [{'key': f'fact-{i}', 'value': '已确认的独立状态事实' * 4} for i in range(protected_count)],
                'hardConstraints': [{'key': 'os', 'operator': 'eq', 'value': 'ios'},
                    {'key': 'price_minor', 'operator': 'lte', 'value': 220000}],
                'softPreferences': [{'useCase': '偏好续航体验，不代表任何数值保证'}],
                'historySummaries': [{'role': 'user' if i%2==0 else 'assistant',
                    'summary': f'历史第{i}条，只作历史背景，过时预算3000元不能覆盖现有硬约束。' * 3,
                    'kind': 'older_summary', 'sourceTurns': [i+1]} for i in range(history_count)],
                'candidateScopeState': {'candidateIds': [], 'comparedIds': [], 'evidenceStatus': 'missing'},
                'evidenceRefs': []}
            inputs.append({'caseId': cid, 'payload': payload})
    json_new(out / 'inputs.json', inputs)
    json_new(out / 'protocol.json', {'at': now(), 'inputCount': len(inputs), 'budgets': [4000,2000,1000,500,100],
        'helperSha256': file_sha(lib.__file__), 'dataClass': 'SYNTHETIC_STRESS_NOT_MODEL_DIALOGUES',
        'gate': 'exact duplicate compile; retained protected items or justified fail-closed overflow', 'modelCalls': 0})
    results = []
    for case in inputs:
        run = lib.make_run(case['caseId'], 5)
        items = lib.context_items_from_pack(lib.Pack(case['payload']), run)
        for component in lib.COMPONENTS:
            selected = lib.variant(items, component)
            for policy in lib.POLICIES:
                for budget in (4000,2000,1000,500,100):
                    first = lib.compile_once(run, selected, budget, policy, case['payload']['goal'])
                    second = lib.compile_once(run, selected, budget, policy, case['payload']['goal'])
                    row = {'caseId': case['caseId'], 'component': component, 'policy': policy, 'budget': budget,
                           'deterministicExact': first == second, **first}
                    results.append(row); append(out / 'rows.jsonl', row)
    good = all(r['deterministicExact'] and (r.get('allProtectedRetained') if r['status']=='COMPILED'
                      else r.get('expectedBecauseProtectedExceedsBudget')) for r in results)
    result = {'status': 'PASS_OFFLINE_MECHANICS' if good else 'HOLD_OFFLINE_MECHANICS',
        'rows': len(results), 'compilations': len(results)*2, 'byStatus': dict(Counter(r['status'] for r in results)),
        'actualProviderTokensMeasured': False, 'answerQualityMeasured': False, 'modelCalls': 0}
    json_new(out / 'result.json', result)
    print(result, flush=True)


if __name__ == '__main__': main()
