import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import supervised_review as review


class ClosedReviewBoundary(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cohort = self.root / "cohort"
        self.cohort.mkdir()
        self.outer = self.root / "cohort_supervisor"
        self.outer.mkdir()
        self.start = {"kind": "SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL",
            "jobs": [{"label": label, "output": str(self.cohort / (label + "001"))}
                for label in "ABC"], "sources": {"fixture": "hash"}, "plannedTurnsEach": 24}
        self.save(self.cohort / "started.json", self.start)
        self.save(self.cohort / "result.json", {"completeMatchedCollection": True})
        self.save(self.outer / "result.json", {"childExitCode": 0})
        self.here = patch.object(review, "HERE", self.root)
        self.here.start()
        self.addCleanup(self.here.stop)
        self.verify = patch.object(review, "verify_sources")
        self.verifier = self.verify.start()
        self.addCleanup(self.verify.stop)

    @staticmethod
    def save(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_closed_exact_three(self):
        attempts, turns = review.closed_attempts(self.cohort)
        self.assertEqual(attempts, ["cohort/A001", "cohort/B001", "cohort/C001"])
        self.assertEqual(turns, 24)
        self.verifier.assert_called_once_with({"fixture": "hash"})

    def test_running_or_incomplete_rejected(self):
        self.save(self.cohort / "result.json", {"completeMatchedCollection": False})
        with self.assertRaisesRegex(ValueError, "closed_complete"):
            review.closed_attempts(self.cohort)

    def test_unclosed_outer_rejected(self):
        (self.outer / "result.json").unlink()
        with self.assertRaises(FileNotFoundError):
            review.closed_attempts(self.cohort)

    def test_child_escape_rejected(self):
        self.start["jobs"][0]["output"] = str(self.root / "elsewhere")
        self.save(self.cohort / "started.json", self.start)
        with self.assertRaisesRegex(ValueError, "declared_direct_child"):
            review.closed_attempts(self.cohort)

    def test_duplicate_arm_rejected(self):
        self.start["jobs"][0]["label"] = "B"
        self.save(self.cohort / "started.json", self.start)
        with self.assertRaisesRegex(ValueError, "three_unique"):
            review.closed_attempts(self.cohort)

    def test_source_drift_rejected(self):
        self.verifier.side_effect = ValueError("source_drift")
        with self.assertRaisesRegex(ValueError, "source_drift"):
            review.closed_attempts(self.cohort)

    def test_legacy_budget_default_preserved(self):
        args = review.worker_arguments('review-output', ['A', 'B', 'C'], 72)
        self.assertEqual(args[args.index('--packet-budget') + 1], '96000')
        self.assertEqual(args[args.index('--judge-concurrency') + 1], '1')

    def test_explicit_large_review_budget_changes_only_admission_parameter(self):
        old = review.worker_arguments('review-output', ['A', 'B', 'C'], 72)
        new = review.worker_arguments('review-output', ['A', 'B', 'C'], 72, 192000)
        self.assertEqual(len(old), len(new))
        self.assertEqual([(a, b) for a, b in zip(old, new) if a != b], [('96000', '192000')])

    def test_other_review_budgets_rejected(self):
        for value in (0, -1, 128000, 1000000, True, '192000'):
            with self.assertRaisesRegex(ValueError, 'unapproved_review_packet_budget'):
                review.worker_arguments('review-output', ['A', 'B', 'C'], 72, value)


if __name__ == "__main__":
    unittest.main()
