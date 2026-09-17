import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import supervised_review_resume as subject


class ResumeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.parent = self.root / 'review'
        self.parent.mkdir()
        self.outer = self.root / 'review_supervisor'
        self.outer.mkdir()
        (self.parent / 'failure.json').write_text('{}', encoding='utf-8')
        (self.outer / 'started.json').write_text(json.dumps({'supervisorPid': 12345}), encoding='utf-8')
        (self.outer / 'result.json').write_text(json.dumps({'childExitCode': 1}), encoding='utf-8')
        self.base = patch.object(subject, 'HERE', self.root)
        self.base.start()
        self.addCleanup(self.base.stop)

    @patch.object(subject.psutil, 'pid_exists', return_value=False)
    def test_closed_failed_parent(self, probe):
        self.assertEqual(subject.closed_parent(self.parent), self.parent.resolve())
        probe.assert_called_once_with(12345)

    @patch.object(subject.psutil, 'pid_exists', return_value=True)
    def test_live_or_reused_pid_refused_without_kill(self, probe):
        with self.assertRaisesRegex(ValueError, 'pid_still_exists'):
            subject.closed_parent(self.parent)

    @patch.object(subject.psutil, 'pid_exists', return_value=False)
    def test_missing_failure_refused(self, probe):
        (self.parent / 'failure.json').unlink()
        with self.assertRaisesRegex(ValueError, 'failed_closed'):
            subject.closed_parent(self.parent)

    @patch.object(subject.psutil, 'pid_exists', return_value=False)
    def test_completed_review_cannot_repeat(self, probe):
        (self.parent / 'result.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'completed_review'):
            subject.closed_parent(self.parent)

    @patch.object(subject.psutil, 'pid_exists', return_value=False)
    def test_zero_exit_not_a_failed_parent(self, probe):
        (self.outer / 'result.json').write_text(json.dumps({'childExitCode': 0}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'failed_closed'):
            subject.closed_parent(self.parent)


if __name__ == '__main__':
    unittest.main()
