from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from document_paths import DocumentLocations


class DocumentLocationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="document-path-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "docs/archive/status/OLD.md"
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"Original report\n")
        self.entry = {
            "original": "docs/OLD.md",
            "current": "docs/archive/status/OLD.md",
            "relativeLinksFrom": "docs/OLD.md",
            "preserveBytes": True,
            "sha256BeforeMove": hashlib.sha256(self.target.read_bytes()).hexdigest(),
        }

    def manifest(self, entries=None):
        file = self.root / "docs/governance/document-locations.json"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps({"schemaVersion": 1, "entries": entries if entries is not None else [self.entry]}), encoding="utf-8")
        return DocumentLocations(self.root)

    def test_old_path_resolves_without_recreating_it(self):
        locations = self.manifest()
        self.assertEqual(locations.resolve(self.root / "docs/OLD.md"), self.target)
        self.assertFalse((self.root / "docs/OLD.md").exists())
        self.assertEqual(locations.verify()["status"], "PASS")

    def test_original_relative_link_base_is_preserved(self):
        locations = self.manifest()
        self.assertEqual(locations.reference_source(self.target), self.root / "docs/OLD.md")

    def test_public_snapshot_without_mapping_works(self):
        locations = DocumentLocations(self.root)
        self.assertEqual(locations.resolve(self.target), self.target)
        self.assertEqual(locations.entries, [])

    def test_unknown_missing_path_is_not_hidden(self):
        locations = self.manifest()
        missing = self.root / "docs/missing.md"
        self.assertEqual(locations.resolve(missing), missing)
        self.assertFalse(locations.resolve(missing).exists())

    def test_tampered_original_fails(self):
        locations = self.manifest()
        self.target.write_bytes(b"Changed content\n")
        self.assertEqual(locations.verify()["status"], "FAIL")

    def test_missing_original_fails(self):
        locations = self.manifest()
        self.target.unlink()
        self.assertEqual(locations.verify()["status"], "FAIL")

    def test_recreated_old_path_fails(self):
        locations = self.manifest()
        (self.root / "docs/OLD.md").write_bytes(b"duplicate")
        self.assertEqual(locations.verify()["status"], "FAIL")

    def test_duplicate_mapping_rejected(self):
        with self.assertRaises(ValueError):
            self.manifest([self.entry, self.entry])

    def test_path_outside_docs_rejected(self):
        self.entry["current"] = "docs/../../outside.md"
        with self.assertRaises(ValueError):
            self.manifest()


if __name__ == "__main__":
    unittest.main()
