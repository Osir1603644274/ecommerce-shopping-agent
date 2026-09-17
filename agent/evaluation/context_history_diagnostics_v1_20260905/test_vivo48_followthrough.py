import unittest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from copy import deepcopy
from . import vivo48_followthrough as follow
from .vivo48_followthrough import HERE, targets, validate_profile, validate_packet, readiness


class VivoFollowthroughTests(unittest.TestCase):
    def test_fixed_new_targets(self):
        rows = targets()
        self.assertEqual(len(rows), 7)
        self.assertEqual(len(set(rows.values())), 7)
        self.assertTrue(all(p.parent == HERE and p.name.startswith('core48_v7_vivo_') for p in rows.values()))

    def test_only_approved_profile(self):
        row = {'profile': 'vivo48', 'plannedTurnsEach': 48, 'order': 'CAB',
            'scriptSha256': '60e945655d27f36a9ffe10d5e1e39e99d458b0370a948e561440db1f91573485'}
        cohort, output = HERE / 'core48_v7_vivo_cohort001', HERE / 'new_fixture_not_created'
        validate_profile(cohort, output, row)
        for key, value in [('profile', 'general72'), ('plannedTurnsEach', 72), ('order', 'ABC'), ('scriptSha256', 'bad')]:
            bad = deepcopy(row)
            bad[key] = value
            with self.assertRaises(ValueError):
                validate_profile(cohort, output, bad)

    def test_full24_packet_gate(self):
        row = {'status': 'PACKET_ROUND_TRIP_PASS_NO_MODEL_CALLS', 'packets': 24, 'maxApplicationRequestTokens': 192000}
        validate_packet(row)
        for key, value in [('status', 'PASS'), ('packets', 23), ('maxApplicationRequestTokens', 192001), ('maxApplicationRequestTokens', None)]:
            bad = {**row, key: value}
            with self.assertRaises(ValueError):
                validate_packet(bad)

    def test_live_never_authorizes_review(self):
        self.assertFalse(readiness(True, {'childExitCode': 0}, {'completeMatchedCollection': True}))
        self.assertTrue(readiness(False, {'childExitCode': 0}, {'completeMatchedCollection': True}))
        with self.assertRaises(ValueError):
            readiness(False, None, {'completeMatchedCollection': True})
        with self.assertRaises(ValueError):
            readiness(False, {'childExitCode': 0}, {'completeMatchedCollection': False})

    def pipeline(self, size=150000, unknown=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cohort = root / 'core48_v7_vivo_cohort001'
            cohort.mkdir()
            outer = root / (cohort.name + '_supervisor')
            outer.mkdir()
            manifest = {'profile': 'vivo48', 'plannedTurnsEach': 48, 'order': 'CAB', 'sources': {},
                'scriptSha256': '60e945655d27f36a9ffe10d5e1e39e99d458b0370a948e561440db1f91573485'}
            for p, value in [(cohort / 'started.json', manifest),
                (cohort / 'result.json', {'completeMatchedCollection': True}),
                (outer / 'result.json', {'childExitCode': 0})]:
                p.write_text(json.dumps(value), encoding='utf8')
            calls = []
            def fake_run(args, **kwargs):
                module, arguments = args[5], args[6:]
                calls.append((module, arguments))
                if module.endswith('.native_cost_audit'):
                    Path(arguments[1]).write_text(json.dumps({'unknownUsageCalls': ['fixture'] if unknown else [],
                        'failedOrUnclosedCalls': []}), encoding='utf8')
                elif module.endswith('context_history_review_v2_20260905.run'):
                    target = Path(arguments[0])
                    target.mkdir()
                    (target / 'result.json').write_text(json.dumps({'status': 'PACKET_ROUND_TRIP_PASS_NO_MODEL_CALLS',
                        'packets': 24, 'maxApplicationRequestTokens': size}), encoding='utf8')
                return SimpleNamespace(returncode=0)
            with patch.object(follow, 'HERE', root), patch.object(follow, 'same_process_alive', return_value=False), \
                 patch.object(follow, 'closed_attempts', return_value=(['A001', 'B001', 'C001'], 48)), \
                 patch.object(follow.subprocess, 'run', side_effect=fake_run):
                if unknown or size > 192000:
                    with self.assertRaises(ValueError):
                        follow.run(cohort, root / 'follow', 1, 1.)
                    self.assertTrue((root / 'follow/failure.json').exists())
                else:
                    follow.run(cohort, root / 'follow', 1, 1.)
                    self.assertFalse(json.loads((root / 'follow/result.json').read_text(encoding='utf8'))['goalComplete'])
            return calls

    def test_pipeline_orders_three_native_audits_before_review(self):
        calls = self.pipeline()
        self.assertEqual([m.rsplit('.', 1)[1] for m, _ in calls],
            ['native_cost_audit'] * 3 + ['cohort_report', 'run', 'supervised_review', 'review_closeout'])
        self.assertEqual(calls[5][1][-2:], ['--packet-budget', '192000'])

    def test_packet_overflow_never_launches_judge(self):
        calls = self.pipeline(size=192001)
        self.assertEqual(len(calls), 5)
        self.assertFalse(any(m.endswith('.supervised_review') for m, _ in calls))

    def test_unknown_cost_never_launches_judge(self):
        self.assertEqual(len(self.pipeline(unknown=True)), 1)


if __name__ == '__main__':
    unittest.main()
