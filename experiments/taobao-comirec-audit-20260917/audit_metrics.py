"""Audit pinned upstream evaluation without importing TensorFlow or running training."""
import ast
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path('D:/agent-datasets/taobao-comirec-audit-20260917')


def standard(ranked, targets, k):
    assert len(ranked) == len(set(ranked)), 'Predictions must be unique'
    truth = set(targets)
    assert truth and k > 0
    hit = [int(i in truth) for i in ranked[:k]]
    dcg = sum(v / math.log2(n + 2) for n, v in enumerate(hit))
    idcg = sum(1 / math.log2(n + 2) for n in range(min(k, len(truth))))
    return {'recall': sum(hit) / len(truth), 'ndcg': dcg / idcg,
            'hitrate': float(any(hit))}


def run_upstream(targets, multi=False):
    # Only the audited evaluate_full/prepare_data definitions are executed.
    source = ROOT / 'source/src/train.py'
    tree = ast.parse(source.read_text(encoding='utf8'))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
             and n.name in {'prepare_data', 'evaluate_full'}]
    class Index:
        def __init__(self, *args): pass
        def add(self, *args): pass
        def search(self, users, k):
            return np.tile([2., 1.], (len(users), 1)), np.tile([1, 9], (len(users), 1))
    class Model:
        def output_item(self, session): return np.zeros((10, 2))
        def output_user(self, session, history):
            return np.zeros((1, 2, 2) if multi else (1, 2))
    env = {'args': SimpleNamespace(topN=2, embedding_dim=2), 'math': math, 'np': np,
           'faiss': SimpleNamespace(StandardGpuResources=lambda: None,
                                   GpuIndexFlatConfig=SimpleNamespace, GpuIndexFlatIP=Index)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), env)
    batches = [(([123], [targets]), ([[3, 4]], [[1., 1.]]))]
    return env['evaluate_full'](None, batches, Model(), '', 1, {}, save=True)


def main():
    cases = []
    for truth in [[1, 2], [1, 1, 2]]:
        for multi in [False, True]:
            cases.append({'synthetic_targets': truth, 'predictions': [1, 9],
                          'upstream_branch': 'multi_interest' if multi else 'single_interest',
                          'upstream': run_upstream(truth, multi),
                          'standard_unique_item_metrics': standard([1, 9], truth, 2)})
    assert cases[0]['upstream']['ndcg'] == 1
    assert math.isclose(cases[0]['standard_unique_item_metrics']['ndcg'], .6131471927654584)
    assert standard([1, 2], [1, 2], 2)['ndcg'] == 1
    assert standard([8, 9], [1, 2], 2)['ndcg'] == 0
    assert standard([9, 1], [1], 2)['ndcg'] < standard([1, 9], [1], 2)['ndcg']
    assert standard([1, 9], [1, 1, 2], 2) == standard([1, 9], [1, 2], 2)
    try:
        standard([1, 1], [1, 2], 2)
    except AssertionError:
        pass
    else:
        raise AssertionError('duplicate predictions accepted')
    result = {'status': 'METRIC_MISMATCH_REPRODUCED_NO_MODEL_TRAINED',
              'commit': 'a576eed8b605a531f2971136ce6ae87739d47693', 'cases': cases,
              'note': 'Synthetic metric checks, not Taobao model performance.',
              'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in sorted((ROOT / 'source').rglob('*')) if p.is_file()}}
    (ROOT / 'metric-audit.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
