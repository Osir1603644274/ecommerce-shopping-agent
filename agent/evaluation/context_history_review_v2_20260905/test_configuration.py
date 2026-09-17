import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

from .run import validate_configuration


class ReviewConfigurationTests(unittest.TestCase):
    def test_old_and_explicit_large_budgets(self):
        for budget in (1000, 96000, 128000, 192000):
            validate_configuration(72, 6, budget)

    def test_invalid_budgets_lengths_and_types(self):
        for values in [(0, 6, 192000), (73, 6, 192000), (72, 13, 192000),
                       (72, 0, 192000), (72, 6, 999), (72, 6, 192001),
                       (72, 6, True), (72, 6, '192000')]:
            with self.assertRaisesRegex(ValueError, 'review_configuration_out_of_bounds'):
                validate_configuration(*values)

    def call_real_cli(self, budget):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        output = Path(temp.name) / 'preflight'
        nonce = uuid.uuid4().hex
        # Deliberately nonexistent sources: real CLI must pass admission, then
        # fail at file loading before any SubscriptionClient can be created.
        attempts = ['missing-review-' + nonce + '-' + arm for arm in 'ABC']
        args = [sys.executable, '-X', 'utf8', '-B', '-m',
                'agent.evaluation.context_history_review_v2_20260905.run', str(output),
                '--attempts', *attempts, '--turns', '72', '--chunk-size', '6',
                '--packet-budget', str(budget), '--judge-concurrency', '1', '--prepare-only']
        result = subprocess.run(args, capture_output=True, text=True, encoding='utf8', timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return result, output

    def test_actual_192k_cli_passes_guard_without_model_calls(self):
        result, output = self.call_real_cli(192000)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('FileNotFoundError', result.stderr)
        self.assertIn('turn-01.json', result.stderr)
        self.assertNotIn('review_configuration_out_of_bounds', result.stderr)
        failure = json.loads((output / 'failure.json').read_text(encoding='utf8'))
        self.assertEqual(failure['type'], 'FileNotFoundError')
        self.assertFalse(list(output.rglob('call-*')))
        self.assertFalse(list(output.rglob('runtime.json')))

    def test_actual_above_ceiling_cli_rejected_before_output(self):
        result, output = self.call_real_cli(192001)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('review_configuration_out_of_bounds', result.stderr)
        self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
