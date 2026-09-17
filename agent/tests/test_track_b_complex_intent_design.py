import hashlib
import json
import tempfile
import unittest
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
except ModuleNotFoundError:  # The repository declares it in the optional dev extra.
    Draft202012Validator = None

from evaluation.build_track_b_complex_intent_design import (
    APPROVED_INTENT_GROUPS,
    SOLVABLE_DEFINITIONS,
    Product,
    SOURCE_EVIDENCE_REPO_PATH,
    atom_status,
    build,
    build_action_intents,
    build_contract,
    build_multi_turn,
    make_atom,
    observe_group,
)


class TrackBComplexIntentDesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent_root = Path(__file__).resolve().parents[1]
        cls.fixture = Path(__file__).resolve().parent / "fixtures" / "kuaisearch_track_b_complex" / "evidence_products.jsonl"
        cls.products = {}
        for line in cls.fixture.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            cls.products[row["itemId"]] = Product(
                item_id=row["itemId"], category_key=row["categoryKey"], title=row["title"],
                brand=row["brand"], seller=row["seller"], attrs=tuple(row["normalizedAttrValues"]),
                raw_attr=row["evidenceRefs"][0]["rawValue"],
            )

    def test_and_in_not_in_and_soft_grade_primitives(self):
        tshirt = self.products["fixture-t1"]
        self.assertEqual(atom_status(tshirt, make_atom("a", "sleeve_length", ["short"], "hard")), "pass")
        self.assertEqual(atom_status(tshirt, make_atom("b", "sleeve_length", ["short", "long"], "hard")), "pass")
        self.assertEqual(atom_status(tshirt, make_atom("c", "tshirt_fit", ["slim"], "hard", operator="NOT_IN")), "fail")
        # Soft/hard is a grading layer on the same evidence status, not a different truth rule.
        self.assertEqual(atom_status(tshirt, make_atom("d", "tshirt_material", ["cotton"], "soft")), "pass")

    def test_cotton_blend_counts_as_cotton_but_not_as_explicit_pure_cotton(self):
        tshirt = self.products["fixture-t1"]
        cotton = atom_status(tshirt, make_atom("a", "tshirt_material", ["cotton"], "soft"))
        pure = atom_status(tshirt, make_atom("b", "tshirt_material", ["pure_cotton"], "hard"))
        self.assertEqual(cotton, "pass")
        self.assertNotEqual(pure, "pass")

    def test_flat_low_conflict_and_explicit_or_exception(self):
        shoe = self.products["fixture-s1"]
        only_flat = make_atom("a", "heel_height", ["flat"], "hard")
        flat_or_low = make_atom("b", "heel_height", ["flat", "low"], "hard")
        self.assertEqual(atom_status(shoe, only_flat), "conflict")
        self.assertEqual(atom_status(shoe, flat_or_low), "pass")

    def test_mid_low_is_unknown_for_mid_waist(self):
        jeans = self.products["fixture-j1"]
        self.assertEqual(atom_status(jeans, make_atom("a", "waist_height", ["mid"], "hard")), "unknown")

    def test_synonym_and_inclusion_normalization(self):
        jeans = self.products["fixture-j1"]
        shoe = self.products["fixture-s1"]
        tshirt = self.products["fixture-t2"]
        self.assertEqual(atom_status(jeans, make_atom("a", "leg_shape", ["wide"], "hard")), "pass")
        self.assertEqual(atom_status(shoe, make_atom("b", "closure", ["pull_on"], "hard")), "pass")
        polyester = observe_group(tshirt, "tshirt_material")
        self.assertEqual(polyester["attrValues"].count("polyester"), 1)
        self.assertFalse(polyester["conflicts"])

    def test_title_attr_conflict_never_overrides_attr(self):
        shoe = self.products["fixture-s2"]
        self.assertEqual(atom_status(shoe, make_atom("a", "heel_height", ["flat"], "hard")), "conflict")

    def test_three_variants_share_one_qrel_contract(self):
        contracts = [build_contract(row) for row in SOLVABLE_DEFINITIONS]
        for contract in contracts:
            self.assertEqual(len(contract["queryVariants"]), 3)
            self.assertEqual(len({v["semanticContractHash"] for v in contract["queryVariants"]}), 1)
            self.assertEqual(contract["qrelContract"]["reuseKey"], contract["intentGroupId"])

    def test_approval_scope_records_only_t1_t4_intent_approval(self):
        contracts = {row["intentGroupId"]: build_contract(row) for row in SOLVABLE_DEFINITIONS}
        for intent_id, contract in contracts.items():
            approved = intent_id in APPROVED_INTENT_GROUPS
            self.assertEqual(contract["provenance"]["humanApproved"], approved)
            self.assertEqual(contract["approval"]["queryNaturalness"], "approved" if approved else "pending")
            self.assertEqual(contract["approval"]["semanticEquivalence"], "approved" if approved else "pending")
            self.assertEqual(contract["approval"]["productAnswers"], "pending")
            self.assertEqual(contract["goldStatus"], "pending")
        actions = build_action_intents()
        multi = build_multi_turn(contracts)
        self.assertTrue(all(a["approval"]["queryNaturalness"] == "pending" for a in actions))
        self.assertTrue(all(m["approval"]["dialogueNaturalness"] == "pending" for m in multi))

    def test_schemas_validate_generated_records(self):
        contracts = {row["intentGroupId"]: build_contract(row) for row in SOLVABLE_DEFINITIONS}
        cases = [
            ("track_b_complex_intent_contract_v1.schema.json", list(contracts.values())),
            ("track_b_complex_action_intent_v1.schema.json", build_action_intents()),
            ("track_b_complex_multiturn_v1.schema.json", build_multi_turn(contracts)),
        ]
        for schema_name, records in cases:
            schema = json.loads((self.agent_root / "evaluation" / "schemas" / schema_name).read_text(encoding="utf-8"))
            self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
            if Draft202012Validator is not None:
                validator = Draft202012Validator(schema)
                for record in records:
                    validator.validate(record)
            else:
                # Offline stdlib fallback: enforce root required keys and constants.
                required = set(schema.get("required", []))
                constants = {key: spec["const"] for key, spec in schema.get("properties", {}).items() if "const" in spec}
                for record in records:
                    self.assertFalse(required - set(record), (schema_name, required - set(record)))
                    for key, expected in constants.items():
                        self.assertEqual(record.get(key), expected, (schema_name, key))

    def test_offline_double_build_is_byte_identical(self):
        schema_dir = self.agent_root / "evaluation" / "schemas"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first"
            second = root / "second"
            build(self.fixture, first, schema_dir)
            build(self.fixture, second, schema_dir)
            for output in (first, second):
                gate = json.loads((output / "idempotence_check.json").read_text(encoding="utf-8"))
                self.assertIs(gate["canonicalArtifactRoundTrip"], True)
                self.assertIs(gate["passed"], True)
                for name in (
                    "solvable_intent_contracts_pending.jsonl",
                    "single_turn_action_intents_pending.jsonl",
                    "multi_turn_scenarios_pending.jsonl",
                    "blind_approval_cards_pending.jsonl",
                ):
                    payload = (output / name).read_bytes()
                    self.assertNotIn(b"\r\n", payload, name)
                    self.assertNotIn(b"\r", payload, name)
            first_files = sorted(path.name for path in first.iterdir())
            second_files = sorted(path.name for path in second.iterdir())
            self.assertEqual(first_files, second_files)
            for name in first_files:
                left = hashlib.sha256((first / name).read_bytes()).hexdigest()
                right = hashlib.sha256((second / name).read_bytes()).hexdigest()
                self.assertEqual(left, right, name)

    def test_manifest_source_path_is_stable_across_repository_roots(self):
        schema_dir = self.agent_root / "evaluation" / "schemas"
        fixture_bytes = self.fixture.read_bytes()
        fixture_sha256 = hashlib.sha256(fixture_bytes).hexdigest()
        manifests = []
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            roots = [temp_root / "checkout-alpha", temp_root / "checkout-beta"]
            for root in roots:
                evidence = root / Path(SOURCE_EVIDENCE_REPO_PATH)
                evidence.parent.mkdir(parents=True, exist_ok=True)
                evidence.write_bytes(fixture_bytes)
                output = root / "generated" / "complex_intent_design_v1"
                build(evidence.resolve(), output.resolve(), schema_dir.resolve())
                manifest_bytes = (output / "manifest.json").read_bytes()
                manifest = json.loads(manifest_bytes)
                self.assertEqual(manifest["sourceEvidence"]["path"], SOURCE_EVIDENCE_REPO_PATH)
                self.assertEqual(manifest["sourceEvidence"]["bytes"], len(fixture_bytes))
                self.assertEqual(manifest["sourceEvidence"]["sha256"], fixture_sha256)
                self.assertNotIn(str(root), manifest_bytes.decode("utf-8"))
                self.assertNotIn("\\", manifest["sourceEvidence"]["path"])
                gate = json.loads((output / "idempotence_check.json").read_text(encoding="utf-8"))
                self.assertIs(gate["canonicalArtifactRoundTrip"], True)
                self.assertIs(gate["passed"], True)
                manifests.append(manifest_bytes)
        self.assertEqual(manifests[0], manifests[1])


if __name__ == "__main__":
    unittest.main()
