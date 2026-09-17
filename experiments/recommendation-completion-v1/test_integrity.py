import unittest
import numpy as np
from common import *
from ranking import make_fit

class Integrity(unittest.TestCase):
    def request(self):
        return {'request_id':'x','source':SOURCE,'user_id':'u','history':[{'item_id':'seen'}],
                'seen_all_fit_item_ids':['seen']}
    def test_outside_catalog_target_remains_denominator(self):
        pred=[{'request_id':'x','source':SOURCE,'arm':'a','item_ids':['yes']}]
        details,result=score(pred,[self.request()],[{'request_id':'x','target_item_ids':['yes','missing']}],
                             {'positive_item_user_counts':{}},[{'item_id':'yes'}])
        self.assertEqual(result['targets_outside_catalog'],1)
        self.assertEqual(details[0]['recall_at_100'],.5)
        self.assertAlmostEqual(details[0]['ndcg_at_10'],1/(1+1/math.log2(3)))
    def test_missing_predictions_rejected(self):
        r=self.request();r2={**r,'request_id':'y'}
        with self.assertRaises(AssertionError):
            score([{'request_id':'x','source':SOURCE,'arm':'a','item_ids':[]}],[r,r2],
                 [{'request_id':'x','target_item_ids':['yes']},{'request_id':'y','target_item_ids':['yes']}],
                 {'positive_item_user_counts':{}},[{'item_id':'yes'}])
    def test_seen_low_rated_items_rejected(self):
        with self.assertRaises(AssertionError):
            score([{'request_id':'x','source':SOURCE,'arm':'a','item_ids':['seen']}],[self.request()],
                  [{'request_id':'x','target_item_ids':['yes']}],{'positive_item_user_counts':{}},[{'item_id':'seen'}])
    def test_future_event_not_in_snapshot(self):
        events=[{'user_id':'u','item_id':'a','rating':5,'timestamp':1},
                {'user_id':'u','item_id':'low','rating':1,'timestamp':1},
                {'user_id':'u','item_id':'future','rating':5,'timestamp':3}]
        fit=make_fit(events,1)
        self.assertEqual(fit['user_positive_items'],{'u':['a']})
        self.assertEqual(fit['user_reviewed_items'],{'u':['a','low']})
        self.assertNotIn('future',fit['positive_item_user_counts'])
    def test_rank_negative_scores_and_ties(self):
        self.assertEqual(rank([-1,-1,-3],['a','b','c'],{'a'}),['b','c'])
        self.assertEqual(rank([-1,-1,-3],['a','b','c'],set(),positive=True),[])
    def test_basket_permutation_invariance(self):
        import torch
        from sequence import BasketModel,batch_tensor
        torch.manual_seed(17)
        for arch in ['mean','causal']:
            model=BasketModel(5,arch).eval()
            with torch.inference_mode():
                a=model(batch_tensor([[[1,2],[3,4]]],'cpu'))
                b=model(batch_tensor([[[2,1],[4,3]]],'cpu'))
            torch.testing.assert_close(a,b)
    def test_sequence_no_same_day_target_in_history(self):
        from sequence import examples
        events=[{'user_id':'u','item_id':i,'rating':5,'timestamp':t} for i,t in [('a',1),('b',1),('c',2),('d',2)]]
        ex=examples(events,{'a':1,'b':2,'c':3,'d':4})
        self.assertEqual(len(ex),2)
        for row in ex:
            self.assertEqual(row[2],[[1,2]])
            self.assertEqual(row[4],{1,2,3,4})
            self.assertIn(row[3],[3,4])

if __name__=='__main__':unittest.main()
