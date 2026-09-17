"""Paired, scenario-cluster descriptive uncertainty; never a population claim."""
import json
import random
from collections import defaultdict
from .common import HERE, json_new, rows, file_sha


def q(values, p):
    values = sorted(values)
    pos = (len(values)-1)*p
    lower = int(pos); upper = min(lower+1, len(values)-1)
    return values[lower] + (values[upper]-values[lower])*(pos-lower)


def analyze(attempt):
    out = HERE / 'p3' / attempt
    data = rows(out / 'traces.jsonl')
    arms = ('CTX1a','CTX1b')
    groups = defaultdict(list)
    for row in data: groups[row['scenarioId']].append(row)
    def ratios(selected):
        a,b = ([r for r in selected if r['arm']==arm] for arm in arms)
        return [sum(r['usage']['total_tokens'] for r in b)/sum(r['usage']['total_tokens'] for r in a),
                q([r['durationMs'] for r in b], .95)/q([r['durationMs'] for r in a], .95)]
    samples = [[],[]]
    rng = random.Random(20260904)
    names = sorted(groups)
    for _ in range(2000):
        selected = [r for name in rng.choices(names, k=len(names)) for r in groups[name]]
        for index, value in enumerate(ratios(selected)): samples[index].append(value)
    point = ratios(data)
    intervals = [[q(s,.025),q(s,.975)] for s in samples]
    report = {'status': 'DESCRIPTIVE_SYNTHETIC_CORPUS_ONLY', 'rows':len(data),
        'scenarioClusters':len(names), 'caseIds':len({r['caseId'] for r in data}),
        'totalTokenRatio':point[0], 'totalTokenRatioClusterBootstrap95':intervals[0],
        'p95LatencyRatio':point[1], 'p95LatencyRatioClusterBootstrap95':intervals[1],
        'tokenGate':point[0]<=.9 and intervals[0][1]<1, 'p95PointGate':point[1]<=1.2,
        'fidelityByArm': {arm:{'exact':sum(r['exact'] for r in data if r['arm']==arm),
                               'total':sum(r['arm']==arm for r in data)} for arm in arms},
        'method':'2000 bootstrap draws of scenario clusters; all 3 blocks and both arms retained per draw',
        'statisticalFallacyChecks':'11/11 reviewed; no p-values or human population causal/NI claims',
        'cautions':['reused development corpus','same-agent selection and scoring','synthetic family dependence',
                    'no outcome filtering; failed fidelity rows remain in cost/latency summaries',
                    'API responses are nondeterministic; this is not an exact reproduction claim'],
        'traceSha256':file_sha(out/'traces.jsonl')}
    json_new(out / 'analysis.json', report)
    print(attempt, report['totalTokenRatio'], report['totalTokenRatioClusterBootstrap95'])


if __name__ == '__main__':
    import sys
    analyze(sys.argv[1])
