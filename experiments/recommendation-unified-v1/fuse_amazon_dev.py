"""One predeclared equal-RRF development extension; never changes the parent run."""
import collections
import hashlib
import json
import math
from pathlib import Path

PARENT = Path('D:/agent-datasets/recommendation-unified-v1/amazon-luxury-dev-001')
OUT = Path('D:/agent-datasets/recommendation-unified-v1/amazon-luxury-fusion-dev-001')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path):
    with path.open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def predict():
    if OUT.exists():
        raise RuntimeError('Preserve existing extension attempt')
    OUT.mkdir(parents=True)
    manifest = json.loads((PARENT / 'MANIFEST.json').read_text(encoding='utf-8'))
    for filename in ['predictions.jsonl', 'public_histories.jsonl', 'catalog.jsonl']:
        assert sha(PARENT / filename) == manifest['artifacts_sha256'][filename]
    write(OUT / 'PROTOCOL.json', {
        'status': 'DEVELOPMENT_EXTENSION_PREDECLARED_BEFORE_FUSION_SCORING',
        'parent': str(PARENT), 'parent_manifest_sha256': sha(PARENT / 'MANIFEST.json'),
        'parent_predictions_sha256': sha(PARENT / 'predictions.jsonl'),
        'weights': {'itemcf': 0.5, 'content_tfidf': 0.5}, 'rrf_k': 60,
        'per_route_input_depth': 100, 'final_output_depth': 100, 'grid_search': False,
        'tie_break': 'native item ID lexicographic',
        'limits': 'Two retrieved lists, up to 200 union candidates; same final Top100 budget. Additional route cost is not held constant. Development only; no final split scoring.'})
    public = {r['request_id']: r for r in rows(PARENT / 'public_histories.jsonl')}
    catalog = {r['item_id'] for r in rows(PARENT / 'catalog.jsonl')}
    grouped = collections.defaultdict(dict)
    for row in rows(PARENT / 'predictions.jsonl'):
        if row['arm'] in ['itemcf', 'content_tfidf']:
            assert row['arm'] not in grouped[row['request_id']]
            grouped[row['request_id']][row['arm']] = row['item_ids']
    assert set(grouped) == set(public)
    predictions = []
    for rid, arms in sorted(grouped.items()):
        assert set(arms) == {'itemcf', 'content_tfidf'}
        scores = collections.Counter()
        for ranking in arms.values():
            assert len(ranking) == len(set(ranking))
            for rank, item in enumerate(ranking, 1):
                scores[item] += 0.5 / (60 + rank)
        ranking = sorted(scores, key=lambda item: (-scores[item], item))[:100]
        assert set(ranking) <= catalog
        assert not set(ranking).intersection(public[rid]['seen_all_fit_item_ids'])
        predictions.append({'request_id': rid, 'arm': 'equal_rrf', 'item_ids': ranking})
    write(OUT / 'predictions.json', predictions)
    write(OUT / 'PREDICTED.json', {'predictions_sha256': sha(OUT / 'predictions.json'),
          'script_sha256': sha(Path(__file__)), 'labels_used_by_predict': False})


def evaluate():
    predicted = json.loads((OUT / 'PREDICTED.json').read_text(encoding='utf-8'))
    assert sha(OUT / 'predictions.json') == predicted['predictions_sha256']
    assert sha(Path(__file__)) == predicted['script_sha256']
    parent_manifest = json.loads((PARENT / 'MANIFEST.json').read_text(encoding='utf-8'))
    for filename in ['labels.private.jsonl', 'fit_artifacts.json', 'predictions.jsonl']:
        assert sha(PARENT / filename) == parent_manifest['artifacts_sha256'][filename]
    labels = {r['request_id']: set(r['target_item_ids']) for r in rows(PARENT / 'labels.private.jsonl')}
    warm = set(json.loads((PARENT / 'fit_artifacts.json').read_text(encoding='utf-8'))['positive_item_user_counts'])
    predictions = rows(PARENT / 'predictions.jsonl') + json.loads((OUT / 'predictions.json').read_text(encoding='utf-8'))
    sums, users, per_user = collections.defaultdict(collections.Counter), collections.Counter(), []
    for row in predictions:
        truth = labels[row['request_id']]
        ranking = row['item_ids']
        hits = set(ranking[:100]) & truth
        idcg = sum(1 / math.log2(i + 2) for i in range(min(10, len(truth))))
        ndcg = sum(1 / math.log2(i + 2) for i, item in enumerate(ranking[:10]) if item in truth) / idcg
        stats = {'recall100': len(hits) / len(truth), 'ndcg10': ndcg,
                 'hit100': int(bool(hits)), 'cold_targets_retrieved': len(hits - warm),
                 'targets_retrieved': len(hits)}
        sums[row['arm']].update(stats)
        users[row['arm']] += 1
        per_user.append({'request_id': row['request_id'], 'arm': row['arm'], **stats})
    assert set(users.values()) == {len(labels)}
    result = {'status': 'DEVELOPMENT_ONLY_EQUAL_WEIGHT_FUSION_COMPLETE', 'users': len(labels),
              'targets': sum(map(len, labels.values())),
              'cold_targets': sum(len(t - warm) for t in labels.values()), 'final_evaluated': False,
              'metrics': {a: {k: v / users[a] if k in ['recall100', 'ndcg10', 'hit100'] else v
                             for k, v in counter.items()} for a, counter in sums.items()},
              'interpretation': 'Development observation; no trained ranker, confidence interval, final/generalization or production claim'}
    write(OUT / 'per_user.json', per_user)
    write(OUT / 'RESULT.json', result)
    write(OUT / 'MANIFEST.json', {p.name: sha(p) for p in sorted(OUT.iterdir()) if p.is_file()})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    predict()
    evaluate()
