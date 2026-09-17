"""Read-only startup gate: healthy process != usable product/inventory chain."""
import json
import time
from pathlib import Path
import httpx

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / '.runtime/merged-commerce'

def main():
    marker = RUNTIME / 'microservices-release.json'
    if not marker.exists():
        return
    state = json.loads(marker.read_text(encoding='utf8'))
    token = (RUNTIME / 'secrets/catalog-internal.token').read_text().strip()
    ids = []
    with (ROOT / 'datasets/current/used-phone/catalog.jsonl').open(encoding='utf8') as source:
        for line in source:
            if line.strip():
                ids.append(int(json.loads(line)['itemId']))
            if len(ids) == 10:
                break
    assert len(ids) == 10 and len(set(ids)) == 10, 'Readiness sample is incomplete'
    report = {'status': 'STARTED', 'readOnly': True, 'sampleIds': ids, 'instances': []}
    try:
        with httpx.Client(timeout=8, trust_env=False) as client:
            for url in state['catalogUrls']:
                assert url in ('http://127.0.0.1:18201', 'http://127.0.0.1:18202')
                checks = []
                for attempt in range(3):
                    start = time.monotonic()
                    try:
                        response = client.post(url + '/api/products/resolve',
                            headers={'X-Internal-Service-Token': token}, json={'productIds': ids})
                        response.raise_for_status()
                        payload = response.json()
                        rows = payload.get('data')
                        assert payload.get('success') is True and isinstance(rows, list)
                        assert {int(row['id']) for row in rows} == set(ids), 'Incomplete catalog binding'
                        assert all(type(row.get('availableQuantity')) is int and row['availableQuantity'] >= 0 for row in rows), 'Inventory read is not verified'
                        checks.append({'status': 'PASS', 'durationMs': round((time.monotonic()-start)*1000)})
                        break
                    except (httpx.HTTPError, ValueError, TypeError, KeyError, AssertionError) as exc:
                        checks.append({'status': 'FAILED', 'errorType': type(exc).__name__,
                                       'durationMs': round((time.monotonic()-start)*1000)})
                        if attempt == 2:
                            raise RuntimeError('Product/inventory read unavailable: ' + url) from None
                        time.sleep(0.3)
                    finally:
                        report['instances'] = [r for r in report['instances'] if r['url'] != url] + [{'url': url, 'attempts': checks}]
        report['status'] = 'PASS'
        print('Product/inventory read readiness passed for both catalog instances (read-only).')
    except Exception:
        report['status'] = 'FAILED'
        raise
    finally:
        (RUNTIME / 'read-dependencies.latest.json').write_text(json.dumps(report, indent=2), encoding='utf8')

if __name__ == '__main__':
    main()
