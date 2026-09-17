"""Descriptive MySQL query comparison. Run only after other timing experiments finish.

Requires pymysql and a dedicated MySQL instance. Creates a NEW backend_bench_* database;
never drops a database or touches the application's existing data. Credentials stay in a config file.
"""
import argparse
import hashlib
import json
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path


def canonical(rows):
    return json.dumps(rows, default=str, sort_keys=True, separators=(',', ':')).encode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--orders', type=int, choices=[10000, 100000, 1000000], default=100000)
    parser.add_argument('--repetitions', type=int, default=10)
    args = parser.parse_args()
    import pymysql
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    database = config.pop('database')
    if not database.startswith('backend_bench_') or not database.replace('_', '').isalnum():
        raise SystemExit('A new backend_bench_* database is required')
    if config.get('host') not in ('127.0.0.1', 'localhost') or config.get('port', 3306) == 3306:
        raise SystemExit('Use a dedicated loopback MySQL instance on a non-default port')
    if args.repetitions < 3 or args.repetitions > 100:
        raise SystemExit('Repetitions must be 3..100')
    args.output.mkdir(parents=True, exist_ok=False)
    metadata = {'status': 'IN_PROGRESS', 'database': database, 'orders': args.orders,
                'startedAt': datetime.now().astimezone().isoformat(),
                'scriptSha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'scope': 'SQL microbenchmark; not HTTP capacity or production throughput'}
    (args.output / 'manifest.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    conn = pymysql.connect(**config, charset='utf8mb4', autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    try:
        with conn.cursor() as sql:
            sql.execute(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci')
            sql.execute(f'USE `{database}`')
            # Query-visible schema and indexes mirror customer_order/order_item; fixtures have no live business side effects.
            sql.execute('''CREATE TABLE customer_order (
                id VARCHAR(36) PRIMARY KEY, order_no VARCHAR(32), user_id VARCHAR(36),
                idempotency_key VARCHAR(128), request_hash VARCHAR(64), status VARCHAR(32),
                total_minor BIGINT, discount_minor BIGINT, payable_minor BIGINT, currency VARCHAR(16),
                user_coupon_id VARCHAR(36), expires_at TIMESTAMP NULL, paid_at TIMESTAMP NULL,
                completed_at TIMESTAMP NULL, cancelled_at TIMESTAMP NULL, version BIGINT,
                created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NULL,
                UNIQUE KEY uk_customer_order_no(order_no),
                UNIQUE KEY uk_customer_order_idempotency(user_id,idempotency_key),
                INDEX idx_customer_order_user_created(user_id,created_at),
                INDEX idx_customer_order_expiry(status,expires_at)) ENGINE=InnoDB''')
            sql.execute('''CREATE TABLE order_item (
                id BIGINT AUTO_INCREMENT PRIMARY KEY, order_id VARCHAR(36), item_type VARCHAR(32), item_id BIGINT,
                title_snapshot VARCHAR(255), unit_price_minor BIGINT, quantity INT, subtotal_minor BIGINT,
                evidence_json JSON, INDEX idx_order_item_order(order_id)) ENGINE=InnoDB''')
            digest = hashlib.sha256()
            epoch = datetime(2026, 1, 1)
            for start in range(0, args.orders, 1000):
                rows, items = [], []
                for i in range(start, min(start + 1000, args.orders)):
                    identity = f'{i:036d}'
                    user = 'heavy-user' if i % 2 == 0 else f'user-{i % 1000:04d}'
                    created = epoch + timedelta(seconds=i // 3)
                    row = (identity, f'O{i:012d}', user, str(i), 'fixture', 'PAID' if i % 5 else 'CANCELLED',
                           100, 0, 100, 'CNY', None, created + timedelta(days=1), None, None, None, 0, created, created)
                    rows.append(row)
                    items.append((identity, 'PRODUCT', 1001, 'fixture', 100, 1, 100, '{}'))
                    digest.update(canonical(row))
                sql.executemany('INSERT INTO customer_order VALUES(' + ','.join(['%s'] * 18) + ')', rows)
                sql.executemany('INSERT INTO order_item(order_id,item_type,item_id,title_snapshot,unit_price_minor,quantity,subtotal_minor,evidence_json) VALUES(' + ','.join(['%s'] * 8) + ')', items)
            sql.execute('ANALYZE TABLE customer_order,order_item')
            sql.execute('SELECT VERSION() AS version')
            metadata['mysqlVersion'] = sql.fetchone()['version']
            metadata['fixtureSha256'] = digest.hexdigest()
            base = "SELECT * FROM customer_order WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT 20"

            def read_page(batched):
                started = time.perf_counter()
                sql.execute(base, ('heavy-user',))
                orders = sql.fetchall()
                grouped = {r['id']: [] for r in orders}
                if batched:
                    sql.execute('SELECT * FROM order_item WHERE order_id IN (' + ','.join(['%s'] * len(orders)) + ') ORDER BY order_id,id', tuple(grouped))
                    for item in sql.fetchall(): grouped[item['order_id']].append(item)
                else:
                    for identity in grouped:
                        sql.execute('SELECT * FROM order_item WHERE order_id=%s ORDER BY id', (identity,))
                        grouped[identity] = sql.fetchall()
                for row in orders: row['items'] = grouped[row['id']]
                return {'milliseconds': (time.perf_counter() - started) * 1000,
                        'sha256': hashlib.sha256(canonical(orders)).hexdigest(), 'sqlCount': 2 if batched else 21}

            for _ in range(2): read_page(False); read_page(True)
            samples = []
            for repeat in range(args.repetitions):
                pair = {}
                for arm in (['n_plus_one', 'batch'] if repeat % 2 == 0 else ['batch', 'n_plus_one']):
                    pair[arm] = read_page(arm == 'batch')
                if pair['n_plus_one']['sha256'] != pair['batch']['sha256']:
                    raise AssertionError('Comparison returned different data')
                samples.append(pair)
            sql.execute('EXPLAIN ANALYZE ' + base, ('heavy-user',))
            (args.output / 'explain-first-page.json').write_text(json.dumps(sql.fetchall(), default=str, indent=2), encoding='utf-8')
            offset = args.orders // 4
            sql.execute('SELECT created_at,id FROM customer_order WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT %s,1', ('heavy-user', offset - 1))
            anchor = sql.fetchone()
            seek = 'SELECT * FROM customer_order WHERE user_id=%s AND (created_at < %s OR (created_at=%s AND id<%s)) ORDER BY created_at DESC,id DESC LIMIT 20'
            params = ('heavy-user', anchor['created_at'], anchor['created_at'], anchor['id'])
            sql.execute(seek, params); seek_rows = sql.fetchall()
            sql.execute('SELECT * FROM customer_order WHERE user_id=%s ORDER BY created_at DESC,id DESC LIMIT %s,20', ('heavy-user', offset))
            if canonical(seek_rows) != canonical(sql.fetchall()): raise AssertionError('Deep page mismatch')
            sql.execute('EXPLAIN ANALYZE ' + seek, params)
            (args.output / 'explain-deep-page.json').write_text(json.dumps(sql.fetchall(), default=str, indent=2), encoding='utf-8')
            result = {'status': 'BOUNDED_SQL_COMPARISON_ACCEPT', 'samples': samples,
                      'medianMs': {arm: statistics.median(p[arm]['milliseconds'] for p in samples) for arm in ['n_plus_one', 'batch']},
                      'samePageContent': True, 'deepPageEquivalent': True, 'productionCapacity': 'NOT_MEASURED'}
            (args.output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
            metadata['status'] = 'COMPLETE'
    except Exception as error:
        metadata['status'] = 'FAILED'
        metadata['errorType'] = type(error).__name__
        raise
    finally:
        conn.close()
        (args.output / 'manifest.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
