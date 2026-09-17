"""Build source-preserving metadata outside frozen datasets, with resumable batches."""
from __future__ import annotations
import argparse
import collections as C
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'agent'))
from app.catalog_data import clean, decode_record, digest, quality_flags


def write(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def build(output: Path, source_manifest: Path, *, small_path: Path, phone_path: Path):
    output.mkdir(parents=True, exist_ok=True)
    if (output / 'MANIFEST.json').exists():
        raise ValueError('completed artifact is immutable; open it instead of rebuilding')
    spec = json.loads(source_manifest.read_text(encoding='utf-8'))
    specs = {x['id']: x for x in spec['sources']}
    sources = {'kuaisearch': specs['kuaisearch_items_lite.train.jsonl'],
               'multicpr': specs['multicpr_corpus.tsv']}
    binding = {'source_manifest_sha256': digest(source_manifest),
               'small_sha256': digest(small_path), 'phones_sha256': digest(phone_path),
               'builder_sha256': digest(Path(__file__)),
               'contract_code_sha256': digest(ROOT / 'agent/app/catalog_data.py')}
    if (output / 'BINDING.json').exists():
        if json.loads((output / 'BINDING.json').read_text(encoding='utf-8')) != binding:
            raise ValueError('resume binding changed')
    else:
        write(output / 'BINDING.json', binding)
    small = rows(small_path); phones = {str(x['itemId']): x for x in rows(phone_path)}
    # Candidate relations only. A unique tuple is not promoted to native identity.
    tuples = {(clean(x['title']), clean(x.get('brand')), clean(x.get('seller_name'))) for x in small}
    db = sqlite3.connect(output / 'metadata.sqlite')
    db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=FULL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS records(docid TEXT NOT NULL, source TEXT NOT NULL,
          source_line INTEGER NOT NULL,byte_offset INTEGER NOT NULL,byte_length INTEGER NOT NULL,
          record_sha256 TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS progress(source TEXT PRIMARY KEY,source_line INTEGER,byte_offset INTEGER,complete INTEGER);
        CREATE TABLE IF NOT EXISTS tuple_matches(tuple_json TEXT,docid TEXT,PRIMARY KEY(tuple_json,docid));
        CREATE TABLE IF NOT EXISTS phone_matches(item_id TEXT PRIMARY KEY,docid TEXT,record_sha256 TEXT);
        CREATE TABLE IF NOT EXISTS quality_counts(source TEXT,flag TEXT,n INTEGER,PRIMARY KEY(source,flag));
        CREATE TABLE IF NOT EXISTS legacy_links(legacy_docid TEXT PRIMARY KEY,status TEXT,candidates TEXT);
    ''')
    for source, info in sources.items():
        path = Path(info['path'])
        if digest(path) != info['sha256']:
            raise ValueError('raw source hash differs: ' + source)
        checkpoint = db.execute('SELECT source_line,byte_offset,complete FROM progress WHERE source=?', (source,)).fetchone()
        line, offset, complete = checkpoint or (0, 0, 0)
        if complete:
            continue
        print(json.dumps({'source': source, 'resume_line': line}), flush=True)
        batch = []; links = []; phone_batch = []; counts = C.Counter()
        def commit(end):
            db.executemany('INSERT INTO records VALUES(?,?,?,?,?,?)', batch)
            db.executemany('INSERT OR IGNORE INTO tuple_matches VALUES(?,?)', links)
            db.executemany('INSERT INTO phone_matches VALUES(?,?,?)', phone_batch)
            for flag, n in counts.items():
                db.execute('INSERT INTO quality_counts VALUES(?,?,?) ON CONFLICT(source,flag) DO UPDATE SET n=n+excluded.n', (source, flag, n))
            db.execute('INSERT OR REPLACE INTO progress VALUES(?,?,?,?)', (source, line, offset, int(end)))
            db.commit(); batch.clear(); links.clear(); phone_batch.clear(); counts.clear()
        with path.open('rb') as f:
            f.seek(offset)
            for raw in f:
                start = offset; offset += len(raw); line += 1
                record = decode_record(source, raw); sha = hashlib.sha256(raw).hexdigest()
                batch.append((record['docid'], source, line, start, len(raw), sha))
                counts.update(quality_flags(record))
                if source == 'kuaisearch':
                    key = (record['title'], record['brand'], record['seller'])
                    if key in tuples:
                        links.append((json.dumps(key, ensure_ascii=False), record['docid']))
                    pid = str(record['raw']['item_id'])
                    if pid in phones:
                        p = phones[pid]
                        if key != (clean(p['title']), clean(p.get('brand')), clean(p.get('seller'))):
                            raise ValueError('phone native ID has different title/brand/seller')
                        phone_batch.append((pid, record['docid'], sha))
                if len(batch) >= 50000:
                    commit(False)
                    if line % 1000000 == 0:
                        print(json.dumps({'source': source, 'rows': line}), flush=True)
            commit(True)
    print('create unique document index', flush=True)
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS records_docid ON records(docid)')
    found = C.defaultdict(list)
    for key, did in db.execute('SELECT tuple_json,docid FROM tuple_matches ORDER BY docid'):
        found[key].append(did)
    link_counts = C.Counter()
    for x in small:
        key = json.dumps((clean(x['title']), clean(x.get('brand')), clean(x.get('seller_name'))), ensure_ascii=False)
        candidates = found[key]
        state = 'unique_tuple_candidate' if len(candidates) == 1 else 'ambiguous_tuple_candidates' if candidates else 'unmapped'
        db.execute('INSERT OR REPLACE INTO legacy_links VALUES(?,?,?)', (x['doc_id'], state, json.dumps(candidates)))
        link_counts[state] += 1
    phone_links = []
    matched = {pid: (did, sha) for pid, did, sha in db.execute('SELECT * FROM phone_matches')}
    for pid, p in phones.items():
        if pid in matched:
            did, sha = matched[pid]
            phone_links.append({'itemId': pid, 'nativeDocid': did, 'state': 'native_id_title_brand_seller_match',
                                'recordSha256': sha, 'linkMeaning': 'same_source_listing_not_physical_inspection'})
        else:
            phone_links.append({'itemId': pid, 'nativeDocid': None, 'state': 'derived_identity_not_native_id',
                                'attributeJoinAllowed': False})
    (output / 'phone-links.jsonl').write_text(''.join(json.dumps(x, ensure_ascii=False) + '\n' for x in phone_links), encoding='utf-8')
    counts = dict(db.execute('SELECT source,count(*) FROM records GROUP BY source'))
    expected = {'kuaisearch': 6634118, 'multicpr': 1002822}
    if counts != expected or len(phone_links) != 439 or len(matched) != 252:
        raise ValueError('unexpected full-source or phone cardinality')
    quality = list(db.execute('SELECT * FROM quality_counts ORDER BY source,flag'))
    db.commit(); db.execute('PRAGMA wal_checkpoint(TRUNCATE)'); db.execute('PRAGMA journal_mode=DELETE'); db.close()
    stats = {'records': counts, 'legacy_link_counts': dict(link_counts), 'phone_links': len(phone_links),
             'native_phone_links': len(matched), 'quality_flags': quality, 'no_source_or_qrel_edits': True}
    write(output / 'BUILD-RESULTS.json', stats)
    artifacts = {name: {'sha256': digest(output / name), 'bytes': (output / name).stat().st_size}
                 for name in ('metadata.sqlite', 'phone-links.jsonl', 'BUILD-RESULTS.json', 'BINDING.json')}
    write(output / 'MANIFEST.json', {'version': 'catalog-data-repair-v1', 'status': 'COMPLETE',
          'sources': {s: {'path': x['path'], 'sha256': x['sha256']} for s, x in sources.items()},
          'binding': binding, 'artifacts': artifacts, 'built_at': time.strftime('%Y-%m-%d %H:%M:%S')})
    print(json.dumps(stats, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.output, Path('D:/agent-datasets/search-stage1-v1/sources.json'),
          small_path=ROOT/'datasets/current/kuaisearch/documents.jsonl',
          phone_path=ROOT/'datasets/current/used-phone/catalog.jsonl')
