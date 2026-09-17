"""Frozen-source helpers and strict, arm-independent recommendation scoring."""
import collections, hashlib, json, math, os, sys
from pathlib import Path
import numpy as np

OLD_CODE = Path(__file__).resolve().parents[1] / 'recommendation-unified-v1'
sys.path.insert(0, str(OLD_CODE))
import amazon_pilot as old
BASE = Path('D:/agent-datasets/recommendation-unified-v1/amazon-luxury-dev-001')
ROOT = Path(os.environ.get('REC_COMPLETION_ROOT','D:/agent-datasets/recommendation-completion-v1'))
SOURCE = old.SOURCE

def rows(path): return [r for _, r in old.read_rows(Path(path))]
def read(path): return json.loads(Path(path).read_text(encoding='utf-8'))
def sha(path): return old.sha256(Path(path))
def write(path, value): old.write_json(Path(path), value)
def write_rows(path, value): old.write_rows(Path(path), value)

def verify_base():
    old.verify_sealed(BASE)

def rank(scores, ids, seen, k=100, positive=False):
    scores = np.asarray(scores)
    allowed = np.isfinite(scores) & np.array([i not in seen for i in ids])
    if positive: allowed &= scores > 0
    take = np.flatnonzero(allowed)
    # IDs supplied lexicographically; stable score sort supplies deterministic ties.
    take = take[np.argsort(-scores[take], kind='stable')][:k]
    return [ids[i] for i in take]

def rrf(lists, k=100):
    result = collections.Counter()
    for listing in lists:
        for n, item in enumerate(listing, 1): result[item] += 1 / len(lists) / (60+n)
    return sorted(result, key=lambda i: (-result[i], i))[:k]

def score(predictions, public, labels, fit, catalog):
    req = {r['request_id']: r for r in public}
    truth = {r['request_id']: set(r['target_item_ids']) for r in labels}
    assert len(req) == len(public) == len(truth) == len(labels) and req.keys() == truth.keys()
    assert all(truth.values())
    ids = {p['item_id'] for p in catalog}
    arms = sorted({p['arm'] for p in predictions})
    indexed = {}
    for p in predictions:
        key = (p['request_id'], p['arm']); listing = p['item_ids']
        assert key not in indexed and key[0] in req and p['source'] == SOURCE
        assert len(listing) <= 100 and len(listing) == len(set(listing)) and set(listing) <= ids
        assert not set(listing) & set(req[key[0]]['seen_all_fit_item_ids'])
        indexed[key] = listing
    assert len(indexed) == len(req)*len(arms)
    buckets = collections.defaultdict(list); details = []
    warm = set(fit['positive_item_user_counts'])
    for rid in sorted(req):
        assert not truth[rid] & set(req[rid]['seen_all_fit_item_ids'])
        for arm in arms:
            listing = indexed[rid, arm]
            metric = old.target_metrics(listing, truth[rid])
            details.append({'request_id': rid, 'user_id': req[rid]['user_id'], 'arm': arm, **metric})
            buckets[arm, 'all'].append(metric)
            buckets[arm, 'history_'+old.sparsity(len({h['item_id'] for h in req[rid]['history']}))].append(metric)
            for name, subset in [('warm_item',truth[rid]&warm),('cold_item',truth[rid]-warm)]:
                if subset: buckets[arm, name].append(old.target_metrics(listing,subset))
    metrics = collections.defaultdict(dict)
    for (arm, name), values in buckets.items():
        metrics[arm][name] = {'users': len(values), 'targets': sum(v['targets'] for v in values),
            'retrieved_targets': sum(v['retrieved_targets'] for v in values),
            **{key: float(np.mean([v[key] for v in values])) for key in ['recall_at_100','hit_at_100','ndcg_at_10','candidate_count']}}
    return details, {'users': len(req), 'targets':sum(map(len,truth.values())),
                     'targets_outside_catalog':sum(len(t-ids) for t in truth.values()),'metrics':dict(metrics)}

def seal(folder):
    folder = Path(folder)
    write(folder/'MANIFEST.json', {'files':{p.name:sha(p) for p in sorted(folder.iterdir()) if p.is_file() and p.name!='MANIFEST.json'},
                                  'code':{p.name:sha(p) for p in sorted(Path(__file__).parent.glob('*.py'))}})
