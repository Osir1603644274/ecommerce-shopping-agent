import copy
import json
import shutil
import socket
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from evaluation.build_track_b_used_phone_ai_judged_freeze import (
    FROZEN_INPUT_SHA256,
    sha256_file,
    write_jsonl,
)
from evaluation.finalize_track_b_used_phone_ai_judged_pilot import (
    ACTION_SEMANTIC_MAP,
    EXPECTED_OUTPUT_FILES,
    INPUT_RELATIVE_PATHS,
    STATUS,
    default_base_dir,
    finalize,
    validate_output_bundle,
)


class FinalizeTrackBUsedPhoneAiJudgedPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_root = Path(__file__).resolve().parents[1]
        cls.base_dir = default_base_dir()
        cls.contract_path = (
            cls.agent_root
            / "tests"
            / "fixtures"
            / "kuaisearch_track_b_used_phone_ai_judged_freeze"
            / "freeze_contract_v1.json"
        )
        cls.contract = json.loads(cls.contract_path.read_text(encoding="utf-8"))

    @staticmethod
    def read_jsonl(path: Path) -> list[dict]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def make_mutable_input_copy(self, destination: Path) -> Path:
        base = destination / "base"
        for relative in INPUT_RELATIVE_PATHS.values():
            source = self.base_dir / relative
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        comparison_source = self.base_dir / "used_phone_agent_pilot_post_blind_comparison_v1"
        comparison_target = base / comparison_source.name
        manifest = json.loads((comparison_source / "comparison_manifest.json").read_text(encoding="utf-8"))
        for descriptor in manifest["artifacts"]:
            source = comparison_source / descriptor["path"]
            target = comparison_target / descriptor["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return base

    def test_contract_fixture_matches_implementation_and_frozen_inputs(self):
        self.assertEqual(self.contract["status"], STATUS)
        self.assertEqual(self.contract["inputSha256"], FROZEN_INPUT_SHA256)
        self.assertEqual(
            {key: tuple(value) for key, value in self.contract["semanticPairs"].items()},
            ACTION_SEMANTIC_MAP,
        )
        for role, relative in INPUT_RELATIVE_PATHS.items():
            self.assertEqual(sha256_file(self.base_dir / relative), self.contract["inputSha256"][role])

    def test_real_freeze_validates_all_schemas_counts_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "freeze"
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
                manifest = finalize(self.base_dir, output)
            self.assertEqual(manifest, validate_output_bundle(output))
            self.assertEqual({path.name for path in output.iterdir()}, EXPECTED_OUTPUT_FILES)
            self.assertEqual(manifest["status"], STATUS)
            self.assertEqual(manifest["counts"], {
                "candidateQrelRows": 80,
                "caseActionRows": 8,
                "queryConstraintRows": 8,
                "adjudicationRows": 10,
            })
            qrels = self.read_jsonl(output / "ai_judged_candidate_qrel_v1.jsonl")
            self.assertEqual(Counter(str(row["relevanceGrade"]) if row["relevanceGrade"] is not None else "null" for row in qrels), Counter(self.contract["expectedGradeDistribution"]))
            self.assertEqual(Counter("true" if row["eligible"] is True else "false" if row["eligible"] is False else "unknown" for row in qrels), Counter(self.contract["expectedEligibleDistribution"]))
            self.assertEqual(Counter(row["finalBasis"] for row in qrels), Counter(self.contract["expectedFinalBasisDistribution"]))
            self.assertTrue(all(row["humanApproved"] is False for row in qrels))
            self.assertTrue(all(row["labelKind"] == "ai_judged_not_human_gold" for row in qrels))

            actions = self.read_jsonl(output / "ai_judged_case_action_v1.jsonl")
            self.assertEqual(len(actions), 8)
            self.assertTrue(all(row["semanticAgreement"] for row in actions))
            by_case = {row["reviewCaseId"]: row for row in actions}
            self.assertEqual(by_case["BLIND-CASE-005"]["comparisonSupport"]["recommendedCandidateHasLowerBatteryBandThanA"], True)
            self.assertIsNotNone(by_case["BLIND-CASE-004"]["supportingActionOnlyReview"])
            self.assertIsNotNone(by_case["BLIND-CASE-007"]["supportingActionOnlyReview"])

            constraints = self.read_jsonl(output / "ai_judged_query_constraints_v1.jsonl")
            self.assertEqual(len(constraints), 8)
            self.assertTrue(all(row["stableVariantCount"] == 1 for row in constraints))
            self.assertEqual(sum(row["sourceReviewRowCount"] for row in constraints), 83)

    def test_two_independent_temp_builds_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "first", root / "second"
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden")):
                finalize(self.base_dir, first)
                finalize(self.base_dir, second)
            self.assertEqual(
                {path.name for path in first.iterdir()},
                {path.name for path in second.iterdir()},
            )
            for name in sorted(EXPECTED_OUTPUT_FILES):
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)

    def test_rejects_hash_drift_missing_duplicate_and_extra_review_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = self.read_jsonl(self.base_dir / INPUT_RELATIVE_PATHS["blindReviewer"])
            variants = {
                "missing": original[:-1],
                "duplicate": [*original[:-1], copy.deepcopy(original[0])],
                "extra": [*original, copy.deepcopy(original[0])],
            }
            for name, rows in variants.items():
                with self.subTest(name=name):
                    variant_root = root / name
                    base = self.make_mutable_input_copy(variant_root)
                    reviewer = base / INPUT_RELATIVE_PATHS["blindReviewer"]
                    write_jsonl(reviewer, rows)
                    hashes = dict(FROZEN_INPUT_SHA256)
                    hashes["blindReviewer"] = sha256_file(reviewer)
                    expected_error = "duplicate review key" if name == "duplicate" else "expected 83 JSONL rows"
                    with self.assertRaisesRegex(ValueError, expected_error):
                        finalize(base, variant_root / "out", expected_hashes=hashes)

            drift_root = root / "hash-drift"
            base = self.make_mutable_input_copy(drift_root)
            reviewer = base / INPUT_RELATIVE_PATHS["blindReviewer"]
            changed = copy.deepcopy(original)
            changed[0]["reason"] += " drift"
            write_jsonl(reviewer, changed)
            with self.assertRaisesRegex(ValueError, "SHA-256 drift for blindReviewer"):
                finalize(base, drift_root / "out")

    def test_refuses_overwrite_and_detects_artifact_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "freeze"
            finalize(self.base_dir, output)
            with self.assertRaisesRegex(FileExistsError, "refusing to overwrite"):
                finalize(self.base_dir, output)
            report = output / "freeze_report.md"
            report.write_text(report.read_text(encoding="utf-8") + "offline audit note\n", encoding="utf-8", newline="\n")
            with self.assertRaisesRegex(ValueError, "manifest artifact hash/size drift"):
                validate_output_bundle(output)


if __name__ == "__main__":
    unittest.main()
