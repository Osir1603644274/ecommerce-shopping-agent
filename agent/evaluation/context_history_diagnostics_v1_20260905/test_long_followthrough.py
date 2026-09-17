import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from . import long_followthrough as follow


class FollowThroughTests(unittest.TestCase):
    def test_live_process_always_waits_even_with_terminal_files(self):
        self.assertFalse(follow.readiness(True, {'childExitCode': 0}, {'completeMatchedCollection': True}))
        self.assertFalse(follow.readiness(True, None, None))

    def test_only_dead_and_complete_is_ready(self):
        self.assertTrue(follow.readiness(False, {'childExitCode': 0}, {'completeMatchedCollection': True}))

    def test_missing_terminal_is_failure_not_restart(self):
        for terminal, collection in [(None, None), ({'childExitCode': 0}, None),
                                      (None, {'completeMatchedCollection': True})]:
            with self.assertRaisesRegex(ValueError, 'gone_without_complete_terminal'):
                follow.readiness(False, terminal, collection)

    def test_failed_or_incomplete_is_not_reviewable(self):
        for terminal, collection in [({'childExitCode': 1}, {'completeMatchedCollection': True}),
                                      ({'childExitCode': 0}, {'completeMatchedCollection': False})]:
            with self.assertRaises(ValueError):
                follow.readiness(False, terminal, collection)

    def test_pid_reuse_does_not_match_creation(self):
        with patch.object(follow.psutil, 'Process') as make:
            make.return_value.create_time.return_value = 200.
            make.return_value.is_running.return_value = True
            self.assertFalse(follow.same_process_alive(10, 100.))
            self.assertTrue(follow.same_process_alive(10, 200.))

    def test_exact_cohort_command_normalizes_windows_separators(self):
        module = 'agent.evaluation.context_history_diagnostics_v1_20260905.serial_long_cohort'
        self.assertTrue(follow.command_matches_cohort([module, 'F:/agent/cohort'], 'f:\\agent\\cohort'))
        self.assertFalse(follow.command_matches_cohort([module, 'F:/agent/cohort-other'], 'F:/agent/cohort'))
        self.assertFalse(follow.command_matches_cohort(['unrelated', 'F:/agent/cohort'], 'F:/agent/cohort'))

    def test_existing_outputs_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'existing_output_preserved'):
                follow.require_new([p])
            follow.require_new([p / 'new'])

    def test_partial_json_while_alive_is_wait_not_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'result.json'
            p.write_text('{', encoding='utf8')
            self.assertIsNone(follow.read_optional_terminal(p, True))
            with self.assertRaisesRegex(ValueError, 'closed_terminal_corrupt'):
                follow.read_optional_terminal(p, False)

    def exercise_pipeline(self, packet_size):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        cohort = root / 'core72_v7_general_cohort001'
        cohort.mkdir()
        outer = root / 'core72_v7_general_cohort001_supervisor'
        outer.mkdir()
        for path, value in [(cohort / 'started.json', {'sources': {}}),
                            (cohort / 'result.json', {'completeMatchedCollection': True}),
                            (outer / 'result.json', {'childExitCode': 0})]:
            path.write_text(json.dumps(value), encoding='utf8')
        invoked = []
        def fake_run(args, **kwargs):
            module, arguments = args[5], args[6:]
            invoked.append((module, arguments))
            if module.endswith('.native_cost_audit'):
                Path(arguments[1]).write_text(json.dumps({'unknownUsageCalls': [], 'failedOrUnclosedCalls': []}), encoding='utf8')
            elif module.endswith('context_history_review_v2_20260905.run'):
                target = Path(arguments[0]); target.mkdir()
                (target / 'result.json').write_text(json.dumps({'status': 'PACKET_ROUND_TRIP_PASS_NO_MODEL_CALLS',
                    'packets': 36, 'maxApplicationRequestTokens': packet_size}), encoding='utf8')
            return SimpleNamespace(returncode=0)
        with patch.object(follow, 'HERE', root), patch.object(follow, 'same_process_alive', return_value=False), \
             patch.object(follow, 'closed_attempts', return_value=(['A001', 'B001', 'C001'], 72)), \
             patch.object(follow.subprocess, 'run', side_effect=fake_run):
            if packet_size > 192000:
                with self.assertRaisesRegex(ValueError, 'whole_cohort_packet_preflight'):
                    follow.run(cohort, root / 'follow', 1, 1.)
            else:
                follow.run(cohort, root / 'follow', 1, 1.)
                final = json.loads((root / 'follow/result.json').read_text(encoding='utf8'))
                self.assertFalse(final['goalComplete'])
        return invoked

    def test_packet_overflow_stops_before_judges(self):
        calls = self.exercise_pipeline(192001)
        self.assertEqual(len(calls), 3)
        self.assertFalse(any(name.endswith('.supervised_review') for name, _ in calls))

    def test_success_orders_all_steps_and_passes_explicit_budget(self):
        calls = self.exercise_pipeline(152227)
        self.assertEqual([name.rsplit('.', 1)[1] for name, _ in calls],
                         ['native_cost_audit', 'cohort_report', 'run', 'supervised_review', 'review_closeout'])
        self.assertEqual(calls[3][1][-2:], ['--packet-budget', '192000'])


if __name__ == '__main__':
    unittest.main()
