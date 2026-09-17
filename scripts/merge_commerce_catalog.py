"""One-shot, transactional local catalog merge. Source is read-only; reruns never reseed stock.

Requires the approved maintenance window and a separately verified SQL restoration.
Credentials are read from existing container metadata and are never printed.
"""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path
import pymysql

ROOT = Path(__file__).resolve().parents[1]
REVISION = 'merged-used-phone-439-20260909-v1'

def environment(container):
    doc = json.loads(subprocess.check_output(['docker', 'inspect', container], text=True, encoding='utf-8'))[0]
    return dict(v.split('=', 1) for v in doc['Config']['Env'] if '=' in v)

def connect(env, port, source=False):
    return pymysql.connect(host='127.0.0.1', port=port, user=env['DB_USER' if source else 'MYSQL_USER'],
        password=env['DB_PASSWORD' if source else 'MYSQL_PASSWORD'],
        database=env['DB_NAME' if source else 'MYSQL_DATABASE'], charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor, autocommit=False)

def digest(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()

def run(apply=False):
    bundle = ROOT / 'datasets/current/used-phone'
    raw = (bundle / 'prices.jsonl').read_bytes()
    manifest = json.loads((bundle / 'price_manifest.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(raw).hexdigest() == manifest['output']['sha256']
    prices = {int(p['itemId']): p for p in map(json.loads, raw.splitlines())}
    source = connect(environment('agent-used-phone-demo-search-backend-439-v1'), 13316, True)
    target = connect(environment('local-life-mysql'), 13306)
    with source, target, source.cursor() as src, target.cursor() as dst:
        src.execute('SET TRANSACTION READ ONLY')
        src.execute('SELECT * FROM product ORDER BY id')
        products = src.fetchall()
        ids = [p['id'] for p in products]
        assert len(products) == len(prices) == 439 and set(ids) == set(prices)
        src.execute('SELECT * FROM product_attribute ORDER BY product_id,attribute_key')
        attrs = src.fetchall()
        dst.execute('SELECT COUNT(*) AS n FROM customer_order WHERE status IN (\'PENDING_PAYMENT\',\'REFUNDING\')')
        assert dst.fetchone()['n'] == 0, 'Unresolved orders: stop before catalog switch'
        dst.execute('SELECT * FROM catalog_state WHERE catalog_version=%s', (REVISION,))
        existing = dst.fetchone()
        if existing:
            dst.execute("SELECT id FROM product WHERE lifecycle_status='ACTIVE' ORDER BY id")
            assert [p['id'] for p in dst.fetchall()] == ids
            dst.execute('SELECT COUNT(*) AS n FROM product_local_offer WHERE source_revision=%s', (REVISION,))
            assert dst.fetchone()['n'] == 439
            return {'status': 'already_applied_no_stock_or_price_changes', 'activeProducts': 439}
        dst.execute('SELECT id FROM product')
        old_ids = {p['id'] for p in dst.fetchall()}
        assert not old_ids.intersection(ids), 'Product identity overlap requires explicit mapping'
        preserved = {}
        for table in ['customer_order', 'order_item', 'payment_record', 'inventory_stock']:
            dst.execute(f'SELECT * FROM {table} ORDER BY id')
            preserved[table] = digest(dst.fetchall())
        report = {'status': 'audited', 'sourceProducts': 439, 'sourceAttributes': len(attrs),
            'historicalProductsRetained': len(old_ids), 'sourcePriceHash': manifest['output']['sha256']}
        if not apply:
            return report
        def insert(table, row):
            columns = list(row)
            dst.execute(f"INSERT INTO {table} ({','.join('`'+c+'`' for c in columns)}) VALUES ({','.join(['%s']*len(columns))})", list(row.values()))
        # All writes commit together. An error rolls back archiving as well.
        for p in products:
            insert('product', p)
        for a in attrs:
            insert('product_attribute', {k: v for k, v in a.items() if k != 'id'})
        for ident in ids:
            price = prices[ident]
            assert price['priceStatus'] == 'synthetic' and price['referencePriceMinor'] > 0
            insert('product_local_offer', {'product_id': ident, 'price_minor': price['referencePriceMinor'],
                'currency': price['currency'], 'price_kind': 'local_simulated', 'source_revision': REVISION})
            insert('inventory_stock', {'item_type': 'PRODUCT', 'item_id': ident, 'total_quantity': 10, 'available_quantity': 10})
        dst.execute("UPDATE product SET lifecycle_status='ARCHIVED',entity_version=entity_version+1 WHERE lifecycle_status='ACTIVE' AND id NOT IN (" + ','.join(['%s']*439) + ')', ids)
        content_hash = hashlib.sha256(','.join(map(str, ids)).encode()).hexdigest()
        insert('catalog_state', {'catalog_version': REVISION, 'product_count': 439, 'content_hash': content_hash})
        for table, expected in preserved.items():
            if table == 'inventory_stock':
                dst.execute('SELECT * FROM inventory_stock WHERE NOT (item_type=\'PRODUCT\' AND item_id IN (' + ','.join(['%s']*439) + ')) ORDER BY id', ids)
            else:
                dst.execute(f'SELECT * FROM {table} ORDER BY id')
            assert digest(dst.fetchall()) == expected, f'History changed: {table}'
        target.commit()
        report.update(status='committed', historyHashes=preserved, activeProducts=439, initializedStock=4390)
        return report

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.apply), ensure_ascii=False, indent=2))
