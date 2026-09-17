import unittest

from .cohort_report import compare


class CompleteRatioTests(unittest.TestCase):
    def case(self, tokens=1000, ms=1000, complete=True, unsafe=None):
        return {"completeNativeTokenTotal": tokens, "completeConversationMs": ms,
            "dataCollectionComplete": complete, "unsafeTerminalTurns": unsafe or []}

    def test_exact_gate_edges_still_need_quality(self):
        value = compare(self.case(), self.case(900, 1150))
        self.assertTrue(value["tokenAtLeast10Percent"])
        self.assertTrue(value["timeIncreaseAtMost15Percent"])
        self.assertFalse(value["qualityAcceptance"])
        self.assertEqual(value["overallDecision"], "HOLD_QUALITY_REVIEW_PENDING")

    def test_unknown_usage_is_not_zero(self):
        value = compare(self.case(), self.case(None))
        self.assertIsNone(value["tokenSavingPercent"])
        self.assertIsNone(value["tokenAtLeast10Percent"])

    def test_incomplete_episode_has_no_ratios(self):
        for base, other in ((self.case(complete=False), self.case()), (self.case(), self.case(complete=False))):
            value = compare(base, other)
            self.assertIsNone(value["tokenSavingPercent"])
            self.assertIsNone(value["timeGrowthPercent"])

    def test_failed_turn_is_not_hidden_by_cost_savings(self):
        value = compare(self.case(), self.case(800, 800, unsafe=[7]))
        self.assertEqual(value["unsafeTurnsOther"], [7])
        self.assertEqual(value["overallDecision"], "HOLD_QUALITY_REVIEW_PENDING")

    def test_over_threshold_not_rounded_into_pass(self):
        value = compare(self.case(), self.case(901, 1151))
        self.assertFalse(value["tokenAtLeast10Percent"])
        self.assertFalse(value["timeIncreaseAtMost15Percent"])

    def test_zero_base_is_unavailable_not_divide_by_zero(self):
        value = compare(self.case(0, 0), self.case(0, 0))
        self.assertIsNone(value["tokenSavingPercent"])
        self.assertIsNone(value["timeGrowthPercent"])


if __name__ == "__main__":
    unittest.main()
