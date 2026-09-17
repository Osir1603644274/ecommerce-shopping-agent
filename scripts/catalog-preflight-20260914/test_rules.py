import unittest
from rules import *

class RulesTests(unittest.TestCase):
    def fields(self):
        return dict(title='手机', brand='unknown', seller='unknown', categoryL1='unknown', categoryL2='unknown', categoryL3='unknown')
    def test_missing(self):
        for x in [None, '', 'UNKNOWN', '其它/OTHER']:
            self.assertEqual(normalize(x), 'unknown')
    def test_real_text(self):
        self.assertEqual(normalize('  小米  手机 '), '小米 手机')
    def test_bad_text(self):
        with self.assertRaises(ValueError): normalize({'brand': 'x'})
    def test_native_ids(self):
        for x in [True, None, -1, 0, '01', '1.0', '1e2', WIDTH]:
            with self.assertRaises(ValueError): native_id(x)
    def test_namespace(self):
        a=candidate('kuaisearch','123',self.fields(),'v')
        b=candidate('multicpr','123',self.fields(),'v')
        self.assertNotEqual(a['id'],b['id'])
        self.assertLess(int(b['id']),2**53)
    def test_preserve(self):
        x=candidate('kuaisearch','123',self.fields(),'v',89)
        self.assertEqual(x['id'],'89'); self.assertEqual(x['action'],'PRESERVE_EXISTING_NO_WRITES')
    def test_replay(self):
        self.assertEqual(simulated_price('kuaisearch','1'),simulated_price('kuaisearch','1'))
        for i in range(1,1001):
            p=simulated_price('multicpr',str(i)); self.assertTrue(1000<=p<=999900 and p%100==0)
    def test_limits(self):
        x=candidate('kuaisearch','1',self.fields(),'v'); x['title']='a'*513
        self.assertIn('too_long:title',validate(x))
        x['title']='😀'*257; self.assertIn('too_long:title',validate(x))
    def test_corruption(self):
        x=candidate('kuaisearch','1',self.fields(),'v'); x['title']='a\x00b\ufffd'
        self.assertEqual(len(validate(x)),2)
    def test_multicpr(self):
        i,f=decode('multicpr','12\t红糖\n'.encode()); self.assertEqual(i,'12'); self.assertEqual(f['seller'],'unknown')
    def test_kuaisearch(self):
        i,f=decode('kuaisearch',b'{"item_id":12,"item_title":"X"}')
        self.assertEqual(f['brand'],'unknown')
    def test_bad_json(self):
        with self.assertRaises(ValueError): decode('kuaisearch',b'not json')
    def test_title_does_not_merge_identity(self):
        a=candidate('kuaisearch','1',self.fields(),'v')
        b=candidate('kuaisearch','2',self.fields(),'v')
        self.assertNotEqual(a['id'],b['id'])
    def test_maximum_native_id(self):
        x=candidate('multicpr',native_id(WIDTH-1),self.fields(),'v')
        self.assertLess(int(x['id']),2**53)
    def test_missing_price_not_verified(self):
        x=candidate('multicpr','1',self.fields(),'v')
        self.assertIsNone(x['snapshotPriceMinor'])
        self.assertEqual(x['priceStatus'],'missing')
        self.assertEqual(x['localOffer']['priceKind'],'local_simulated')
    def test_stock_conservation(self):
        s=candidate('kuaisearch','1',self.fields(),'v')['initialStock']
        self.assertEqual(s['total'],s['available']+s['reserved']+s['sold'])
    def test_exact_length_allowed(self):
        x=candidate('kuaisearch','1',self.fields(),'v');x['title']='a'*512
        self.assertEqual(validate(x),[])

if __name__=='__main__': unittest.main()
