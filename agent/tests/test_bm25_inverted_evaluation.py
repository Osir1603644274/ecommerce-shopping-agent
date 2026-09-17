import unittest

from app.bm25 import BM25Document, BM25Index
from evaluation.bm25_inverted_evaluation import build_bm25_inverted_evaluation_report


class Bm25InvertedEvaluationTests(unittest.TestCase):
    def test_preserves_reference_ranking(self):
        index = BM25Index(
            [
                BM25Document("r1", "安静 插座", {"reviewId": "r1"}),
                BM25Document("r2", "安静 咖啡", {"reviewId": "r2"}),
                BM25Document("r3", "聚会 音乐", {"reviewId": "r3"}),
            ]
        )

        report = build_bm25_inverted_evaluation_report(
            ["安静 插座", "音乐"],
            index=index,
            limit=3,
        )

        self.assertTrue(report["summary"]["checks"]["allRankingsEquivalent"])


if __name__ == "__main__":
    unittest.main()
