import unittest

from .review_conversation import DIMENSIONS, disagreements, validate_judgment


class ReviewTests(unittest.TestCase):
    def test_review_preserves_actual_runtime_cap_and_state(self):
        from .review_evidence import validated_turn_evidence
        evidence = validated_turn_evidence({"turn": 1, "contextPolicyEvidence": [
            {"answerFormat": {"maxProducts": 3}}], "postState": {"domainState": {"shoppingGuide": {
                "requirements": [{"key": "os", "priority": "hard", "value": "ios"}]}}}})
        self.assertEqual(evidence["answerFormatContracts"], [{"maxProducts": 3}])
        self.assertEqual(evidence["persistedRequirements"][0]["priority"], "hard")

    def test_all_targets_exactly_once_and_original_quotes(self):
        sample = {"sampleId": "s", "turns": [{"user": "预算1000", "assistant": "预算2000"}]}
        score = {"sampleId": "s", "turn": 1, **{key: 2 for key in DIMENSIONS}, "rationale": "预算错",
            "seriousErrors": [{"category": "hard_constraint", "assistantQuote": "预算2000",
                "userTurn": 1, "userQuote": "预算1000", "reason": "未经授权提高预算"}]}
        result = validate_judgment({"scores": [score]}, [sample], [1])
        self.assertEqual(list(result), [("s", 1)])
        with self.assertRaisesRegex(ValueError, "coverage"):
            validate_judgment({"scores": [score, score]}, [sample], [1])
        score["seriousErrors"][0]["assistantQuote"] = "伪造引文"
        with self.assertRaisesRegex(ValueError, "quote"):
            validate_judgment({"scores": [score]}, [sample], [1])

    def test_same_serious_presence_different_kind_is_disagreement(self):
        first = {("s", 1): {**{key: 3 for key in DIMENSIONS}, "seriousErrors": [{"category": "hard_constraint"}]}}
        second = {("s", 1): {**{key: 3 for key in DIMENSIONS}, "seriousErrors": [{"category": "wrong_reference"}]}}
        self.assertEqual(disagreements(first, second), [("s", 1)])


if __name__ == "__main__":
    unittest.main()
