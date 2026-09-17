from contextlib import closing
import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from warehouse_simulator import create_server


class WarehouseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / 'warehouse.sqlite'
        self.faults = Path(self.temp.name) / 'faults.json'
        self.token = 'test-only-warehouse-token-12345'
        self.start()

    def start(self):
        self.server = create_server(self.database, self.token, 0, self.faults)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def tearDown(self):
        self.stop()
        assert Path(self.temp.name).resolve().parent == Path(tempfile.gettempdir()).resolve()
        self.temp.cleanup()

    def call(self, path, body=None):
        request = urllib.request.Request(self.base + path, data=body,
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)

    def command(self, quantity=1):
        return json.dumps(dict(requestKey='fulfillment-v1:order-1', orderId='order-1',
                               itemType='PRODUCT', itemId=1001, quantity=quantity), separators=(',', ':')).encode()

    def test_duplicate_and_restart_keep_one_durable_shipment(self):
        first = self.call('/shipments', self.command())
        self.assertEqual(first, self.call('/shipments', self.command()))
        self.stop()
        self.start()
        self.assertEqual(first, self.call('/shipments/by-request/fulfillment-v1%3Aorder-1'))
        self.assertEqual(first['commandHash'], hashlib.sha256(self.command()).hexdigest())
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM shipment').fetchone()[0], 1)

    def test_lost_response_is_reconciled_after_commit(self):
        self.faults.write_text(json.dumps({'mode': 'commit_then_drop_once'}))
        with self.assertRaises(Exception):
            self.call('/shipments', self.command())
        receipt = self.call('/shipments/by-request/fulfillment-v1%3Aorder-1')
        self.assertEqual(receipt, self.call('/shipments', self.command()))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM shipment').fetchone()[0], 1)

    def test_conflict_cannot_change_existing_shipment(self):
        receipt = self.call('/shipments', self.command())
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.call('/shipments', self.command(2))
        self.assertEqual(failure.exception.code, 409)
        self.assertEqual(receipt, self.call('/shipments/by-request/fulfillment-v1%3Aorder-1'))

    def test_concurrent_identical_commands_are_idempotent(self):
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: self.call('/shipments', self.command()), range(2)))
        self.assertEqual(results[0], results[1])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM shipment').fetchone()[0], 1)

    def test_unavailable_then_recovery(self):
        self.faults.write_text(json.dumps({'mode': 'unavailable'}))
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.call('/shipments', self.command())
        self.assertEqual(failure.exception.code, 503)
        self.faults.write_text('{}')
        self.assertEqual(self.call('/shipments', self.command())['orderId'], 'order-1')

    def cart(self, revision=2):
        return dict(schemaVersion='warehouse.cart.v2',requestKey=f'fulfillment-v2:order-cart:{revision}',
                    orderId='order-cart',revision=revision,
                    items=[dict(itemType='PRODUCT',itemId=1001,quantity=2),dict(itemType='PRODUCT',itemId=1002,quantity=1)])

    def test_cart_v2_dispatch_duplicate_and_bytes_bound_receipt(self):
        raw=json.dumps(self.cart(),separators=(',',':')).encode()
        first=self.call('/shipments',raw)
        self.assertEqual(first,self.call('/shipments',raw))
        self.assertEqual(first['commandHash'],hashlib.sha256(raw).hexdigest())
        self.stop();self.start()
        self.assertEqual(first,self.call('/shipments/by-request/fulfillment-v2%3Aorder-cart%3A2'))
        with self.assertRaises(urllib.error.HTTPError) as failure:
            self.call('/shipments',json.dumps(self.cart(3)).encode())
        self.assertEqual(failure.exception.code,409) # Cannot ship a new revision after this order was dispatched.

    def test_cart_v2_rejects_duplicate_sku_and_contract_identity(self):
        cases=[]
        value=self.cart();value['items'].append(value['items'][0]);cases.append(value)
        value=self.cart();value['requestKey']='fulfillment-v2:order-cart:1';cases.append(value)
        value=self.cart();value['schemaVersion']='warehouse.cart.v3';cases.append(value)
        value=self.cart();value['items'][0]['quantity']=0;cases.append(value)
        value=self.cart();value['items']=[None];cases.append(value)
        for value in cases:
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.call('/shipments',json.dumps(value).encode())
            self.assertEqual(failure.exception.code,409)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM shipment').fetchone()[0],0)


if __name__ == '__main__':
    unittest.main()
