import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from . import run as module


class CohortPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="review-path-test-")
        self.root = Path(self.tmp.name).resolve()
        self.scope = patch.object(module, "HERE", self.root)
        self.scope.start()

    def tearDown(self):
        self.scope.stop()
        self.tmp.cleanup()

    def declare(self, jobs, kind="SERIAL_DEVELOPMENT_COHORT_NOT_FORMAL"):
        cohort = self.root / "cohort"
        cohort.mkdir(exist_ok=True)
        (cohort / "started.json").write_text(json.dumps({"kind": kind, "jobs": jobs}), encoding="utf-8")

    def test_legacy_flat_path_is_unchanged(self):
        self.assertEqual(module.resolve_attempt("oldA001"), self.root / "oldA001")

    def test_declared_nested_cohort_only(self):
        expected = self.root / "cohort/A001"
        self.declare([{"output": str(expected)}])
        self.assertEqual(module.resolve_attempt("cohort/A001"), expected)
        with self.assertRaisesRegex(ValueError, "not_uniquely_declared"):
            module.resolve_attempt("cohort/B001")

    def test_missing_or_wrong_manifest_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing_cohort_binding"):
            module.resolve_attempt("cohort/A001")
        self.declare([{"output": str(self.root / "cohort/A001")}], kind="arbitrary")
        with self.assertRaisesRegex(ValueError, "wrong_cohort_kind"):
            module.resolve_attempt("cohort/A001")

    def test_duplicate_binding_rejected(self):
        job = {"output": str(self.root / "cohort/A001")}
        self.declare([job, job])
        with self.assertRaisesRegex(ValueError, "not_uniquely_declared"):
            module.resolve_attempt("cohort/A001")

    def test_escape_root_and_extra_nesting_rejected(self):
        for name in ("..", ".", "cohort/../../outside", "cohort/A001/nested"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                module.resolve_attempt(name)


if __name__ == "__main__":
    unittest.main()
