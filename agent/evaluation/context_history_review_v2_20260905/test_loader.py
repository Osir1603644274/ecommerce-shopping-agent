import json
import unittest

from agent.evaluation.context_history_strategies_v1_20260905.artifacts import HERE
from .run import load_samples, prepare


class StreamingLoaderTests(unittest.TestCase):
    def test_all_vivo24_packet_hashes_match_completed_review(self):
        attempts = [f"core24_v3_{arm}001" for arm in "ABC"]
        expected = json.loads((HERE / "core24_v3_complete_review001/packet_audit.json").read_text(encoding="utf-8"))
        samples, mapping = load_samples(attempts, 24)
        actual = []
        for start in range(1, 25, 6):
            for sample in samples:
                _, _, audit = prepare(sample, list(range(start, start + 6)))
                actual.append(audit)
        self.assertEqual(actual, expected)
        old_mapping = json.loads((HERE / "core24_v3_complete_review001/mapping_PRIVATE_NOT_IN_JUDGE_INPUT.json").read_text(encoding="utf-8"))
        self.assertEqual(mapping, old_mapping)


if __name__ == "__main__":
    unittest.main()
