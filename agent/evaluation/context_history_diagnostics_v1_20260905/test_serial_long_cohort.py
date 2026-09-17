from pathlib import Path
import unittest
from unittest.mock import patch

from . import serial_long_cohort as cohort


class FixedLongProfiles(unittest.TestCase):
    def test_general_fixed_pairing_and_budgets(self):
        script, digest, turns, order, jobs = cohort.schedule(cohort.HERE / "NOT_STARTED_TEST", "general72")
        self.assertEqual((turns, order), (72, "BCA"))
        self.assertEqual([job["label"] for job in jobs], list(order))
        for job in jobs:
            args = job["arguments"]
            self.assertEqual(args[args.index("--script") + 1], str(script))
            self.assertEqual(args[args.index("--input-budget") + 1], "96000")
            self.assertEqual(args[args.index("--attempt-timeout") + 1], "7200")
        self.assertIn("16000", jobs[0]["arguments"])
        self.assertIn("32000", jobs[1]["arguments"])
        self.assertNotIn("--working-budget", jobs[2]["arguments"])

    def test_vivo_fixed_length_and_counterorder(self):
        script, digest, turns, order, jobs = cohort.schedule(cohort.HERE / "NOT_STARTED_TEST", "vivo48")
        self.assertEqual((turns, order), (48, "CAB"))
        self.assertEqual([job["label"] for job in jobs], list(order))

    def test_source_script_drift_rejected_before_output(self):
        with patch.object(cohort, "file_sha", return_value="changed"):
            with self.assertRaisesRegex(ValueError, "script_drift"):
                cohort.schedule(Path("NEVER_CREATED"), "general72")

    def test_current_sut_drift_rejected_before_output(self):
        target = cohort.HERE / "NOT_STARTED_TEST"
        self.assertFalse(target.exists())
        with patch.object(cohort, "verify_sources", side_effect=RuntimeError("source_changed")):
            with self.assertRaisesRegex(RuntimeError, "source_changed"):
                cohort.run(target, "general72")
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
