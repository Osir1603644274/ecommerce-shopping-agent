import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import prepare_smoke as smoke


class RecommendationSmokeTests(unittest.TestCase):
    def test_unretrieved_truth_stays_in_denominator(self):
        recall, ndcg, hit = smoke.metrics(['a'], {'a', 'b'}, 10)
        self.assertEqual(recall, 0.5)
        self.assertAlmostEqual(ndcg, 1 / (1 + 1 / math.log2(3)))
        self.assertEqual(hit, 1)

    def test_no_hits_is_zero(self):
        self.assertEqual(smoke.metrics(['c'], {'a'}, 10), (0, 0, 0))

    def test_ideal_ranking_is_one(self):
        self.assertEqual(smoke.metrics(['a', 'b'], {'a', 'b'}, 10), (1, 1, 1))

    def test_predict_cannot_read_labels_or_repeat_seen_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            files = {
                'PROTOCOL.json': {},
                'public.json': [{'request_id': 'r', 'history': [{'item_id': 'a', 'time_index': 1}],
                                 'all_seen_item_ids': ['a']}],
                'fit_popularity.json': {'a': 10, 'b': 5, 'c': 1},
                'fit_neighbors.json': {'a': [['a', 1], ['c', 0.5]]},
            }
            # This fixture uses numeric IDs, matching the native KuaiSearch domain.
            mapping = {'a': '1', 'b': '2', 'c': '3'}
            files['public.json'][0]['history'][0]['item_id'] = '1'
            files['public.json'][0]['all_seen_item_ids'] = ['1']
            files['fit_popularity.json'] = {mapping[k]: v for k, v in files['fit_popularity.json'].items()}
            files['fit_neighbors.json'] = {'1': [['1', 1], ['3', 0.5]]}
            for name, obj in files.items():
                smoke.write(out / name, obj)
            smoke.write(out / 'PREPARED.json', {'files': {n: smoke.sha(out / n) for n in files}})
            original = smoke.read
            accessed = []
            def guarded(path):
                accessed.append(path.name)
                self.assertNotIn('private', path.name)
                return original(path)
            with patch.object(smoke, 'read', side_effect=guarded):
                smoke.predict(out)
            result = original(out / 'predictions.json')[0]
            self.assertEqual(result['itemcf'], ['3'])
            self.assertEqual(result['itemcf_popular_fallback'], ['3', '2'])
            self.assertNotIn('labels.private.json', accessed)


if __name__ == '__main__':
    unittest.main()
