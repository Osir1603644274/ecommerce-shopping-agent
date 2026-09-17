import unittest
from .vivo_state_audit import expected, lane_view, equivalent_requirement


class ExplicitStateAuditTests(unittest.TestCase):
    def test_budget_change_boundaries(self):
        for turn, amount in [(2,160000),(6,160000),(7,140000),(13,140000),(14,150000),(26,150000),(27,144000),(38,144000),(39,160000),(48,160000)]:
            self.assertEqual(expected(turn)[0]["price_minor"], ("lte",amount))

    def test_revocation_and_restoration(self):
        self.assertIn("battery_health", expected(33)[0])
        self.assertNotIn("battery_health", expected(34)[0])
        self.assertIn("battery_health", expected(46)[0])
        self.assertNotIn("brand", expected(20)[1])
        self.assertIn("brand", expected(38)[1])
        self.assertNotIn("brand", expected(43)[1])
        self.assertIn("screen_originality", expected(25)[1])
        self.assertIn("screen_originality", expected(26)[0])

    def test_constraints_projection_has_no_soft_lane_or_priority_field(self):
        raw = [{"key":"price_minor","operator":"lte","value":160000,"source":"user"}]
        hard, soft = expected(2)
        rows, lanes = lane_view("constraints", raw, hard, soft)
        self.assertEqual(lanes, (("hard",hard),))
        self.assertEqual(rows[0]["priority"], "hard")
        self.assertNotIn("priority", raw[0])

    def test_guide_preserves_explicit_priorities(self):
        hard, soft = expected(2)
        raw = [{"key":"brand","priority":"soft","value":"vivo"}]
        rows, lanes = lane_view("guide", raw, hard, soft)
        self.assertIs(rows, raw)
        self.assertEqual(lanes, (("hard",hard),("soft",soft)))

    def test_action_restriction_is_not_a_product_filter(self):
        rows, _ = lane_view("constraints", [{"key":"agent_contact_seller","operator":"eq","value":False}], *expected(8))
        self.assertEqual(rows, [])

    def test_finite_enum_complement_includes_unknown_behavior(self):
        item = {"key":"battery_health","operator":"not_in","value":["lt70","70_80"],"unit":"enum","source":"user","priority":"hard"}
        self.assertTrue(equivalent_requirement("battery_health", "in", ["80_90","90_plus"], item))
        item["value"] = ["lt70"]
        self.assertFalse(equivalent_requirement("battery_health", "in", ["80_90","90_plus"], item))

    def test_non_enum_budget_operator_cannot_be_silently_equated(self):
        self.assertFalse(equivalent_requirement("price_minor", "lte", 140000, {"operator":"gte","value":140000}))


if __name__ == "__main__":
    unittest.main()
