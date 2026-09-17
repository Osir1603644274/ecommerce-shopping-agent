import json
from pathlib import Path
import tempfile
import unittest

from .cohort_report import load_environment_notes
from .close_interrupted_cohort import digest


class EnvironmentNotesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cohort = self.root / "cohort"
        self.cohort.mkdir()

    def fixture(self):
        reference = self.root / "maintenance.json"
        reference.write_text("{}", encoding="utf-8")
        note = {"events": [{"sourceResult": str(reference), "sourceResultSha256": digest(reference)}]}
        (self.cohort / "environment_notes.json").write_text(json.dumps(note), encoding="utf-8")
        return reference, note

    def test_optional_legacy_cohort_is_explicitly_absent(self):
        self.assertEqual(load_environment_notes(self.cohort), (None, {}))

    def test_source_receipt_and_notes_are_both_bound(self):
        reference, expected = self.fixture()
        notes, bindings = load_environment_notes(self.cohort)
        self.assertEqual(notes, expected)
        self.assertEqual(bindings[str(reference)], digest(reference))
        self.assertEqual(len(bindings), 2)

    def test_changed_receipt_cannot_silently_explain_times(self):
        reference, expected = self.fixture()
        reference.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source_changed"):
            load_environment_notes(self.cohort)

    def test_reference_outside_study_rejected_before_read(self):
        note = {"events": [{"sourceResult": str(self.root.parent / "NOT_READ.json"), "sourceResultSha256": "x"}]}
        (self.cohort / "environment_notes.json").write_text(json.dumps(note), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside_study"):
            load_environment_notes(self.cohort)


if __name__ == "__main__":
    unittest.main()
