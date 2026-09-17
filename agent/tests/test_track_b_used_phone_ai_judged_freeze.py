import hashlib
import json
import shutil
import socket
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from evaluation.build_track_b_used_phone_ai_judged_freeze import (
    ACTION_SCHEMA_VERSION,
    AUDIT_SCHEMA_VERSION,
    CONSTRAINT_SCHEMA_VERSION,
    FROZEN_INPUT_SHA256,
    MANIFEST_SCHEMA_VERSION,
    QREL_SCHEMA_VERSION,
    STATUS,
    build,
)


class TrackBUsedPhoneAiJudgedFreezeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_root = Path(__file__).resolve().parents[1]
        cls.repo_root = cls.agent_root.parent
        cls.schemas = cls.agent_root / "evaluation" / "schemas"
        cls.fixture_contract = json.loads((
            cls.agent_root / "tests" / "fixtures" / "kuaisearch_track_b_used_phone_ai_judged_freeze" / "frozen_input_contract.json"
        ).read_text(encoding="utf-8"))
        dataset = cls.repo_root / "data" / "processed" / "ecommerce" / "kuaisearch_synthetic_evidence_track_b_v01" / "09807c773ce67360ed8df30842e372182fcf7ad9"
        cls.pilot = dataset / "used_phone_agent_pilot_v1"
        cls.comparison = dataset / "used_phone_agent_pilot_post_blind_comparison_v1"

    def paths(self):
        return {
            "reviewer": self.pilot / "independent_ai_blind_review_v1.jsonl",
            "case_action_review": self.pilot / "independent_ai_blind_case_action_review_v1.jsonl",
            "adjudication": self.comparison / "independent_ai_adjudication_v1.jsonl",
            "adjudication_summary": self.comparison / "independent_ai_adjudication_summary_v1.json",
            "comparison_manifest": self.comparison / "comparison_manifest.json",
            "formal_cases": self.pilot / "used_phone_agent_cases_pending.jsonl",
            "sealed_mapping": self.pilot / "blind_id_mapping_sealed_not_gold.json",
            "sealed_suggestions": self.pilot / "automatic_judgment_suggestions_sealed_not_gold.jsonl",
        }

    def run_build(self, paths, output, expected_hashes=None):
        return build(
            paths["reviewer"], paths["case_action_review"], paths["adjudication"],
            paths["adjudication_summary"], paths["comparison_manifest"], paths["formal_cases"],
            paths["sealed_mapping"], paths["sealed_suggestions"], output, self.schemas,
            expected_hashes=expected_hashes or self.fixture_contract["inputSha256"],
            expected_grade_distribution=self.fixture_contract["expectedFinalGrade"],
            expected_eligible_distribution=self.fixture_contract["expectedFinalEligible"],
        )

    @staticmethod
    def jsonl(path):
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_frozen_input_fixture_matches_module_contract(self):
        self.assertEqual(self.fixture_contract["inputSha256"], FROZEN_INPUT_SHA256)
        role_paths = {
            "blindReviewer": self.paths()["reviewer"],
            "blindCaseActionReview": self.paths()["case_action_review"],
            "independentAiAdjudication": self.paths()["adjudication"],
            "independentAiAdjudicationSummary": self.paths()["adjudication_summary"],
            "comparisonManifest": self.paths()["comparison_manifest"],
            "formalCases": self.paths()["formal_cases"],
            "sealedMapping": self.paths()["sealed_mapping"],
            "sealedSuggestions": self.paths()["sealed_suggestions"],
        }
        self.assertEqual({role: self.sha(path) for role, path in role_paths.items()}, FROZEN_INPUT_SHA256)

    def test_builds_expected_ai_only_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "freeze"
            manifest = self.run_build(self.paths(), output)
            qrel = self.jsonl(output / "ai_judged_candidate_qrel_v1.jsonl")
            actions = self.jsonl(output / "ai_judged_case_action_v1.jsonl")
            constraints = self.jsonl(output / "ai_judged_query_constraints_v1.jsonl")
            audit = json.loads((output / "freeze_audit.json").read_text(encoding="utf-8"))

            self.assertEqual(manifest["status"], STATUS)
            self.assertEqual(len(qrel), 80)
            self.assertEqual(Counter("null" if row["relevanceGrade"] is None else str(row["relevanceGrade"]) for row in qrel), Counter(self.fixture_contract["expectedFinalGrade"]))
            self.assertEqual(Counter("true" if row["eligible"] is True else "false" if row["eligible"] is False else "unknown" for row in qrel), Counter(self.fixture_contract["expectedFinalEligible"]))
            self.assertEqual(Counter(row["finalBasis"] for row in qrel), Counter({"reviewer_sealed_exact_agreement": 70, "independent_ai_adjudication": 10}))
            self.assertTrue(all(row["labelKind"] == "ai_judged_not_human_gold" and row["humanApproved"] is False and row["aiOnly"] is True for row in qrel))
            self.assertTrue(all(row["reviewerValue"] == row["sealedAutomaticValue"] for row in qrel if row["finalBasis"] == "reviewer_sealed_exact_agreement"))

            self.assertEqual(len(actions), 8)
            self.assertTrue(all(row["semanticAgreement"] for row in actions))
            self.assertEqual({row["semanticPairMapVersion"] for row in actions}, {self.fixture_contract["actionSemanticMapVersion"]})
            compare = next(row for row in actions if row["reviewCaseId"] == "BLIND-CASE-005")
            self.assertTrue(compare["comparisonSupport"]["recommendedCandidateHasLowerBatteryBandThanA"])
            self.assertEqual(compare["comparisonSupport"]["candidateABatteryBand"], "90%+")
            self.assertEqual(compare["comparisonSupport"]["candidateBBatteryBand"], "80%-90%")
            self.assertIsNotNone(next(row for row in actions if row["reviewCaseId"] == "BLIND-CASE-004")["supportingActionOnlyReview"])
            self.assertIsNotNone(next(row for row in actions if row["reviewCaseId"] == "BLIND-CASE-007")["supportingActionOnlyReview"])

            self.assertEqual(len(constraints), 8)
            self.assertTrue(all(row["stableVariantCount"] == 1 for row in constraints))
            self.assertEqual({row["sourceReviewRowCount"] for row in constraints}, {1, 16})
            self.assertFalse(audit["structuredEvidenceAuditTrail"]["semanticAgreementClaimed"])
            self.assertFalse(audit["structuredEvidenceAuditTrail"]["humanFieldAdjudicationClaimed"])
            self.assertTrue(audit["candidatePoolBoundary"]["unpooledOrUnjudgedProductsAreNotAssumedNegative"])
            self.assertFalse(manifest["humanGoldWritten"])
            self.assertFalse(manifest["finalClosedTestWritten"])

    def test_output_schemas_validate(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "freeze"
            self.run_build(self.paths(), output)
            fixtures = [
                ("ai_judged_candidate_qrel_v1.jsonl", "track_b_used_phone_ai_judged_candidate_qrel_v1.schema.json", QREL_SCHEMA_VERSION, True),
                ("ai_judged_case_action_v1.jsonl", "track_b_used_phone_ai_judged_case_action_v1.schema.json", ACTION_SCHEMA_VERSION, True),
                ("ai_judged_query_constraints_v1.jsonl", "track_b_used_phone_ai_judged_query_constraints_v1.schema.json", CONSTRAINT_SCHEMA_VERSION, True),
                ("freeze_audit.json", "track_b_used_phone_ai_judged_freeze_audit_v1.schema.json", AUDIT_SCHEMA_VERSION, False),
                ("manifest.json", "track_b_used_phone_ai_judged_freeze_manifest_v1.schema.json", MANIFEST_SCHEMA_VERSION, False),
            ]
            for data_name, schema_name, version, is_jsonl in fixtures:
                schema = json.loads((self.schemas / schema_name).read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema)
                records = self.jsonl(output / data_name) if is_jsonl else [json.loads((output / data_name).read_text(encoding="utf-8"))]
                for record in records:
                    self.assertEqual(record["schemaVersion"], version)
                    validator.validate(record)

    def test_rejects_hash_drift_and_review_row_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.paths()
            drift = root / "drift-review.jsonl"
            shutil.copyfile(paths["reviewer"], drift)
            drift.write_bytes(drift.read_bytes().replace(b"exclude", b"EXCLUDE", 1))
            changed = dict(paths)
            changed["reviewer"] = drift
            with self.assertRaisesRegex(ValueError, "SHA-256 drift"):
                self.run_build(changed, root / "out-drift")

            original_lines = paths["reviewer"].read_text(encoding="utf-8").splitlines()
            variants = {
                "missing": original_lines[:-1],
                "extra": original_lines + [original_lines[-1]],
                "duplicate": original_lines[:-1] + [original_lines[0]],
            }
            for name, lines in variants.items():
                with self.subTest(name=name):
                    reviewer = root / f"{name}.jsonl"
                    reviewer.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
                    changed = dict(paths)
                    changed["reviewer"] = reviewer
                    hashes = dict(self.fixture_contract["inputSha256"])
                    hashes["blindReviewer"] = self.sha(reviewer)
                    with self.assertRaises(ValueError):
                        self.run_build(changed, root / f"out-{name}", hashes)

    def test_rejects_missing_or_duplicate_adjudication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self.paths()
            lines = paths["adjudication"].read_text(encoding="utf-8").splitlines()
            variants = {"missing": lines[:-1], "duplicate": lines[:-1] + [lines[0]]}
            for name, content in variants.items():
                with self.subTest(name=name):
                    adjudication = root / f"{name}-adjudication.jsonl"
                    adjudication.write_text("\n".join(content) + "\n", encoding="utf-8", newline="\n")
                    changed = dict(paths)
                    changed["adjudication"] = adjudication
                    hashes = dict(self.fixture_contract["inputSha256"])
                    hashes["independentAiAdjudication"] = self.sha(adjudication)
                    with self.assertRaises(ValueError):
                        self.run_build(changed, root / f"out-{name}", hashes)

    def test_offline_double_build_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "root-a" / "freeze", root / "root-b" / "freeze"
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
                self.run_build(self.paths(), first)
                self.run_build(self.paths(), second)
            names = sorted(path.name for path in first.iterdir())
            self.assertEqual(names, sorted(path.name for path in second.iterdir()))
            for name in names:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
