import hashlib
import json
import tempfile
import unittest
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:
    Draft202012Validator = None

from evaluation.build_track_b_complex_category_capability_audit import (
    AUDIT_ID,
    RULE_VERSION,
    SCHEMA_VERSION,
    build,
)


class TrackBComplexCategoryCapabilityAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_root = Path(__file__).resolve().parents[1]
        cls.evidence = cls.agent_root / "tests" / "fixtures" / "kuaisearch_track_b_complex" / "evidence_products.jsonl"
        cls.feasibility = cls.agent_root / "tests" / "fixtures" / "kuaisearch_track_b_category_audit" / "feasibility.json"
        cls.schema = cls.agent_root / "evaluation" / "schemas" / "track_b_complex_category_capability_audit_v1.schema.json"

    def test_fixture_build_is_offline_audit_only_and_schema_conformant(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "audit"
            manifest = build(self.evidence, self.feasibility, output, self.schema)
            audit = json.loads((output / "complex_category_capability_audit.json").read_text(encoding="utf-8"))
            self.assertEqual(audit["schemaVersion"], SCHEMA_VERSION)
            self.assertEqual(audit["auditId"], AUDIT_ID)
            self.assertEqual(audit["ruleVersion"], RULE_VERSION)
            self.assertFalse(audit["networkUsed"])
            self.assertFalse(audit["usesLlmOrApi"])
            self.assertFalse(audit["provenance"]["queriesGenerated"])
            self.assertFalse(audit["provenance"]["qrelsGenerated"])
            self.assertFalse(audit["provenance"]["formalMetricsRun"])
            self.assertEqual(audit["provenance"]["adjudicationMode"], "independent_ai_blind_audit")
            self.assertFalse(audit["provenance"]["humanApproved"])
            self.assertFalse(manifest["queriesGenerated"])
            self.assertFalse(manifest["qrelsGenerated"])
            serialized = json.dumps(audit, ensure_ascii=False).casefold()
            self.assertNotIn("querytext", serialized)
            self.assertNotIn("qrel", " ".join(path.name.casefold() for path in output.iterdir()))
            for source in audit["inputs"].values():
                self.assertFalse(Path(source["path"]).is_absolute())
                self.assertNotIn("\\", source["path"])
            schema = json.loads(self.schema.read_text(encoding="utf-8"))
            if Draft202012Validator is not None:
                Draft202012Validator(schema).validate(audit)
            else:
                self.assertFalse(set(schema["required"]) - set(audit))

    def test_input_bytes_and_hashes_are_from_actual_fixture_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "audit"
            build(self.evidence, self.feasibility, output, self.schema)
            audit = json.loads((output / "complex_category_capability_audit.json").read_text(encoding="utf-8"))
            expected = {
                "evidenceProductsAudit": self.evidence,
                "exactCategoryAttributeFeasibilityAudit": self.feasibility,
            }
            for name, path in expected.items():
                self.assertEqual(audit["inputs"][name]["bytes"], path.stat().st_size)
                self.assertEqual(audit["inputs"][name]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_double_build_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "first", root / "second"
            build(self.evidence, self.feasibility, first, self.schema)
            build(self.evidence, self.feasibility, second, self.schema)
            names = sorted(path.name for path in first.iterdir())
            self.assertEqual(names, sorted(path.name for path in second.iterdir()))
            for name in names:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
