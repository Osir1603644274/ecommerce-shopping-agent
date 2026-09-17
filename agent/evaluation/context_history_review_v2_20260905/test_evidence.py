import unittest

from .evidence import decode, encode, validated_turn_evidence


class EvidenceTests(unittest.TestCase):
    def test_exact_round_trip_and_no_input_mutation(self):
        product = {"productId": "21", "title": "手机", "displayedThisTurn": True,
            "selectionType": "full_match", "checks": [{"key": "os", "expected": "ios", "actual": "ios"}]}
        sample = {"sampleId": "blind-1", "turns": [{"user": "之前第二款", "assistant": "未知"}],
            "verifiedEvidenceByTurn": [{"turn": 1, "products": [product], "displayedProductIds": ["21"]},
                {"turn": 2, "products": [product], "displayedProductIds": ["21"]}]}
        packet = encode([sample])
        self.assertEqual(len(packet["evidenceDictionary"]), 2)
        self.assertEqual(decode(packet), [sample])
        self.assertIn("products", sample["verifiedEvidenceByTurn"][0])

    def test_conflicting_facts_are_not_collapsed(self):
        sample = {"sampleId": "blind-1", "turns": [], "verifiedEvidenceByTurn": [
            {"turn": 1, "products": [{"productId": "1", "priceMinor": 100, "checks": []}]},
            {"turn": 2, "products": [{"productId": "1", "priceMinor": 200, "checks": []}]}]}
        packet = encode([sample])
        self.assertEqual(len(packet["evidenceDictionary"]), 2)
        self.assertEqual(decode(packet), [sample])

    def test_empty_turn_preserved(self):
        samples = [{"sampleId": "x", "turns": [], "verifiedEvidenceByTurn": [
            {"turn": 1, "products": [], "validationPassedThisTurn": False}]}]
        self.assertEqual(decode(encode(samples)), samples)

    def test_nested_specifications_and_missing_columns(self):
        samples = [{"sampleId": "x", "turns": [], "verifiedEvidenceByTurn": [{"turn": 1,
            "products": [{"productId": "1", "specifications": {"os": "ios", "screen": None}, "checks": []},
                {"productId": "2", "specifications": {"os": "ios", "screen": None}, "priceMinor": 1, "checks": []}]}]}]
        packet = encode(samples)
        self.assertEqual(decode(packet), samples)
        self.assertEqual(len(packet["evidenceDictionary"]), 3)

    def test_actual_ranking_is_not_sorted_by_identifier(self):
        row = {"turn": 1, "traceSummary": {"phases": [{"phase": "validator", "outcome": "passed"}]},
            "toolTraces": [{"tool": "search_products", "ok": True, "detail": {"rankedItemIds": [9, 1, 7], "candidates": []}},
                {"tool": "search_products", "ok": True, "detail": {"rankedItemIds": [7, 9, 1], "candidates": []}}]}
        evidence = validated_turn_evidence(row)
        self.assertEqual(evidence["rankedProductIds"], ["7", "9", "1"])
        self.assertEqual(evidence["rankedProductBatches"][0]["orderedProductIds"], ["9", "1", "7"])


if __name__ == "__main__":
    unittest.main()
