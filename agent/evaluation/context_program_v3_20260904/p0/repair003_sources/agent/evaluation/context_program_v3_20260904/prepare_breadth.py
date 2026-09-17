"""Freeze broader synthetic conversations; no claim of human holdout labels."""
from copy import deepcopy
from .common import HERE, append, json_new, now, sha


def build(split, count):
    data = []
    families = (
        ('budget_storage', ['budget', 'storage', 'preference']),
        ('withdraw_reinstate', ['storage', 'withdraw_budget', 'budget']),
        ('system_reversal', ['android', 'budget', 'ios']),
        ('long_history', ['preference', 'history', 'history', 'history', 'budget', 'storage']),
        ('preference_retraction', ['preference', 'withdraw_preference', 'fluency']),
        ('storage_withdrawal', ['storage', 'withdraw_storage', 'budget']),
        ('scoped_comparison', ['budget', 'compare', 'preference']),
        ('empty_candidate_recovery', ['impossible_budget', 'budget', 'storage']),
    )
    for index in range(count):
        family, ops = families[index % len(families)]
        amount = (1600 if split == 'dev' else 1700) + (index // 8)*50
        storage = 128 if index%2 else 256
        if split == 'confirm':
            # Compositional holdout: same operations in new longer sequences.
            # Not independent primitive families; report this limitation.
            ops = ['storage', *ops, 'withdraw_budget', 'budget', 'preference']
        texts = {
            'budget': f'预算改为{amount}元，系统等其他硬条件不变。',
            'storage': f'加一条硬要求：存储至少{storage}GB，其他条件不变。',
            'preference': '前面硬条件别放宽，把续航体验也考虑进去。',
            'withdraw_preference': '不用考虑续航体验了，硬条件不变。',
            'fluency': '再考虑日常流畅性，硬条件不变。',
            'withdraw_budget': '取消预算限制，其他硬条件不变。',
            'withdraw_storage': '取消存储容量要求，预算和系统条件不变。',
            'android': '系统改为只看安卓，其他条件不变。',
            'ios': '系统改回只看iOS，其他条件不变。',
            'history': '请解释选机时应怎样看待卖家的宣传文字，不要改变已有筛选条件。',
            'compare': '比较刚才推荐的前两款；若没有两个可信候选，请明确说明，不要编造对象。',
            'impossible_budget': '预算临时改为1元，找不到就明确说无候选，不要放宽条件。',
        }
        cid = f'ctxv3-{split}-{index+1:03d}'
        expected = {'os': 'ios', 'price_minor': 220000}
        turns = []
        sequence = [('initial', '想买二手手机，只看iOS，预算2200元以内。')]
        sequence += [(op, texts[op]) for op in ops]
        sequence += [('final', '综合此前全部条件说明如何取舍，不得擅自修改硬要求。')]
        for t, (op, message) in enumerate(sequence, 1):
            absent = []
            if op == 'budget': expected['price_minor'] = amount*100
            elif op == 'impossible_budget': expected['price_minor'] = 100
            elif op == 'storage': expected['storage_gb'] = storage
            elif op == 'withdraw_storage': expected.pop('storage_gb', None)
            elif op == 'withdraw_budget': expected.pop('price_minor', None)
            elif op in ('android', 'ios'): expected['os'] = op
            absent = [key for key in ('price_minor', 'storage_gb') if key not in expected]
            turns.append({'turnId': f'{cid}-t{t:02d}', 'semanticTurn': t, 'operation': op,
                'rawUserText': message, 'messageSha256': sha(message), 'turnProvenance': 'SYNTHETIC_SCRIPTED',
                'expectedHard': deepcopy(expected), 'absentKeys': absent})
        data.append({'conversationId': cid, 'split': split, 'familyId': split + ':' + family,
            'primitiveFamily': family, 'turns': turns, 'datasetRole': 'SYNTHETIC_COMPOSITION_NOT_HUMAN_HOLDOUT'})
    return data


def main():
    out = HERE / 'p1/breadth001'; out.mkdir(parents=True, exist_ok=False)
    for split, count in (('dev',16), ('confirm',64)):
        data = build(split, count)
        for case in data: append(out / f'{split}.jsonl', case)
    json_new(out / 'protocol.json', {'at': now(), 'devConversations':16, 'confirmConversations':64,
        'primitiveFamilies':8, 'author':'AI_RULE_AUTHORED', 'humanLabels':False,
        'coverage':['constraint override','withdrawal','reinstatement','long history','preference retraction',
                    'reference to first two candidates','empty candidate recovery'],
        'limitations':['generated before new development execution but authored by same agent',
            'confirmation shares primitive families; no population-level NI claim',
            'automated state oracles do not replace human answer-quality judgments']})
    print('Frozen dev16 / confirm64 across eight primitive families', flush=True)


if __name__ == '__main__': main()
