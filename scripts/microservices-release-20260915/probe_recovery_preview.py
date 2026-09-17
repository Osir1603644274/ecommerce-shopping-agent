"""Bounded, read-only order-preview probe using the dedicated browser-test account."""
import json
import time
from pathlib import Path

import httpx

ROOT = Path('D:/agent-experiments/chat-recovery-20260916/live')
account = json.loads((ROOT / 'session.private.json').read_text())['account']
assert account['username'].startswith('recovery-test-')
state = json.loads((ROOT / 'android.json').read_text(encoding='utf-8'))
product_id = int(state['cards'][0]['id'])
records = []
with httpx.Client(timeout=15, trust_env=False) as client:
    auth = client.post('http://127.0.0.1:8080/api/auth/login', json=account)
    auth.raise_for_status()
    token = auth.json()['data']['accessToken']
    for round_index in range(3):
        for port in (18211, 18212, 8080):
            start = time.perf_counter()
            record = {'round': round_index + 1, 'port': port, 'productId': product_id}
            try:
                response = client.post(f'http://127.0.0.1:{port}/api/orders/preview',
                    headers={'Authorization': f'Bearer {token}'},
                    json={'itemType': 'PRODUCT', 'itemId': product_id, 'quantity': 1})
                record.update(status=response.status_code, body=response.json())
            except Exception as exc:
                record['error'] = type(exc).__name__
            record['durationMs'] = round((time.perf_counter() - start) * 1000)
            records.append(record)
            print(json.dumps({k: v for k, v in record.items() if k != 'body'}), flush=True)
            time.sleep(.3)
(ROOT / 'preview-probe.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
