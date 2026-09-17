import copy
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator

from evaluation.build_track_b_complex_intent_design import write_jsonl
from evaluation.build_track_b_used_phone_agent_pilot import build as build_pilot
from evaluation.compare_track_b_used_phone_blind_review import ACTION_CANONICAL_MAP, STATUS, compare


class TrackBUsedPhonePostBlindCompareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_root = Path(__file__).resolve().parents[1]
        cls.evidence_fixture = cls.agent_root / "tests" / "fixtures" / "kuaisearch_track_b_used_phone_pilot" / "evidence_products.jsonl"
        cls.policy_path = cls.agent_root / "tests" / "fixtures" / "kuaisearch_track_b_used_phone_post_blind" / "synthetic_reviewer_policy.json"
        cls.schemas = cls.agent_root / "evaluation" / "schemas"
        cls.reviewer_schema = cls.schemas / "track_b_used_phone_blind_reviewer_output_v2.schema.json"

    def make_inputs(self, root: Path) -> dict[str, Path]:
        pilot = root / "pilot"
        base_evidence = [json.loads(line) for line in self.evidence_fixture.read_text(encoding="utf-8").splitlines()]
        expanded_evidence = []
        for copy_index in range(4):
            for source in base_evidence:
                row = copy.deepcopy(source)
                row["itemId"] = f"{source['itemId']}-synthetic-{copy_index}"
                expanded_evidence.append(row)
        synthetic_evidence_path = root / "tests" / "fixtures" / "synthetic_evidence_products.jsonl"
        synthetic_evidence_path.parent.mkdir(parents=True, exist_ok=True)
        write_jsonl(synthetic_evidence_path, expanded_evidence)
        build_pilot(synthetic_evidence_path, pilot, self.schemas, expected_product_count=len(expanded_evidence))
        policy = json.loads(self.policy_path.read_text(encoding="utf-8"))
        templates = [json.loads(line) for line in (pilot / "blind_reviewer_output_templates_pending.jsonl").read_text(encoding="utf-8").splitlines()]
        mapping = json.loads((pilot / "blind_id_mapping_sealed_not_gold.json").read_text(encoding="utf-8"))
        cases = [json.loads(line) for line in (pilot / "used_phone_agent_cases_pending.jsonl").read_text(encoding="utf-8").splitlines()]
        suggestions = [json.loads(line) for line in (pilot / "automatic_judgment_suggestions_sealed_not_gold.jsonl").read_text(encoding="utf-8").splitlines()]
        case_reverse = mapping["reviewCaseIdToCaseId"]
        candidate_reverse = mapping["blindCandidateDisplayIdToCandidateDisplayId"]
        cases_by_id = {case["caseId"]: case for case in cases}
        suggestions_by_key = {(row["caseId"], row["candidateDisplayId"]): row for row in suggestions}

        reviews = []
        for template in templates:
            row = copy.deepcopy(template)
            original_case_id = case_reverse[row["reviewCaseId"]]
            case = cases_by_id[original_case_id]
            row["independentlyExtractedConstraints"] = copy.deepcopy(case["constraintContract"]["atoms"])
            row["reason"] = "Synthetic offline reviewer fixture decision; not a human judgment."
            if row["candidateDisplayId"] is not None:
                row["expectedAction"] = "recommend"
                original_candidate_id = candidate_reverse[row["candidateDisplayId"]]
                automatic = suggestions_by_key[(original_case_id, original_candidate_id)]["automaticSuggestion"]
                row["relevanceGrade"] = automatic["relevanceGrade"]
                row["eligible"] = automatic["eligible"]
                row["hardViolations"] = [{"atomId": atom_id} for atom_id in automatic["constraintViolation"]]
                row["hardUnknowns"] = [{"atomId": atom_id} for atom_id in automatic["hardEvidenceUnknownOrConflict"]]
                row["softAssessment"] = {"fixtureOnly": True}
                row["evidenceCitations"] = [
                    {"atomId": evidence.get("atomId"), "source": evidence["source"]}
                    for evidence in automatic["requirementEvidence"]
                ]
            else:
                row["expectedAction"] = ACTION_CANONICAL_MAP[case["expectedAction"]]
            reviews.append(row)

        candidate_indexes = [index for index, row in enumerate(reviews) if row["candidateDisplayId"] is not None]
        forced_index = candidate_indexes[policy["forcedCandidateOrdinal"]]
        reviews[forced_index]["relevanceGrade"] = policy["forcedRelevanceGrade"]
        reviews[forced_index]["eligible"] = policy["forcedEligible"]
        reviewer = root / "synthetic_reviewer.jsonl"
        write_jsonl(reviewer, reviews)
        return {
            "reviewer": reviewer,
            "mapping": pilot / "blind_id_mapping_sealed_not_gold.json",
            "suggestions": pilot / "automatic_judgment_suggestions_sealed_not_gold.jsonl",
            "cases": pilot / "used_phone_agent_cases_pending.jsonl",
            "pilot": pilot,
        }

    def run_compare(self, inputs: dict[str, Path], output: Path):
        return compare(
            inputs["reviewer"], self.reviewer_schema, inputs["mapping"],
            inputs["suggestions"], inputs["cases"], output, self.schemas,
        )

    def test_synthetic_fixture_generates_pending_gate_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)
            output = root / "comparison"
            manifest = self.run_compare(inputs, output)
            summary = json.loads((output / "agreement_summary.json").read_text(encoding="utf-8"))
            disagreements = [json.loads(line) for line in (output / "disagreement_queue.jsonl").read_text(encoding="utf-8").splitlines()]
            structured = [json.loads(line) for line in (output / "structured_constraint_evidence_review.jsonl").read_text(encoding="utf-8").splitlines()]
            actions = [json.loads(line) for line in (output / "action_only_comparison.jsonl").read_text(encoding="utf-8").splitlines()]

            self.assertEqual(manifest["status"], STATUS)
            self.assertEqual(summary["status"], STATUS)
            self.assertEqual(summary["counts"]["reviewRows"], 83)
            self.assertEqual(summary["counts"]["candidateRows"], 80)
            self.assertEqual(summary["counts"]["actionOnlyRows"], 3)
            self.assertEqual(len(structured), 83)
            self.assertEqual(len(actions), 3)
            self.assertEqual(len(disagreements), 1)
            self.assertEqual({item["field"] for row in disagreements for item in row["exactFieldDifferences"]}, {"relevanceGrade", "eligible"})
            self.assertEqual(summary["exactAgreement"]["expectedAction"], {"compared": 3, "agreed": 3, "disagreed": 0, "notApplicable": 80})
            self.assertEqual(summary["actionComparisonPolicy"]["candidateRowTreatment"], "notApplicable")
            self.assertEqual(summary["actionComparisonPolicy"]["comparisonMethod"], "versioned_canonical_pair_map_not_raw_string_equality")
            self.assertEqual({row["reviewCaseId"] for row in actions}, {"BLIND-CASE-004", "BLIND-CASE-005", "BLIND-CASE-007"})
            reviews = [json.loads(line) for line in inputs["reviewer"].read_text(encoding="utf-8").splitlines()]
            mapping = json.loads(inputs["mapping"].read_text(encoding="utf-8"))
            suggestions = [json.loads(line) for line in inputs["suggestions"].read_text(encoding="utf-8").splitlines()]
            role_by_original_candidate = {row["candidateDisplayId"]: row["candidateRole"] for row in suggestions}
            reviewed_original_candidates = {
                mapping["blindCandidateDisplayIdToCandidateDisplayId"][row["candidateDisplayId"]]
                for row in reviews if row["candidateDisplayId"] is not None
            }
            self.assertEqual({role_by_original_candidate[candidate_id] for candidate_id in reviewed_original_candidates}, {"judgment_candidate"})
            self.assertEqual(len(reviewed_original_candidates), 80)
            self.assertTrue(all(row["canonicalActionMatch"] for row in actions))
            self.assertTrue(all(row["reviewerExpectedAction"] != row["formalExpectedAction"] for row in actions))
            self.assertTrue(all(row["status"] == STATUS for row in disagreements + structured + actions))
            self.assertTrue(all(row["semanticAgreement"] is None and row["requiresAiAdjudication"] for row in structured))
            self.assertFalse(manifest["formalGoldWritten"])
            self.assertFalse(manifest["formalQrelWritten"])
            self.assertFalse(manifest["provenance"]["humanApproved"])
            self.assertTrue(manifest["provenance"]["aiOnly"])

    def test_output_schemas_validate_synthetic_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)
            output = root / "comparison"
            self.run_compare(inputs, output)
            fixtures = [
                ("agreement_summary.json", "track_b_used_phone_post_blind_summary_v2.schema.json", False),
                ("disagreement_queue.jsonl", "track_b_used_phone_post_blind_disagreement_v2.schema.json", True),
                ("structured_constraint_evidence_review.jsonl", "track_b_used_phone_post_blind_structured_review_v1.schema.json", True),
                ("action_only_comparison.jsonl", "track_b_used_phone_post_blind_action_comparison_v2.schema.json", True),
            ]
            for data_name, schema_name, is_jsonl in fixtures:
                schema = json.loads((self.schemas / schema_name).read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                validator = Draft202012Validator(schema)
                if is_jsonl:
                    records = [json.loads(line) for line in (output / data_name).read_text(encoding="utf-8").splitlines()]
                else:
                    records = [json.loads((output / data_name).read_text(encoding="utf-8"))]
                self.assertTrue(records)
                for record in records:
                    validator.validate(record)

    def test_rejects_missing_duplicate_unknown_and_blank_reviews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)
            original = [json.loads(line) for line in inputs["reviewer"].read_text(encoding="utf-8").splitlines()]
            variants = {}
            variants["missing"] = original[:-1]
            duplicate = copy.deepcopy(original)
            duplicate[-1] = copy.deepcopy(duplicate[0])
            variants["duplicate"] = duplicate
            unknown = copy.deepcopy(original)
            unknown[0]["candidateDisplayId"] = unknown[0]["reviewCaseId"] + "-CAND-ffffffffffff"
            variants["unknown"] = unknown
            blank = copy.deepcopy(original)
            blank[0]["expectedAction"] = ""
            variants["blank_action"] = blank
            blank_reason = copy.deepcopy(original)
            blank_reason[0]["reason"] = ""
            variants["blank_reason"] = blank_reason
            blank_constraints = copy.deepcopy(original)
            blank_constraints[0]["independentlyExtractedConstraints"] = []
            variants["blank_constraints"] = blank_constraints
            blank_evidence = copy.deepcopy(original)
            blank_evidence[0]["evidenceCitations"] = []
            variants["blank_evidence"] = blank_evidence
            reordered = copy.deepcopy(original)
            reordered[0], reordered[1] = reordered[1], reordered[0]
            variants["reordered"] = reordered
            for name, records in variants.items():
                with self.subTest(name=name):
                    reviewer = root / f"{name}.jsonl"
                    write_jsonl(reviewer, records)
                    changed = dict(inputs)
                    changed["reviewer"] = reviewer
                    with self.assertRaises(ValueError):
                        self.run_compare(changed, root / f"out-{name}")
            blank_line_reviewer = root / "blank-line.jsonl"
            blank_line_reviewer.write_text(inputs["reviewer"].read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")
            changed = dict(inputs)
            changed["reviewer"] = blank_line_reviewer
            with self.assertRaisesRegex(ValueError, "blank JSONL row"):
                self.run_compare(changed, root / "out-blank-line")

    def test_offline_double_build_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = self.make_inputs(root)
            first, second = root / "first", root / "second"
            with patch.object(socket, "create_connection", side_effect=AssertionError("network forbidden in fixture test")):
                self.run_compare(inputs, first)
                self.run_compare(inputs, second)
            names = sorted(path.name for path in first.iterdir())
            self.assertEqual(names, sorted(path.name for path in second.iterdir()))
            for name in names:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
