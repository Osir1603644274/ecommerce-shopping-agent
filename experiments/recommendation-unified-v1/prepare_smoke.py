"""Bounded dev-only recommendation smoke. No query input, GPU, or service writes.

prepare reads fit/dev requests and writes public inputs + separate private labels.
predict reads public inputs and fit-only artifacts, never labels or query text.
evaluate reads frozen predictions and labels; it cannot select model settings.
"""
import argparse
import collections
import hashlib
import itertools
import json
import math
import sqlite3
import time
from pathlib import Path

AUDIT = Path('D:/agent-datasets/recommendation-feasibility-20260916/audit.sqlite3')
DEFAULT_OUT = Path('D:/agent-datasets/recommendation-unified-v1/dev-smoke-001')
SEED = 'recommendation-unified-v1-20260916'
CUTOFF = 747693


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def connect():
    db = sqlite3.connect(AUDIT.as_uri() + '?mode=ro', uri=True)
    db.execute('PRAGMA cache_size=-4096')
    db.execute('PRAGMA temp_store=FILE')
    return db


def prepare(out, limit):
    if out.exists():
        raise RuntimeError('Existing attempt preserved: choose a NEW output directory')
    out.mkdir(parents=True)
    protocol = {
        'status': 'PREDECLARED_BEFORE_PREDICTION', 'source': 'kuaisearch',
        'source_database': str(AUDIT), 'source_database_sha256': sha(AUDIT),
        'fit_cutoff': CUTOFF, 'seed': SEED,
        'request_count_limit': limit, 'history_items_limit': 20,
        'user_covisitation_items_limit': 50, 'neighbors_per_anchor': 100,
        'outputs_k': [10, 100], 'target_type': 'later_search_request_click',
        'arms': ['popular', 'itemcf', 'itemcf_popular_fallback'],
        'user_selection': 'first eligible positive dev request per user; lowest SHA256(seed:user_id)',
        'eligibility': '>=5 distinct strictly earlier fit clicks and >=2 earlier positive fit requests',
        'candidate_catalog': 'ALL fit-clicked items; never append heldout targets',
        'history_policy': 'fit only; no dev/test feedback; no order inside multi-click request',
        'itemcf': 'binary co-user count on latest <=50 unique fit items, cosine normalized by full fit user support; sum over latest <=20 history items; top100 neighbors per anchor',
        'primary_metric': 'macro Recall@100, novel clicked targets only, uncovered targets retained',
        'ndcg': 'binary nDCG@10, IDCG from all novel clicked targets, not retrieved hits',
        'limitations': ['development smoke, not blind test', 'positive-history cohort only',
            'sampled users and full fit-clicked catalog; not all-user or all-product quality',
            'search-conditioned feedback; no homepage CTR or causal recommendation claim',
            'unobserved products are not negatives; fit-click catalog excludes cold target items',
            'global fit cooccurrence is association, not within-session ordered transitions'],
        'training': 'no neural training; fit-only popularity and cooccurrence counting',
    }
    write(out / 'PROTOCOL.json', protocol)
    db = connect()
    rows = db.execute("SELECT uid,t,sid,part,clicks FROM req WHERE part IN ('fit','dev') ORDER BY uid,t,sid")
    selected = []
    eligible_users = 0
    for uid, ur in itertools.groupby(rows, key=lambda x: x[0]):
        history, positive_requests, chosen = {}, 0, None
        for timestamp, batch in itertools.groupby(ur, key=lambda x: x[1]):
            batch = list(batch)
            for _, t, sid, part, clicks in batch:
                targets = json.loads(clicks)
                if part == 'dev' and chosen is None and targets and len(history) >= 5 and positive_requests >= 2:
                    ordered = sorted(history, key=lambda i: (-history[i], i))[:20]
                    assert all(history[i] < t and history[i] <= CUTOFF for i in ordered)
                    chosen = (
                        {'source': 'kuaisearch', 'user_id': str(uid), 'request_id': str(sid),
                         'time_index': t, 'history': [{'item_id': str(i), 'time_index': history[i]} for i in ordered],
                         'all_seen_item_ids': [str(i) for i in sorted(history)]},
                        {'request_id': str(sid), 'novel_clicked_item_ids': [str(i) for i in targets if i not in history],
                         'all_clicked_item_ids': [str(i) for i in targets]})
            for _, t, sid, part, clicks in batch:
                if part == 'fit':
                    targets = json.loads(clicks)
                    for item in targets:
                        history[item] = t
                    positive_requests += bool(targets)
        if chosen is not None:
            eligible_users += 1
            key = hashlib.sha256(f'{SEED}:{uid}'.encode()).hexdigest()
            selected.append((key, chosen))
            selected.sort(key=lambda x: x[0])
            del selected[limit:]
    public = [x[1][0] for x in selected]
    labels = [x[1][1] for x in selected]
    write(out / 'public.json', public)
    write(out / 'labels.private.json', labels)
    print(f'prepared {len(public)} requests from {eligible_users} eligible dev users', flush=True)
    counts = {int(i): int(n) for i, n in db.execute("SELECT item,COUNT(DISTINCT uid) FROM clk WHERE part='fit' GROUP BY item")}
    anchors = {int(h['item_id']) for r in public for h in r['history']}
    neighbors = {a: collections.Counter() for a in anchors}
    fit_users = 0
    rows = db.execute("SELECT uid,t,clicks FROM req WHERE part='fit' ORDER BY uid,t,sid")
    for uid, ur in itertools.groupby(rows, key=lambda x: x[0]):
        hist = {}
        for _, t, clicks in ur:
            for item in json.loads(clicks):
                hist[item] = t
        latest = set(sorted(hist, key=lambda i: (-hist[i], i))[:50])
        for anchor in latest.intersection(anchors):
            neighbors[anchor].update(latest - {anchor})
        fit_users += 1
    encoded = {}
    for anchor, counter in neighbors.items():
        scores = [(str(item), n / math.sqrt(counts[anchor] * counts[item])) for item, n in counter.items()]
        scores.sort(key=lambda x: (-x[1], int(x[0])))
        encoded[str(anchor)] = scores[:100]
    write(out / 'fit_popularity.json', {str(i): n for i, n in counts.items()})
    write(out / 'fit_neighbors.json', encoded)
    db.close()
    frozen = {'eligible_dev_users': eligible_users, 'selected_requests': len(public),
              'fit_users': fit_users, 'fit_catalog_items': len(counts), 'history_anchor_items': len(anchors),
              'files': {n: sha(out / n) for n in ['PROTOCOL.json', 'public.json', 'labels.private.json',
                         'fit_popularity.json', 'fit_neighbors.json']}, 'script_sha256': sha(Path(__file__))}
    write(out / 'PREPARED.json', frozen)
    print('fit artifacts frozen', json.dumps({k: v for k, v in frozen.items() if k != 'files'}), flush=True)


def predict(out):
    prepared = read(out / 'PREPARED.json')
    for name in ['PROTOCOL.json', 'public.json', 'fit_popularity.json', 'fit_neighbors.json']:
        assert sha(out / name) == prepared['files'][name]
    if (out / 'predictions.json').exists():
        raise RuntimeError('Frozen predictions already exist')
    public = read(out / 'public.json')
    counts = read(out / 'fit_popularity.json')
    neighbors = read(out / 'fit_neighbors.json')
    popular = sorted(counts, key=lambda i: (-counts[i], int(i)))
    results = []
    started = time.monotonic()
    for r in public:
        assert 'query' not in r and 'targets' not in r
        seen = set(r['all_seen_item_ids'])
        pop = [i for i in popular if i not in seen][:100]
        scores = collections.Counter()
        for h in r['history']:
            for item, score in neighbors[h['item_id']]:
                if item not in seen:
                    scores[item] += score
        cf = sorted(scores, key=lambda i: (-scores[i], int(i)))[:100]
        hybrid = (cf + [i for i in pop if i not in set(cf)])[:100]
        results.append({'request_id': r['request_id'], 'popular': pop, 'itemcf': cf,
                        'itemcf_popular_fallback': hybrid})
        for items in [pop, cf, hybrid]:
            assert len(items) == len(set(items)) and not seen.intersection(items)
            assert all(item in counts for item in items)
    write(out / 'predictions.json', results)
    write(out / 'PREDICTED.json', {'prediction_sha256': sha(out / 'predictions.json'),
          'predict_seconds': time.monotonic() - started, 'label_or_query_file_opened_by_predict': False,
          'input_files': ['public.json', 'fit_popularity.json', 'fit_neighbors.json']})


def metrics(ranking, truth, k):
    hit = sum(i in truth for i in ranking[:k])
    recall = hit / len(truth)
    dcg = sum(1 / math.log2(p + 2) for p, i in enumerate(ranking[:k]) if i in truth)
    idcg = sum(1 / math.log2(p + 2) for p in range(min(len(truth), k)))
    return recall, dcg / idcg, int(hit > 0)


def evaluate(out):
    prepared = read(out / 'PREPARED.json')
    receipt = read(out / 'PREDICTED.json')
    assert sha(out / 'predictions.json') == receipt['prediction_sha256']
    assert sha(out / 'labels.private.json') == prepared['files']['labels.private.json']
    labels = {r['request_id']: set(r['novel_clicked_item_ids']) for r in read(out / 'labels.private.json')}
    predictions = read(out / 'predictions.json')
    catalog = set(read(out / 'fit_popularity.json'))
    per_request, counters = [], collections.defaultdict(collections.Counter)
    target_count = covered_count = scorable = 0
    for r in predictions:
        truth = labels[r['request_id']]
        if not truth:
            continue
        scorable += 1
        target_count += len(truth)
        covered_count += len(truth & catalog)
        row = {'request_id': r['request_id'], 'novel_targets': len(truth), 'catalog_covered_targets': len(truth & catalog)}
        for arm in ['popular', 'itemcf', 'itemcf_popular_fallback']:
            recall100, _, hit100 = metrics(r[arm], truth, 100)
            _, ndcg10, _ = metrics(r[arm], truth, 10)
            row[arm] = {'recall100': recall100, 'ndcg10': ndcg10, 'hit100': hit100}
            counters[arm].update(row[arm])
        per_request.append(row)
    assert scorable > 0
    write(out / 'per_request.json', per_request)
    write(out / 'RESULT.json', {'status': 'DEV_SMOKE_COMPLETE_NOT_BENCHMARK',
          'selected_requests': len(predictions), 'scorable_novel_target_requests': scorable,
          'repeat_only_requests_excluded_from_novel_metric': len(predictions) - scorable,
          'novel_targets': target_count, 'catalog_covered_targets': covered_count,
          'target_catalog_coverage': covered_count / target_count,
          'macro_metrics': {arm: {k: v / scorable for k, v in sums.items()} for arm, sums in counters.items()},
          'inference': 'Descriptive development smoke; no statistical superiority or generalization claim',
          'training': 'fit-only counting, no neural training', 'test_requests_read': 0})


def main():
    p = argparse.ArgumentParser()
    p.add_argument('stage', choices=['prepare', 'predict', 'evaluate'])
    p.add_argument('--out', type=Path, default=DEFAULT_OUT)
    p.add_argument('--users', type=int, default=128)
    args = p.parse_args()
    if args.users <= 0 or args.users > 512:
        raise SystemExit('Bounded smoke requires 1..512 users')
    if args.stage == 'prepare':
        prepare(args.out, args.users)
    elif args.stage == 'predict':
        predict(args.out)
    else:
        evaluate(args.out)


if __name__ == '__main__':
    main()
