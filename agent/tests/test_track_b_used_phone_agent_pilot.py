import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:
    Draft202012Validator = None

from evaluation.build_track_b_used_phone_agent_pilot import (
    CASE_SCHEMA_VERSION,
    BLIND_CASE_SCHEMA_VERSION,
    CANDIDATE_SCHEMA_VERSION,
    JUDGMENT_SCHEMA_VERSION,
    REVIEW_OUTPUT_SCHEMA_VERSION,
    STATUS,
    build,
)


class TrackBUsedPhoneAgentPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_root = Path(__file__).resolve().parents[1]
        cls.fixture = cls.agent_root / "tests" / "fixtures" / "kuaisearch_track_b_used_phone_pilot" / "evidence_products.jsonl"
        cls.schemas = cls.agent_root / "evaluation" / "schemas"

    def build_fixture(self, output: Path):
        return build(self.fixture, output, self.schemas, expected_product_count=15)

    def test_exact_eight_case_distribution_and_action_cases_have_no_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            cases = [json.loads(line) for line in (output / "used_phone_agent_cases_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            candidates = [json.loads(line) for line in (output / "blind_candidates_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(cases), 8)
            self.assertEqual(Counter(case["caseType"] for case in cases), Counter({
                "multi_objective_tradeoff": 2,
                "negation_constraint": 1,
                "must_clarify": 1,
                "product_comparison": 1,
                "substitute_recommendation": 1,
                "no_solution_or_evidence_insufficient": 1,
                "same_session_constraint_update": 1,
            }))
            candidate_case_ids = {row["reviewCaseId"] for row in candidates}
            self.assertNotIn("BLIND-CASE-004", candidate_case_ids)
            self.assertNotIn("BLIND-CASE-007", candidate_case_ids)
            for case in cases:
                self.assertEqual(case["goldStatus"], STATUS)
                self.assertFalse(case["provenance"]["humanApproved"])
                self.assertTrue(case["provenance"]["aiOnly"])

    def test_blind_candidates_hide_suggestions_and_retrieval_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            rows = [json.loads(line) for line in (output / "blind_candidates_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            forbidden = {
                "status", "pass", "fail", "unknown", "conflict", "conflictReasons",
                "observedCanonicalValues", "suggestedGrade", "relevanceGrade", "eligible",
                "utility", "decisionUtility", "constraintViolation", "hardEvidenceUnknownOrConflict",
                "caseType", "expectedAction", "goldStatus", "actionContract", "comparisonContract",
                "substituteContract", "multiTurnContract", "expectedFinalState", "constraintContract",
                "retrievalSystem", "retrievalRank", "retrievalScore", "rank", "score",
            }

            def keys(value):
                if isinstance(value, dict):
                    result = set(value)
                    for child in value.values():
                        result |= keys(child)
                    return result
                if isinstance(value, list):
                    result = set()
                    for child in value:
                        result |= keys(child)
                    return result
                return set()

            self.assertTrue(rows)
            for row in rows:
                self.assertFalse(keys(row) & forbidden)
                self.assertNotIn("caseId", row)
                self.assertRegex(row["reviewCaseId"], r"^BLIND-CASE-00[1-8]$")
                self.assertRegex(row["candidateDisplayId"], r"^BLIND-CASE-00[1-8]-CAND-[0-9a-f]{12}$")
                self.assertTrue(row["candidateDisplayId"].startswith(row["reviewCaseId"] + "-CAND-"))
                self.assertFalse(row["provenance"]["humanApproved"])
                self.assertTrue(row["provenance"]["aiOnly"])
                self.assertFalse(row["provenance"]["automaticSuggestedLabelVisible"])
                self.assertIn("userContext", row)
                self.assertIn("title", row["candidate"])
                self.assertIn("brand", row["candidate"])
                self.assertIn("seller", row["candidate"])
                self.assertTrue(row["evidenceRefs"])
                for evidence in row["evidenceRefs"]:
                    self.assertIn("lineNumber", evidence["source"])
                    self.assertIn("rawValue", evidence)

    def test_blind_cases_hide_case_answers_and_include_complete_user_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            rows = [json.loads(line) for line in (output / "blind_cases_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(rows), 8)
            forbidden = {
                "caseType", "expectedAction", "goldStatus", "actionContract", "comparisonContract",
                "substituteContract", "multiTurnContract", "expectedFinalState", "constraintContract",
                "status", "pass", "fail", "unknown", "conflict", "relevanceGrade", "eligible",
                "decisionUtility", "constraintViolation", "rank", "score",
            }

            def keys(value):
                if isinstance(value, dict):
                    return set(value) | set().union(*(keys(child) for child in value.values()), set())
                if isinstance(value, list):
                    return set().union(*(keys(child) for child in value), set())
                return set()

            for row in rows:
                self.assertFalse(keys(row) & forbidden)
                self.assertNotIn("caseId", row)
                self.assertRegex(row["reviewCaseId"], r"^BLIND-CASE-00[1-8]$")
                context = row["userContext"]
                self.assertTrue(context["messages"])
                self.assertTrue(all(message["role"] == "user" and message["text"] for message in context["messages"]))
                self.assertTrue(all(set(message) == {"role", "text"} for message in context["messages"]))
                self.assertFalse(row["provenance"]["humanApproved"])
                self.assertTrue(row["provenance"]["aiOnly"])
            comparison = next(row for row in rows if row["reviewCaseId"] == "BLIND-CASE-005")
            substitute = next(row for row in rows if row["reviewCaseId"] == "BLIND-CASE-006")
            multi = next(row for row in rows if row["reviewCaseId"] == "BLIND-CASE-008")
            self.assertEqual({item["label"] for item in comparison["userVisibleCandidateContext"]}, {"候选 A", "候选 B"})
            self.assertEqual(len(substitute["userVisibleCandidateContext"]), 1)
            self.assertEqual(substitute["userVisibleCandidateContext"][0]["label"], "先前商品")
            self.assertEqual(len(multi["userContext"]["messages"]), 2)

    def test_sealed_grade_eligibility_and_utility_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            rows = [json.loads(line) for line in (output / "automatic_judgment_suggestions_sealed_not_gold.jsonl").read_text(encoding="utf-8").splitlines()]
            seen = set()
            for row in rows:
                suggestion = row["automaticSuggestion"]
                grade = suggestion["relevanceGrade"]
                seen.add(grade)
                if grade in {2, 3}:
                    self.assertIs(suggestion["eligible"], True)
                    self.assertEqual(suggestion["decisionUtility"], 1)
                    self.assertFalse(suggestion["constraintViolation"])
                elif grade == 1:
                    self.assertIs(suggestion["eligible"], False)
                    self.assertEqual(suggestion["decisionUtility"], 0)
                    self.assertTrue(suggestion["constraintViolation"])
                else:
                    self.assertIn(suggestion["eligible"], {"unknown", None})
                    self.assertIn(suggestion["decisionUtility"], {0, None})
                self.assertEqual(row["goldStatus"], "not_gold_pending_independent_ai_blind_audit")
                self.assertEqual(row["blindAdjudication"]["status"], "pending")
            self.assertTrue({1, 2, 3, None}.issubset(seen))

    def test_multiturn_state_supersede_and_retain_invariant(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            cases = [json.loads(line) for line in (output / "used_phone_agent_cases_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            case = next(row for row in cases if row["caseType"] == "same_session_constraint_update")
            contract = case["multiTurnContract"]
            initial_hard = {row["group"]: row for row in contract["initialState"]["hard"]}
            final_hard = {row["group"]: row for row in contract["expectedFinalState"]["hard"]}
            final_soft = {row["group"]: row for row in contract["expectedFinalState"]["soft"]}
            self.assertEqual(initial_hard["os"]["allowedValues"], ["android"])
            self.assertEqual(final_hard["os"]["allowedValues"], ["ios"])
            self.assertEqual(set(contract["turnDelta"]["retain"]), {"battery_health", "screen_originality"})
            self.assertEqual(set(final_soft), {"battery_health", "screen_originality"})
            self.assertIn("motherboard_repair", final_hard)
            atom_state = {(a["group"], a["operator"], tuple(a["allowedValues"]), a["importance"]) for a in case["constraintContract"]["atoms"]}
            expected_state = {
                (row["group"], row["operator"], tuple(row["allowedValues"]), importance)
                for importance, rows in (("hard", contract["expectedFinalState"]["hard"]), ("soft", contract["expectedFinalState"]["soft"]))
                for row in rows
            }
            self.assertEqual(atom_state, expected_state)

    def test_comparison_and_substitute_have_item_evidence_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            rows = [json.loads(line) for line in (output / "blind_candidates_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            compare = [row for row in rows if row["reviewCaseId"] == "BLIND-CASE-005"]
            substitute = [row for row in rows if row["reviewCaseId"] == "BLIND-CASE-006"]
            self.assertEqual({row["userVisibleRole"] for row in compare}, {"候选 A", "候选 B"})
            self.assertTrue(any(row["userVisibleRole"] == "先前商品" for row in substitute))
            for row in compare + substitute:
                self.assertTrue(row["evidenceRefs"])
                self.assertTrue(any(ref["field"] == "attr_value" for ref in row["evidenceRefs"]))

    def test_reviewer_output_templates_are_blank_and_schema_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            rows = [json.loads(line) for line in (output / "blind_reviewer_output_templates_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(rows)
            action_case_ids = {"BLIND-CASE-004", "BLIND-CASE-005", "BLIND-CASE-007"}
            self.assertEqual({row["reviewCaseId"] for row in rows if row["candidateDisplayId"] is None}, action_case_ids)
            for row in rows:
                self.assertEqual(row["schemaVersion"], REVIEW_OUTPUT_SCHEMA_VERSION)
                self.assertEqual(row["independentlyExtractedConstraints"], [])
                self.assertIsNone(row["expectedAction"])
                self.assertIsNone(row["relevanceGrade"])
                self.assertIsNone(row["eligible"])
                self.assertEqual(row["hardViolations"], [])
                self.assertEqual(row["hardUnknowns"], [])
                self.assertIsNone(row["softAssessment"])
                self.assertEqual(row["evidenceCitations"], [])
                self.assertIsNone(row["reason"])
                self.assertTrue(row["aiOnly"])
                self.assertFalse(row["humanApproved"])

    def test_schemas_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            manifest = self.build_fixture(output)
            cases = [json.loads(line) for line in (output / "used_phone_agent_cases_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            blind = [json.loads(line) for line in (output / "blind_candidates_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            judgments = [json.loads(line) for line in (output / "automatic_judgment_suggestions_sealed_not_gold.jsonl").read_text(encoding="utf-8").splitlines()]
            blind_cases = [json.loads(line) for line in (output / "blind_cases_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            reviewer_outputs = [json.loads(line) for line in (output / "blind_reviewer_output_templates_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            fixtures = [
                ("track_b_used_phone_agent_case_v1.schema.json", CASE_SCHEMA_VERSION, cases),
                ("track_b_used_phone_blind_case_v2.schema.json", BLIND_CASE_SCHEMA_VERSION, blind_cases),
                ("track_b_used_phone_blind_candidate_v3.schema.json", CANDIDATE_SCHEMA_VERSION, blind),
                ("track_b_used_phone_blind_reviewer_output_v2.schema.json", REVIEW_OUTPUT_SCHEMA_VERSION, reviewer_outputs),
                ("track_b_used_phone_automatic_judgment_v1.schema.json", JUDGMENT_SCHEMA_VERSION, judgments),
            ]
            for name, version, records in fixtures:
                schema = json.loads((self.schemas / name).read_text(encoding="utf-8"))
                for record in records:
                    self.assertEqual(record["schemaVersion"], version)
                    if Draft202012Validator is not None:
                        Draft202012Validator(schema).validate(record)
                    else:
                        self.assertFalse(set(schema["required"]) - set(record))
            self.assertEqual(manifest["status"], STATUS)
            self.assertFalse(manifest["provenance"]["humanApproved"])
            self.assertFalse(manifest["provenance"]["formalGold"])
            self.assertFalse(manifest["provenance"]["formalQrel"])
            self.assertFalse(manifest["networkUsed"])
            envelope = json.loads((output / "blind_bundle_manifest.json").read_text(encoding="utf-8"))
            allowed = {item["path"] for item in envelope["files"]}
            self.assertIn("blind_cases_pending.jsonl", allowed)
            self.assertIn("blind_candidates_pending.jsonl", allowed)
            self.assertIn("blind_reviewer_output_templates_pending.jsonl", allowed)
            self.assertNotIn("used_phone_agent_cases_pending.jsonl", allowed)
            self.assertNotIn("automatic_judgment_suggestions_sealed_not_gold.jsonl", allowed)
            self.assertNotIn("blind_id_mapping_sealed_not_gold.json", allowed)

    def test_blind_bundle_text_has_no_semantic_ids_or_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            envelope = json.loads((output / "blind_bundle_manifest.json").read_text(encoding="utf-8"))
            original_case_ids = {
                "UP-C01-TRADEOFF-IOS", "UP-C02-TRADEOFF-ANDROID", "UP-C03-NEGATION-REPAIR",
                "UP-C04-CLARIFY-CONDITION", "UP-C05-COMPARISON-EVIDENCE", "UP-C06-SUBSTITUTE-RETAIN",
                "UP-C07-ABSTAIN-EVIDENCE", "UP-C08-MULTITURN-UPDATE",
            }
            semantic_labels = {"clarify", "abstain", "multiturn", "tradeoff", "negation", "comparison", "substitute"}
            for item in envelope["files"]:
                text = (output / item["path"]).read_text(encoding="utf-8")
                lowered = text.lower()
                for original_case_id in original_case_ids:
                    self.assertNotIn(original_case_id, text, item["path"])
                for label in semantic_labels:
                    self.assertNotIn(label, lowered, item["path"])

    def test_sealed_opaque_id_mapping_is_bijective_and_mergeable(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "pilot"
            self.build_fixture(output)
            mapping = json.loads((output / "blind_id_mapping_sealed_not_gold.json").read_text(encoding="utf-8"))
            blind_cases = [json.loads(line) for line in (output / "blind_cases_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            blind_candidates = [json.loads(line) for line in (output / "blind_candidates_pending.jsonl").read_text(encoding="utf-8").splitlines()]
            judgments = [json.loads(line) for line in (output / "automatic_judgment_suggestions_sealed_not_gold.jsonl").read_text(encoding="utf-8").splitlines()]

            case_forward = mapping["caseIdToReviewCaseId"]
            case_reverse = mapping["reviewCaseIdToCaseId"]
            candidate_forward = mapping["candidateDisplayIdToBlindCandidateDisplayId"]
            candidate_reverse = mapping["blindCandidateDisplayIdToCandidateDisplayId"]
            self.assertEqual(len(case_forward), 8)
            self.assertEqual(len(case_forward), len(case_reverse))
            self.assertEqual(len(candidate_forward), len(blind_candidates))
            self.assertEqual(len(candidate_forward), len(candidate_reverse))
            self.assertEqual({row["reviewCaseId"] for row in blind_cases}, set(case_reverse))
            self.assertEqual({row["candidateDisplayId"] for row in blind_candidates}, set(candidate_reverse))
            self.assertTrue(all(case_reverse[opaque] == original for original, opaque in case_forward.items()))
            self.assertTrue(all(candidate_reverse[opaque] == original for original, opaque in candidate_forward.items()))

            sealed_keys = {(row["caseId"], row["candidateDisplayId"]) for row in judgments}
            merged_keys = {
                (case_reverse[row["reviewCaseId"]], candidate_reverse[row["candidateDisplayId"]])
                for row in blind_candidates
            }
            self.assertEqual(merged_keys, sealed_keys)

    def test_double_build_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first, second = root / "first", root / "second"
            self.build_fixture(first)
            self.build_fixture(second)
            names = sorted(path.name for path in first.iterdir())
            self.assertEqual(names, sorted(path.name for path in second.iterdir()))
            for name in names:
                self.assertEqual((first / name).read_bytes(), (second / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
