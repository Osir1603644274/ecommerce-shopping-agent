import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from evaluation.kuaisearch_synthetic_evidence_benchmark import (
    PILOT_QUERY_BLUEPRINTS,
    auto_judgment,
    product_to_audit_row,
    query_record,
    scan_sources,
)
from evaluation.build_track_b_short_query_extension import (
    SHORT_QUERY_DEFINITIONS,
    judge as judge_short_extension,
    query_record as extension_query_record,
)


class KuaiSearchSyntheticEvidenceBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project_root = Path(__file__).resolve().parents[1]
        fixture = Path(__file__).resolve().parent / "fixtures" / "kuaisearch_track_b"
        cls.scan = scan_sources(
            fixture / "items.jsonl",
            fixture / "relevance.jsonl",
            verify_pinned=False,
        )
        cls.query = query_record(PILOT_QUERY_BLUEPRINTS[0])

    def test_streaming_join_excludes_ambiguous_key(self):
        self.assertEqual(self.scan.source_audit["sources"]["itemsLite"]["rows"], 5)
        self.assertEqual(self.scan.source_audit["join"]["uniqueEvidenceProducts"], 3)
        self.assertEqual(self.scan.source_audit["join"]["ambiguousRelevanceRowsExcluded"], 1)

    def test_missing_is_unknown_and_explicit_alternative_is_fail(self):
        products = {product.item_id: product for product in self.scan.products}
        passed = auto_judgment(self.query, products["1"])
        failed = auto_judgment(self.query, products["2"])
        missing = auto_judgment(self.query, products["3"])

        self.assertEqual(passed["autoPrelabel"]["relevanceGrade"], 3)
        self.assertIs(passed["autoPrelabel"]["eligible"], True)
        self.assertEqual(failed["autoPrelabel"]["relevanceGrade"], 0)
        self.assertIs(failed["autoPrelabel"]["eligible"], False)
        self.assertEqual(missing["autoPrelabel"]["relevanceGrade"], 1)
        self.assertEqual(missing["autoPrelabel"]["eligible"], "unknown")
        self.assertEqual(missing["autoPrelabel"]["requirementAtoms"][0]["status"], "missing")

    def test_source_editorial_score_does_not_affect_prelabel(self):
        products = {product.item_id: product for product in self.scan.products}
        judgment = auto_judgment(self.query, products["1"])
        self.assertFalse(judgment["labelProvenance"]["usesSourceEditorialScore"])
        self.assertFalse(judgment["labelProvenance"]["humanConfirmed"])
        self.assertEqual(judgment["goldStatus"], "not_gold_automatic_prelabel")

    def test_query_schema_accepts_pending_synthetic_query(self):
        schema_path = self.project_root / "evaluation" / "track_b_query.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(self.query)

    def test_candidate_schema_accepts_pending_prelabel(self):
        product = self.scan.products[0]
        judgment = auto_judgment(self.query, product)
        judgment["candidateDisplayId"] = f"{self.query['queryId']}-c01"
        judgment["evidenceProduct"] = product_to_audit_row(product)
        schema_path = self.project_root / "evaluation" / "track_b_candidate_prelabel.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(judgment)

    def test_short_query_extension_uses_explicit_category_atom_and_hierarchical_grade(self):
        definition = next(
            row for row in SHORT_QUERY_DEFINITIONS if row["slotId"] == "tb-tshirt-04-long-sleeve"
        )
        query = extension_query_record(definition)
        products = {product.item_id: product for product in self.scan.products}
        short_sleeve = judge_short_extension(query, products["1"])
        long_sleeve = judge_short_extension(query, products["2"])

        self.assertEqual(len(query["requirementAtoms"]), 2)
        self.assertEqual(query["requirementAtoms"][0]["attributeGroup"], "exact_category")
        self.assertEqual(short_sleeve["relevanceGrade"], 1)
        self.assertIs(short_sleeve["eligible"], False)
        self.assertEqual(long_sleeve["relevanceGrade"], 3)
        self.assertIs(long_sleeve["eligible"], True)

        query_schema = json.loads(
            (self.project_root / "evaluation" / "track_b_short_query_v2.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(query_schema).validate(query)

        long_sleeve["candidateDisplayId"] = f"{query['queryId']}-c01"
        decision_schema = json.loads(
            (
                self.project_root
                / "evaluation"
                / "track_b_short_query_decision_v2.schema.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(decision_schema).validate(long_sleeve)


if __name__ == "__main__":
    unittest.main()
