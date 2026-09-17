"""Bounded replay of the reported budget omission; no order/payment writes."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

import httpx

OUT = Path('D:/agent-datasets/phone-budget-table-20260913-v1')
BASE = 'http://127.0.0.1:5173'
PREFIX = '/api/commerce-demo/workspace'


def save(name, value):
    path = OUT / name
    if path.exists():
        raise RuntimeError('Do not overwrite prior evidence: ' + name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


async def main():
    db_path = Path('.runtime/web-query-intake/conversations.sqlite3').resolve()
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
        rows = db.execute('SELECT payload_json FROM workspace_messages WHERE request_id=?',
                          ('5b716507-01b0-4d4b-bec9-ec8739f070cd',)).fetchall()
    save('reported-request.json', [json.loads(row[0]) for row in rows])
    async with httpx.AsyncClient(timeout=20) as c:
        offers = []
        for iid in (1710698, 320633, 1841291965546398700):
            r = await c.get(f'http://127.0.0.1:8080/api/products/{iid}/purchase-view')
            r.raise_for_status()
            offers.append(r.json())
        save('reported-product-offers.json', offers)
    headers = {'Sec-Fetch-Site': 'same-origin', 'Origin': BASE,
               'X-Conversation-Source': 'automated_test'}
    results = []
    async with httpx.AsyncClient(base_url=BASE, timeout=30, headers=headers) as c:
        r = await c.get(PREFIX)
        r.raise_for_status()
        c.headers['X-CSRF-Token'] = r.json()['csrfToken']
        # The first query is verbatim; the second checks a natural budget change.
        for index, query in enumerate(('推荐一部 2000 元左右的二手手机', '预算改成1500元以内'), 1):
            rid = str(uuid.uuid4())
            start = time.perf_counter()
            r = await c.post(PREFIX + '/run', json={'message': query, 'requestId': rid, 'mode': 'continuous'})
            r.raise_for_status()
            while time.perf_counter() - start < 180:
                r = await c.get(PREFIX)
                r.raise_for_status()
                value = r.json()
                if value.get('run', {}).get('status') in {'completed', 'failed', 'clarification', 'interrupted', 'ended'}:
                    break
                await asyncio.sleep(1)
            safe = {k: v for k, v in value.items() if k != 'csrfToken'}
            save(f'live{index:03d}.json', safe)
            answer = next((m for m in value['messages'] if m['role'] == 'assistant' and m['requestId'] == rid), {})
            cards = answer.get('cards', [])
            row = {'query': query, 'status': value['run']['status'], 'seconds': round(time.perf_counter()-start, 2),
                   'products': [{k: p.get(k) for k in ('id','title','priceMinor','priceKind')} for p in cards],
                   'answer': answer.get('content')}
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            assert row['status'] == 'completed' and cards
            if index == 1:
                assert row['answer'].startswith('本轮先按不超过 ¥2,000.00'), 'Missing budget interpretation'
            ceiling = 200000 if index == 1 else 150000
            assert all(type(p['priceMinor']) is int and p['priceMinor'] <= ceiling for p in cards)
            assert cards[0]['priceMinor'] >= ceiling * .85, 'No product near stated budget'
    save('RESULTS.json', {'scope': 'two-turn bounded replay; not general relevance evaluation', 'turns': results})


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=OUT)
    OUT = parser.parse_args().output.resolve()
    asyncio.run(main())
